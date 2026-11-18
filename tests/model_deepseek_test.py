# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for DeepSeekChatModel with mocked API responses.

Tests cover both non-streaming and streaming modes, verifying that:
- Non-stream mode returns a single ChatResponse with is_last=True.
- Stream mode yields n delta ChatResponses (is_last=False) followed by
  1 final ChatResponse (is_last=True) with the full accumulated content.
"""

from typing import Any
import asyncio
from copy import deepcopy
import hashlib
import json
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel, Field

from utils import AnyString

from agentscope.exception import StructuredOutputError
from agentscope.message import (
    AssistantMsg,
    DataBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ThinkingBlock,
    UserMsg,
    URLSource,
)
from agentscope.model import DeepSeekChatModel, StructuredResponse
from agentscope.agent import Agent, ContextConfig
from agentscope.state import AgentState
from agentscope.credential import DeepSeekCredential
from agentscope.tool import ToolChoice

A = AnyString()


class TestDeepSeekContextBudget(IsolatedAsyncioTestCase):
    """Dense tool results must trigger compaction before provider rejection."""

    async def test_json_tool_results_trigger_compression(self) -> None:
        """Reproduce the long JSON path through the actual compression entry."""
        model = _make_model()
        self.addAsyncCleanup(model.client.close)
        model.context_size = 10_000
        payload = json.dumps(
            {
                "records": [
                    {
                        "id": hashlib.sha256(str(i).encode()).hexdigest(),
                        "value": "12345.67",
                    }
                    for i in range(200)
                ]
            }
        )
        call = ToolCallBlock(
            id="read-observations",
            name="ReadObservations",
            input="{}",
            state="finished",
        )
        result = ToolResultBlock(
            id=call.id,
            name=call.name,
            output=payload,
            state="success",
        )
        state = AgentState(
            context=[
                UserMsg("user", "核对这些记录。"),
                AssistantMsg("assistant", [call, result]),
            ]
        )
        model.generate_structured_output = AsyncMock(
            return_value=StructuredResponse(
                content={
                    "task_overview": "核对记录",
                    "current_state": "数据已取得",
                    "important_discoveries": "保留原始值",
                    "next_steps": "核对来源",
                    "context_to_preserve": "用户要求中文",
                }
            )
        )
        agent = Agent(
            name="assistant",
            system_prompt="使用中文。",
            model=model,
            state=state,
            context_config=ContextConfig(trigger_ratio=0.7),
        )
        await agent.compress_context()
        model.generate_structured_output.assert_awaited_once()
        self.assertTrue(state.summary)
        self.assertLess(
            await model.count_tokens(**await agent._prepare_model_input()),
            7000,
        )

    async def test_large_schemas_and_arguments_count_toward_budget(
        self,
    ) -> None:
        """The budget includes tool schemas and outgoing arguments as well."""
        model = _make_model()
        self.addAsyncCleanup(model.client.close)
        schema = {
            "type": "function",
            "function": {
                "name": "Inspect",
                "description": "schema " * 3000,
                "parameters": {"type": "object"},
            },
        }
        arguments = json.dumps(
            {
                "keys": [
                    hashlib.sha256(str(i).encode()).hexdigest()
                    for i in range(150)
                ]
            }
        )
        total = await model.count_tokens(
            [
                AssistantMsg(
                    "assistant",
                    [
                        ToolCallBlock(
                            id="inspect", name="Inspect", input=arguments
                        ),
                    ],
                )
            ],
            [schema],
        )
        self.assertGreater(total, 14_000)

    async def test_media_keeps_its_flat_budget(self) -> None:
        """The text correction must not inflate the independent media budget."""
        model = _make_model()
        self.addAsyncCleanup(model.client.close)
        count = await model.count_tokens(
            [
                UserMsg(
                    "user",
                    [
                        DataBlock(
                            source=URLSource(
                                url="file:///not-read.png",
                                media_type="image/png",
                            )
                        ),
                    ],
                )
            ],
            None,
        )
        self.assertEqual(count, 2000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model(stream: bool = False) -> Any:
    return DeepSeekChatModel(
        credential=DeepSeekCredential(api_key="test"),
        model="deepseek-v4-pro",
        stream=stream,
        context_size=65_536,
    )


def _mock_completion(
    text: Any = None,
    tool_calls: Any = None,
    reasoning: Any = None,
    response_id: str = "deepseek-1",
) -> MagicMock:
    """Build a mock non-streaming ChatCompletion response."""
    msg = MagicMock()
    msg.content = text
    msg.reasoning_content = reasoning
    msg.tool_calls = None

    if tool_calls:
        tc_mocks = []
        for tc in tool_calls:
            m = MagicMock()
            m.id = tc["id"]
            m.function.name = tc["name"]
            m.function.arguments = tc["arguments"]
            tc_mocks.append(m)
        msg.tool_calls = tc_mocks

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.id = response_id
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.prompt_cache_hit_tokens = 0
    return resp


def _make_stream_chunk(
    delta_text: str | None = None,
    delta_reasoning: str | None = None,
    tool_calls: list | None = None,
    response_id: str = "deepseek-1",
    usage: dict | None = None,
    has_choices: bool = True,
) -> MagicMock:
    """Build a single mock streaming chunk."""
    chunk = MagicMock()
    chunk.id = response_id

    if usage:
        chunk.usage = MagicMock()
        chunk.usage.prompt_tokens = usage.get("prompt_tokens", 0)
        chunk.usage.completion_tokens = usage.get("completion_tokens", 0)
        chunk.usage.prompt_cache_hit_tokens = usage.get(
            "prompt_cache_hit_tokens",
            0,
        )
    else:
        chunk.usage = None

    if has_choices:
        delta = MagicMock()
        delta.content = delta_text
        delta.reasoning_content = delta_reasoning
        delta.tool_calls = tool_calls
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]
    else:
        chunk.choices = []

    return chunk


def _make_tool_call_delta(
    index: int,
    tc_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> MagicMock:
    """Build a tool_call delta item for streaming."""
    tc = MagicMock()
    tc.index = index
    tc.id = tc_id
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


class _MockAsyncStream:
    """Mock async stream that acts as an async context manager + iterator."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks
        self._index = 0

    async def __aenter__(self) -> "_MockAsyncStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> "_MockAsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


