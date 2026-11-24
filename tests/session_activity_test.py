"""The account stream reports only persisted, final results."""

import asyncio
from contextlib import aclosing
from functools import partial
import json
from unittest import TestCase
from unittest.mock import patch

import shared_agent_defaults_test as fixtures
from agentscope.app._router._session import stream_session_updates
from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import ReplyEndEvent, ReplyStartEvent
from agentscope.message import UserMsg
from utils import MockModel


class SessionActivityTest(TestCase):
    def test_waiting_reply_completes_with_the_original_question(self):
        app = fixtures.SharedDefaultsRedisTest()
        self.addCleanup(app.doCleanups)
        app.setUp()
        sid = app.create(name="A fixed session name")
        owner, agent_id = "viewer", "shared"
        bus = app.app.state.message_bus
        calls = 0

        async def reply_source(_agent, **_kwargs):
            nonlocal calls
            calls += 1
            reply_id = f"reply-{calls}"
            status = {1: "WAITING", 5: "BLOCKED"}.get(calls, "READY")
            yield ReplyStartEvent(session_id=sid, reply_id=reply_id, name="Fixture")
            yield ReplyEndEvent(
                session_id=sid,
                reply_id=reply_id,
                finished_reason="interrupted" if calls == 4 else "completed",
                metadata={"analysisCompletion": {"status": status}},
            )

        async def frame(events):
            data = await asyncio.wait_for(anext(events), timeout=3)
            return json.loads(data.removeprefix("data: "))

        async def run_sequence():
            response = await stream_session_updates(user_id=owner, message_bus=bus)
            run = partial(app.app.state.chat_service.run, owner, sid, agent_id)
            async with aclosing(response.body_iterator) as events:
                self.assertEqual(await frame(events), {"type": "ready"})
                with patch("agentscope.app._service._chat.get_model", return_value=MockModel()), \
                     patch("agentscope.agent.Agent.reply_stream", reply_source):
                    await run(UserMsg("viewer", "\n 分板块核对预算。\n使用已核验的数据"))
                    self.assertEqual((await frame(events))["status"], "running")
                    self.assertEqual((await frame(events))["status"], "awaiting_external_result")
                    self.assertEqual(await bus.log_read(MessageBusKeys.session_updates(owner)), [])
                    summary = await app.storage.get_session_summary(owner, agent_id, sid)
                    self.assertEqual(summary.parked_status, "awaiting_external_result")

                    await run(None)
                    await run(UserMsg("viewer", "列出净利润预算为负的板块？"))
                    await run(UserMsg("viewer", "被取消的请求"))
                    await run(UserMsg("viewer", "因缺少数据无法完成的请求"))

                live = []
                while len(live) < 2:
                    event = await frame(events)
                    if event["type"] == "reply_completed":
                        saved = await app.storage.get_message(owner, sid, event["reply_id"])
                        self.assertEqual(saved.finished_reason, "completed")
                        live.append(event)
                self.assertEqual([(event["reply_id"], event["question"]) for event in live], [
                    ("reply-2", "分板块核对预算"), ("reply-3", "列出净利润预算为负的板块"),
                ])

            replay = await stream_session_updates(user_id=owner, message_bus=bus)
            async with aclosing(replay.body_iterator) as events:
                for event in live:
                    receipt = {key: value for key, value in event.items() if key != "question"}
                    self.assertEqual(await frame(events), {**receipt, "replayed": True})
                self.assertEqual(await frame(events), {"type": "ready"})

            other = await stream_session_updates(user_id="other-user", message_bus=bus)
            async with aclosing(other.body_iterator) as events:
                self.assertEqual(await frame(events), {"type": "ready"})

        app.portal.call(run_sequence)
