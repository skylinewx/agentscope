"""Persistent FIFO inputs, claimed under the same lock as agent execution.

The dispatcher only requests a run. Selection/removal happen inside the
session lock, and are committed before any model or tool is invoked. An
unfinished claim is paused after a crash; it is never silently replayed.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from ..._logging import logger
from ...event import CustomEvent
from ...message import Msg
from ...types import ReplyFinishedReason
from .._bus_ops import publish_session_event
from ..message_bus import MessageBusKeys
from ..storage._model._message_queue import MessageQueueRecord, QueuedMessage
from ._session import SessionService, SessionStatus


class MessageQueueService:
    """Owns queue operations, restart reconciliation and run boundaries."""

    def __init__(
        self, storage, message_bus, chat_service, registry, access, auth=None
    ):
        self.storage, self.bus, self.chat = storage, message_bus, chat_service
        self.registry, self.access, self.auth = registry, access, auth
        self.worker_id = uuid4().hex
        self._task = None
        self._wake = asyncio.Event()
        self._lease = None

    @staticmethod
    def worker_key(worker_id):
        return f"agentscope:message_queue_worker:{worker_id}"

    async def __aenter__(self):
        self._lease = self.bus.acquire_lock(
            self.worker_key(self.worker_id), ttl_secs=30
        )
        await self._lease.__aenter__()
        self._task = asyncio.create_task(
            self._loop(), name="message-queue-dispatcher"
        )
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._lease:
            await self._lease.__aexit__(*exc)

    async def _loop(self):
        while True:
            self._wake.clear()
            try:
                for queue in await self.storage.list_message_queues():
                    await self.reconcile(queue)
            except Exception:
                logger.exception(
                    "Message queue reconciliation failed; will retry."
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def ensure_session(self, user_id, agent_id, session_id):
        await self.access.resolve_agent(user_id, agent_id)
        session = await self.storage.get_session(user_id, agent_id, session_id)
        if session is None:
            raise HTTPException(404, "Session not found.")
        return session

    async def read(self, user_id, agent_id, session_id):
        session = await self.ensure_session(user_id, agent_id, session_id)
        queue = await self.storage.get_message_queue(user_id, session_id)
        if queue is not None:
            return queue
        queue = MessageQueueRecord(
            id=session_id, user_id=user_id, agent_id=agent_id
        )
        if await self.bus.is_locked(MessageBusKeys.session_lock(session_id)):
            queue.activity = "running"
        else:
            self._park(queue, session)
        return queue

    async def _change(self, initial, mutate):
        def apply(queue):
            mutate(queue)
            queue.revision += 1

        queue = await self.storage.mutate_message_queue(initial, apply)
        self._wake.set()
        # Persistence is the acknowledgement boundary. A failed notification
        # must not make a successfully committed command appear to fail.
        try:
            await publish_session_event(
                self.bus,
                queue.id,
                CustomEvent(
                    name="message_queue_updated",
                    value={"revision": queue.revision},
                ).model_dump(mode="json"),
            )
        except Exception:
            logger.exception("Queue saved, but live notification failed.")
        return queue

    @staticmethod
    def validate_message(message):
        if message.role != "user" or not message.content:
            raise HTTPException(
                422, "A user message with content is required."
            )
        if not message.id or len(message.id) > 128:
            raise HTTPException(422, "Invalid message id.")
        if any(
            block.type not in ("text", "data", "file")
            for block in message.content
        ):
            raise HTTPException(
                422, "Only text and attachments can be queued."
            )
        if not any(
            block.type != "text" or block.text.strip()
            for block in message.content
        ):
            raise HTTPException(422, "Message cannot be empty.")

    async def enqueue(self, user_id, agent_id, session_id, message):
        async with self.bus.acquire_lock(
            f"agentscope:message_submission:{session_id}", ttl_secs=30
        ):
            return await self._enqueue(user_id, agent_id, session_id, message)

    async def _enqueue(self, user_id, agent_id, session_id, message):
        self.validate_message(message)
        initial = await self.read(user_id, agent_id, session_id)
        # Covers delayed retries after the bounded receipt window rotated.
        if await self.storage.get_message(user_id, session_id, message.id):
            return initial

        def add(queue):
            if message.id in queue.receipts:
                return
            if len(queue.items) >= 100:
                raise HTTPException(409, "Queue is full (100 messages).")
            if (
                not queue.items
                and queue.activity == "idle"
                and queue.pause_reason in ("stopped", "error", "interrupted")
            ):
                queue.paused, queue.pause_reason = False, None
            queue.items.append(
                QueuedMessage(message=message, sequence=queue.next_sequence)
            )
            queue.next_sequence += 1
            queue.receipts = [*queue.receipts[-255:], message.id]

        return await self._change(initial, add)

    @staticmethod
    def can_start(queue):
        return (
            queue.activity == "idle"
            and not queue.items
            and not queue.editing_id
            and (not queue.paused or queue.pause_reason != "manual")
        )

    async def submit(self, user_id, agent_id, session_id, message):
        """Start an idle conversation immediately; queue only behind work.

        Admission is acknowledged under the execution lock, before model
        assembly. The HTTP request never waits for a model response.
        """
        # Serialize admission, not the whole conversation. A following HTTP
        # request must not enqueue ahead of an idle message being admitted.
        async with self.bus.acquire_lock(
            f"agentscope:message_submission:{session_id}", ttl_secs=30
        ):
            return await self._submit(user_id, agent_id, session_id, message)

    async def _submit(self, user_id, agent_id, session_id, message):
        self.validate_message(message)
        initial = await self.read(user_id, agent_id, session_id)
        if await self.storage.get_message(user_id, session_id, message.id):
            return "started"
        local = self.registry.get(session_id)
        if (
            not self.can_start(initial)
            or (local and not local.done())
            or await self.bus.is_locked(
                MessageBusKeys.session_lock(session_id)
            )
        ):
            queue = await self._enqueue(user_id, agent_id, session_id, message)
            return (
                "queued"
                if any(item.message.id == message.id for item in queue.items)
                else "started"
            )

        admitted = asyncio.get_running_loop().create_future()

        async def run():
            lock = self.bus.acquire_lock(
                MessageBusKeys.session_lock(session_id),
                ttl_secs=MessageBusKeys.SESSION_RUN_TTL_SECS,
            )
            try:
                # Another process may win after the optimistic liveness read.
                async with asyncio.timeout(0.2):
                    await lock.__aenter__()
            except TimeoutError:
                await self._enqueue(user_id, agent_id, session_id, message)
                admitted.set_result("queued")
                return
            try:
                await self.chat.run(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    input_msg=message,
                    submission=admitted,
                )
            finally:
                await lock.__aexit__(None, None, None)

        try:
            task = self.registry.spawn(
                run(), session_id=session_id, user_id=user_id
            )
        except RuntimeError:
            await self._enqueue(user_id, agent_id, session_id, message)
            return "queued"

        def finished(_task):
            error = None if _task.cancelled() else _task.exception()
            if not admitted.done():
                admitted.set_exception(
                    error
                    or HTTPException(503, "Message could not be started.")
                )

        task.add_done_callback(finished)
        # Disconnecting the HTTP client must not cancel an accepted run.
        try:
            return await asyncio.shield(admitted)
        except asyncio.CancelledError:
            # Keep admission ordered even if the submitting tab disconnects.
            await admitted
            raise

    async def action(
        self,
        user_id,
        agent_id,
        session_id,
        operation,
        item_id=None,
        version=None,
        message=None,
    ):
        initial = await self.read(user_id, agent_id, session_id)
        if message is not None:
            self.validate_message(message)

        def apply(queue):
            if operation in ("pause", "resume", "stop"):
                queue.paused = operation != "resume"
                queue.pause_reason = (
                    "stopped"
                    if operation == "stop"
                    else "manual" if queue.paused else None
                )
                return
            if operation == "restore":
                item = next(
                    (x for x in queue.deleted if x.message.id == item_id), None
                )
                if (
                    item is None
                    or (
                        datetime.now(timezone.utc) - item.deleted_at
                    ).total_seconds()
                    > 10
                ):
                    raise HTTPException(409, "Undo expired.")
                if len(queue.items) >= 100:
                    raise HTTPException(409, "Queue is full.")
                queue.deleted.remove(item)
                item.deleted_at = None
                item.version += 1
                queue.items.append(item)
                queue.items.sort(key=lambda x: x.sequence)
                return
            item = next(
                (x for x in queue.items if x.message.id == item_id), None
            )
            if item is None:
                raise HTTPException(
                    409, "Message has already started or was removed."
                )
            if version != item.version:
                raise HTTPException(
                    409,
                    "Message changed in another window. Reload and try again.",
                )
            if operation == "begin_edit":
                if queue.editing_id not in (None, item_id):
                    raise HTTPException(
                        409, "Finish editing the other message first."
                    )
                queue.editing_id = item_id
                item.version += 1
            elif operation in ("save", "cancel_edit"):
                if queue.editing_id != item_id:
                    raise HTTPException(
                        409, "Message is no longer being edited."
                    )
                if operation == "save":
                    if message is None or message.id != item_id:
                        raise HTTPException(
                            422, "The edited message id must match."
                        )
                    item.message = message
                    item.version += 1
                queue.editing_id = None
            elif operation == "delete":
                queue.items.remove(item)
                if queue.editing_id == item_id:
                    queue.editing_id = None
                item.deleted_at = datetime.now(timezone.utc)
                queue.deleted = [*queue.deleted[-19:], item]
            else:
                raise HTTPException(422, "Unknown queue operation.")

        return await self._change(initial, apply)

    @staticmethod
    def _park(queue, session):
        status = SessionService.derive_parked_status(session.state.context)
        reason = {
            SessionStatus.AWAITING_PERMISSION: "permission",
            SessionStatus.AWAITING_EXTERNAL_RESULT: "external",
        }.get(status)
        if not reason and session.state.middle_context.get(
            "background_tools_v1"
        ):
            reason = "background"
        if reason:
            queue.activity, queue.waiting_reason = "waiting", reason
        return bool(reason)

    async def reconcile(self, snapshot):
        sid = snapshot.id
        local = self.registry.get(sid)
        if (local and not local.done()) or await self.bus.is_locked(
            MessageBusKeys.session_lock(sid)
        ):
            return
        if (
            await self.storage.get_session(
                snapshot.user_id, snapshot.agent_id, sid
            )
            is None
        ):
            await self.storage.delete_message_queue(snapshot.user_id, sid)
            return
        if self.auth and not await self.auth.is_active(snapshot.user_id):
            return
        if snapshot.activity == "running" or (
            snapshot.activity == "waiting"
            and snapshot.waiting_reason == "background"
            and (
                not snapshot.worker_id
                or not await self.bus.is_locked(
                    self.worker_key(snapshot.worker_id)
                )
            )
        ):
            # Re-check under the execution lock. A continuation may have won
            # since the liveness read above; never overwrite its new state.
            async with self.bus.acquire_lock(MessageBusKeys.session_lock(sid)):
                entered_history = (
                    snapshot.active_id
                    and await self.storage.get_message(
                        snapshot.user_id, sid, snapshot.active_id
                    )
                )

                def recover(queue):
                    if queue.revision == snapshot.revision:
                        if (
                            queue.active_item is not None
                            and not entered_history
                        ):
                            queue.items.insert(0, queue.active_item)
                        queue.active_item = None
                        queue.discard_background = True
                        (
                            queue.activity,
                            queue.active_id,
                            queue.waiting_reason,
                        ) = ("idle", None, None)
                        queue.paused, queue.pause_reason = True, "interrupted"

                await self._change(snapshot, recover)
            return
        if (
            snapshot.activity != "idle"
            or snapshot.paused
            or snapshot.editing_id
            or not snapshot.items
        ):
            return
        try:
            self.registry.spawn(
                self.chat.run(
                    user_id=snapshot.user_id,
                    session_id=sid,
                    agent_id=snapshot.agent_id,
                    consume_queue=True,
                ),
                session_id=sid,
                user_id=snapshot.user_id,
            )
        except RuntimeError:
            pass  # A resume won locally; the next sweep retries the queue.

    @asynccontextmanager
    async def turn(
        self,
        user_id,
        agent_id,
        session_id,
        input_msg,
        consume_queue=False,
        submission=None,
    ):
        """Called only while ChatService owns the distributed session lock."""
        initial = await self.storage.get_message_queue(user_id, session_id)
        initial = initial or MessageQueueRecord(
            id=session_id, user_id=user_id, agent_id=agent_id
        )
        session = await self.storage.get_session(user_id, agent_id, session_id)
        if session is None:
            yield None
            return
        discard_background = (
            consume_queue or submission is not None
        ) and initial.discard_background
        if discard_background:
            session.state.middle_context.pop("background_tools_v1", None)
        claimed = [None]
        disposition = ["started"]

        def start(queue):
            claimed[0] = None
            if submission is not None:
                if input_msg.id in queue.receipts:
                    disposition[0] = (
                        "queued"
                        if any(
                            item.message.id == input_msg.id
                            for item in queue.items
                        )
                        else "started"
                    )
                    return
                item = QueuedMessage(
                    message=input_msg, sequence=queue.next_sequence
                )
                if not self.can_start(queue) or self._park(queue, session):
                    if len(queue.items) >= 100:
                        raise HTTPException(
                            409, "Queue is full (100 messages)."
                        )
                    queue.items.append(item)
                    disposition[0] = "queued"
                else:
                    queue.paused, queue.pause_reason = False, None
                    queue.active_id = input_msg.id
                    queue.active_item = item
                    queue.discard_background = False
                    claimed[0] = input_msg
                queue.next_sequence += 1
                queue.receipts = [*queue.receipts[-255:], input_msg.id]
                if claimed[0] is None:
                    return
            elif consume_queue:
                if (
                    queue.activity != "idle"
                    or queue.paused
                    or queue.editing_id
                    or not queue.items
                ):
                    return
                if self._park(queue, session):
                    return
                item = queue.items.pop(0)
                queue.active_id = item.message.id
                queue.active_item = item.model_copy(deep=True)
                queue.discard_background = False
                claimed[0] = item.message.model_copy(deep=True)
            else:
                messages = (
                    input_msg if isinstance(input_msg, list) else [input_msg]
                )
                users = [
                    msg
                    for msg in messages
                    if isinstance(msg, Msg) and msg.role == "user"
                ]
                if users:
                    queue.active_id = users[-1].id
            queue.activity, queue.waiting_reason, queue.worker_id = (
                "running",
                None,
                self.worker_id,
            )

        try:
            queue = await self._change(initial, start)
        except NotImplementedError:
            if consume_queue:
                raise
            yield input_msg
            return
        if submission is not None and claimed[0] is None:
            submission.set_result(disposition[0])
            yield None
            return
        if consume_queue:
            if claimed[0] is None:
                yield None
                return
            input_msg = claimed[0]
            input_msg.created_at = input_msg.finished_at = datetime.now(
                timezone.utc
            ).isoformat()
        failed = False
        try:
            if consume_queue or submission is not None:
                if discard_background:
                    await self.storage.update_session_state(
                        user_id, agent_id, session_id, session.state
                    )
                # Write the user input at dispatch time even if agent assembly
                # subsequently fails. Pending entries never enter history.
                await self.storage.upsert_message(
                    user_id, session_id, input_msg
                )
                await publish_session_event(
                    self.bus,
                    session_id,
                    CustomEvent(
                        name="queued_message_started",
                        value={"message": input_msg.model_dump(mode="json")},
                    ).model_dump(mode="json"),
                )
                if submission is not None:
                    submission.set_result("started")
            yield input_msg
        except BaseException:
            failed = True
            raise
        finally:
            task = asyncio.create_task(self.settle(queue, failed))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def settle(self, initial, failed=False):
        """Persist a whole-run boundary before releasing the execution lock."""
        session = await self.storage.get_session(
            initial.user_id, initial.agent_id, initial.id
        )
        if session is None:
            return
        messages, _ = await self.storage.list_messages(
            initial.user_id, initial.id, limit=5
        )
        reply = next(
            (x for x in reversed(messages) if x.role == "assistant"), None
        )
        background = bool(
            await self.bus.registry_getall(MessageBusKeys.bg_tasks(initial.id))
        )

        def finish(queue):
            terminal_failure = reply and reply.finished_reason in (
                ReplyFinishedReason.ERROR,
                ReplyFinishedReason.INTERRUPTED,
                ReplyFinishedReason.EXCEED_MAX_ITERS,
            )
            if (
                not failed
                and not terminal_failure
                and self._park(queue, session)
            ):
                return
            if (
                failed
                or reply is None
                or reply.finished_reason != ReplyFinishedReason.COMPLETED
            ):
                queue.paused = True
                if queue.pause_reason != "stopped":
                    queue.pause_reason = (
                        "interrupted"
                        if failed
                        or (
                            reply
                            and reply.finished_reason
                            == ReplyFinishedReason.INTERRUPTED
                        )
                        else "error"
                    )
            elif background:
                queue.activity, queue.waiting_reason = "waiting", "background"
                return
            queue.activity, queue.active_id, queue.waiting_reason = (
                "idle",
                None,
                None,
            )
            queue.active_item = None

        await self._change(initial, finish)
