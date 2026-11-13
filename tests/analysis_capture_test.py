"""Exercise the actual SDK onion chain, MCP adapter and offload worker."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "agent_service"))

import mcp.types
from agentscope.agent import Agent
from agentscope.app._manager import BackgroundTaskManager
from agentscope.app._service._resources import _LeasedTool
from agentscope.app.message_bus import MessageBus
from agentscope.app.middleware import ToolOffloadMiddleware
from agentscope.message import ToolCallBlock
from agentscope.middleware import MiddlewareBase
from agentscope.tool import Toolkit, ToolResponse
from agentscope.tool._adapters import MCPTool
from analysis_extension.capture import ReceiptCapture
from analysis_extension.models import Limits, Owner, decimal_value, exact_loads
from analysis_extension.store import AnalysisStore
from utils import MockModel


class CaptureMiddleware(MiddlewareBase):
    def __init__(self, capture):
        self.capture = capture

    async def on_acting(self, agent, input_kwargs, next_handler):
        async for item in self.capture.run(agent, input_kwargs, next_handler):
            yield item


class CaptureTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = AnalysisStore(self.directory.name)
        self.owner = Owner("u", "a", "s")
        self.allowed = AsyncMock()

    def make_tool(self, text, delay=0, resource_id="registered-budget", sent=None, tool_name="execute_budget_query", input_schema=None):
        async def call_tool(*args, **kwargs):
            if sent is not None:
                sent.append((args, kwargs))
            await asyncio.sleep(delay)
            return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=text)])
        inner = MCPTool("not-a-hardcoded-prefix", mcp.types.Tool(name=tool_name, inputSchema=input_schema or {
            "type": "object", "properties": {"contractVersion": {"const": "2.0"}},
            "required": ["contractVersion"]}, _meta={"version": "2.0", "exactContractResource": "semantic://contracts/external-query"}), session=SimpleNamespace(call_tool=call_tool))
        entry = SimpleNamespace(resource_id=resource_id, fingerprint="connection-v1", users=1, release=AsyncMock())
        return _LeasedTool(inner, entry)

    def agent(self, tool, timeout=5):
        bus = MagicMock(spec=MessageBus)
        bus.registry_get = AsyncMock(return_value=None)
        manager = BackgroundTaskManager(message_bus=bus)
        capture = ReceiptCapture(self.store, self.owner, frozenset({"registered-budget"}), self.allowed)
        agent = Agent(name="capture-test", system_prompt="test", model=MockModel(),
            toolkit=Toolkit(tools=[tool]), middlewares=[ToolOffloadMiddleware(manager, bus, "u", "a", timeout_secs=timeout), CaptureMiddleware(capture)])
        return agent, manager

    async def test_source_adoption_guidance_reaches_stream_and_final_response_once(self):
        from agentscope.tool import ToolChunk
        from analysis_business_query_test import business_fixture
        from analysis_extension.middleware import AnalysisMiddleware
        from analysis_extension.turn_completion import TurnCompletion
        fixture, receipt = business_fixture()
        tool = self.make_tool(json.dumps(receipt))
        middleware = AnalysisMiddleware(self.store, self.owner, {'registered-budget'}, self.allowed)
        middleware.service.turn = TurnCompletion(middleware.service, {
            'turnKey': 'source-guidance', 'request': fixture['request']['question'],
            'mode': 'DIRECT', 'goal': '查询科目', 'requiredEvidence': 'VALUES', 'failures': {},
            'followupContext': {'mode': 'NEW'}, 'buildRefs': []})
        agent = Agent(name='guidance', system_prompt='test', model=MockModel(),
                      toolkit=Toolkit(tools=[tool]), middlewares=[middleware])
        call = ToolCallBlock(id='guidance-call', name=tool.name,
                            input=json.dumps({'contractVersion': '2.0', **fixture['request']}))
        events = [event async for event in agent._acting(call)]
        response = next(event for event in events if isinstance(event, ToolResponse))
        chunks = [event for event in events if isinstance(event, ToolChunk)]
        streamed = '\n'.join(block.text for event in chunks for block in event.content)
        final = '\n'.join(block.text for block in response.content)
        for text in ('来源引用', 'src_', '先用 AnalysisTask.subjectSources'):
            self.assertIn(text, streamed)
            self.assertIn(text, final)
        self.assertEqual(streamed.count('本次查询不会替换已采用的分析输入'), 1)
        self.assertEqual(final.count('本次查询不会替换已采用的分析输入'), 1)
        saved = self.store.get_capture(self.owner, response.metadata['analysisCaptureRef'])
        self.assertEqual(json.loads(saved['response']['content'][0]['text']), receipt)

    async def test_host_request_id_reaches_actual_mcp_and_saved_receipt(self):
        sent = []
        tool = self.make_tool('{"contractVersion":"2.0","data":{}}', sent=sent)
        agent, _ = self.agent(tool)
        submitted = {'contractVersion': '2.0', 'requestId': 'model-placeholder', 'year': 2026}
        call = ToolCallBlock(id='host-id-call', name=tool.name, input=json.dumps(submitted))
        events = [event async for event in agent._acting(call)]
        response = next(event for event in events if isinstance(event, ToolResponse))
        saved = self.store.get_capture(self.owner, response.metadata['analysisCaptureRef'])
        args, kwargs = sent[0]
        actual = kwargs.get('arguments', args[1] if len(args) > 1 else None)
        self.assertIsNotNone(actual)
        self.assertEqual(actual, saved['request'])
        self.assertTrue(actual['requestId'].startswith('host_'))
        self.assertEqual(json.loads(saved['submittedRequestText']), submitted)

    async def test_host_formula_preparation_uses_native_schema_with_source_proof(self):
        from agentscope.permission import PermissionBehavior
        from analysis_extension.middleware import AnalysisMiddleware
        from analysis_extension.turn_completion import TurnCompletion
        from analysis_extension.capture import leased_mcp_origin
        from analysis_extension.query_contract import model_schema
        sent = []
        native = {'type': 'object', 'additionalProperties': False,
                  'properties': {'contractVersion': {'const': '2.0'}, 'requestId': {'type': 'string'},
                                 'purpose': {'type': 'string'}, 'question': {'type': 'string'},
                                 'basis': {'type': 'object'}},
                  'required': ['contractVersion', 'requestId', 'basis']}
        tool = self.make_tool('{"contractVersion":"2.0","data":{}}', sent=sent, input_schema=native)
        tool._tool._tool.meta.update(risk='READ_ONLY', dataBoundary='PINNED_SOURCE_SNAPSHOT_ASOF', evidencePolicy='EVIDENCE_REQUIRED')
        tool.model_input_schema = model_schema(native)
        middleware = AnalysisMiddleware(self.store, self.owner, {'registered-budget'}, self.allowed)
        turn = TurnCompletion(middleware.service, {'turnKey': 'formula-host', 'request': '分析前面结果',
            'mode': 'ANALYSIS', 'goal': '分析前面结果', 'requiredEvidence': 'FORMULAS', 'failures': {},
            'followupContext': {'mode': 'NEW'}, 'buildRefs': []})
        middleware.service.turn = turn
        agent = Agent(name='host-preparation', system_prompt='test', model=MockModel(), toolkit=Toolkit(tools=[tool]))
        agent._check_permission = AsyncMock(return_value=SimpleNamespace(behavior=PermissionBehavior.ALLOW, updated_input=None))
        proof = {'kind': 'FACTS', 'source': {'executionId': 'execution', 'resultSetId': 'set', 'factIds': ['fact']},
                 'contextId': 'context', 'querySessionId': 'session'}
        refs, error = await middleware._call_readonly_source(agent, turn,
            leased_mcp_origin(tool, frozenset({'registered-budget'})), 'execute_budget_query',
            {'contractVersion': '2.0', 'purpose': 'ANALYZE', 'question': '分析前面结果', 'basis': proof})
        self.assertIsNone(error)
        self.assertEqual(len(refs), 1)
        self.assertEqual(self.store.get_capture(self.owner, refs[0])['request']['basis'], proof)
        self.assertEqual(len(sent), 1)
        agent._check_permission.assert_awaited_once()

    async def test_schema_repair_is_frozen_before_preparation_and_actual_execution(self):
        sent, prepared = [], []
        tool = self.make_tool('{"contractVersion":"2.0","data":{}}', sent=sent,
            input_schema={'type': 'object', 'properties': {'contractVersion': {'const': '2.0'},
                'groupBy': {'type': 'array', 'items': {'oneOf': [{'type': 'string'},
                    {'type': 'object', 'properties': {'scopeId': {'type': 'string'}}, 'required': ['scopeId']}]}}}})
        capture = ReceiptCapture(self.store, self.owner, frozenset({'registered-budget'}), self.allowed)
        capture.request_preparer = lambda origin, request: prepared.append(request) or request
        agent = Agent(name='canonical-test', system_prompt='test', model=MockModel(),
                      toolkit=Toolkit(tools=[tool]), middlewares=[CaptureMiddleware(capture)])
        for index, raw in enumerate(('板块', '["板块"]', ['板块'], [{'scopeId': 'region'}])):
            submitted = {'contractVersion': '2.0', 'groupBy': raw, 'intentDraft': {
                         'valueFilter': {'op': 'LT', 'value': '-0.000000000000000001'}}}
            events = [event async for event in agent._acting(ToolCallBlock(
                id='normalize-' + str(index), name=tool.name, input=json.dumps(submitted)))]
            response = next(event for event in events if isinstance(event, ToolResponse))
            saved = self.store.get_capture(self.owner, response.metadata['analysisCaptureRef'])
            args, kwargs = sent[-1]
            actual = kwargs.get('arguments', args[1] if len(args) > 1 else None)
            self.assertEqual(actual, saved['request'])
            self.assertEqual(prepared[-1], {key: value for key, value in actual.items() if key != 'requestId'})
            self.assertEqual(actual['groupBy'], raw if isinstance(raw, list) else ['板块'])
            self.assertEqual(actual['intentDraft']['valueFilter'], submitted['intentDraft']['valueFilter'])
            self.assertEqual(json.loads(saved['submittedRequestText']), submitted)

    async def test_malformed_model_arguments_return_tool_error_without_aborting_turn(self):
        from agentscope.message import ToolResultState
        sent = []
        tool = self.make_tool('{"contractVersion":"2.0","data":{}}', sent=sent)
        agent, _ = self.agent(tool)
        for payload in ('{"querySessionId": unquoted-identifier}', '[]', 'null'):
            call = ToolCallBlock(id='bad-json', name=tool.name, input=payload)
            events = [event async for event in agent._acting(call)]
            response = next(event for event in events if isinstance(event, ToolResponse))
            self.assertEqual(response.state, ToolResultState.ERROR)
            self.assertEqual(json.loads(response.content[0].text)['error']['code'], 'INVALID_JSON')
            self.assertNotIn('analysisCaptureRef', response.metadata)
        self.assertEqual(sent, [])
        # 同一工具在修正参数后仍可正常执行，错误没有中止会话或伪造来源回执。
        await self.invoke(agent, tool)
        self.assertEqual(len(sent), 1)

    async def test_host_owned_query_id_is_absent_from_model_schema_but_sent_to_native(self):
        from analysis_extension.query_contract import model_schema
        native = {'type': 'object', 'properties': {'contractVersion': {'const': '2.0'},
                  'requestId': {'type': 'string'}}, 'required': ['contractVersion', 'requestId']}
        sent = []
        tool = self.make_tool('{"contractVersion":"2.0","data":{}}', sent=sent, input_schema=native)
        tool.model_input_schema = model_schema(native)
        self.assertNotIn('requestId', tool.model_input_schema['properties'])
        agent, _ = self.agent(tool)
        events = await self.invoke(agent, tool)
        response = next(event for event in events if isinstance(event, ToolResponse))
        saved = self.store.get_capture(self.owner, response.metadata['analysisCaptureRef'])
        self.assertTrue(saved['request']['requestId'].startswith('host_'))
        self.assertNotIn('requestId', json.loads(saved['submittedRequestText']))
        args, kwargs = sent[0]
        self.assertEqual(kwargs.get('arguments', args[1] if len(args) > 1 else None), saved['request'])

    async def test_successful_identical_query_reuses_capture_only_in_same_turn(self):
        sent, cache = [], {}
        receipt = json.dumps({'contractVersion': '2.0', 'status': 'SUCCEEDED', 'data': {
            'rowCount': 1, 'columns': [{'name': 'amount', 'label': '费用', 'type': 'DECIMAL_STRING', 'unit': '元'}],
            'rows': [['123456789.1200']]}})
        tool = self.make_tool(receipt, sent=sent)
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, self.owner, frozenset({'registered-budget'}), self.allowed)
        capture.turn_key_provider = lambda: 'turn-one'
        capture.query_cache_provider = lambda: cache
        async def execute(**kwargs):
            sent.append(kwargs['tool_call'].input)
            from agentscope.message import TextBlock
            yield ToolResponse(content=[TextBlock(text=receipt)])
        async def query(call_id, year=2026):
            call = ToolCallBlock(id=call_id, name=tool.name,
                                input=json.dumps({'contractVersion': '2.0', 'requestId': call_id, 'year': year}))
            return [item async for item in capture.run(agent, {'tool_call': call}, execute) if isinstance(item, ToolResponse)]
        first = (await query('first'))[0]
        reused = (await query('second'))[0]
        self.assertEqual(len(sent), 1)
        self.assertEqual(first.metadata['analysisCaptureRef'], reused.metadata['analysisCaptureRef'])
        self.assertTrue(reused.metadata['queryPerformance']['reused'])
        self.assertEqual(first.metadata['public_progress'], reused.metadata['public_progress'])
        fact = first.metadata['public_progress']['facts'][1]
        self.assertEqual(fact['value'], '123456789.1200')
        self.assertEqual(fact['sourceId'], first.metadata['analysisCaptureRef'])
        await query('changed', 2025)
        self.assertEqual(len(sent), 2)
        capture.turn_key_provider = lambda: 'turn-two'
        await query('new-turn')
        self.assertEqual(len(sent), 3)
        self.assertGreaterEqual(self.allowed.await_count, 4)

    async def invoke(self, agent, tool):
        call = ToolCallBlock(id="original-call", name=tool.name, input='{"contractVersion":"2.0"}')
        return [event async for event in agent._acting(call)]

    async def test_partial_failed_or_revoked_query_is_never_reused(self):
        from agentscope.message import TextBlock
        from analysis_extension.models import AnalysisError
        cache, calls = {}, []
        tool = self.make_tool('{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, self.owner, frozenset({'registered-budget'}), self.allowed)
        capture.turn_key_provider = lambda: 'one'
        capture.query_cache_provider = lambda: cache
        status = 'PARTIAL'
        async def execute(**kwargs):
            calls.append(status)
            yield ToolResponse(content=[TextBlock(text=json.dumps({'contractVersion': '2.0', 'status': status, 'data': {}}))])
        async def query():
            call = ToolCallBlock(id='same-call', name=tool.name, input='{"contractVersion":"2.0"}')
            return [item async for item in capture.run(agent, {'tool_call': call}, execute)]
        for status in ('PARTIAL', 'FAILED'):
            await query()
            await query()
            self.assertEqual(cache, {})
        status = 'SUCCEEDED'
        await query()
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(cache), 1)
        self.allowed.side_effect = AnalysisError('ACCESS_DENIED', '已撤销访问')
        with self.assertRaises(AnalysisError):
            await query()
        self.assertEqual(len(calls), 5)

    async def test_large_final_response_captured_without_float_roundtrip(self):
        text = '{"contractVersion":"2.0","data":{"value":123456789012345678.123456789,"padding":"' + "预算" * 300000 + '"}}'
        tool = self.make_tool(text)
        agent, _ = self.agent(tool)
        events = await self.invoke(agent, tool)
        response = next(x for x in events if isinstance(x, ToolResponse))
        saved = self.store.get_capture(self.owner, response.metadata["analysisCaptureRef"])
        self.assertEqual(saved["response"]["content"][0]["text"], text)
        value = exact_loads(saved["response"]["content"][0]["text"])["data"]["value"]
        self.assertEqual(str(value), "123456789012345678.123456789")
        compact = exact_loads(response.content[0].text)
        self.assertEqual(compact['receipt']['data']['value'], '123456789012345678.123456789')
        self.assertTrue(response.metadata['queryPerformance']['compacted'])
        self.assertLess(len(response.content[0].text), 24000)
        from agentscope.tool import ToolChunk
        chunks = [event for event in events if isinstance(event, ToolChunk)]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content[0].text, response.content[0].text)
        from analysis_extension.query_results import read_receipt
        page = read_receipt(exact_loads(text), compact['captureRef'], '/data/padding', offset=1200, limit=10)
        self.assertEqual(page['value'], '预算' * 500)
        self.assertEqual(page['nextOffset'], 2200)

    async def test_background_completion_saved_after_outer_placeholder(self):
        tool = self.make_tool('{"contractVersion":"2.0","data":{"done":true}}', delay=0.12)
        agent, manager = self.agent(tool, timeout=0.01)
        await self.invoke(agent, tool)
        self.assertEqual(self.store.list_captures(self.owner)["total"], 0)
        await asyncio.sleep(0.25)
        self.assertEqual(self.store.list_captures(self.owner)["total"], 1)
        saved = self.store.get_capture(self.owner, self.store.list_captures(self.owner)["items"][0]["captureRef"])
        self.assertEqual(saved["captureState"], "success")
        self.assertTrue(exact_loads(saved["response"]["content"][0]["text"])["data"]["done"])

    async def test_resource_identity_is_required_not_name_suffix(self):
        tool = self.make_tool('{"contractVersion":"2.0"}', resource_id="other-resource")
        agent, _ = self.agent(tool)
        await self.invoke(agent, tool)
        self.assertEqual(self.store.list_captures(self.owner)["total"], 0)

    async def test_cancelled_capture_is_not_complete(self):
        capture = ReceiptCapture(self.store, self.owner, frozenset({"registered-budget"}), self.allowed)
        tool = self.make_tool('{}')
        agent, _ = self.agent(tool)
        async def interrupted(**kwargs):
            raise asyncio.CancelledError()
            yield
        call = ToolCallBlock(id="cancelled", name=tool.name, input='{"contractVersion":"2.0"}')
        with self.assertRaises(asyncio.CancelledError):
            _ = [event async for event in capture.run(agent, {"tool_call": call}, interrupted)]
        self.assertEqual(self.store.list_captures(self.owner)["items"][0]["captureState"], "INTERRUPTED")

    async def test_oversize_not_silently_truncated(self):
        self.store.limits = Limits(max_receipt_bytes=128)
        tool = self.make_tool('{"contractVersion":"2.0","data":{"text":"' + 'x' * 500 + '"}}')
        agent, _ = self.agent(tool)
        response = next(x for x in await self.invoke(agent, tool) if isinstance(x, ToolResponse))
        self.assertEqual(response.metadata["analysisCaptureError"]["code"], "INPUT_TOO_LARGE")
        self.assertEqual(self.store.list_captures(self.owner)["total"], 0)
        self.assertIn('x' * 500, response.content[0].text)

    async def test_input_float_precision_is_not_reconstructed(self):
        from analysis_extension.models import AnalysisError
        with self.assertRaises(AnalysisError):
            decimal_value(1.23)
