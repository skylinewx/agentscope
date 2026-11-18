"""Sidebar projections are separate from the complete execution records."""
from pydantic import BaseModel
from ._model import SessionRecord
from ...state import ToolContext


class SessionSummary(BaseModel):
    session: SessionRecord
    parked_status: str

    @classmethod
    def from_record(cls, record: SessionRecord):
        # Reuse the execution status rule; derive before dropping the context.
        from .._service._session import SessionService
        status = SessionService.derive_parked_status(record.state.context).value
        state = record.state.model_copy(update={
            'context': [], 'summary': '', 'tool_context': ToolContext(), 'middle_context': {},
        })
        return cls(session=record.model_copy(update={'state': state}), parked_status=status)
