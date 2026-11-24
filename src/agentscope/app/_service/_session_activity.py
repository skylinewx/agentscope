"""Publish the sidebar status and final, persisted replies to the owner."""

from ..._logging import logger
from ...message import Msg
from ...state import AgentState
from ...types import ReplyFinishedReason
from ..message_bus import MessageBus, MessageBusKeys
from ._session import SessionService, SessionStatus


QUESTION_TITLE_KEY = "app_session_question_title"
WAITING_FOR_RESULT_KEY = "app_session_waiting_for_result"
_QUESTION_TITLE_LENGTH = 160


def question_title(messages: list[Msg]) -> str:
    """Use the first nonblank line of the question that started this reply."""
    for message in messages:
        if message.role != "user":
            continue
        for line in (message.get_text_content() or "").splitlines():
            title = line.strip().rstrip("。！？.!?").rstrip()
            if title:
                if len(title) > _QUESTION_TITLE_LENGTH:
                    return title[:_QUESTION_TITLE_LENGTH].rstrip() + "…"
                return title
    return ""


def latest_question_title(state: AgentState) -> str:
    last_user = next((msg for msg in reversed(state.context) if msg.role == "user"), None)
    return question_title([last_user]) if last_user is not None else ""


def completion_status(reply: Msg) -> str | None:
    """Read the final delivery verdict when the analysis middleware supplies it."""
    completion = reply.metadata.get("analysisCompletion")
    return completion.get("status") if isinstance(completion, dict) else None


def parked_session_status(state: AgentState) -> SessionStatus:
    """Include analysis continuations waiting on an offloaded tool."""
    status = SessionService.derive_parked_status(state.context)
    if status == SessionStatus.IDLE and state.middle_context.get(WAITING_FOR_RESULT_KEY):
        return SessionStatus.AWAITING_EXTERNAL_RESULT
    return status


class SessionActivity:
    """Emit changes under the session's run lock to keep their order."""

    def __init__(
        self, bus: MessageBus, user_id: str, agent_id: str, session_id: str,
    ) -> None:
        self._bus = bus
        self._key = MessageBusKeys.session_updates(user_id)
        self._identity = {"agent_id": agent_id, "session_id": session_id}
        self._status: SessionStatus | None = None

    async def __aenter__(self) -> "SessionActivity":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._status == SessionStatus.RUNNING:
            await self.set_status(SessionStatus.IDLE)
        elif self._status is not None:
            # A subscriber can join during cleanup (e.g. auto-naming)
            # while the physical run lock still makes a list read busy.
            await self._publish({"type": "session_status", "status": self._status.value})

    async def set_status(self, status: SessionStatus) -> None:
        """Update the sidebar when execution starts, parks, or stops."""
        if status != self._status:
            self._status = status
            await self._publish({"type": "session_status", "status": status.value})

    async def reply_finished(self, reply: Msg, title: str) -> None:
        """Record a result only after its final message has been persisted."""
        verdict = completion_status(reply)
        if reply.finished_reason != ReplyFinishedReason.COMPLETED or verdict in ("BLOCKED", "INCOMPLETE"):
            await self.set_status(SessionStatus.IDLE)
            return
        parked = SessionService.derive_parked_status([reply])
        if parked != SessionStatus.IDLE:
            await self.set_status(parked)
        elif verdict == "WAITING":
            await self.set_status(SessionStatus.AWAITING_EXTERNAL_RESULT)
        else:
            self._status = SessionStatus.IDLE
            await self._publish({
                "type": "reply_completed", "reply_id": reply.id, "question": title,
            })

    async def _publish(self, event: dict) -> None:
        payload = {**self._identity, **event}
        try:
            if event["type"] == "reply_completed":
                # Reconnecting clients need only the result identity to restore
                # unread dots. The live notice alone needs the question text.
                receipt = {key: value for key, value in payload.items() if key != "question"}
                entry_id = await self._bus.log_append(
                    self._key, receipt,
                    max_len=MessageBusKeys.SESSION_COMPLETION_REPLAY_MAX_LEN,
                )
                payload = {**payload, "_entry_id": entry_id}
            await self._bus.publish(self._key, payload)
        except Exception:  # pylint: disable=broad-except
            logger.warning("Failed to publish session activity", exc_info=True)
