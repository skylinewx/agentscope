"""Capture the complete permitted tool response, before outer middleware."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import logging
from time import perf_counter

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from .models import AnalysisError, Owner, digest, exact_loads, json_values, now
from .store import AnalysisStore
from .query_results import encode
from .adapters import default_sources


def leased_mcp_origin(tool, resource_ids: frozenset[str]) -> dict | None:
    # The service's actual registered resource, not a model-provided name.
    from agentscope.app._service._resources import _LeasedTool
    from agentscope.tool._adapters import MCPTool
    if not isinstance(tool, _LeasedTool) or not isinstance(tool._tool, MCPTool):
        return None
    entry, inner = tool._entry, tool._tool
    if entry.resource_id not in resource_ids:
        return None
    return {"resourceId": entry.resource_id, "serverName": inner.mcp_name,
            "originalToolName": inner._tool.name, "connectionFingerprint": entry.fingerprint}


def host_request(owner, turn_key, origin, request):
    """同一提问的同义重试沿用标识，参数、来源或提问改变则获得新标识。"""
    if request.get('contractVersion') != '2.0' or 'requestId' not in request:
        return request
    from .query_attempts import logical_request, request_identity
    return {**request, 'requestId': request_identity(logical_request(owner, turn_key, origin, request))}


class ReceiptCapture:
    def __init__(self, store: AnalysisStore, owner: Owner, resource_ids: frozenset[str], authorize, number_formats: dict | None = None, sources=None):
        self.store, self.owner, self.resource_ids, self.authorize = store, owner, resource_ids, authorize
        self.number_formats = number_formats or {}
        self.sources = sources or default_sources()
        self.action_ref_provider = lambda: None
        self.turn_key_provider = lambda: None
        self.query_cache_provider = lambda: None
        self.task_context_provider = lambda: {}
        self.request_preparer = lambda origin, request: request

    def _model_projection(self, adapter, receipt, capture, source_chars):
        """资料与原协议导航共享输出额度，完整原回执始终先保存。"""
        from .analysis_materials import response_materials, materials
        from .query_results import compact_receipt, MODEL_CHARS, COMPACT_AT
        reference = capture['captureRef']
        material = response_materials(capture, self.sources, store=self.store, owner=self.owner)
        if material is not None and adapter.adapter_id == 'budget-v2':
            for budget in (14000, 6000):
                if len(encode(material)) > budget:
                    material = materials(capture, self.sources, max_chars=budget, store=self.store, owner=self.owner)
                remaining = MODEL_CHARS - len(encode(material)) - 500
                projected = compact_receipt(receipt, reference, max(source_chars, COMPACT_AT), max_chars=remaining)
                if projected is not None:
                    projected['analysisMaterials'] = material
                    projected['analysisRead'] = {'section': 'materials', 'captureRef': reference}
                    return projected
        return adapter.compact(receipt, reference, source_chars)

    async def run(self, agent, input_kwargs, next_handler):
        from .query_continuation import continuation_request, combined_response, MAX_PAGES, MAX_CONTINUATION_SECONDS
        tool = await agent.toolkit.get_tool(input_kwargs['tool_call'].name)
        origin = leased_mcp_origin(tool, self.resource_ids)
        adapter = self.sources.for_tool(tool, origin)
        enabled = (adapter is not None and adapter.adapter_id == 'budget-v2' and origin
                   and origin['originalToolName'] == 'execute_semantic_plan' and not self.action_ref_provider())
        events, seen, started, stopped = [], set(), perf_counter(), 'SOURCE_STOPPED'
        retries = []
        while True:
            response = None
            async for event in self._run_page(agent, input_kwargs, next_handler):
                if enabled and isinstance(event, ToolResponse):
                    response = event
                else:
                    yield event
            if response is None:
                return
            if not response.metadata.get('analysisCaptureRef'):
                # 存储故障时保留此前每页及最终错误，不声称已完整捕获。
                response = response.model_copy(deep=True)
                response.metadata['analysisCaptureRefs'] = [event.metadata['analysisCaptureRef'] for event in events]
                response.content = [block for event in events for block in event.content] + response.content
                yield response
                return
            try:
                saved = self.store.get_capture(self.owner, response.metadata['analysisCaptureRef'])
                receipt = adapter.raw_receipt(saved['response'])
                from .query_attempts import can_retry, exhausted
                if not response.metadata.get('analysisCaptureError') and can_retry(self.store, saved.get('queryAttempt'), receipt):
                    # 来源已明确终态失败且允许重试；保留失败证据，宿主领取下一身份。
                    # 失败尝试不伪装成续页或成功数据，不增加模型决策轮次。
                    retries.append({'captureRef': saved['captureRef'], 'executionId': receipt['executionId'],
                                    'errorCode': receipt['error'].get('code')})
                    logging.getLogger(__name__).info('查询终态失败后重试：尝试=%s 原执行=%s 原因=%s',
                        saved['queryAttempt']['attempt'] + 1, receipt['executionId'], receipt['error'].get('code'))
                    call = input_kwargs['tool_call'].model_copy(update={'input': encode(saved['request'])})
                    input_kwargs = {**input_kwargs, 'tool_call': call}
                    await asyncio.sleep(0.25)
                    continue
                if exhausted(self.store, saved.get('queryAttempt'), receipt):
                    recovery = {'code': 'QUERY_RETRY_LIMIT_REACHED', 'retryable': False,
                                'retryCount': saved['queryAttempt']['attempt'],
                                'sourceExecutionId': receipt['executionId'],
                                'message': '来源仍失败，本轮宿主重试额度已用尽。保留原始错误说明未完成范围，不重复提交或修改业务参数绕过。'}
                    response.metadata['queryRecovery'] = recovery
                    response.content.append(TextBlock(text=encode({'hostQueryRecovery': recovery})))
                request = continuation_request(receipt, saved['request']) if response.state == ToolResultState.SUCCESS else None
            except (AnalysisError, ValueError, TypeError, OSError, sqlite3.Error):
                request = None
            events.append(response)
            if retries:
                response.metadata['queryRetries'] = retries.copy()
                response.metadata.setdefault('queryPerformance', {}).update(
                    retryCount=len(retries), elapsedMs=round((perf_counter() - started) * 1000, 2))
            if not request:
                break
            cursor = request['resumeRef']
            if cursor in seen or cursor == saved['request'].get('resumeRef'):
                stopped = 'REPEATED_CURSOR'
                break
            if len(events) >= MAX_PAGES or perf_counter() - started >= MAX_CONTINUATION_SECONDS:
                stopped = 'CONTINUATION_BUDGET'
                break
            seen.add(cursor)
            # 同一调用身份登记每页，后台恢复能一次找回所有证据；不生成新的业务计划。
            call = input_kwargs['tool_call'].model_copy(update={'input': encode(request)})
            input_kwargs = {**input_kwargs, 'tool_call': call}
        if len(events) == 1:
            yield events[0]
        else:
            try:
                response = combined_response(self, adapter, events, started, stopped)
            except (AnalysisError, ValueError, TypeError, OSError, sqlite3.Error):
                # 投影失败不丢失已取得证据，也不误报查询失败。
                logging.getLogger(__name__).exception('连续查询回执投影失败，保留各页原始输出')
                response = events[-1].model_copy(deep=True)
                response.content = [block for event in events for block in event.content]
                response.metadata['analysisCaptureRefs'] = [event.metadata['analysisCaptureRef'] for event in events]
            yield response

    async def _run_page(self, agent, input_kwargs, next_handler):
        call = input_kwargs["tool_call"]
        tool = await agent.toolkit.get_tool(call.name)
        origin = leased_mcp_origin(tool, self.resource_ids)
        adapter = self.sources.for_tool(tool, origin)
        if adapter is None:
            async for event in next_handler(**input_kwargs):
                yield event
            return
        await self.authorize()
        action_run = self.action_ref_provider()
        turn_key = self.turn_key_provider()
        # Preserve the exact submitted input string as well as its parsed form.
        request_text = call.input if isinstance(call.input, str) else json.dumps(call.input)
        submitted_request_text = request_text
        try:
            request = json_values(exact_loads(request_text))
            if not isinstance(request, dict):
                raise AnalysisError('INVALID_JSON', '工具参数必须是 JSON 对象；字符串与标识须使用双引号。')
            normalized = self.request_preparer(origin, request) if adapter.adapter_id == 'budget-v2' else request
        except AnalysisError as error:
            # 在发出外部查询前返回可执行的修复参数；错误仍由本轮工具记录接管。
            yield ToolResponse(id=call.id, state=ToolResultState.ERROR,
                               content=[TextBlock(text=encode({'error': error.as_dict()}))],
                               metadata={'analysisTurnKey': turn_key})
            return
        attempt = None
        if adapter.adapter_id == 'budget-v2':
            if turn_key and normalized.get('contractVersion') == '2.0' and 'requestId' in normalized:
                from .query_attempts import prepare
                try:
                    normalized, attempt = prepare(self.store, self.owner, turn_key, origin, normalized)
                except (OSError, sqlite3.Error):
                    yield ToolResponse(id=call.id, state=ToolResultState.ERROR,
                        content=[TextBlock(text=encode({'error': {'code': 'STORE_UNAVAILABLE',
                            'message': '查询执行身份暂不可保存，本次尚未发出来源请求。'}}))],
                        metadata={'analysisTurnKey': turn_key})
                    return
            else:
                normalized = host_request(self.owner, turn_key, origin, normalized)
        if normalized != request:
            request = normalized
            request_text = json.dumps(request, ensure_ascii=False)
            # 保存原提交与实际执行条件；标识和缓存均基于已继承范围后的请求。
            call = call.model_copy(update={'input': request_text})
            input_kwargs = {**input_kwargs, 'tool_call': call}
        base = {"toolName": call.name, "toolCallId": call.id, **origin,
                "sourceAdapter": adapter.snapshot(),
                "requestText": request_text, "submittedRequestText": submitted_request_text,
                "request": request, "capturedAt": now(),
                "numberFormat": self.number_formats.get(origin["resourceId"]), "turnKey": turn_key,
                **({'queryAttempt': attempt} if attempt else {})}
        # 只复用当前用户轮、同一连接及完全相同的只读执行参数。
        # PARTIAL/后台执行/失败回执不缓存，续取仍执行原恢复协议。
        cache = self.query_cache_provider()
        cache_key = (digest([origin, request]) if isinstance(cache, dict) and turn_key and not action_run
                     and adapter.adapter_id == 'budget-v2'
                     and origin['originalToolName'] == 'execute_semantic_plan' else None)
        if cache_key and cache_key in cache:
            try:
                saved = self.store.get_capture(self.owner, cache[cache_key])
                if saved.get('turnKey') == turn_key and saved.get('request') == request:
                    event = ToolResponse.model_validate(saved['response'])
                    receipt = adapter.raw_receipt(saved['response'])
                    ref = saved['captureRef']
                    # 与首次执行保持相同投影规则，不能吞掉额外内容块；投影失败保留原回执。
                    content = saved['response']['content']
                    if len(content) == 1 and content[0].get('type') == 'text':
                        try:
                            compact = self._model_projection(adapter, receipt, saved, len(content[0].get('text', '')))
                            if compact is not None:
                                event.content = [TextBlock(text=encode(compact))]
                        except (AnalysisError, ValueError, TypeError):
                            logging.getLogger(__name__).exception('已保存查询回执投影失败，保留原始返回')
                    from agentscope.app._service._progress_context import public_observations
                    event.metadata['public_progress'] = public_observations(receipt, ref, call.name, request)
                    event.metadata.update(analysisCaptureRef=ref, analysisTurnKey=turn_key,
                                          queryPerformance={'reused': True, 'elapsedMs': 0,
                                                            'modelChars': sum(len(getattr(block, 'text', '')) for block in event.content)})
                    if saved.get('gridFiles'):
                        from .grid_files import file_summary
                        event.content.append(TextBlock(text=encode({'captureRef': ref, 'gridFiles': file_summary(saved),
                            'taskContext': self.task_context_provider(),
                            'nextRead': {'section': 'gridFiles', 'captureRef': ref},
                            'note': '这份完整网格是原始资料，预览仅说明结构。结合 taskContext 中的当前问题和承接范围，读取适用 Skill 的大网格/Python 文档后编写代码筛选、计算；使用 AnalysisPython.gridInputs 和 grid_rows，不重复展开。最终结果可在计算时通过 answerOutputs 选择，探索输出不用全部写入答复。'})))
                    yield event
                    return
            except (AnalysisError, OSError, sqlite3.Error):
                cache.pop(cache_key, None)
        finished = False
        started = perf_counter()
        try:
            async for event in next_handler(**input_kwargs):
                if isinstance(event, ToolResponse):
                    source_response_ms = (perf_counter() - started) * 1000
                    grid_receive_ms, capture_store_ms, projection_ms = 0, 0, 0
                    response = event.model_dump(mode="json")
                    summary = {}
                    receipt = None
                    try:
                        receipt = adapter.raw_receipt(response)
                        summary = adapter.summary(receipt)
                    except AnalysisError as exc:
                        summary = {"diagnostic": exc.as_dict()}
                    await self.authorize()
                    saved_ref = None
                    try:
                        grid_files, grid_errors = [], []
                        if receipt is not None and adapter.adapter_id == 'budget-v2':
                            from .grid_files import receive_files
                            grid_started = perf_counter()
                            try:
                                grid_files = await receive_files(receipt, tool, self.store, self.owner)
                            except (AnalysisError, OSError, ValueError) as failure:
                                grid_errors = [failure.as_dict() if isinstance(failure, AnalysisError)
                                               else {'code': 'GRID_FILE_DOWNLOAD', 'message': '网格文件接收失败，原始回执仍保留。'}]
                            grid_receive_ms = (perf_counter() - grid_started) * 1000
                        store_started = perf_counter()
                        ref = self.store.capture(self.owner, {**base, "response": response,
                            "captureState": response["state"], "receiptSummary": summary,
                            **({'gridFiles': grid_files} if grid_files else {}),
                            **({'gridFileErrors': grid_errors} if grid_errors else {})})
                        capture_store_ms = (perf_counter() - store_started) * 1000
                        event = event.model_copy(deep=True)
                        event.metadata["analysisCaptureRef"] = ref
                        event.metadata["analysisTurnKey"] = turn_key
                        saved_ref = ref
                        if attempt:
                            from .query_attempts import record
                            record(self.store, self.owner, turn_key, attempt, receipt)
                            event.metadata['queryAttempt'] = attempt
                        if cache_key and not grid_errors and response['state'] == 'success' and receipt and receipt.get('status') == 'SUCCEEDED':
                            cache[cache_key] = ref
                            while len(cache) > 128:
                                cache.pop(next(iter(cache)))
                        if action_run:
                            self.store.record_action_capture(self.owner, action_run, ref, response["state"])
                    except (AnalysisError, OSError, sqlite3.Error) as exc:
                        # Original query remains usable; never claim an input was saved.
                        if not isinstance(exc, AnalysisError):
                            exc = AnalysisError("STORE_UNAVAILABLE", "分析存储暂不可用，原查询结果仍保留。")
                        event = event.model_copy(deep=True)
                        event.metadata["analysisCaptureError"] = exc.as_dict()
                        event.metadata["analysisTurnKey"] = turn_key
                        event.content.append(TextBlock(text="\n分析输入保存失败：" + str(exc)))
                    if receipt is not None and saved_ref is not None:
                        # Preserve a bounded, attributable observation before
                        # projection/offloading hides the original result body.
                        from agentscope.app._service._progress_context import public_observations
                        event.metadata['public_progress'] = public_observations(receipt, saved_ref, call.name, request)
                        # Store succeeds first. Keep additional blocks intact, and never
                        # convert a projection failure into a false capture failure.
                        source_chars = sum(len(b.get('text', '')) for b in response['content'])
                        compacted = False
                        projection_started = perf_counter()
                        if len(response['content']) == 1 and response['content'][0].get('type') == 'text':
                            try:
                                compact = self._model_projection(adapter, receipt, self.store.get_capture(self.owner, saved_ref), source_chars)
                                if compact is not None:
                                    event.content = [TextBlock(text=encode(compact))]
                                    compacted = True
                            except (AnalysisError, ValueError, TypeError):
                                logging.getLogger(__name__).exception('查询回执投影失败，保留原始返回')
                        if grid_files or grid_errors:
                            from .grid_files import file_summary
                            event.content.append(TextBlock(text=encode({'captureRef': saved_ref,
                                'gridFiles': file_summary({'gridFiles': grid_files}), 'gridFileErrors': grid_errors,
                                'taskContext': self.task_context_provider(),
                                'nextRead': {'section': 'gridFiles', 'captureRef': saved_ref},
                                'note': '这份完整网格是原始资料，预览仅说明结构。结合 taskContext 中的当前问题和承接范围，读取适用 Skill 的大网格/Python 文档后编写代码筛选、计算。使用 AnalysisPython.gridInputs[{captureRef,fileRef}] 和 grid_rows(ref, where)，不需要通读全部文件到上下文，也不重复逐页展开。最终计算可用 answerOutputs 选择答复产物。下载失败时不得按空表分析。'})))
                        model_chars = sum(len(getattr(b, 'text', '')) for b in event.content)
                        projection_ms = (perf_counter() - projection_started) * 1000
                        event.metadata['queryPerformance'] = {'sourceChars': source_chars, 'modelChars': model_chars,
                            'compacted': compacted, 'reused': False, 'elapsedMs': round((perf_counter() - started) * 1000, 2),
                            'sourceResponseMs': round(source_response_ms, 2),
                            'gridReceiveMs': round(grid_receive_ms, 2), 'captureStoreMs': round(capture_store_ms, 2),
                            'projectionMs': round(projection_ms, 2),
                            'timingBoundary': '以上分段包含于当前工具总耗时；sourceResponseMs 包含网络和远端处理，不代表 SQL 耗时。'}
                        logging.getLogger(__name__).info('查询工具=%s 提问=%s 调用=%s 状态=%s 原始字符=%d 模型字符=%d 已压缩=%s 耗时毫秒=%.2f',
                            call.name, turn_key, call.id, summary.get('status'), source_chars, model_chars,
                            compacted, (perf_counter() - started) * 1000)
                    finished = True
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            if not finished:
                self._record_interruption(base, "INTERRUPTED", action_run)
            raise
        except Exception:
            if not finished:
                self._record_interruption(base, "ERROR", action_run)
            raise

    def _record_interruption(self, base, state, action_run=None):
        try:
            ref = self.store.capture(self.owner, {**base, "captureState": state,
                "response": {"content": [], "state": state}, "receiptSummary": {"status": state}})
            if action_run:
                self.store.record_action_capture(self.owner, action_run, ref, state)
        except (AnalysisError, OSError, sqlite3.Error):
            # A full/unavailable store must not mask the original cancellation.
            pass
