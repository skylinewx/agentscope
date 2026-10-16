# -*- coding: utf-8 -*-
"""Editable MCP configurations that retain write-only credentials."""

import re
from typing import Any
from urllib.parse import urlsplit

from ..storage import MCPRecord

KEEP_SECRET = "[KEEP_EXISTING_SECRET]"
_SECRET_FLAG = re.compile(
    r"(?:key|token|secret|password|authorization|credential|header)", re.I
)


def editable_mcp(record: MCPRecord) -> dict:
    """Expose configuration structure without stored secret values."""
    data = record.client.model_dump(mode="json")
    config = data["mcp_config"]
    secrets: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str) and value:
            secrets.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(record.values)
    for key in ("env", "headers"):
        values = config.get(key) or {}
        collect(values)
        if key in config:
            config[key] = {name: KEEP_SECRET for name in values} or None
    url = config.get("url")
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.query or parsed.username or parsed.fragment:
                config["url"] = KEEP_SECRET
        except ValueError:
            config["url"] = KEEP_SECRET
    args = config.get("args") or []
    secret_next = False
    for index, argument in enumerate(args):
        if secret_next:
            args[index] = KEEP_SECRET
            secret_next = False
        elif argument.startswith("-") and _SECRET_FLAG.search(argument):
            if "=" in argument:
                args[index] = KEEP_SECRET
            else:
                secret_next = True

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            if any(secret in value for secret in secrets):
                return KEEP_SECRET
        elif isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [redact(item) for item in value]
        return value

    data["mcp_config"] = {
        key: value if key == "type" else redact(value)
        for key, value in config.items()
    }
    return data


def restore_mcp_secrets(record: MCPRecord, replacement: dict) -> dict:
    """Retain placeholders only at their original, redacted positions."""
    original = record.client.model_dump(mode="json")
    visible = editable_mcp(record)

    def restore(value: Any, previous: Any, shown: Any) -> Any:
        if isinstance(value, str) and value == KEEP_SECRET:
            if shown != KEEP_SECRET or not isinstance(previous, str):
                raise ValueError("A secret placeholder has no saved value.")
            return previous
        if isinstance(value, dict):
            old = previous if isinstance(previous, dict) else {}
            mask = shown if isinstance(shown, dict) else {}
            return {
                key: restore(item, old.get(key), mask.get(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            old_list = previous if isinstance(previous, list) else []
            mask_list = shown if isinstance(shown, list) else []
            return [
                restore(
                    item,
                    old_list[index] if index < len(old_list) else None,
                    mask_list[index] if index < len(mask_list) else None,
                )
                for index, item in enumerate(value)
            ]
        return value

    return restore(replacement, original, visible)
