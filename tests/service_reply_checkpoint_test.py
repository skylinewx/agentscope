"""Refresh a running reply after more events than the bounded replay log holds."""
import asyncio
from functools import partial
from unittest import TestCase
from unittest.mock import patch

from pydantic import TypeAdapter

import shared_agent_defaults_test as fixtures
from agentscope.app._service._reply_progress import apply_progress
from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import (
    AgentEvent, CustomEvent, ModelCallStartEvent, ReplyStartEvent, ReplyEndEvent,
    TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent,
)
from agentscope.message import Msg, UserMsg
from utils import MockModel


class ReplyCheckpointTests(TestCase):
    def test_live_history_and_replay_recover_long_reply_without_start_event(self):
        for cls in (fixtures.SharedDefaultsRedisTest, fixtures.SharedDefaultsSQLTest):
            with self.subTest(storage=cls.__name__):
                app = cls()
                try:
                    app.setUp()
                    sid = app.create(name='Running reply refresh regression')
                    ready, release = asyncio.Event(), asyncio.Event()

                    async def source(_agent, **_kwargs):
                        yield ReplyStartEvent(session_id=sid, reply_id='reply', name='Fixture')
                        yield ModelCallStartEvent(reply_id='reply', model_name='fixture')
                        yield TextBlockStartEvent(reply_id='reply', block_id='answer')
                        for _ in range(MessageBusKeys.SESSION_REPLAY_MAX_LEN + 100):
                            yield TextBlockDeltaEvent(reply_id='reply', block_id='answer', delta='字')
                        ready.set()
                        await release.wait()
                        for _ in range(50):
                            yield TextBlockDeltaEvent(reply_id='reply', block_id='answer', delta='尾')
                        yield TextBlockEndEvent(reply_id='reply', block_id='answer')
                        yield ReplyEndEvent(session_id=sid, reply_id='reply', finished_reason='completed')

                    async def model_for(viewer, config, access):
                        await access.resolve_credential(viewer, config.credential_id)
                        return MockModel(context_size=1000000)

                    with patch('agentscope.app._service._chat.get_model', side_effect=model_for), \
                         patch('agentscope.agent.Agent.reply_stream', source):
                        future = app.portal.start_task_soon(partial(
                            app.app.state.chat_service.run, user_id='viewer', agent_id='shared',
                            session_id=sid, input_msg=UserMsg('viewer', 'Long reply fixture'),
                        ))
                        try:
                            app.portal.call(partial(asyncio.wait_for, ready.wait(), timeout=20))
                            response = app.client.get(f'/sessions/{sid}/messages',
                                                      params={'agent_id': 'shared'}, headers=app.headers)
                            self.assertEqual(response.status_code, 200, response.text)
                            history = response.json()
                            self.assertTrue(history['is_running'])
                            self.assertEqual(len(history['messages']), 2)
                            restored = Msg.model_validate(history['messages'][-1])
                            self.assertIsNone(restored.finished_reason)
                            self.assertTrue(restored.metadata['agentProgress']['steps'])
                            before_subscribe = restored.model_copy(deep=True)
                            bus = app.app.state.message_bus
                            remaining = app.portal.call(partial(
                                bus.log_read, MessageBusKeys.session_events(sid),
                                since=history['event_cursor'], max_count=2000,
                            ))
                            self.assertLess(len(remaining), 128)
                            self.assertFalse(any(e['type'] == 'REPLY_START' for _, e in remaining))
                            for _, value in remaining:
                                event = TypeAdapter(AgentEvent).validate_python(value)
                                if isinstance(event, CustomEvent):
                                    apply_progress(restored, event)
                                else:
                                    restored.append_event(event)
                            self.assertEqual(restored.get_text_content(), '字' * 1100)
                        finally:
                            app.portal.call(release.set)
                            future.result(timeout=20)
                    final = app.client.get(f'/sessions/{sid}/messages',
                                           params={'agent_id': 'shared'}, headers=app.headers).json()
                    self.assertFalse(final['is_running'])
                    self.assertEqual(final['messages'][-1]['finished_reason'], 'completed')
                    expected = '字' * 1100 + '尾' * 50
                    self.assertEqual(Msg.model_validate(final['messages'][-1]).get_text_content(), expected)
                    # Completion between the HTTP history read and the SSE
                    # subscribe must not remove the missing answer suffix.
                    delayed = app.portal.call(partial(
                        bus.log_read, MessageBusKeys.session_events(sid),
                        since=history['event_cursor'], max_count=2000,
                    ))
                    for _, value in delayed:
                        event = TypeAdapter(AgentEvent).validate_python(value)
                        if isinstance(event, CustomEvent):
                            apply_progress(before_subscribe, event)
                        else:
                            before_subscribe.append_event(event)
                    self.assertEqual(before_subscribe.get_text_content(), expected)
                    self.assertEqual(before_subscribe.metadata, final['messages'][-1]['metadata'])
                finally:
                    app.doCleanups()
