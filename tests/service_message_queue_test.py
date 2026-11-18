"""Queue semantics against real SQL and Redis storage, without model calls."""

import asyncio
from contextlib import nullcontext
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi import HTTPException

from agentscope.app._manager import ChatRunRegistry
from agentscope.app._service._message_queue import MessageQueueService
from agentscope.app.message_bus import InMemoryMessageBus, MessageBusKeys
from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    RedisStorage,
    SessionConfig,
    ChatModelConfig,
)
from agentscope.message import (
    AssistantMsg,
    UserMsg,
    ToolCallBlock,
    ToolCallState,
)
from agentscope.types import ReplyFinishedReason
from agentscope.app.storage._model._message_queue import (
    MessageQueueRecord,
    QueuedMessage,
)


class _Access:
    async def resolve_agent(self, user_id, agent_id):
        return SimpleNamespace(id=agent_id)


class _QueueContract:
    async def asyncSetUp(self):
        self.storage = await self.make_storage()
        self.addAsyncCleanup(self.storage.aclose)
        self.bus = InMemoryMessageBus()
        self.addAsyncCleanup(self.bus.aclose)
        self.registry = await ChatRunRegistry().__aenter__()
        self.addAsyncCleanup(self.registry.__aexit__, None, None, None)
        self.service = MessageQueueService(
            self.storage,
            self.bus,
            SimpleNamespace(run=self.execute),
            self.registry,
            _Access(),
        )
        self.session = await self.storage.upsert_session(
            "owner",
            "agent",
            SessionConfig(
                workspace_id="workspace",
                chat_model_config=ChatModelConfig(
                    type="test",
                    credential_id="test",
                    model="test",
                    parameters={},
                ),
            ),
            session_id="session",
        )
        self.seen = []

    async def read(self):
        return await self.service.read("owner", "agent", "session")

    async def add(self, text):
        return await self.service.enqueue(
            "owner", "agent", "session", UserMsg("user", text, id=text)
        )

    async def action(self, operation, item=None, message=None):
        return await self.service.action(
            "owner",
            "agent",
            "session",
            operation,
            item_id=item.message.id if item else None,
            version=item.version if item else None,
            message=message,
        )

    async def complete(self, message, reason=ReplyFinishedReason.COMPLETED):
        session = await self.storage.get_session("owner", "agent", "session")
        self.seen.append(
            (
                message.get_text_content(),
                [m.get_text_content() for m in session.state.context],
            )
        )
        reply = AssistantMsg(
            "agent",
            f"answer:{message.get_text_content()}",
            finished_at=datetime.now(timezone.utc).isoformat(),
            finished_reason=reason,
        )
        session.state.context.extend([message, reply])
        await self.storage.upsert_message("owner", "session", reply)
        await self.storage.update_session_state(
            "owner", "agent", "session", session.state
        )

    async def execute(
        self,
        user_id="owner",
        session_id="session",
        agent_id="agent",
        consume_queue=True,
        input_msg=None,
        submission=None,
    ):
        lock = (
            nullcontext()
            if submission is not None
            else self.bus.acquire_lock(MessageBusKeys.session_lock(session_id))
        )
        async with lock:
            async with self.service.turn(
                user_id,
                agent_id,
                session_id,
                input_msg,
                consume_queue if submission is None else False,
                submission=submission,
            ) as message:
                if message is not None:
                    await self.complete(message)

    async def test_idle_submission_starts_without_ever_entering_pending(self):
        revisions = []
        change = self.service._change

        async def observed(initial, mutate):
            record = await change(initial, mutate)
            revisions.append([item.message.id for item in record.items])
            return record

        self.service._change = observed
        status = await self.service.submit(
            "owner", "agent", "session", UserMsg("user", "direct", id="direct")
        )
        task = self.registry.get("session")
        if task:
            await task
        self.assertEqual(status, "started")
        self.assertEqual(self.seen[0][0], "direct")
        self.assertTrue(revisions)
        self.assertTrue(all(not items for items in revisions))
        self.assertIsNotNone(
            await self.storage.get_message("owner", "session", "direct")
        )

    async def test_busy_submission_queues_while_idle_submission_runs(self):
        entered, release = asyncio.Event(), asyncio.Event()
        complete = self.complete

        async def blocked(message):
            entered.set()
            await release.wait()
            await complete(message)

        self.complete = blocked
        self.assertEqual(
            await self.service.submit(
                "owner",
                "agent",
                "session",
                UserMsg("user", "first", id="first"),
            ),
            "started",
        )
        await entered.wait()
        self.assertEqual(
            await self.service.submit(
                "owner",
                "agent",
                "session",
                UserMsg("user", "second", id="second"),
            ),
            "queued",
        )
        queue = await self.read()
        self.assertEqual(queue.active_id, "first")
        self.assertEqual([item.message.id for item in queue.items], ["second"])
        task = self.registry.get("session")
        release.set()
        await task
        self.complete = complete
        await self.execute()
        self.assertEqual([item[0] for item in self.seen], ["first", "second"])

    async def test_direct_retries_execute_only_once(self):
        message = UserMsg("user", "once", id="once")
        await asyncio.gather(
            *(
                self.service.submit("owner", "agent", "session", message)
                for _ in range(8)
            )
        )
        task = self.registry.get("session")
        if task:
            await task
        await self.execute()
        self.assertEqual([item[0] for item in self.seen], ["once"])
        self.assertEqual((await self.read()).items, [])

    async def test_following_enqueue_cannot_overtake_direct_admission(self):
        entering, admit, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        change, complete = self.service._change, self.complete

        async def delayed(initial, mutate):
            if mutate.__name__ == "start":
                entering.set()
                await admit.wait()
            return await change(initial, mutate)

        async def blocked(message):
            await release.wait()
            await complete(message)

        self.service._change, self.complete = delayed, blocked
        first = asyncio.create_task(
            self.service.submit(
                "owner",
                "agent",
                "session",
                UserMsg("user", "first", id="first"),
            )
        )
        await entering.wait()
        # A separate service instance models a second application worker.
        other = MessageQueueService(
            self.storage,
            self.bus,
            self.service.chat,
            ChatRunRegistry(),
            _Access(),
        )
        second = asyncio.create_task(
            other.enqueue(
                "owner",
                "agent",
                "session",
                UserMsg("user", "second", id="second"),
            )
        )
        await asyncio.sleep(0.02)
        self.assertFalse(second.done())
        admit.set()
        self.assertEqual(await first, "started")
        await second
        queue = await self.read()
        self.assertEqual(queue.active_id, "first")
        self.assertEqual([item.message.id for item in queue.items], ["second"])
        task = self.registry.get("session")
        release.set()
        await task

    async def test_direct_submission_respects_pause_and_backlog(self):
        await self.action("pause")
        status = await self.service.submit(
            "owner", "agent", "session", UserMsg("user", "first", id="first")
        )
        self.assertEqual(status, "queued")
        await self.action("resume")
        status = await self.service.submit(
            "owner", "agent", "session", UserMsg("user", "second", id="second")
        )
        self.assertEqual(status, "queued")
        self.assertEqual(
            [item.message.id for item in (await self.read()).items],
            ["first", "second"],
        )
        self.assertEqual(self.seen, [])

    async def test_fifo_reads_previous_answer_and_pending_not_in_history(self):
        await self.add("first")
        await self.add("second")
        self.assertEqual(
            (await self.storage.list_messages("owner", "session"))[0], []
        )
        await self.execute()
        self.assertEqual(
            [x.message.id for x in (await self.read()).items], ["second"]
        )
        await self.execute()
        self.assertEqual([x[0] for x in self.seen], ["first", "second"])
        self.assertIn("answer:first", self.seen[1][1])

    async def test_duplicate_submission_and_two_dispatchers(self):
        await asyncio.gather(*(self.add("same") for _ in range(8)))
        self.assertEqual(len((await self.read()).items), 1)
        await asyncio.gather(self.execute(), self.execute())
        self.assertEqual(len(self.seen), 1)
        await self.add("same")
        self.assertEqual((await self.read()).items, [])

    async def test_edit_holds_dispatch_preserves_position_and_rejects_stale_save(
        self,
    ):
        initial = await self.add("first")
        old = initial.items[0]
        await self.add("second")
        edited = (await self.action("begin_edit", old)).items[0]
        await self.execute()
        self.assertEqual(self.seen, [])
        changed = old.message.model_copy(deep=True)
        changed.content[0].text = "edited"
        with self.assertRaises(HTTPException) as error:
            await self.action("save", old, changed)
        self.assertEqual(error.exception.status_code, 409)
        saved = await self.action("save", edited, changed)
        self.assertEqual(saved.items[0].enqueued_at, old.enqueued_at)
        await self.execute()
        self.assertEqual(self.seen[0][0], "edited")
        self.assertEqual((await self.read()).items[0].message.id, "second")

    async def test_edit_blocks_next_but_does_not_stop_current(self):
        await self.add("first")
        next_item = (await self.add("second")).items[1]
        async with self.bus.acquire_lock(
            MessageBusKeys.session_lock("session")
        ):
            async with self.service.turn(
                "owner", "agent", "session", None, True
            ) as current:
                editing = (await self.action("begin_edit", next_item)).items[0]
                await self.complete(current)
        await self.execute()
        self.assertEqual(len(self.seen), 1)
        await self.action("cancel_edit", editing)
        await self.execute()
        self.assertEqual(len(self.seen), 2)

    async def test_delete_undo_original_order_and_cannot_change_started_item(
        self,
    ):
        first = (await self.add("first")).items[0]
        await self.add("second")
        await self.action("delete", first)
        await self.action("restore", first)
        restored = (await self.read()).items[0]
        self.assertEqual(restored.message.id, first.message.id)
        async with self.bus.acquire_lock(
            MessageBusKeys.session_lock("session")
        ):
            async with self.service.turn(
                "owner", "agent", "session", None, True
            ) as current:
                for action in ("delete", "begin_edit"):
                    with self.assertRaises(HTTPException):
                        await self.action(action, restored)
                await self.complete(current)

    async def test_error_and_stop_pause_remaining_until_resume(self):
        for reason in (
            ReplyFinishedReason.ERROR,
            ReplyFinishedReason.INTERRUPTED,
        ):
            await self.add(f"first-{reason}")
            await self.add(f"second-{reason}")
            async with self.bus.acquire_lock(
                MessageBusKeys.session_lock("session")
            ):
                async with self.service.turn(
                    "owner", "agent", "session", None, True
                ) as current:
                    if reason == ReplyFinishedReason.INTERRUPTED:
                        await self.action("stop")
                    await self.complete(current, reason)
            queue = await self.read()
            self.assertTrue(queue.paused)
            await self.execute()
            self.assertEqual(len((await self.read()).items), 1)
            await self.action("resume")
            await self.execute()
            self.assertEqual((await self.read()).items, [])

    async def test_manual_pause_and_edit_survive_service_recreation(self):
        item = (await self.add("first")).items[0]
        await self.action("pause")
        edited = (await self.action("begin_edit", item)).items[0]
        self.service = MessageQueueService(
            self.storage,
            self.bus,
            SimpleNamespace(run=self.execute),
            self.registry,
            _Access(),
        )
        self.assertTrue((await self.read()).paused)
        self.assertEqual((await self.read()).editing_id, "first")
        await self.action("cancel_edit", edited)
        await self.execute()
        self.assertEqual(self.seen, [])
        await self.action("resume")
        await self.execute()
        self.assertEqual(len(self.seen), 1)

    async def test_parked_confirmation_and_background_do_not_advance(self):
        await self.add("first")
        await self.add("second")
        async with self.bus.acquire_lock(
            MessageBusKeys.session_lock("session")
        ):
            async with self.service.turn(
                "owner", "agent", "session", None, True
            ):
                session = await self.storage.get_session(
                    "owner", "agent", "session"
                )
                reply = AssistantMsg(
                    "agent",
                    [
                        ToolCallBlock(
                            id="tool",
                            name="tool",
                            input="{}",
                            state=ToolCallState.ASKING,
                        )
                    ],
                )
                session.state.context.append(reply)
                await self.storage.upsert_message("owner", "session", reply)
                await self.storage.update_session_state(
                    "owner", "agent", "session", session.state
                )
        queue = await self.read()
        self.assertEqual(queue.waiting_reason, "permission")
        await self.service.reconcile(queue)
        self.assertIsNone(self.registry.get("session"))
        async with self.bus.acquire_lock(
            MessageBusKeys.session_lock("session")
        ):
            async with self.service.turn("owner", "agent", "session", None):
                session.state.context.clear()
                session.state.middle_context["background_tools_v1"] = {
                    "tool": {"tool": "tool"}
                }
                reply.finished_at, reply.finished_reason = (
                    datetime.now(timezone.utc).isoformat(),
                    ReplyFinishedReason.COMPLETED,
                )
                await self.storage.upsert_message("owner", "session", reply)
                await self.storage.update_session_state(
                    "owner", "agent", "session", session.state
                )
        self.assertEqual((await self.read()).waiting_reason, "background")
        async with self.bus.acquire_lock(
            self.service.worker_key(self.service.worker_id)
        ):
            await self.service.reconcile(await self.read())
            self.assertIsNone(self.registry.get("session"))
        # A crashed background worker cannot silently release the next input.
        await self.service.reconcile(await self.read())
        self.assertEqual((await self.read()).pause_reason, "interrupted")
        await self.action("resume")
        await self.execute()
        self.assertEqual(self.seen[-1][0], "second")
        self.assertEqual((await self.read()).items, [])

    async def test_abandoned_claim_is_not_replayed_and_unstarted_input_is_retained(
        self,
    ):
        queue = await self.add("first")
        await self.add("second")

        def claim(record):
            record.active_item = record.items.pop(0)
            record.active_id = record.active_item.message.id
            record.activity = "running"

        await self.storage.mutate_message_queue(queue, claim)
        await self.service.reconcile(await self.read())
        recovered = await self.read()
        self.assertTrue(recovered.paused)
        self.assertEqual(
            [x.message.id for x in recovered.items], ["first", "second"]
        )
        self.assertEqual(self.seen, [])

    async def test_owner_scope_and_delete_cascade(self):
        await self.add("secret")
        with self.assertRaises(HTTPException) as error:
            await self.service.read("stranger", "agent", "session")
        self.assertEqual(error.exception.status_code, 404)
        await self.storage.delete_session("owner", "agent", "session")
        self.assertIsNone(
            await self.storage.get_message_queue("owner", "session")
        )

    async def test_concurrent_append_does_not_clobber_session_state(self):
        await asyncio.gather(
            *(self.add(f"item-{i}") for i in range(8)),
            self.storage.update_session_state(
                "owner", "agent", "session", self.session.state
            ),
        )
        self.assertEqual(len((await self.read()).items), 8)


