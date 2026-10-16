"""Reply presentation: emitted IDs, middleware continuation and persistence."""
from unittest import IsolatedAsyncioTestCase, TestCase

from utils import MockModel
from agent_basic_test import MockSequentialTool
from agentscope.agent import Agent, InjectionConfig
from agentscope.agent._reply_presentation import ReplyPresentationTracker
from agentscope.event import (
    ModelCallStartEvent, ReplyEndEvent, TextBlockStartEvent,
    TextBlockDeltaEvent, ToolCallStartEvent,
)
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit


class PresentationTrackerTests(TestCase):
    def test_only_last_accepted_model_invocation_is_final(self):
        tracker = ReplyPresentationTracker()
        for event in [
            ModelCallStartEvent(reply_id='r', model_name='mock'),
            TextBlockStartEvent(reply_id='r', block_id='progress'),
            ToolCallStartEvent(reply_id='r', tool_call_id='tool', tool_call_name='Read'),
            ModelCallStartEvent(reply_id='r', model_name='mock'),
            TextBlockStartEvent(reply_id='r', block_id='rejected-candidate'),
            # A middleware swallowed the candidate end and requested a new call.
            ModelCallStartEvent(reply_id='r', model_name='mock'),
            TextBlockStartEvent(reply_id='r', block_id='answer-1'),
            TextBlockStartEvent(reply_id='r', block_id='answer-2'),
        ]:
            tracker.apply(event)
        end = ReplyEndEvent(reply_id='r', session_id='s')
        tracker.apply(end)
        self.assertEqual(end.metadata['presentation']['answer_block_ids'], ['answer-1', 'answer-2'])

    def test_interruptions_do_not_hide_partial_answer(self):
        tracker = ReplyPresentationTracker()
        tracker.apply(TextBlockStartEvent(reply_id='r', block_id='partial'))
        for reason in ('error', 'interrupted'):
            end = ReplyEndEvent(reply_id='r', session_id='s', finished_reason=reason)
            tracker.apply(end)
            self.assertNotIn('presentation', end.metadata)

    def test_tool_only_reply_does_not_promote_commentary(self):
        tracker = ReplyPresentationTracker()
        tracker.apply(TextBlockStartEvent(reply_id='r', block_id='progress'))
        tracker.apply(ToolCallStartEvent(reply_id='r', tool_call_id='tool', tool_call_name='Generate'))
        end = ReplyEndEvent(reply_id='r', session_id='s')
        tracker.apply(end)
        self.assertEqual(end.metadata['presentation']['answer_block_ids'], [])

    def test_metadata_is_persisted_by_value(self):
        message = AssistantMsg(id='r', name='agent', content=[])
        message.append_event(TextBlockStartEvent(reply_id='r', block_id='answer'))
        message.append_event(TextBlockDeltaEvent(reply_id='r', block_id='answer', delta='Final'))
        end = ReplyEndEvent(reply_id='r', session_id='s', metadata={'presentation': {'answer_block_ids': ['answer']}})
        message.append_event(end)
        end.metadata['presentation']['answer_block_ids'].clear()
        restored = Msg.model_validate_json(message.model_dump_json())
        self.assertEqual(restored.metadata['presentation']['answer_block_ids'], ['answer'])
        self.assertEqual(restored.content[0].text, 'Final')


class PresentationStreamTests(IsolatedAsyncioTestCase):
    async def test_agent_stream_marks_streamed_ids_instead_of_model_ids(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                model = MockModel()
                answers = [
                    ChatResponse(content=[TextBlock(id='model-progress', text='Checking'), ToolCallBlock(
                        id='call-1', name='mock_sequential_tool', input='{"input":"x"}',
                    )], is_last=True),
                    ChatResponse(content=[TextBlock(id='model-answer', text='The answer')], is_last=True),
                ]
                model.set_responses([
                    [ChatResponse(content=response.content, is_last=False), response] if streaming else response
                    for response in answers
                ])
                agent = Agent(name='test', system_prompt='test', model=model,
                              toolkit=Toolkit(tools=[MockSequentialTool()]),
                              injection_config=InjectionConfig(inject_runtime_state=False))
                events = [event async for event in agent.reply_stream(UserMsg(name='user', content='Go'))]
                end = next(event for event in reversed(events) if isinstance(event, ReplyEndEvent))
                ids = end.metadata['presentation']['answer_block_ids']
                message = AssistantMsg(id=end.reply_id, name='test', content=[])
                for event in events:
                    message.append_event(event)
                self.assertNotIn('model-answer', ids)
                self.assertEqual([block.text for block in message.content if block.id in ids], ['The answer'])
                self.assertIn('Checking', message.get_text_content())
                self.assertEqual(Msg.model_validate_json(message.model_dump_json()).metadata, message.metadata)
