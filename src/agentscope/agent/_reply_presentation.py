# -*- coding: utf-8 -*-
"""Identify final output using the events that actually reach the client."""

from ..event import (
    AgentEvent,
    DataBlockStartEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    TextBlockStartEvent,
    ToolCallStartEvent,
)
from ..types import ReplyFinishedReason


class ReplyPresentationTracker:
    """Track one model invocation at a time, after reply middleware.

    Model block IDs differ from their streamed IDs. Recording emitted start
    events also handles middleware that discards a candidate answer and asks
    the agent to continue. Only an accepted reply end classifies the output.
    """

    def __init__(self) -> None:
        self.output_ids: list[str] = []
        self.has_tool_calls = False

    def apply(self, event: AgentEvent) -> None:
        """Attach the final block selection to a successful terminal event."""
        if isinstance(event, ModelCallStartEvent):
            self.output_ids = []
            self.has_tool_calls = False
        elif isinstance(event, (TextBlockStartEvent, DataBlockStartEvent)):
            if event.block_id not in self.output_ids:
                self.output_ids.append(event.block_id)
        elif isinstance(event, ToolCallStartEvent):
            self.has_tool_calls = True
        elif isinstance(event, ReplyEndEvent) and event.finished_reason in (
            ReplyFinishedReason.COMPLETED,
            ReplyFinishedReason.EXCEED_MAX_ITERS,
        ):
            # A tool-only structured-output reply legitimately has no text.
            # Errors/interruptions deliberately keep their partial output.
            event.metadata.setdefault(
                "presentation",
                {
                    "answer_block_ids": (
                        [] if self.has_tool_calls else list(self.output_ids)
                    ),
                },
            )
