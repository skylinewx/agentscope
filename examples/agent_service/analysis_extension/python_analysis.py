"""Execute secondary analysis against immutable facts, then register derived values."""
import asyncio
from copy import deepcopy
import json
import logging
import sys
from time import perf_counter
from uuid import uuid4

from .models import AnalysisError, canonical, digest, now
from .python_worker import MAX_BYTES, GRID_WHERE_FIELDS, validate_code
from .python_products import bind_single_grid, load_files, products, deliver_files


def input_record(ref, value):
    """执行与复用使用同一输入投影，补核验引用不应使未改变的数据重新计算。"""
    record = {'ref': ref, **{key: deepcopy(value[key]) for key in
        ('value', 'valueState', 'unit', 'scale', 'period', 'executionPeriod', 'dimensions', 'dimensionNames',
         'effectiveDimensions', 'objectId', 'objectLabel', 'headers', 'measureCode', 'sourceMeasureCode',
         'formula', 'formulaStatus', 'cellAddress', 'inputRole', 'grain', 'rowIdentity',
         'stepId', 'bindingRef', 'observationId', 'batchId', 'rowIndex', 'columnName', 'cellKey',
         'operator', 'checkScope', 'sourceValue', 'localValue', 'localDifference', 'difference', 'formulaTrace',
         'outputKind', 'columns', 'rows', 'data', 'text', 'filename', 'sourceFactIds', 'kind', 'contentDigest') if key in value}}
    if value.get('operator') and value['operator'] != 'PYTHON':
        # COUNT、SORT、公式等算子结构不同，完整保留其业务输出，避免脚本再查一次派生详情。
        record.update({key: deepcopy(item) for key, item in value.items()
                       if key not in ('derivedId', 'scopeId', 'inputRefs', 'sourceFactIds')})
    if value.get('operator') == 'FORMULA_CHECK':
        # 与页面绑定使用同一字段解释，不能将逐层缺数改成本层缺数，或把差额重算为另一口径。
        from .validation import executable_field, selected_value
        for field in ('sourceValue', 'localValue', 'localDifference', 'difference'):
            record[field] = selected_value(value, field) if executable_field(value, field) else None
    return record


def file_continuation(grid_inputs, file_inputs):
    """文件执行只返回执行引用；后续代码复用已授权输入，不伪造业务分析版本。"""
    inputs = {key: deepcopy(value) for key, value in (('gridInputs', grid_inputs), ('fileInputs', file_inputs)) if value}
    return {'input': inputs,
            'hint': '这是文件执行记录，未创建 analysisRef。若还需计算，沿用 input 中的文件引用并提交新代码和 requestKey；不要把 pythonRunRef 填入 analysisRef。'}


