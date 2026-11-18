"""Owner-scoped queue API. No pending input is inserted into chat history."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ...message import Msg
from ..deps import get_current_user_id
from ..storage._model._message_queue import MessageQueueRecord, QueuedMessage

message_queue_router = APIRouter(prefix="/sessions", tags=["sessions"])


class QueueSubmission(BaseModel):
    message: Msg


class QueueAction(BaseModel):
    operation: Literal[
        "pause",
        "resume",
        "begin_edit",
        "save",
        "cancel_edit",
        "delete",
        "restore",
    ]
    item_id: str | None = None
    version: int | None = None
    message: Msg | None = None


class QueueView(BaseModel):
    revision: int
    items: list[QueuedMessage]
    active_id: str | None
    activity: str
    waiting_reason: str | None
    paused: bool
    pause_reason: str | None
    editing_id: str | None

    @classmethod
    def of(cls, record: MessageQueueRecord):
        return cls.model_validate(record.model_dump())


@message_queue_router.get("/{session_id}/queue", response_model=QueueView)
async def get_queue(
    session_id: str,
    request: Request,
    agent_id: str = Query(),
    user_id: str = Depends(get_current_user_id),
):
    return QueueView.of(
        await request.app.state.message_queue_service.read(
            user_id, agent_id, session_id
        )
    )


@message_queue_router.post("/{session_id}/queue", response_model=QueueView)
async def enqueue(
    session_id: str,
    body: QueueSubmission,
    request: Request,
    agent_id: str = Query(),
    user_id: str = Depends(get_current_user_id),
):
    service = request.app.state.message_queue_service
    # The composer can be one event behind. If execution just finished,
    # admit directly rather than briefly exposing an unnecessary queue row.
    await service.submit(user_id, agent_id, session_id, body.message)
    return QueueView.of(await service.read(user_id, agent_id, session_id))


@message_queue_router.post(
    "/{session_id}/queue/actions", response_model=QueueView
)
async def change_queue(
    session_id: str,
    body: QueueAction,
    request: Request,
    agent_id: str = Query(),
    user_id: str = Depends(get_current_user_id),
):
    return QueueView.of(
        await request.app.state.message_queue_service.action(
            user_id,
            agent_id,
            session_id,
            **body.model_dump(exclude={"message"}),
            message=body.message,
        )
    )