# ---------------------------------------------------------------------------
# Non-streaming tests
# ---------------------------------------------------------------------------


class TestDeepSeekNonStream(IsolatedAsyncioTestCase):
    """Tests for DeepSeekChatModel in non-streaming mode."""

    def setUp(self) -> None:
        self.model = _make_model(stream=False)
        self.mock_client = MagicMock()
        self.model.client = self.mock_client

    async def test_text_response(self) -> None:
        """Non-stream text response returns a single ChatResponse."""
        mock_create = AsyncMock(
            return_value=_mock_completion(text="Hello world!"),
        )
        self.mock_client.chat.completions.create = mock_create

        result = await self.model([])

        self.assertEqual(
            (result.is_last, result.content),
            (
                True,
                [
                    TextBlock.model_construct(
                        id=A,
                        created_at=A,
                        text="Hello world!",
                    ),
                ],
            ),
        )
        self.assertEqual(result.id, "deepseek-1")

    async def test_extra_body_is_not_mutated(self) -> None:
        """Defaulting thinking.type leaves the caller's extra_body intact."""
        mock_create = AsyncMock(
            return_value=_mock_completion(text="Hello!"),
        )
        self.mock_client.chat.completions.create = mock_create
        extra_body = {"custom": "value", "thinking": {"budget_tokens": 1024}}

        await self.model([], extra_body=extra_body)

        self.assertDictEqual(
            extra_body,
            {"custom": "value", "thinking": {"budget_tokens": 1024}},
        )
        self.assertDictEqual(
            mock_create.await_args.kwargs["extra_body"],
            {
                "custom": "value",
                "thinking": {"budget_tokens": 1024, "type": "disabled"},
            },
        )

    async def test_tool_call_response(
        self,
    ) -> None:
        """Non-stream tool call response creates ToolCallBlocks."""
        mock_create = AsyncMock(
            return_value=_mock_completion(
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "get_weather",
                        "arguments": '{"city":"Beijing"}',
                    },
                    {
                        "id": "call-2",
                        "name": "get_time",
                        "arguments": '{"tz":"UTC"}',
                    },
                ],
            ),
        )
        self.mock_client.chat.completions.create = mock_create

        result = await self.model([])

        self.assertEqual(
            (result.is_last, result.content),
            (
                True,
                [
                    ToolCallBlock.model_construct(
                        created_at=A,
                        id="call-1",
                        name="get_weather",
                        input='{"city":"Beijing"}',
                    ),
                    ToolCallBlock.model_construct(
                        created_at=A,
                        id="call-2",
                        name="get_time",
                        input='{"tz":"UTC"}',
                    ),
                ],
            ),
        )

    async def test_thinking_response(
        self,
    ) -> None:
        """Non-stream response with reasoning creates ThinkingBlock."""
        mock_create = AsyncMock(
            return_value=_mock_completion(
                text="The answer is 42.",
                reasoning="Let me think step by step...",
            ),
        )
        self.mock_client.chat.completions.create = mock_create

        result = await self.model([])

        self.assertEqual(
            (result.is_last, result.content),
            (
                True,
                [
                    ThinkingBlock.model_construct(
                        id=A,
                        created_at=A,
                        thinking="Let me think step by step...",
                    ),
                    TextBlock.model_construct(
                        id=A,
                        created_at=A,
                        text="The answer is 42.",
                    ),
                ],
            ),
        )


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestDeepSeekStream(IsolatedAsyncioTestCase):
    """Tests for DeepSeekChatModel in streaming mode."""

    def setUp(self) -> None:
        self.model = _make_model(stream=True)
        self.mock_client = MagicMock()
        self.model.client = self.mock_client

    async def test_stream_text_response(
        self,
    ) -> None:
        """Stream text yields n deltas (is_last=False) + 1 final
        (is_last=True) with full content."""
        chunks = [
            _make_stream_chunk(delta_text="Hello"),
            _make_stream_chunk(delta_text=" world"),
            _make_stream_chunk(delta_text="!"),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertListEqual(
            [(r.is_last, r.content) for r in responses],
            [
                (
                    False,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="Hello",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text=" world",
                        ),
                    ],
                ),
                (
                    False,
                    [TextBlock.model_construct(id=A, created_at=A, text="!")],
                ),
                (
                    True,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="Hello world!",
                        ),
                    ],
                ),
            ],
        )
        self.assertEqual(responses[-1].id, "deepseek-1")

    async def test_stream_thinking_and_text(
        self,
    ) -> None:
        """Stream with thinking + text yields deltas then final with both."""
        chunks = [
            _make_stream_chunk(delta_reasoning="Think"),
            _make_stream_chunk(delta_reasoning="ing..."),
            _make_stream_chunk(delta_text="Answer"),
            _make_stream_chunk(delta_text=" here."),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 8},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertListEqual(
            [(r.is_last, r.content) for r in responses],
            [
                (
                    False,
                    [
                        ThinkingBlock.model_construct(
                            id=A,
                            created_at=A,
                            thinking="Think",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        ThinkingBlock.model_construct(
                            id=A,
                            created_at=A,
                            thinking="ing...",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="Answer",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text=" here.",
                        ),
                    ],
                ),
                (
                    True,
                    [
                        ThinkingBlock.model_construct(
                            id=A,
                            created_at=A,
                            thinking="Thinking...",
                        ),
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="Answer here.",
                        ),
                    ],
                ),
            ],
        )

    async def test_stream_tool_calls(
        self,
    ) -> None:
        """Stream tool calls accumulate across chunks into final response."""
        chunks = [
            _make_stream_chunk(
                delta_text=None,
                tool_calls=[
                    _make_tool_call_delta(0, "call-1", "get_weather", '{"ci'),
                ],
            ),
            _make_stream_chunk(
                delta_text=None,
                tool_calls=[
                    _make_tool_call_delta(0, None, None, 'ty":"BJ"}'),
                ],
            ),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertListEqual(
            [(r.is_last, r.content) for r in responses],
            [
                (
                    False,
                    [
                        ToolCallBlock.model_construct(
                            created_at=A,
                            id="call-1",
                            name="get_weather",
                            input='{"ci',
                        ),
                    ],
                ),
                (
                    False,
                    [
                        ToolCallBlock.model_construct(
                            created_at=A,
                            id="call-1",
                            name="get_weather",
                            input='ty":"BJ"}',
                        ),
                    ],
                ),
                (
                    True,
                    [
                        ToolCallBlock.model_construct(
                            created_at=A,
                            id="call-1",
                            name="get_weather",
                            input='{"city":"BJ"}',
                        ),
                    ],
                ),
            ],
        )

    async def test_stream_text_then_tool_call(
        self,
    ) -> None:
        """Stream with text followed by tool call accumulates both."""
        chunks = [
            _make_stream_chunk(delta_reasoning="Let me check"),
            _make_stream_chunk(delta_text="I'll look it up."),
            _make_stream_chunk(
                delta_text=None,
                tool_calls=[
                    _make_tool_call_delta(
                        0,
                        "call-1",
                        "search",
                        '{"q":"weather"}',
                    ),
                ],
            ),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 15, "completion_tokens": 10},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertListEqual(
            [(r.is_last, r.content) for r in responses],
            [
                (
                    False,
                    [
                        ThinkingBlock.model_construct(
                            id=A,
                            created_at=A,
                            thinking="Let me check",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="I'll look it up.",
                        ),
                    ],
                ),
                (
                    False,
                    [
                        ToolCallBlock.model_construct(
                            created_at=A,
                            id="call-1",
                            name="search",
                            input='{"q":"weather"}',
                        ),
                    ],
                ),
                (
                    True,
                    [
                        ThinkingBlock.model_construct(
                            id=A,
                            created_at=A,
                            thinking="Let me check",
                        ),
                        TextBlock.model_construct(
                            id=A,
                            created_at=A,
                            text="I'll look it up.",
                        ),
                        ToolCallBlock.model_construct(
                            created_at=A,
                            id="call-1",
                            name="search",
                            input='{"q":"weather"}',
                        ),
                    ],
                ),
            ],
        )

    async def test_stream_usage_in_final(
        self,
    ) -> None:
        """Usage information is captured and present in final response."""
        chunks = [
            _make_stream_chunk(delta_text="Hi"),
            _make_stream_chunk(
                has_choices=False,
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 50,
                },
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertListEqual(
            [(r.is_last, r.content) for r in responses],
            [
                (
                    False,
                    [TextBlock.model_construct(id=A, created_at=A, text="Hi")],
                ),
                (
                    True,
                    [TextBlock.model_construct(id=A, created_at=A, text="Hi")],
                ),
            ],
        )
        self.assertEqual(responses[-1].usage.input_tokens, 100)
        self.assertEqual(responses[-1].usage.output_tokens, 20)
        self.assertEqual(responses[-1].usage.cache_input_tokens, 50)


class _StructuredReply(BaseModel):
    """A schema whose constraints must survive tool-choice selection."""

    value: int = Field(ge=1, strict=True)


class TestDeepSeekStructuredOutput(IsolatedAsyncioTestCase):
    """Exercise strategy selection through the SDK request and validation."""

    async def asyncSetUp(self) -> None:
        self.model = _make_model()
        self.addAsyncCleanup(self.model.client.close)
        self.model.parameters.thinking_enable = True
        self.messages = [UserMsg("user", "Return a positive value.")]
        self.schema = _StructuredReply.model_json_schema()
        self.mock_create = AsyncMock(
            return_value=self._completion({"value": 1})
        )
        self.model.client.chat.completions.create = self.mock_create

    @staticmethod
    def _completion(content: dict) -> MagicMock:
        """Return tool arguments without mocking the structured parser."""
        return _mock_completion(
            tool_calls=[
                {
                    "id": "structured-1",
                    "name": "generate_structured_output",
                    "arguments": json.dumps(content),
                },
            ],
        )

    def _request_strategies(self) -> list[tuple[str, str | None]]:
        """Read the actual thinking switch and tool choice sent to the SDK."""
        strategies = []
        for call in self.mock_create.await_args_list:
            request = call.kwargs
            choice = request.get("tool_choice")
            if isinstance(choice, dict):
                choice = choice["function"]["name"]
            strategies.append(
                (request["extra_body"]["thinking"]["type"], choice)
            )
        return strategies

    async def test_thinking_starts_with_auto_and_keeps_schema(self) -> None:
        """A valid thinking response needs only one provider request."""
        result = await self.model.generate_structured_output(
            self.messages,
            self.schema,
        )

        self.mock_create.assert_awaited_once()
        request = self.mock_create.await_args.kwargs
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["extra_body"]["thinking"]["type"], "enabled")
        self.assertEqual(
            request["tools"][0]["function"]["parameters"], self.schema
        )
        self.assertEqual(result.content, {"value": 1})

    async def test_disabled_thinking_keeps_forced_choice(self) -> None:
        """Non-thinking requests retain the existing forced-tool strategy."""
        self.model.parameters.thinking_enable = False

        await self.model.generate_structured_output(self.messages, self.schema)

        self.mock_create.assert_awaited_once()
        request = self.mock_create.await_args.kwargs
        self.assertEqual(
            request["tool_choice"],
            {
                "type": "function",
                "function": {"name": "generate_structured_output"},
            },
        )
        self.assertEqual(request["extra_body"]["thinking"]["type"], "disabled")

    async def test_thinking_override_is_request_local(self) -> None:
        """Concurrent overrides agree with the wire request without mutation."""
        for configured_thinking in (True, False):
            with self.subTest(configured_thinking=configured_thinking):
                self.model.parameters.thinking_enable = configured_thinking
                self.mock_create.reset_mock()
                bodies = [
                    {"custom": "first", "thinking": {"type": "enabled"}},
                    {"custom": "second", "thinking": {"type": "disabled"}},
                    {"custom": "third", "thinking": {"budget_tokens": 1024}},
                ]
                original_bodies = deepcopy(bodies)

                await asyncio.gather(
                    *(
                        self.model.generate_structured_output(
                            self.messages,
                            self.schema,
                            extra_body=body,
                        )
                        for body in bodies
                    ),
                )

                self.assertEqual(self.mock_create.await_count, 3)
                for call in self.mock_create.await_args_list:
                    request = call.kwargs
                    if request["extra_body"]["thinking"]["type"] == "enabled":
                        self.assertEqual(request["tool_choice"], "auto")
                    else:
                        self.assertEqual(
                            request["tool_choice"]["function"]["name"],
                            "generate_structured_output",
                        )
                self.assertEqual(bodies, original_bodies)
                self.assertEqual(
                    self.model.parameters.thinking_enable,
                    configured_thinking,
                )

    async def test_explicit_tool_choice_is_preserved(self) -> None:
        """An explicit caller choice still bypasses the default strategy."""
        await self.model.generate_structured_output(
            self.messages,
            self.schema,
            tool_choice=ToolChoice(mode="generate_structured_output"),
        )

        self.mock_create.assert_awaited_once()
        self.assertEqual(
            self.mock_create.await_args.kwargs["tool_choice"]["function"][
                "name"
            ],
            "generate_structured_output",
        )

    async def test_invalid_schema_is_rejected_for_all_strategies(self) -> None:
        """Both JSON Schema and Pydantic constraints remain mandatory."""
        for schema in (self.schema, _StructuredReply):
            with self.subTest(schema_type=type(schema).__name__):
                self.mock_create.reset_mock()
                self.mock_create.return_value = self._completion({"value": 0})

                with self.assertRaises(StructuredOutputError):
                    await self.model.generate_structured_output(
                        self.messages,
                        schema,
                    )

                self.assertEqual(
                    self._request_strategies(),
                    [
                        ("enabled", "auto"),
                        ("disabled", "generate_structured_output"),
                        ("enabled", None),
                    ],
                )

    async def test_valid_fallback_does_not_change_next_call(self) -> None:
        """Fallbacks still validate and never reintroduce thinking + forced."""
        self.mock_create.side_effect = [
            self._completion({}),
            self._completion({"value": 2}),
            self._completion({"value": 3}),
        ]

        first = await self.model.generate_structured_output(
            self.messages,
            _StructuredReply,
        )
        second = await self.model.generate_structured_output(
            self.messages,
            _StructuredReply,
        )

        self.assertEqual(
            (first.content, second.content), ({"value": 2}, {"value": 3})
        )
        self.assertEqual(
            self._request_strategies(),
            [
                ("enabled", "auto"),
                ("disabled", "generate_structured_output"),
                ("enabled", "auto"),
            ],
        )

    async def test_plain_text_does_not_bypass_schema(self) -> None:
        """Auto permits text on the wire, but structured output still rejects it."""
        self.mock_create.return_value = _mock_completion(text='{"value": 1}')

        with self.assertRaises(StructuredOutputError):
            await self.model.generate_structured_output(
                self.messages, self.schema
            )

        self.assertEqual(self.mock_create.await_count, 3)

    async def test_transient_retry_stays_on_auto(self) -> None:
        """Transient failures use the existing retry budget with auto unchanged."""
        import httpx
        import openai

        self.model.max_retries = 1
        self.model.retry_delay = 0
        self.mock_create.side_effect = [
            openai.APITimeoutError(
                request=httpx.Request("POST", "https://example.test")
            ),
            self._completion({"value": 1}),
        ]

        result = await self.model.generate_structured_output(
            self.messages, self.schema
        )

        self.assertEqual(result.content, {"value": 1})
        self.assertEqual(
            [
                call.kwargs["tool_choice"]
                for call in self.mock_create.await_args_list
            ],
            ["auto", "auto"],
        )

    async def test_streaming_auto_still_validates_tool_arguments(self) -> None:
        """The real stream parser and structured validator remain in the path."""
        self.model.stream = True
        self.mock_create.return_value = _MockAsyncStream(
            [
                _make_stream_chunk(
                    tool_calls=[
                        _make_tool_call_delta(
                            0,
                            tc_id="structured-1",
                            name="generate_structured_output",
                            arguments='{"value":',
                        ),
                    ],
                ),
                _make_stream_chunk(
                    tool_calls=[_make_tool_call_delta(0, arguments="1}")],
                ),
            ],
        )

        result = await self.model.generate_structured_output(
            self.messages,
            _StructuredReply,
        )

        self.mock_create.assert_awaited_once()
        self.assertEqual(
            self.mock_create.await_args.kwargs["tool_choice"], "auto"
        )
        self.assertEqual(result.content, {"value": 1})


class TestDeepSeekModelParameters(unittest.TestCase):
    """Tests for DeepSeekChatModel.Parameters."""

    def test_thinking_enable_stored_on_model(self) -> None:
        """thinking_enable is accessible through model.parameters."""
        model = DeepSeekChatModel(
            credential=DeepSeekCredential(api_key="test"),
            model="deepseek-reasoner",
            stream=False,
            context_size=65_536,
            parameters=DeepSeekChatModel.Parameters(thinking_enable=True),
        )
        self.assertTrue(model.parameters.thinking_enable)

    def test_reasoning_effort_stored_on_model(self) -> None:
        """reasoning_effort is accessible through model.parameters."""
        model = DeepSeekChatModel(
            credential=DeepSeekCredential(api_key="test"),
            model="deepseek-reasoner",
            stream=False,
            context_size=65_536,
            parameters=DeepSeekChatModel.Parameters(reasoning_effort="max"),
        )
        self.assertEqual(model.parameters.reasoning_effort, "max")


# ---------------------------------------------------------------------------
# Shared _format_tools fixtures
# ---------------------------------------------------------------------------

_FT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the time",
            "parameters": {
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "required": ["timezone"],
            },
        },
    },
]