class SQLMessageQueueTest(_QueueContract, IsolatedAsyncioTestCase):
    async def test_alembic_provisions_queue_schema(self):
        async with AsyncSQLAlchemyStorage(
            f'sqlite+aiosqlite:///{(Path(self.tmp.name) / "migrated.db").as_posix()}',
            create_tables=False,
            auto_migrate=True,
        ) as storage:
            initial = MessageQueueRecord(
                id="migration", user_id="owner", agent_id="agent"
            )
            await storage.mutate_message_queue(
                initial,
                lambda record: record.items.append(
                    QueuedMessage(
                        message=UserMsg("user", "migrated"), sequence=0
                    ),
                ),
            )
            self.assertEqual(len(await storage.list_message_queues()), 1)

    async def make_storage(self):
        Path(".cache/message-queue-tests").mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(
            dir=Path(".cache/message-queue-tests").resolve()
        )
        self.addCleanup(self.tmp.cleanup)
        return await AsyncSQLAlchemyStorage(
            f'sqlite+aiosqlite:///{(Path(self.tmp.name) / "queue.db").as_posix()}'
        ).__aenter__()

    async def test_disk_reopen_retains_pending_input(self):
        await self.add("survives-restart")
        await self.storage.aclose()
        self.storage = await AsyncSQLAlchemyStorage(
            f'sqlite+aiosqlite:///{(Path(self.tmp.name) / "queue.db").as_posix()}'
        ).__aenter__()
        self.addAsyncCleanup(self.storage.aclose)
        queue = await self.storage.get_message_queue("owner", "session")
        self.assertEqual(queue.items[0].message.id, "survives-restart")


class RedisMessageQueueTest(_QueueContract, IsolatedAsyncioTestCase):
    async def make_storage(self):
        storage = RedisStorage()
        storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return storage
