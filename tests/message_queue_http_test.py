"""Real HTTP -> dispatcher -> ChatService -> Agent -> persisted context."""

import asyncio
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import shared_agent_defaults_test as fixtures
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse
from utils import MockModel


class MessageQueueHTTPTest(TestCase):
    def test_idle_http_starts_immediately_and_busy_http_queues(self):
        fixture = fixtures.SharedDefaultsRedisTest()
        try:
            root = Path(".cache/queue-api-tests").resolve()
            root.mkdir(parents=True, exist_ok=True)
            temporary = fixtures.tempfile.TemporaryDirectory
            with patch.object(
                fixtures.tempfile,
                "TemporaryDirectory",
                side_effect=lambda *args, **kwargs: temporary(
                    *args, **{**kwargs, "dir": root}
                ),
            ):
                fixture.setUp()
            sid = fixture.create(name="Immediate submission fixture")
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def model_for(viewer, config, access):
                await access.resolve_credential(viewer, config.credential_id)
                calls.append(viewer)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                model = MockModel(context_size=1000000)
                model.set_responses(
                    [
                        ChatResponse(
                            content=[TextBlock(text=f"answer-{len(calls)}")],
                            is_last=True,
                        )
                    ]
                )
                return model

            def send(text):
                response = fixture.client.post(
                    "/chat/",
                    headers=fixture.headers,
                    json={
                        "agent_id": "shared",
                        "session_id": sid,
                        "input": UserMsg("viewer", text, id=text).model_dump(
                            mode="json"
                        ),
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()["status"]

            def read():
                response = fixture.client.get(
                    f"/sessions/{sid}/queue",
                    params={"agent_id": "shared"},
                    headers=fixture.headers,
                )
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            async def finish():
                release.set()
                async with asyncio.timeout(15):
                    while True:
                        queue = await fixture.storage.get_message_queue(
                            "viewer", sid
                        )
                        if queue.activity == "idle" and not queue.items:
                            return
                        await asyncio.sleep(0.02)

            with patch(
                "agentscope.app._service._chat.get_model",
                side_effect=model_for,
            ):
                self.assertEqual(send("first"), "started")
                fixture.portal.call(asyncio.wait_for, entered.wait(), 10)
                self.assertEqual(read()["items"], [])
                self.assertEqual(read()["active_id"], "first")
                messages, _ = fixture.portal.call(
                    fixture.storage.list_messages, "viewer", sid
                )
                self.assertEqual(
                    [m.id for m in messages if m.role == "user"], ["first"]
                )
                self.assertEqual(send("second"), "queued")
                self.assertEqual(
                    [item["message"]["id"] for item in read()["items"]],
                    ["second"],
                )
                self.assertEqual(send("first"), "started")
                fixture.portal.call(finish)
                # An older running-state snapshot may still send to /queue.
                # The server must also start that input directly once idle.
                response = fixture.client.post(
                    f"/sessions/{sid}/queue",
                    params={"agent_id": "shared"},
                    headers=fixture.headers,
                    json={
                        "message": UserMsg(
                            "viewer", "third", id="third"
                        ).model_dump(mode="json")
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["items"], [])
                fixture.portal.call(finish)
            messages, _ = fixture.portal.call(
                fixture.storage.list_messages, "viewer", sid
            )
            self.assertEqual(
                [m.get_text_content() for m in messages],
                [
                    "first",
                    "answer-1",
                    "second",
                    "answer-2",
                    "third",
                    "answer-3",
                ],
            )
            self.assertEqual(len(calls), 3)
        finally:
            fixture.doCleanups()

    def test_real_chat_fifo_edit_resume_and_owner_isolation(self):
        fixture = fixtures.SharedDefaultsRedisTest()
        try:
            root = Path(".cache/queue-api-tests").resolve()
            root.mkdir(parents=True, exist_ok=True)
            temporary = fixtures.tempfile.TemporaryDirectory
            with patch.object(
                fixtures.tempfile,
                "TemporaryDirectory",
                side_effect=lambda *args, **kwargs: temporary(
                    *args, **{**kwargs, "dir": root}
                ),
            ):
                fixture.setUp()
            sid = fixture.create(name="Queue integration fixture")
            url = f"/sessions/{sid}/queue"
            options = {
                "params": {"agent_id": "shared"},
                "headers": fixture.headers,
            }

            def action(operation, **kwargs):
                res = fixture.client.post(
                    url + "/actions",
                    json={"operation": operation, **kwargs},
                    **options,
                )
                self.assertEqual(res.status_code, 200, res.text)
                return res.json()

            def read():
                res = fixture.client.get(url, **options)
                self.assertEqual(res.status_code, 200, res.text)
                return res.json()

            self.assertEqual(
                fixture.client.get(
                    url,
                    params={"agent_id": "shared"},
                    headers={"X-User-ID": "owner"},
                ).status_code,
                404,
            )
            action("pause")
            for text in ("first", "second"):
                response = (
                    fixture.client.post(
                        "/chat/",
                        headers=fixture.headers,
                        json={
                            "agent_id": "shared",
                            "session_id": sid,
                            "input": UserMsg(
                                "viewer", text, id=text
                            ).model_dump(mode="json"),
                        },
                    )
                    if text == "first"
                    else fixture.client.post(
                        url,
                        json={
                            "message": UserMsg(
                                "viewer", text, id=text
                            ).model_dump(mode="json")
                        },
                        **options,
                    )
                )
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(read()["items"]), 2)
            self.assertEqual(
                fixture.portal.call(
                    fixture.storage.list_messages, "viewer", sid
                )[0],
                [],
            )
            calls = []
            failures = []
            original_report = fixture.app.state.chat_service._report_failure

            async def report(*args, **kwargs):
                failures.append(repr(args[3] if len(args) > 3 else kwargs))
                return await original_report(*args, **kwargs)

            first_entered, release = asyncio.Event(), asyncio.Event()

            async def model_for(viewer, config, access):
                await access.resolve_credential(viewer, config.credential_id)
                session = await fixture.storage.get_session(
                    "viewer", "shared", sid
                )
                calls.append(
                    [m.get_text_content() for m in session.state.context]
                )
                if len(calls) == 1:
                    first_entered.set()
                    await release.wait()
                model = MockModel(context_size=1000000)
                model.set_responses(
                    [
                        ChatResponse(
                            content=[TextBlock(text=f"answer-{len(calls)}")],
                            is_last=True,
                        )
                    ]
                )
                return model

            async def wait_for(check):
                async with asyncio.timeout(15):
                    while not await check():
                        await asyncio.sleep(0.02)

            async def finished():
                q = await fixture.storage.get_message_queue("viewer", sid)
                return q.activity == "idle" and not q.items

            async def released():
                release.set()

            with (
                patch(
                    "agentscope.app._service._chat.get_model",
                    side_effect=model_for,
                ),
                patch.object(
                    fixture.app.state.chat_service,
                    "_report_failure",
                    side_effect=report,
                ),
            ):
                action("resume")
                try:
                    fixture.portal.call(
                        asyncio.wait_for, first_entered.wait(), 10
                    )
                except TimeoutError:
                    self.fail(f"First queued run did not start: {failures}")
                pending = read()["items"][0]
                editing = action(
                    "begin_edit", item_id="second", version=pending["version"]
                )["items"][0]
                fixture.portal.call(released)

                async def first_finished():
                    q = await fixture.storage.get_message_queue("viewer", sid)
                    return q.activity == "idle"

                fixture.portal.call(wait_for, first_finished)
                self.assertEqual(
                    len(calls), 1, "Editing must hold the next turn."
                )
                changed = UserMsg("viewer", "second-edited", id="second")
                action(
                    "save",
                    item_id="second",
                    version=editing["version"],
                    message=changed.model_dump(mode="json"),
                )
                fixture.portal.call(wait_for, finished)
            messages, _ = fixture.portal.call(
                fixture.storage.list_messages, "viewer", sid
            )
            self.assertEqual(
                [m.get_text_content() for m in messages],
                ["first", "answer-1", "second-edited", "answer-2"],
            )
            self.assertIn(
                "answer-1",
                calls[1],
                "Second turn did not load the first answer.",
            )
            self.assertFalse(read()["paused"])
        finally:
            fixture.doCleanups()
