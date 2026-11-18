"""Durable user-message queue, independent of the agent's session snapshot."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ._base import _RecordBase
from ....message import Msg


class QueuedMessage(BaseModel):
    """One pending input. Its order and original enqueue time survive edits."""

    message: Msg
    sequence: int
    version: int = 0
    enqueued_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    deleted_at: datetime | None = None


class MessageQueueRecord(_RecordBase):
    """The id is the session id; mutations use storage compare-and-swap."""

    user_id: str
    agent_id: str
    revision: int = 0
    next_sequence: int = 0
    items: list[QueuedMessage] = Field(default_factory=list)
    # Short-lived tombstones retain sequence numbers for undo.
    deleted: list[QueuedMessage] = Field(default_factory=list)
    # Submission receipts also cover failures before user history was written.
    receipts: list[str] = Field(default_factory=list)
    active_id: str | None = None
    active_item: QueuedMessage | None = None
    activity: Literal["idle", "running", "waiting"] = "idle"
    waiting_reason: Literal["permission", "external", "background"] | None = (
        None
    )
    worker_id: str | None = None
    discard_background: bool = False
    paused: bool = False
    pause_reason: (
        Literal["manual", "stopped", "error", "interrupted"] | None
    ) = None
    editing_id: str | None = None