async def execute_python(code, inputs, timeout_seconds):
    try:
        tree = validate_code(code)
    except (ValueError, SyntaxError) as error:
        raise AnalysisError('PYTHON_CODE_INVALID', str(error)) from error
    payload = canonical({'code': code, 'inputs': inputs, 'timeoutSeconds': timeout_seconds})
    if len(payload) > MAX_BYTES:
        raise AnalysisError('PYTHON_INPUT_LIMIT', '输入超出 Python 单次处理容量；保留完整业务范围，选择必要字段和引用。')
    from .python_environment import execution_plan, cleanup
    prepared = perf_counter()
    try:
        plan = await execution_plan(tree, inputs)
    except (OSError, asyncio.TimeoutError) as error:
        raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '分析执行环境暂不可用；已保留原输入，不能在宿主开放执行。') from error
    payload = canonical({'code': code, 'inputs': plan['inputs'], 'timeoutSeconds': timeout_seconds})
    metadata = {**plan['metadata'], 'prepareMs': round((perf_counter() - prepared) * 1000, 2)}
    process = None
    execution_started = perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(*plan['command'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({'creationflags': 0x08000000} if sys.platform == 'win32' else {}))
        output, errors = await asyncio.wait_for(process.communicate(payload), timeout=timeout_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        if asyncio.current_task().cancelling():
            raise
        raise AnalysisError('PYTHON_TIMEOUT', 'Python 分析超时，子进程已终止；原始数据未改变。') from None
    finally:
        await cleanup(plan)
    metadata['executeMs'] = round((perf_counter() - execution_started) * 1000, 2)
    if len(output) > MAX_BYTES or len(errors) > MAX_BYTES:
        raise AnalysisError('PYTHON_OUTPUT_LIMIT', 'Python 输出超过容量上限。')
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        raise AnalysisError('PYTHON_EXECUTION_FAILED', 'Python 子进程异常退出，可能触发资源限制。',
                            {'exitCode': process.returncode, 'runtime': metadata, 'diagnostic': errors.decode('utf-8', errors='replace')[-1200:]}) from None
    if process.returncode or result.get('error'):
        details = {**result.get('error', {}), 'runtime': metadata}
        if details.get('type') == 'NameError':
            details.update(requiresSourceQuery=False, nextAction='FIX_CODE_NAME',
                           inputVariable='inputs', outputVariable='result',
                           hint='宿主提供 inputs 数组、grid_rows、D/Decimal；result 由代码赋值。输入文件也在 inputs 中，用实际 inputs 项的 ref 读取；其他名称须由代码自己定义，不要重新取数。')
        elif details.get('type') == 'InvalidOperation':
            details.update(requiresSourceQuery=False, nextAction='CHECK_DECIMAL_INPUT',
                           hint='网格包含文本表头，先用实际 measureCode 或 cellKeys 选中业务数据格，再转换 cell["value"]；null 保留为空。已选数据格仍无法转换时保留原值并报告异常，不把异常当 0 或静默丢弃，不重新取数。')
        elif details.get('type') == 'ValueError' and details.get('message', '').startswith('grid_rows 筛选字段'):
            details.update(requiresSourceQuery=False, nextAction='FIX_GRID_FILTER',
                           availableWhereFields=sorted(GRID_WHERE_FIELDS),
                           hint='mapTypes 是数组；measureCode 使用实际指标标识。只修改过滤参数，复用原 gridInputs。')
        raise AnalysisError('PYTHON_EXECUTION_FAILED', 'Python 分析未执行完成。', details)
    class Outputs(list):
        pass
    outputs = Outputs(result['result'])
    outputs.file_reads = result.get('fileReads', [])
    metadata['interpreter'] = result.get('interpreter', {})
    outputs.runtime_metadata = metadata
    return outputs


def validate_python_work_plan(work_plan, operations, base=None):
    """在构建或执行前指出脚本产物与原生算子混用，避免生成报告后才发现待办无法完成。"""
    known = {value['derivedId'] for value in (base or {}).get('derivedValues', [])}
    known.update(reference for operation in operations
                 if isinstance(operation, dict) and isinstance(operation.get('id'), str)
                 for reference in (operation['id'], 'derived_' + operation['id']))
    for task in work_plan or []:
        if (not isinstance(task, dict) or task.get('kind') != 'COMPUTATION'
                or task.get('operator') in (None, 'PYTHON')):
            continue
        if not isinstance(task.get('refs', []), list):
            continue
        unknown = [reference for reference in task.get('refs', []) if isinstance(reference, str) and reference not in known]
        if unknown:
            raise AnalysisError('PYTHON_WORK_PLAN_OPERATOR',
                '工作项引用本次脚本输出时，operator 使用 PYTHON；原生算子工作项应引用 operations 的实际结果。',
                {'taskId': task.get('taskId'), 'operator': task['operator'], 'unresolvedRefs': unknown,
                 'path': '/workPlan', 'requiresSourceQuery': False,
                 'hint': '脚本自行计算比例或合计时使用 kind=COMPUTATION、operator=PYTHON，refs 填 result.name；FORMULA_CHECK/RECONCILE 等原生检查继续保留原算子并在 operations 中声明，不能用脚本替代来源公式检查。'})


async def analyze(service, code, purpose, requestKey, analysisRef=None, inputRefs=None, timeoutSeconds=10,
                  captureRefs=None, selections=None, scope=None, fileInputs=None, gridInputs=None, workPlan=None,
                  answerOutputs=None, operations=None, fallbackReason=None):
    await service.authorize()
    from .code_decisions import decision, log_decision
    capability_decision = decision(fallbackReason, purpose, code)
    if type(timeoutSeconds) is not int or not 1 <= timeoutSeconds <= 60:
        raise AnalysisError('PYTHON_TIMEOUT_LIMIT', '执行时间限定为 1—60 秒。')
    if isinstance(analysisRef, str) and analysisRef.startswith('python_'):
        raise AnalysisError('PYTHON_INPUT_REFS', 'pythonRunRef 是执行记录，不能作为 analysisRef。',
                            {'requiresSourceQuery': False, 'field': 'analysisRef',
                             'nextAction': 'CORRECT_REFERENCE_KIND',
                             'hint': '仅文件执行不会创建 analysisRef；继续同一文件时省略 analysisRef，沿用原 gridInputs/fileInputs。已有业务分析时使用工具实际返回的 analysisRef，不自行转换引用。'})
    if analysisRef and any(value is not None for value in (captureRefs, selections, scope)):
        raise AnalysisError('PYTHON_INPUT_REFS', 'analysisRef 与查询输入 captureRefs/selections/scope 二选一。')
    if workPlan is not None and (not isinstance(workPlan, list) or not analysisRef and captureRefs is None):
        raise AnalysisError('INVALID_WORK_PLAN', 'workPlan 随已有分析或查询输入登记；文件单独执行不创建业务分析计划。')
    if operations is not None and (not isinstance(operations, list) or len(operations) > service.store.limits.max_operations
                                   or operations and not analysisRef and captureRefs is None):
        raise AnalysisError('INVALID_OPERATIONS', 'operations 需要已选查询输入或 analysisRef，沿用现有算子容量。')
    operations = [] if operations is None else operations
    if workPlan:
        validate_python_work_plan(workPlan, operations, service.store.get_analysis(service.owner, analysisRef) if analysisRef else None)
    operation_ids = {'derived_' + operation['id'] for operation in operations
                     if isinstance(operation, dict) and isinstance(operation.get('id'), str)}
    if captureRefs is not None:
        if not scope:
            raise AnalysisError('PYTHON_INPUT_REFS', '直接处理已准备查询结果时提供 captureRefs 和 scope。',
                                {'missingFields': ['scope'],
                                 'requiresSourceQuery': False})
        # 计算尚未执行时先登记必要工作，输出名称在执行成功后绑定真实派生引用。
        pending_work = deepcopy(workPlan)
        for task in pending_work or []:
            if (isinstance(task, dict) and task.get('kind') == 'COMPUTATION'
                    and task.get('operator') in (None, 'PYTHON')):
                task['refs'] = []
        built = await service.build(captureRefs, selections, scope, operations, 'python-input:' + requestKey,
                                    workPlan=pending_work)
        analysisRef = built['analysisRef']
    elif analysisRef and operations:
        pending_work = deepcopy(workPlan)
        for task in pending_work or []:
            if isinstance(task, dict) and task.get('kind') == 'COMPUTATION' and task.get('operator') in (None, 'PYTHON'):
                task['refs'] = []
        built = await service.build(operations=operations, requestKey='python-input:' + requestKey,
                                    baseAnalysisRef=analysisRef, workPlan=pending_work)
        analysisRef = built['analysisRef']
    base = service.store.get_analysis(service.owner, analysisRef) if analysisRef else None
    if service.turn is not None and base is not None:
        from .grouping_policy import grouping_issues
        issues = grouping_issues(service.turn, base)
        if issues:
            raise AnalysisError('GROUPING_RESULT_REQUIRED', issues[0]['message'], {'issues': issues})
    known = {item.get('factId', item.get('derivedId')): item for item in base['facts'] + base['derivedValues']} if base else {}
    def usable_input(value):
        return (value.get('calculationValidation', 'PASS') == 'PASS'
                and value.get('precision') != 'display_only'
                and value.get('valueState') in ('PRESENT', 'NO_VALUE', 'NOT_APPLICABLE'))

    excluded_operations = []
    if inputRefs is None:
        inputRefs = [fact['factId'] for fact in base['facts']] if base else []
        if base:
            # 同一调用里的构成核对可能因来源空值未完成，不应阻断独立的统计。
            # 保留原核对结果和工作项状态，只将可用于计算的派生值自动送入 Python。
            for value in base['derivedValues']:
                if value['derivedId'] not in operation_ids:
                    continue
                if usable_input(value):
                    inputRefs.append(value['derivedId'])
                else:
                    excluded_operations.append({'ref': value['derivedId'],
                        'executionStatus': value.get('executionStatus'), 'valueState': value.get('valueState')})
    elif operations and isinstance(inputRefs, list):
        inputRefs = ['derived_' + reference if isinstance(reference, str) and 'derived_' + reference in operation_ids else reference
                     for reference in inputRefs]
    if (not isinstance(inputRefs, list) or len(inputRefs) > 5000
            or any(not isinstance(ref, str) for ref in inputRefs) or len(set(inputRefs)) != len(inputRefs)
            or any(ref not in known for ref in inputRefs)):
        raise AnalysisError('PYTHON_INPUT_REFS', 'inputRefs 必须引用当前分析内最多 5000 个不重复的真实事实或派生值；仅使用文件时可显式传空数组。')
    inputs = []
    for ref in inputRefs:
        value = known[ref]
        if not usable_input(value):
            raise AnalysisError('PYTHON_INPUT_UNVERIFIED', '所选输入不能用于计算；保留其诊断，改用本次计算实际需要的可用输入。',
                                {'ref': ref, 'analysisRef': analysisRef, 'requiresSourceQuery': False,
                                 'valueState': value.get('valueState'),
                                 'calculationValidation': value.get('calculationValidation'),
                                 'nextRead': {'section': 'derived' if 'derivedId' in value else 'facts',
                                              'analysisRef': analysisRef}})
        inputs.append(input_record(ref, value))
    files = load_files(service, [] if fileInputs is None else fileInputs)
    from .grid_files import resolve_input, read_summary
    if gridInputs is not None and (not isinstance(gridInputs, list) or not 1 <= len(gridInputs) <= 20):
        raise AnalysisError('GRID_INPUT_LIMIT', 'gridInputs 指定 1—20 个已捕获的网格文件。')
    grids = [resolve_input(service, specification) for specification in gridInputs or []]
    if service.turn is not None:
        for value in grids:
            service.turn._add_query_capture(value['captureRef'])
    if len({value['ref'] for value in grids}) != len(grids):
        raise AnalysisError('GRID_INPUT_LIMIT', '同一网格文件不能重复指定。')
    files.extend(grids)
    known.update({value['ref']: value for value in files})
    inputs.extend(files)
    inputRefs = inputRefs + [value['ref'] for value in files]
    if not inputs:
        raise AnalysisError('PYTHON_INPUT_REFS', '需要分析事实、查询回执或工作区数据文件作为输入。')
    request = {'python': {'analysisRef': analysisRef, 'inputRefs': inputRefs, 'code': code,
                         'purpose': purpose, 'timeoutSeconds': timeoutSeconds}, 'inputDigest': digest(inputs)}
    request['python']['capabilityDecision'] = capability_decision
    if workPlan is not None:
        request['python']['workPlan'] = deepcopy(workPlan)
    key = 'python:' + requestKey
    if base is None:
        reused_run = service.store.find_request(service.owner, 'python', key, digest(request))
        if reused_run:
            from .service import compact_derived
            execution = service.store.get_python_run(service.owner, reused_run)
            from .query_result_evidence import bind_answer_outputs
            return {'pythonRunRef': reused_run, 'status': 'COMPLETED', 'derivedValues': compact_derived(execution['outputs']),
                    **bind_answer_outputs(service, execution, answerOutputs),
                    'continuation': file_continuation(gridInputs, fileInputs),
                    'fileReads': read_summary(execution.get('fileReads', []))}
    reused = service.store.find_request(service.owner, 'build', key, digest(request)) if base else None
    if reused:
        artifact = service.store.get_analysis(service.owner, reused)
        from .query_result_evidence import bind_answer_outputs
        execution = artifact['pythonRuns'][-1]
        return {**service._built(artifact), 'pythonRunRef': execution['runRef'],
                'excludedOperationInputs': execution.get('excludedOperationInputs', []),
                **bind_answer_outputs(service, execution, answerOutputs)}
    started = perf_counter()
    # 每次实际执行有独立记录；同参数的新请求或失败重试不能覆盖旧执行证据。
    run_ref = 'python_' + uuid4().hex
    saved_files = [{key: value for key, value in item.items() if key != '_path'} for item in files]
    execution = {'runRef': run_ref, 'code': code, 'purpose': purpose, 'inputRefs': inputRefs,
                 'capabilityDecision': capability_decision,
                 'excludedOperationInputs': excluded_operations,
                 'inputDigest': digest(inputs), 'startedAt': now(), 'timeoutSeconds': timeoutSeconds,
                 'memoryLimitBytes': 256 * 1024 * 1024, 'sourceAnalysisRef': analysisRef,
                 'fileInputs': saved_files, 'precisionPolicy': 'DECIMAL_128', 'nullPolicy': 'EXPLICIT_IN_CODE',
                 # 来源凭据单独冻结，不参与计算输入摘要；补充同值来源不会使旧计算失效。
                 'inputRecords': [{**deepcopy(value), **{key: deepcopy(known[value['ref']][key]) for key in
                    ('pythonRunRef', 'captureRef', 'captureRefs', 'sourceContextId', 'taskName', 'formName', 'cellKey')
                    if key in known[value['ref']]}} for value in inputs if value['ref'] not in {item['ref'] for item in files}],
                 'businessValidation': 'NOT_REVIEWED', 'runtime': 'RESTRICTED_PYTHON'}
    # 先保存执行请求。取消、超时、代码错误仍有独立记录，不伪造成功分析版本。
    service.store.save_python_run(service.owner, run_ref, {**execution, 'status': 'RUNNING'})
    try:
        outputs = await execute_python(code, inputs, timeoutSeconds)
        execution.update(getattr(outputs, 'runtime_metadata', {}))
        execution['fileReads'] = getattr(outputs, 'file_reads', [])
        positions = bind_single_grid(outputs, inputs, execution['fileReads'])
        if positions:
            execution['singleGridSourceBinding'] = {'ref': inputs[0]['ref'], 'outputIndices': positions}
        values = products(outputs, known, inputRefs, base['scope']['scopeId'] if base else run_ref, run_ref)
        deliver_files(service, values)
        execution.update(status='COMPLETED', outputs=values, elapsedMs=round((perf_counter()-started)*1000, 2))
        service.store.save_python_run(service.owner, run_ref, execution)
        logging.getLogger(__name__).info('代码分析 执行=%s 环境=%s 输入=%d 产物=%d 准备毫秒=%.2f 执行毫秒=%.2f 总耗时毫秒=%.2f',
                                        run_ref, execution['runtime'], len(inputs), len(values),
                                        execution.get('prepareMs', 0), execution.get('executeMs', 0), execution['elapsedMs'])
    except BaseException as error:
        execution.update(status='CANCELLED' if isinstance(error, asyncio.CancelledError) else 'FAILED',
                         error=error.as_dict() if isinstance(error, AnalysisError) else {'type': type(error).__name__},
                         elapsedMs=round((perf_counter()-started)*1000, 2))
        service.store.save_python_run(service.owner, run_ref, execution)
        if isinstance(error, AnalysisError):
            error.details = {**(error.details or {}), 'pythonRunRef': run_ref,
                             'nextRead': {'section': 'python', 'pythonRunRef': run_ref}}
            if analysisRef:
                error.details.update(analysisRef=analysisRef, requiresSourceQuery=False,
                                     retryInput={'analysisRef': analysisRef, 'inputRefs': [ref for ref in inputRefs if ref in known and ref not in {item['ref'] for item in files}],
                                                 **file_continuation(gridInputs, fileInputs)['input']})
            else:
                error.details.update(requiresSourceQuery=False, retryInput=file_continuation(gridInputs, fileInputs)['input'])
        raise
    finally:
        log_decision(execution)
        from .timing import record_timing
        record_timing(getattr(service, 'turn', None), 'pythonExecution',
                      (perf_counter() - started) * 1000, includedIn='calculation',
                      pythonRunRef=run_ref, status=execution['status'], inputCount=len(inputs),
                      outputCount=len(execution.get('outputs', [])), codeChars=len(code),
                      prepareMs=execution.get('prepareMs'), executeMs=execution.get('executeMs'),
                      fileReadCount=len(execution.get('fileReads', [])))
    if base is None:
        service.store.complete_python_request(service.owner, key, digest(request), run_ref)
        from .service import compact_derived
        from .query_result_evidence import bind_answer_outputs
        return {'pythonRunRef': run_ref, 'status': 'COMPLETED', 'derivedValues': compact_derived(values),
                **bind_answer_outputs(service, execution, answerOutputs),
                'continuation': file_continuation(gridInputs, fileInputs),
                'fileReads': read_summary(execution.get('fileReads', [])),
                'boundary': ('网格来源及完整文件已绑定；fileReads 记录实际扫描范围。公式和业务聚合仍沿用预算口径。'
                             if grids else '工作区文件已冻结并执行；文件内容不是预算来源核验证据。')}
    artifact = deepcopy(base)
    artifact['captureRefs'] = list(dict.fromkeys([*artifact['captureRefs'], *[value['captureRef'] for value in grids]]))
    artifact['derivedValues'] += values
    artifact['pythonRuns'] = [*artifact.get('pythonRuns', []), execution]
    artifact.update(claims=[], viewPlan=[], findings=[], actions=[], baseAnalysisRef=analysisRef)
    for field in ('freeDocument', 'delivery'):
        artifact.pop(field, None)
    artifact['calculationValidation'] = 'PASS' if all(value['calculationValidation'] == 'PASS' for value in artifact['derivedValues']) else 'BLOCKED'
    artifact['planDigest'] = digest([base['planDigest'], request])
    try:
        # 执行与登记分别留痕：计划引用不合法不能把已执行的脚本伪装成执行失败。
        if workPlan is not None:
            from .completion import continue_plan, completion_summary, validate_target_continuity
            _, effective_work = continue_plan(base, base['plan']['scope'], workPlan)
            output_refs = {output['name']: value['derivedId'] for output, value in zip(outputs, values)}
            output_refs.update({reference.removeprefix('derived_'): reference for reference in operation_ids})
            for task in effective_work:
                if task.get('kind') == 'COMPUTATION':
                    from .scalar_references import normalize_reference
                    aliases = {**output_refs, **{ref: ref for ref in known}}
                    task['refs'] = [normalize_reference(ref, aliases) for ref in task.get('refs', [])]
            artifact['plan']['workPlan'] = effective_work
            artifact.pop('workRefs', None)
            completion_summary(artifact)
            validate_target_continuity(base, artifact)
        artifact = service.store.build(service.owner, request, artifact, key, analysisRef)
    except AnalysisError as error:
        execution['registrationError'] = error.as_dict()
        service.store.save_python_run(service.owner, run_ref, execution)
        error.details = {**(error.details or {}), 'pythonRunRef': run_ref,
                         'nextRead': {'section': 'python', 'pythonRunRef': run_ref}}
        raise
    from .query_result_evidence import bind_answer_outputs
    return {**service._built(artifact), 'pythonRunRef': run_ref,
            'excludedOperationInputs': excluded_operations,
            **bind_answer_outputs(service, execution, answerOutputs),
            'boundary': '代码已执行且输出引用有效；业务含义仍须核对，原始来源事实未改变。'}
