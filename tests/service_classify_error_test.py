# -*- coding: utf-8 -*-
"""Unit tests for :func:`_classify_error` — the fatal-reply exception
classifier that feeds the UI ``ErrorInfo``.

These lock down the behaviour that regressed silently before: status codes
live in different places across clients (``status_code`` on the exception
for openai/anthropic, ``response.status_code`` for raw httpx, ``status``
for aiohttp), connection failures are provider ``TransportError`` subclasses
rather than builtin ``ConnectionError``, and the real error is often one
``__cause__`` deep.
"""

import unittest

import httpx
from pydantic import ValidationError

from agentscope.app._service._errors import (
    _classify_error,
    _classify_type,
    _classify_setup_error,
    _GENERIC_MESSAGE,
)
from agentscope.credential import DeepSeekCredential
from agentscope.exception import DeveloperOrientedException
from agentscope.types import ErrorType


class _StatusError(Exception):
    """openai/anthropic ``APIStatusError`` shape: status promoted onto the
    exception object."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _AiohttpError(Exception):
    """aiohttp ``ClientResponseError`` shape: uses ``status``, not
    ``status_code``."""

    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status = status


def _httpx_status_error(code: int) -> httpx.HTTPStatusError:
    """A real httpx error, whose code lives on ``response`` — the exact
    shape that used to fall through to INTERNAL."""
    request = httpx.Request("POST", "https://api.example.com/v1/chat")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class ClassifyErrorTest(unittest.TestCase):
    """Tests for the reply-error classifier."""

    def test_status_code_mapping(self) -> None:
        """HTTP status maps to the right ErrorType from every source
        (exception ``status_code``, httpx ``response``, aiohttp
        ``status``)."""
        cases = [
            (401, ErrorType.AUTHENTICATION),
            (403, ErrorType.PERMISSION),
            (429, ErrorType.RATE_LIMIT),
            (400, ErrorType.INVALID_REQUEST),
            (404, ErrorType.INVALID_REQUEST),
            (422, ErrorType.INVALID_REQUEST),
            (418, ErrorType.INVALID_REQUEST),  # unmapped 4xx
            (500, ErrorType.UPSTREAM),
            (503, ErrorType.UPSTREAM),
        ]
        for code, expected in cases:
            with self.subTest(source="status_code", code=code):
                self.assertEqual(_classify_type(_StatusError(code)), expected)
            with self.subTest(source="httpx.response", code=code):
                self.assertEqual(
                    _classify_type(_httpx_status_error(code)),
                    expected,
                )
            with self.subTest(source="aiohttp.status", code=code):
                self.assertEqual(
                    _classify_type(_AiohttpError(code)),
                    expected,
                )

    def test_httpx_connection_errors(self) -> None:
        """httpx transport failures classify as CONNECTION."""
        # These are httpx.TransportError subclasses, NOT builtin
        # ConnectionError/TimeoutError — the branch that used to be dead.
        for exc in (
            httpx.ConnectError("refused"),
            httpx.ConnectTimeout("slow"),
            httpx.ReadTimeout("slow"),
            httpx.PoolTimeout("slow"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(_classify_type(exc), ErrorType.CONNECTION)

    def test_builtin_network_errors(self) -> None:
        """Builtin TimeoutError / ConnectionError classify as CONNECTION."""
        self.assertEqual(_classify_type(TimeoutError()), ErrorType.CONNECTION)
        self.assertEqual(
            _classify_type(ConnectionError()),
            ErrorType.CONNECTION,
        )

    def test_cause_chain_is_walked(self) -> None:
        """Status is found on an explicitly chained ``__cause__``."""
        # AgentScope often wraps a provider error one layer deep.
        try:
            try:
                raise _StatusError(401)
            except _StatusError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            self.assertEqual(
                _classify_type(outer),
                ErrorType.AUTHENTICATION,
            )

    def test_context_chain_is_walked(self) -> None:
        # pylint: disable=raise-missing-from
        """Status is found on an implicitly chained ``__context__``."""
        # Intentional implicit chaining (no ``from``) populates __context__.
        try:
            try:
                raise httpx.ConnectError("refused")
            except httpx.ConnectError:
                raise RuntimeError("wrapped")  # noqa: B904
        except RuntimeError as outer:
            self.assertEqual(_classify_type(outer), ErrorType.CONNECTION)

    def test_framework_vs_unknown(self) -> None:
        """Framework errors are INTERNAL; anything else is UNKNOWN."""
        self.assertEqual(
            _classify_type(DeveloperOrientedException("x")),
            ErrorType.INTERNAL,
        )
        self.assertEqual(_classify_type(ValueError("x")), ErrorType.UNKNOWN)

    def test_invalid_api_key_is_authentication_failure(self) -> None:
        """A stored malformed key is actionable even through wrapped errors."""
        try:
            DeepSeekCredential(api_key="sk-private-test-key 中文备注")
        except ValidationError as exc:
            wrapped = RuntimeError("wrapped")
            wrapped.__cause__ = exc
            for error in (exc, wrapped, ExceptionGroup("group", [exc])):
                with self.subTest(error=type(error).__name__):
                    info = _classify_error(error)
                    self.assertEqual(info.type, ErrorType.AUTHENTICATION)
                    self.assertNotIn("sk-private-test-key", info.message)
                    self.assertEqual(
                        _classify_setup_error(error).type,
                        ErrorType.AUTHENTICATION,
                    )
        else:
            self.fail("Malformed API key was accepted")

    def test_legacy_bearer_header_encoding_failure(self) -> None:
        """The original HTTP-header error also points to the credential."""
        with self.assertRaises(UnicodeEncodeError) as caught:
            httpx.Headers({"Authorization": "Bearer sk-test-key 中文备注"})
        self.assertEqual(
            _classify_type(caught.exception),
            ErrorType.AUTHENTICATION,
        )
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = caught.exception
        self.assertEqual(_classify_type(wrapped), ErrorType.AUTHENTICATION)

    def test_unrelated_encoding_error_remains_unknown(self) -> None:
        """Encoding errors outside authentication keep their classification."""
        with self.assertRaises(UnicodeEncodeError) as caught:
            "中文输出".encode("ascii")
        self.assertEqual(_classify_type(caught.exception), ErrorType.UNKNOWN)

    def test_classify_error_returns_generic_message(self) -> None:
        """The ErrorInfo carries the generic per-type message, never the
        raw exception text."""
        info = _classify_error(_StatusError(403))
        self.assertEqual(info.type, ErrorType.PERMISSION)
        self.assertEqual(info.message, _GENERIC_MESSAGE[ErrorType.PERMISSION])
        # Never leak the raw exception text to the UI.
        self.assertNotIn("status 403", info.message)

    def test_every_error_type_has_a_message(self) -> None:
        """Every ErrorType has a generic message entry."""
        for error_type in ErrorType:
            self.assertIn(error_type, _GENERIC_MESSAGE)

    def test_context_overflow_is_specific_and_sanitized(self) -> None:
        """The observed DeepSeek 400 must not lose its cause or leak details."""
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://api.example.com/v1/chat"),
            json={
                "error": {
                    "message": (
                        "This model's maximum context length is 1048576 tokens. "
                        "However, you requested 1051259 tokens. private-request-id"
                    ),
                    "code": "invalid_request_error",
                }
            },
        )
        error = httpx.HTTPStatusError(
            "Bad Request",
            request=response.request,
            response=response,
        )
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = error
        for exc in (error, wrapped, ExceptionGroup("group", [error])):
            info = _classify_error(exc)
            self.assertEqual(info.type, ErrorType.CONTEXT_LENGTH)
            self.assertNotIn("private-request-id", info.message)
            self.assertNotIn("1051259", info.message)

    def test_context_code_and_long_prompt_message(self) -> None:
        """Recognize both structured codes and provider error messages."""
        for detail in (
            {"code": "context_length_exceeded", "message": "limit reached"},
            {"message": "prompt is too long: 1001 tokens > 1000 maximum"},
        ):
            error = _StatusError(400)
            error.body = {"error": detail}
            self.assertEqual(_classify_type(error), ErrorType.CONTEXT_LENGTH)

    def test_ordinary_bad_requests_and_auth_keep_their_meaning(self) -> None:
        """Overflow recognition must not mask other request failures."""
        error = _StatusError(400)
        error.body = {"error": {"message": "Invalid function parameters"}}
        self.assertEqual(_classify_type(error), ErrorType.INVALID_REQUEST)
        error.status_code = 401
        error.body = {"error": {"code": "context_length_exceeded"}}
        self.assertEqual(_classify_type(error), ErrorType.AUTHENTICATION)


if __name__ == "__main__":
    unittest.main()