class TestDeepSeekFormatTools(unittest.TestCase):
    """Tests for DeepSeekChatModel._format_tools."""

    def setUp(self) -> None:
        """Set up model instance."""
        self.model = _make_model()

    def test_auto_mode(self) -> None:
        """Auto mode returns tools unchanged and string 'auto'."""
        fmt_tools, fmt_choice = self.model._format_tools(
            _FT_TOOLS,
            ToolChoice(mode="auto"),
        )
        self.assertEqual(fmt_tools, _FT_TOOLS)
        self.assertEqual(fmt_choice, "auto")

    def test_none_mode(self) -> None:
        """None mode returns tools unchanged and string 'none'."""
        fmt_tools, fmt_choice = self.model._format_tools(
            _FT_TOOLS,
            ToolChoice(mode="none"),
        )
        self.assertEqual(fmt_tools, _FT_TOOLS)
        self.assertEqual(fmt_choice, "none")

    def test_required_mode(self) -> None:
        """Required mode returns tools unchanged and string 'required'."""
        fmt_tools, fmt_choice = self.model._format_tools(
            _FT_TOOLS,
            ToolChoice(mode="required"),
        )
        self.assertEqual(fmt_tools, _FT_TOOLS)
        self.assertEqual(fmt_choice, "required")

    def test_str_mode_force_call(self) -> None:
        """A specific tool name returns a type=function dict."""
        fmt_tools, fmt_choice = self.model._format_tools(
            _FT_TOOLS,
            ToolChoice(mode="get_weather"),
        )
        self.assertEqual(fmt_tools, _FT_TOOLS)
        self.assertEqual(
            fmt_choice,
            {"type": "function", "function": {"name": "get_weather"}},
        )

    def test_tools_filtered(self) -> None:
        """When tool_choice.tools is set, only those tools are included."""
        fmt_tools, fmt_choice = self.model._format_tools(
            _FT_TOOLS,
            ToolChoice(mode="auto", tools=["get_weather"]),
        )
        self.assertEqual(len(fmt_tools), 1)
        self.assertEqual(fmt_tools[0]["function"]["name"], "get_weather")
        self.assertEqual(fmt_choice, "auto")

    def test_no_tool_choice(self) -> None:
        """Without tool_choice, returns tools and None."""
        fmt_tools, fmt_choice = self.model._format_tools(_FT_TOOLS, None)
        self.assertEqual(fmt_tools, _FT_TOOLS)
        self.assertIsNone(fmt_choice)
