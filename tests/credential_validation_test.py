# -*- coding: utf-8 -*-
# pylint: disable=consider-using-with
"""Malformed API keys are rejected before storage or model requests."""
from unittest import TestCase
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from agentscope.app._router._credential import credential_router
from agentscope.app.deps import get_resource_access_service, get_storage
from agentscope.app.storage import CredentialRecord
from agentscope.credential import CredentialFactory, DeepSeekCredential


HEADERS = {"X-User-ID": "alice"}


class CredentialValidationTest(TestCase):
    """Validate API keys while preserving credential display fields."""

    def test_all_api_key_providers_trim_pasted_whitespace(self) -> None:
        """All built-in providers normalize boundary whitespace only."""
        for schema in CredentialFactory.list_schemas():
            if "api_key" not in schema["properties"]:
                continue
            provider = schema["properties"]["type"]["const"]
            with self.subTest(provider=provider):
                credential = CredentialFactory.from_dict(
                    {
                        "type": provider,
                        "api_key": " \t sk-test-key\r\n",
                        "name": "测试凭证",
                    },
                )
                self.assertEqual(
                    credential.api_key.get_secret_value(),
                    "sk-test-key",
                )
                self.assertEqual(credential.name, "测试凭证")

    def test_invalid_key_formats_are_rejected(self) -> None:
        """Notes, embedded whitespace, controls and empty keys fail early."""
        for key in (
            "",
            " \t\r\n",
            "sk-test-key 中文备注",
            "sk-test-key ascii-note",
            "sk-test\nkey",
            "sk-test\tkey",
            "sk-test\x00key",
            "sk-test\x7fkey",
            "sk-test\u200bkey",
        ):
            for value in (key, SecretStr(key)):
                with self.subTest(
                    key=repr(key),
                    secret=isinstance(value, SecretStr),
                ):
                    with self.assertRaises(ValidationError) as caught:
                        DeepSeekCredential(api_key=value)
                    errors = caught.exception.errors(include_input=False)
                    self.assertEqual(errors[0]["type"], "invalid_api_key")

    def test_validation_exception_hides_secret_input(self) -> None:
        """Logging the exception must not disclose the rejected key."""
        key = "sk-private-test-key 中文备注"
        with self.assertRaises(ValidationError) as caught:
            DeepSeekCredential(api_key=key)
        self.assertNotIn("sk-private-test-key", str(caught.exception))

    def test_connection_only_credential_still_loads(self) -> None:
        """Credentials without an API key do not acquire a required field."""
        credential = CredentialFactory.from_dict(
            {"type": "ollama_credential", "name": "本地模型"},
        )
        self.assertEqual(credential.name, "本地模型")


class CredentialRouterValidationTest(TestCase):
    """Creation and updates return safe 422 errors without storing bad keys."""

    def setUp(self) -> None:
        """Mount the credential routes with isolated storage dependencies."""
        self.storage = AsyncMock()
        self.storage.upsert_credential.return_value = "credential-1"
        self.access = AsyncMock()
        record = CredentialRecord(
            id="credential-1",
            user_id="alice",
            data={"type": "deepseek_credential", "api_key": "original-key"},
        )
        self.access.resolve_for_edit.return_value = ("alice", record)
        app = FastAPI()
        app.include_router(credential_router)
        app.dependency_overrides[get_storage] = lambda: self.storage
        app.dependency_overrides[get_resource_access_service] = (
            lambda: self.access
        )
        self.client = self.enterContext(TestClient(app))

    def test_invalid_mutations_do_not_store_or_expose_key(self) -> None:
        """The original failure becomes actionable validation, never a 500."""
        key = "sk-private-test-key 中文备注"
        payload = {"data": {"type": "deepseek_credential", "api_key": key}}
        for method, path in (
            ("POST", "/credential/"),
            ("PATCH", "/credential/credential-1"),
        ):
            with self.subTest(method=method):
                response = self.client.request(
                    method,
                    path,
                    headers=HEADERS,
                    json=payload,
                )
                self.assertEqual(response.status_code, 422)
                detail = response.json()["detail"][0]
                self.assertEqual(detail["type"], "invalid_api_key")
                self.assertEqual(detail["loc"][-1], "api_key")
                self.assertIn("without notes", detail["msg"])
                self.assertNotIn("input", detail)
                self.assertNotIn("sk-private-test-key", response.text)
                self.storage.upsert_credential.assert_not_awaited()

    def test_valid_key_is_normalized_before_storage(self) -> None:
        """Pasted boundary whitespace is removed on the API path as well."""
        response = self.client.post(
            "/credential/",
            headers=HEADERS,
            json={
                "data": {
                    "type": "deepseek_credential",
                    "api_key": " sk-test-key\n",
                    "name": "测试凭证",
                },
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"credential_id": "credential-1"})
        credential = self.storage.upsert_credential.await_args.args[1]
        self.assertEqual(credential.api_key.get_secret_value(), "sk-test-key")
        self.assertEqual(credential.name, "测试凭证")

    def test_invalid_schema_has_no_secret_input(self) -> None:
        """Errors for non-key fields also omit the complete credential dict."""
        response = self.client.post(
            "/credential/",
            headers=HEADERS,
            json={
                "data": {
                    "type": "missing",
                    "api_key": "sk-private-test-key",
                },
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sk-private-test-key", response.text)
        self.storage.upsert_credential.assert_not_awaited()
