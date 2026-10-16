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

    def make_tool(self, text, delay=0, resource_id="registered-budget"):
        async def call_tool(*args, **kwargs):
            await asyncio.sleep(delay)
            return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=text)])
        inner = MCPTool("not-a-hardcoded-prefix", mcp.types.Tool(name="execute_semantic_plan", inputSchema={
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

    async def invoke(self, agent, tool):
        call = ToolCallBlock(id="original-call", name=tool.name, input='{"contractVersion":"2.0"}')
        return [event async for event in agent._acting(call)]

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
