"""Payload diagnostics at the model, toolkit and MCP boundaries."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import types

from agentscope._io_log import io_context
from agentscope.agent import Agent
from agentscope.app._service._resources import _LeasedTool
from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import (
    SystemMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from agentscope.tool import MCPTool, ToolChoice, Toolkit
from hitl_external_execution_test import MockExternalSequentialTool
from model_openai_chat_test import (
    _make_model,
    _make_stream_chunk,
    _mock_completion,
    _MockAsyncStream,
)
from utils import MockModel
from toolkit_test import Tool2


def records(log):
    return [json.loads(record.getMessage()) for record in log.records]


class CallIOTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, AGENTSCOPE_LOG_IO="1"))

    async def test_provider_payload_and_single_accumulated_output(self):
        model = _make_model(stream=True)
        model.client = MagicMock()
        create = AsyncMock(
            return_value=_MockAsyncStream(
                [
                    _make_stream_chunk(delta_text="净利"),
                    _make_stream_chunk(
                        delta_text="润", delta_reasoning="private-thought"
                    ),
                ]
            )
        )
        model.client.chat.completions.create = create
        schemas = [
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in ("AnalysisTask", "discover_query_targets")
        ]
        with self.assertLogs("as.io") as log:
            stream = await model(
                [
                    SystemMsg("system", "预算助手"),
                    UserMsg("user", "查询净利润"),
                ],
                tools=schemas,
                tool_choice=ToolChoice(mode="auto", tools=["AnalysisTask"]),
                extra_headers={"Authorization": "Bearer private-key"},
            )
            chunks = [item async for item in stream]
        sent, received = records(log)
        self.assertEqual(sent["format"], "provider")
        for key in ("messages", "tools", "tool_choice"):
            self.assertEqual(
                sent["payload"][key], create.await_args.kwargs[key]
            )
        self.assertEqual(sent["callId"], received["callId"])
        self.assertEqual(
            [sent["direction"], received["direction"]], ["input", "output"]
        )
        self.assertEqual(received["payload"]["id"], chunks[-1].id)
        self.assertIn(
            "净利润",
            [block.get("text") for block in received["payload"]["content"]],
        )
        self.assertNotIn("private-", str(log.output))
        self.assertEqual(
            create.await_args.kwargs["extra_headers"]["Authorization"],
            "Bearer private-key",
        )

    async def test_concurrent_leased_mcp_calls_correlate_with_tool_and_session(
        self,
    ):
        async def call_tool(name, arguments, **_):
            await asyncio.sleep(0)
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=json.dumps(arguments))
                ],
                structuredContent={
                    "amount": "100.0100",
                    "headers": ["净利润"],
                    "api_key": "secret-out",
                },
            )

        session = SimpleNamespace(call_tool=call_tool)
        upstream = MCPTool(
            "budget",
            types.Tool(
                name="execute_semantic_plan", inputSchema={"type": "object"}
            ),
            session=session,
        )
        entry = SimpleNamespace(users=0, release=AsyncMock())
        toolkit = Toolkit(tools=[_LeasedTool(upstream, entry)])

        async def run(sid):
            with io_context(sessionId=sid):
                call = ToolCallBlock(
                    id=f"call-{sid}",
                    name=upstream.name,
                    input=json.dumps({"year": sid, "password": "secret-in"}),
                )
                return [
                    item
                    async for item in toolkit.call_tool(call, AgentState())
                ]

        with self.assertLogs("as.io") as log:
            returned = await asyncio.gather(run("2025"), run("2026"))
        self.assertIn("secret-in", returned[0][-1].content[0].text)
        self.assertNotIn("secret-", str(log.output))
        for sid in ("2025", "2026"):
            tool_in, mcp_in, mcp_out, tool_out = [
                e for e in records(log) if e["sessionId"] == sid
            ]
            self.assertEqual(
                [e["kind"] for e in (tool_in, mcp_in, mcp_out, tool_out)],
                ["TOOL", "MCP", "MCP", "TOOL"],
            )
            self.assertEqual(mcp_in["toolCallId"], tool_in["callId"])
            self.assertEqual(tool_out["callId"], f"call-{sid}")
            self.assertEqual(mcp_in["callId"], mcp_out["callId"])
            self.assertEqual(mcp_in["payload"]["year"], sid)
            self.assertEqual(
                mcp_out["payload"]["structuredContent"]["amount"], "100.0100"
            )
            self.assertEqual(
                mcp_out["payload"]["structuredContent"]["headers"], ["净利润"]
            )
            self.assertEqual(
                json.loads(tool_out["payload"]["content"][0]["text"])[
                    "password"
                ],
                "[redacted]",
            )

    async def test_partial_stream_failure_keeps_received_output(self):
        model = MockModel()
        model.set_responses(
            [
                [
                    ChatResponse([TextBlock(text="已返回")], is_last=False),
                    RuntimeError("secret-error"),
                ]
            ]
        )
        with self.assertLogs("as.io") as log:
            stream = await model([UserMsg("user", "查询")])
            with self.assertRaises(RuntimeError):
                async for _ in stream:
                    pass
        sent, failed = records(log)
        self.assertEqual(sent["callId"], failed["callId"])
        self.assertEqual(failed["status"], "error")
        self.assertEqual(
            failed["payload"]["partial"]["content"][0]["text"], "已返回"
        )
        self.assertNotIn("secret-error", str(log.output))

    async def test_cancelled_model_stream_has_one_terminal_record(self):
        model = MockModel()
        model.set_responses(
            [
                [
                    ChatResponse([TextBlock(text="部分")], is_last=False),
                    asyncio.CancelledError(),
                ]
            ]
        )
        with self.assertLogs("as.io") as log:
            stream = await model([])
            chunks = [item async for item in stream]
        sent, stopped = records(log)
        self.assertEqual(sent["callId"], stopped["callId"])
        self.assertEqual(stopped["status"], "interrupted")
        self.assertEqual(
            stopped["payload"]["content"][0]["text"],
            chunks[-1].content[0].text,
        )

    async def test_local_streaming_tool_logs_one_result_and_omits_binary(self):
        toolkit = Toolkit(tools=[Tool2()])
        call = ToolCallBlock(id="local", name="tool_2", input="{}")
        with self.assertLogs("as.io") as log:
            result = [
                item async for item in toolkit.call_tool(call, AgentState())
            ]
        sent, completed = records(log)
        self.assertEqual(sent["payload"], {})
        self.assertEqual(completed["callId"], "local")
        self.assertEqual(
            completed["payload"]["content"][0]["text"],
            result[-1].content[0].text,
        )
        self.assertIn("[binary omitted]", str(completed))

    async def test_external_tool_output_pairs_across_resumed_reply(self):
        tool = MockExternalSequentialTool()
        call = ToolCallBlock(
            id="outside", name=tool.name, input='{"input":"净利润"}'
        )
        model = MockModel()
        model.set_responses(
            [
                ChatResponse([call], is_last=True),
                ChatResponse([TextBlock(text="完成")], is_last=True),
            ]
        )
        agent = Agent(
            name="test",
            system_prompt="test",
            model=model,
            toolkit=Toolkit(tools=[tool]),
        )
        with self.assertLogs("as.io") as log:
            async for _ in agent.reply_stream(inputs=UserMsg("user", "查询")):
                pass
            event = ExternalExecutionResultEvent(
                reply_id=agent.state.reply_id,
                execution_results=[
                    ToolResultBlock(
                        id=call.id,
                        name=tool.name,
                        state="success",
                        output=[TextBlock(text="12.30")],
                    ),
                ],
            )
            async for _ in agent.reply_stream(inputs=event):
                pass
        sent, received = [e for e in records(log) if e["kind"] == "TOOL"]
        self.assertEqual(sent["execution"], "external")
        self.assertEqual(sent["callId"], received["callId"])
        self.assertEqual(received["payload"]["output"][0]["text"], "12.30")

    async def test_structured_fallback_logs_the_actual_responses(self):
        model = _make_model(stream=False)
        model.client = MagicMock()
        model.client.chat.completions.create = AsyncMock(
            side_effect=[
                _mock_completion(text="first attempt"),
                _mock_completion(
                    tool_calls=[
                        {
                            "id": "tool-id",
                            "name": "generate_structured_output",
                            "arguments": '{"answer":"净利润"}',
                        }
                    ]
                ),
            ]
        )
        with self.assertLogs("as.io") as log:
            result = await model.generate_structured_output(
                messages=[UserMsg("user", "指标名称")],
                structured_model={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                },
            )
        first, reply, second, completed = records(log)
        self.assertEqual(result.content, {"answer": "净利润"})
        self.assertEqual(first["callId"], completed["callId"])
        self.assertNotEqual(first["strategy"], second["strategy"])
        self.assertEqual(reply["status"], "error")
        self.assertEqual(
            reply["payload"]["response"]["content"][0]["text"], "first attempt"
        )
        self.assertIn(
            "generate_structured_output", str(second["payload"]["tools"])
        )
        self.assertIn("净利润", completed["payload"]["content"][0]["input"])

    async def test_disabled_logging_skips_serialization(self):
        model = MockModel()
        model.set_responses(
            [ChatResponse([TextBlock(text="ok")], is_last=True)]
        )
        with (
            patch.dict(os.environ, AGENTSCOPE_LOG_IO="0"),
            patch("agentscope._io_log._visible") as visible,
        ):
            with self.assertNoLogs("as.io"):
                result = await model([UserMsg("user", "private")])
            visible.assert_not_called()
            self.assertEqual(result.content[0].text, "ok")
