"""Session metadata and status without the complete execution context."""
from pydantic import BaseModel
from ._model import SessionRecord
from ...state import ToolContext


class SessionSummary(BaseModel):
    session: SessionRecord
    parked_status: str
    # Older Redis summaries did not include this flag.
    has_background_tools: bool | None = None

    @classmethod
    def from_record(cls, record: SessionRecord):
        # Reuse the execution status rule; derive before dropping the context.
        from .._service._session_activity import parked_session_status
        status = parked_session_status(record.state).value
        state = record.state.model_copy(update={
            'context': [], 'summary': '', 'tool_context': ToolContext(), 'middle_context': {},
        })
        return cls(
            session=record.model_copy(update={'state': state}),
            parked_status=status,
            has_background_tools=bool(
                record.state.middle_context.get('background_tools_v1'),
            ),
        )
