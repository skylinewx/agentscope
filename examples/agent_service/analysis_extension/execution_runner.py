"""Bounded host orchestration. Only this module combines container I/O with input grants."""
import asyncio
import hashlib
from time import perf_counter
from uuid import uuid4

from .execution_environment import DockerBackend
from .execution_inputs import InputBroker, InputRequestError, PROVENANCE_MODE, delivery_summary
from .execution_protocol import MAX_BYTES, MAX_DATA_BYTES, PROTOCOL_VERSION, encode, read_async, write_async
from .models import AnalysisError
from .numeric_policy import normalize_output
from .python_program import validate_code

backend_factory = DockerBackend


class Outputs(list):
    pass


async def execute(code, inputs, timeout_seconds, context=None):
    context = context or {}
    try:
        validate_code(code)
    except (ValueError, SyntaxError, RecursionError) as error:
        raise AnalysisError('PYTHON_CODE_INVALID', str(error)) from error
    public = [{key: value for key, value in item.items() if key != '_path'} for item in inputs]
    request = {'type': 'INIT', 'protocolVersion': PROTOCOL_VERSION, 'code': code, 'inputs': public}
    if len(encode(request)) > MAX_DATA_BYTES:
        raise AnalysisError('PYTHON_INPUT_LIMIT', '完整冻结输入超过受控传输的单次字节上限，未截断或执行；大网格使用已保存文件的流式读取。')
    run_ref = context.get('runRef') or 'python_' + uuid4().hex
    backend, start = backend_factory(), perf_counter()
    plan = await backend.prepare(session=context.get('sessionKey', run_ref), run_ref=run_ref, timeout_seconds=timeout_seconds)
    metadata = {**plan['metadata'], 'protocolVersion': PROTOCOL_VERSION, 'provenanceMode': PROVENANCE_MODE,
                'prepareMs': round((perf_counter() - start) * 1000 - plan['metadata'].get('queueMs', 0), 2)}
    journal_root = context.get('journalRoot') or plan['journalRoot']
    journal = journal_root / (hashlib.sha256(run_ref.encode()).hexdigest() + '.jsonl')
    broker, process, log_task, result, failure = None, None, None, None, None
    work, logs, log_bytes = [], bytearray(), 0

    async def host_call(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        work.append(task)
        return await asyncio.shield(task)

    async def pump_logs():
        nonlocal log_bytes
        while chunk := await process.stderr.read(16384):
            log_bytes += len(chunk)
            if len(logs) < plan['policy'].log_bytes:
                logs.extend(chunk[:plan['policy'].log_bytes - len(logs)])

    async def exchange():
        await host_call(broker.inline_pending)
        await write_async(process.stdin, request)
        await host_call(broker.inline_sent)
        while True:
            message = await read_async(process.stdout)
            kind = message['type']
            if kind in ('RESULT', 'ERROR'):
                if set(message) - ({'type', 'result', 'interpreter'} if kind == 'RESULT' else {'type', 'error'}):
                    raise ValueError('结果消息不能提交来源记录或审查状态')
                process.stdin.close()
                trailing = await process.stdout.read(1)
                if trailing:
                    raise ValueError('结果之后存在额外执行协议数据')
                await process.wait()
                return message
            try:
                if kind == 'READ_OPEN' and set(message) == {'type', 'sourceRef', 'where'}:
                    response = await host_call(broker.open, message['sourceRef'], message['where'])
                    pending = None
                elif kind == 'READ_NEXT' and set(message) == {'type', 'cursor', 'sequence'}:
                    response, pending = await host_call(broker.next, message['cursor'], message['sequence'])
                elif kind == 'READ_CLOSE' and set(message) == {'type', 'cursor'}:
                    response = await host_call(broker.close_cursor, message['cursor'])
                    pending = None
                else:
                    await host_call(broker.violation, 'UNKNOWN_PROTOCOL_MESSAGE')
                    raise ValueError('执行协议消息或参数不合法')
            except InputRequestError as error:
                await write_async(process.stdin, {'type': 'ERROR', 'message': str(error)[:1600]})
                continue
            await write_async(process.stdin, response)
            if pending is not None:
                await host_call(broker.sent, pending)

    execution_start = perf_counter()
    try:
        broker = await host_call(InputBroker, inputs, journal)
        container_start = perf_counter()
        process = await backend.start(plan)
        metadata['startupMs'] = round((perf_counter() - container_start) * 1000, 2)
        execution_start = perf_counter()
        log_task = asyncio.create_task(pump_logs())
        result = await asyncio.wait_for(exchange(), timeout_seconds)
        await log_task
        if result['type'] == 'ERROR' or process.returncode:
            details = result.get('error', {})
            if not isinstance(details, dict):
                raise ValueError('执行错误必须是对象')
            message = 'Python 分析未执行完成。'
            if details.get('type') == 'TypeError' and 'keys must be str, int, float, bool or None, not tuple' in str(details.get('message', '')):
                message += '计算中可以按多个字段分组。交付结果时，请将每个组合键展开为一行，每个维度和数值各用一个有名称的字段，再使用原输入重试。'
            raise AnalysisError('PYTHON_EXECUTION_FAILED', message,
                {**details, 'exitCode': process.returncode, 'requiresSourceQuery': False})
        if not broker.complete:
            raise AnalysisError('PYTHON_INPUT_INCOMPLETE', '输入提供记录未完整提交，不能登记成功计算。')
        values = result.get('result')
        if not isinstance(values, list) or not 1 <= len(values) <= 1000 or len(encode({'result': values})) > MAX_BYTES:
            raise AnalysisError('PYTHON_OUTPUT_LIMIT', 'Python 产物结构或容量不合法。')
        # Even a forged RESULT cannot bypass the host's precision/output boundary.
        values = normalize_output(values)
        interpreter = result.get('interpreter', {})
        if not isinstance(interpreter, dict):
            raise ValueError('运行环境信息必须是对象')
        metadata['interpreter'] = {key: str(value)[:100] for key, value in interpreter.items()
                                   if key in ('python', 'system', 'architecture')}
    except asyncio.TimeoutError:
        failure = AnalysisError('PYTHON_TIMEOUT', 'Python 分析超时，正在终止整个任务容器；原始数据保留。')
    except asyncio.CancelledError as error:
        failure = error
    except AnalysisError as error:
        failure = error
    except (OSError, EOFError, ValueError, TypeError, KeyError, RecursionError, asyncio.IncompleteReadError) as error:
        failure = AnalysisError('PYTHON_EXECUTION_FAILED', 'Python 执行或输入通道失败。',
            {'type': type(error).__name__, 'message': str(error)[:1600], 'requiresSourceQuery': False})
    finally:
        if broker is not None:
            broker.stopped.set()
        if work:
            await asyncio.gather(*work, return_exceptions=True)
        metadata['executeMs'] = round((perf_counter() - execution_start) * 1000, 2)
        cleanup_start = perf_counter()
        try:
            metadata['cleanupStatus'] = await asyncio.shield(backend.cleanup(plan))
        except Exception:
            metadata['cleanupStatus'] = 'CLEANUP_PENDING'
        metadata['cleanupMs'] = round((perf_counter() - cleanup_start) * 1000, 2)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if log_task is not None:
            log_task.cancel()
            await asyncio.gather(log_task, return_exceptions=True)
        metadata['logBytes'] = log_bytes
        metadata['logTruncated'] = log_bytes > plan['policy'].log_bytes
        try:
            manifest = await asyncio.to_thread(broker.finish) if broker is not None else {}
        except OSError as error:
            manifest = {**broker.manifest, 'journalFinalized': False}
            failure = failure or AnalysisError('PYTHON_INPUT_INCOMPLETE', '输入台账归档失败，不能登记成功计算。', {'type':type(error).__name__})
        metadata['inputDeliveryManifestRef'] = run_ref + ':inputs'
        if not context.get('journalRoot'):
            try:
                journal.unlink(missing_ok=True)
            except OSError:
                pass
    if failure is not None:
        failure.execution_evidence = {'runtime': metadata, 'inputDeliveryManifest': manifest}
        if isinstance(failure, AnalysisError):
            failure.details = {**(failure.details or {}), 'runtime': metadata,
                'diagnostic': logs.decode('utf-8', errors='replace')[-1200:]}
        raise failure
    output = Outputs(values)
    output.runtime_metadata = metadata
    output.input_delivery_manifest = manifest
    output.input_delivery_summary = delivery_summary(manifest)
    output.journal_path = journal if context.get('journalRoot') else None
    return output
