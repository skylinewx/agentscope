"""Opt-in, paired payload logs for service debugging."""

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel

from ._logging import logger

_context = ContextVar("agentscope_io_context", default={})
_model_call = ContextVar("agentscope_model_io", default=None)
_logger = logger.getChild("io")
_secret = re.compile(
    r"(?:api[_-]?key|password|passwd|secret|(?:access|refresh|auth|csrf|bearer)[_-]?token|"
    r"private[_-]?key|authorization|cookie|x-amz-signature)$",
    re.I,
)
_inline_secret = re.compile(
    r"""(?i)(\bBearer\s+)[\w.+/=_~-]+|"""
    r"""(\b(?:api[_-]?key|password|passwd|secret|authorization|(?:access[_-]?)?token)["']?\s*[=:]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|(?:Basic\s+|Bearer\s+)?[^\s&;,}]+)""",
)


def _visible(value):
    """Build a log-only copy. Never stringify arbitrary clients or credentials."""
    if isinstance(value, BaseModel):
        value = value.model_dump(by_alias=True, exclude_none=True)
    elif is_dataclass(value) and not isinstance(value, type):
        value = {
            field.name: getattr(value, field.name) for field in fields(value)
        }
    if isinstance(value, Mapping):
        kind = value.get("type")
        if (
            kind in ("thinking", "redacted_thinking", "reasoning")
            or value.get("thought") is True
        ):
            return {"type": kind or "thought", "content": "[omitted]"}
        result = {}
        for key, item in value.items():
            key = str(key)
            if _secret.search(key) or key.lower() in {
                "token",
                "extra_headers",
                "env",
                "credentials",
                "reasoning_details",
                "reasoning_content",
                "thought_signature",
                "thoughtsignature",
                "encrypted_content",
            }:
                result[key] = "[redacted]"
            elif key.lower() == "headers" and isinstance(item, Mapping):
                # Budget cells also have a 'headers' list of business labels.
                result[key] = {str(header): "[redacted]" for header in item}
            elif key in ("reasoning", "thinking") and isinstance(item, str):
                result[key] = "[omitted]"
            elif (
                key == "images"
                and isinstance(item, list)
                and all(isinstance(v, (str, bytes)) for v in item)
            ):
                result[key] = ["[image omitted]"] * len(item)
            elif key in ("blob", "b64_json", "file_data") or (
                key == "data"
                and isinstance(item, (str, bytes))
                and (
                    kind in ("base64", "image", "audio", "input_audio")
                    or "mimeType" in value
                    or "mime_type" in value
                    or value.get("format") in ("wav", "mp3", "pcm16")
                )
            ):
                result[key] = "[binary omitted]"
            else:
                result[key] = _visible(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_visible(item) for item in value]
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:160]:
            return "[binary omitted]"
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except ValueError:
                pass  # Ordinary text or an incomplete tool argument.
            else:
                visible = _visible(decoded)
                if visible != decoded:
                    return json.dumps(visible, ensure_ascii=False)
                return value
        return _inline_secret.sub(
            lambda m: (m[1] or m[2]) + "[redacted]", value
        )
    if value is None or isinstance(value, (float, int, bool)):
        return value
    if isinstance(value, (date, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return f"[{len(value)} bytes omitted]"
    # The xAI SDK uses protobuf messages rather than Pydantic models.
    if hasattr(value, "DESCRIPTOR") and hasattr(value, "ListFields"):
        from google.protobuf.json_format import MessageToDict

        return _visible(MessageToDict(value, preserving_proto_field_name=True))
    return f"[{type(value).__name__}]"


@contextmanager
def io_context(**identifiers):
    token = _context.set({**_context.get(), **identifiers})
    try:
        yield
    finally:
        _context.reset(token)


def setup_io_file(directory: Path) -> None:
    """Save the example service's opt-in records as UTF-8 JSON Lines."""
    if os.getenv("AGENTSCOPE_LOG_IO") != "1":
        return
    path = directory / f"calls-{os.getpid()}.jsonl"
    if any(
        getattr(handler, "baseFilename", None) == str(path)
        for handler in _logger.handlers
    ):
        return  # Uvicorn can import the example entry point twice.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=32 * 1024**2,
            backupCount=2,
            encoding="utf-8",
            errors="backslashreplace",
        )
    except OSError as error:
        logger.warning(
            "CALL_IO file unavailable (%s), using console",
            type(error).__name__,
        )
        return
    _logger.addHandler(handler)
    logger.info("CALL_IO file: %s", path)


async def tool_chunks(stream, call_id):
    """Restore the caller's context before yielding each tool chunk."""
    try:
        while True:
            with io_context(toolCallId=call_id):
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    return
            yield chunk
    finally:
        with io_context(toolCallId=call_id):
            await stream.aclose()


class CallIO:
    def __init__(self, kind, name, request, *, call_id=None, **identifiers):
        self.enabled = os.getenv(
            "AGENTSCOPE_LOG_IO"
        ) == "1" and _logger.isEnabledFor(logging.INFO)
        self.sent = self.finished = False
        self.model_result = None
        if self.enabled:
            self.request = request
            self.fields = {
                **_context.get(),
                **identifiers,
                "kind": kind,
                "name": name,
                "callId": call_id or uuid4().hex,
            }
            self.started = perf_counter()

    def _write(self, direction, payload, **extra):
        record = {
            "event": "CALL_IO",
            "time": datetime.now(timezone.utc).isoformat(),
            **self.fields,
            "direction": direction,
            **extra,
        }
        try:
            record["payload"] = _visible(payload)
            message = json.dumps(record, ensure_ascii=False)
        except Exception as exc:  # Diagnostics must not affect execution.
            record["payload"] = {"unavailable": type(exc).__name__}
            message = json.dumps(record, ensure_ascii=False)
        _logger.info("%s", message)

    def input(self, payload=None, **extra):
        if self.enabled and not self.sent:
            self.sent = True
            self._write(
                "input", self.request if payload is None else payload, **extra
            )
            self.request = None

    def output(self, payload, status="ok", *, resumed=False):
        if self.enabled and not self.finished:
            extra = {}
            if not resumed:
                self.input()
                extra["elapsedMs"] = round(
                    (perf_counter() - self.started) * 1000
                )
            self.finished = True
            self._write("output", payload, status=status, **extra)

    def failed(self, error, partial=None):
        if not self.enabled or self.finished:
            return
        status = (
            "interrupted"
            if isinstance(error, (asyncio.CancelledError, GeneratorExit))
            else "error"
        )
        payload = {"exception": type(error).__name__}
        if self.model_result is not None:
            payload["response"] = self.model_result
        if partial is not None:
            try:
                payload["partial"] = (
                    partial() if callable(partial) else partial
                )
            except Exception as exc:
                payload["partial"] = {"unavailable": type(exc).__name__}
        self.output(payload, status)

    def __enter__(self):
        self.input()
        return self

    def __exit__(self, exc_type, error, traceback):
        if error is not None:
            self.failed(error)

    @contextmanager
    def model_attempt(self):
        """Bind only during dispatch, never across a yield to a stream consumer."""
        token = _model_call.set(self)
        try:
            yield
        except BaseException as exc:
            self.input(format="agentscope")
            self.failed(exc)
            raise
        finally:
            self.input(format="agentscope")  # Fallback for custom adapters.
            _model_call.reset(token)


def model_request(payload):
    """Record the final SDK arguments after provider-specific formatting."""
    call = _model_call.get()
    if call is not None:
        call.input(payload, format="provider")


def model_response(payload):
    """Retain the actual structured completion until validation succeeds or fails."""
    call = _model_call.get()
    if call is not None and call.enabled:
        call.model_result = payload
