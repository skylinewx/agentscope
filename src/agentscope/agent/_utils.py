# -*- coding: utf-8 -*-
"""The utility classes used in building the agent class."""
from dataclasses import dataclass
import json
from typing import Literal
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel

from ..tool import ToolChoice
from ..message import ToolCallBlock, HintBlock, Msg
from ..event import AgentEvent
from .._logging import logger


def _tool_validation_message(error: ValidationError) -> str:
    """Explain schema errors without sending a whole script or table back to the model."""
    rule = error.validator
    hint = ""
    if rule in ("oneOf", "anyOf"):
        quantity = "exactly one" if rule == "oneOf" else "at least one"
        options = []
        for branch in error.validator_value[:6]:
            label = []
            if isinstance(branch, dict):
                if branch.get("title"):
                    label.append(branch["title"])
                if branch.get("required"):
                    label.append("required fields " + ", ".join(branch["required"]))
                elif branch.get("type"):
                    label.append("type " + str(branch["type"]))
            options.append(" / ".join(label)[:180] or "see schema")
        if len(error.validator_value) > len(options):
            options.append("see remaining alternatives in the schema")
        message = f"Match {quantity} of the allowed forms: " + " | ".join(options)
    elif len(error.message) > 768:
        # Some validators embed the complete instance, including for `not` and `type`.
        constraint = error.validator_value
        compact = isinstance(constraint, (int, float, str)) and len(str(constraint)) <= 80
        detail = f" ({constraint})" if compact else ""
        message = f"Value violates schema rule {rule}{detail}. Check this field in the tool schema"
    else:
        message = error.message
    if rule == "enum" and isinstance(error.instance, str):
        matches = [
            value for value in error.validator_value
            if isinstance(value, str) and value.casefold() == error.instance.casefold()
        ]
        if len(matches) == 1 and len(matches[0]) <= 120:
            replacement = json.dumps(matches[0], ensure_ascii=False)
            hint = f". Retry with {error.json_path} set to {replacement}. Keep the other arguments."
    elif rule == "additionalProperties":
        hint = ". Remove the unexpected fields at this path and keep the remaining arguments."
    return f"{message} (at {error.json_path}){hint}"


@dataclass
class _ToolCallBatch:
    """A batch of tool calls that execute either sequentially or
    concurrently."""

    type: Literal["sequential", "concurrent"]
    """The batch type"""
    tool_calls: list[ToolCallBlock]
    """The list of tool calls in the batch."""


class Acting(BaseModel):
    """Next action: execute the given tool calls."""

    tool_calls: list[ToolCallBlock]


class Reasoning(BaseModel):
    """Next action: another model call, with optional hint and tool choice."""

    hint: HintBlock | None = None
    tool_choice: ToolChoice | None = None


class Exit(BaseModel):
    """Next action: end the reply."""

    exit_msg: Msg
    exit_events: list[AgentEvent] | None = None


def _resolve_timezone(name: str) -> tzinfo:
    """Resolve the given timezone name, falling back to UTC when the name is
    invalid or the timezone database is unavailable, e.g. on Windows or slim
    images without the ``tzdata`` package."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "Failed to resolve the timezone %s, fallback to UTC. Install the "
            "'tzdata' package if the timezone database is missing.",
            repr(name),
        )
        return timezone.utc
