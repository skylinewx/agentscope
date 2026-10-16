"""Capture the complete permitted tool response, before outer middleware."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import logging
from time import perf_counter

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .models import AnalysisError, Owner, exact_loads, json_values, now
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


class ReceiptCapture:
    def __init__(self, store: AnalysisStore, owner: Owner, resource_ids: frozenset[str], authorize, number_formats: dict | None = None, sources=None):
        self.store, self.owner, self.resource_ids, self.authorize = store, owner, resource_ids, authorize
        self.number_formats = number_formats or {}
        self.sources = sources or default_sources()
        self.action_ref_provider = lambda: None

    async def run(self, agent, input_kwargs, next_handler):
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
        # Preserve the exact submitted input string as well as its parsed form.
        request_text = call.input if isinstance(call.input, str) else json.dumps(call.input)
        request = json_values(exact_loads(request_text))
        base = {"toolName": call.name, "toolCallId": call.id, **origin,
                "sourceAdapter": adapter.snapshot(),
                "requestText": request_text, "request": request, "capturedAt": now(),
                "numberFormat": self.number_formats.get(origin["resourceId"])}
        finished = False
        started = perf_counter()
        try:
            async for event in next_handler(**input_kwargs):
                if isinstance(event, ToolResponse):
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
                        ref = self.store.capture(self.owner, {**base, "response": response,
                            "captureState": response["state"], "receiptSummary": summary})
                        event = event.model_copy(deep=True)
                        event.metadata["analysisCaptureRef"] = ref
                        saved_ref = ref
                        if action_run:
                            self.store.record_action_capture(self.owner, action_run, ref, response["state"])
                    except (AnalysisError, OSError, sqlite3.Error) as exc:
                        # Original query remains usable; never claim an input was saved.
                        if not isinstance(exc, AnalysisError):
                            exc = AnalysisError("STORE_UNAVAILABLE", "分析存储暂不可用，原查询结果仍保留。")
                        event = event.model_copy(deep=True)
                        event.metadata["analysisCaptureError"] = exc.as_dict()
                        event.content.append(TextBlock(text="\n分析输入保存失败：" + str(exc)))
                    if receipt is not None and saved_ref is not None:
                        # Store succeeds first. Keep additional blocks intact, and never
                        # convert a projection failure into a false capture failure.
                        source_chars = sum(len(b.get('text', '')) for b in response['content'])
                        compacted = False
                        if len(response['content']) == 1 and response['content'][0].get('type') == 'text':
                            try:
                                compact = adapter.compact(receipt, saved_ref, source_chars)
                                if compact is not None:
                                    event.content = [TextBlock(text=encode(compact))]
                                    compacted = True
                            except (AnalysisError, ValueError, TypeError):
                                logging.getLogger(__name__).exception('Query receipt projection failed; original response retained')
                        model_chars = sum(len(getattr(b, 'text', '')) for b in event.content)
                        event.metadata['queryPerformance'] = {'sourceChars': source_chars, 'modelChars': model_chars,
                            'compacted': compacted, 'elapsedMs': round((perf_counter() - started) * 1000, 2)}
                        logging.getLogger(__name__).info('query tool=%s status=%s source_chars=%d model_chars=%d compacted=%s elapsed_ms=%.2f',
                            call.name, summary.get('status'), source_chars, model_chars, compacted, (perf_counter() - started) * 1000)
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
