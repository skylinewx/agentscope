# -*- coding: utf-8 -*-
"""Resolve user MCP environment references only at runtime."""

import os
import re
from typing import Any

_ENV_REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_environment(value: Any) -> Any:
    """Resolve explicit environment references only in runtime configs."""
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(
                    f"Required environment variable {name} is unset."
                )
            return os.environ[name]

        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [resolve_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_environment(item) for key, item in value.items()}
    return value
