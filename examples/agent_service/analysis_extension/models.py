"""Wire primitives. Exact values are strings; source text is never evaluated."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

SCHEMA_VERSION = 1
ENGINE_VERSION = "1.5.0"
NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


class AnalysisError(ValueError):
    """A bounded, structured diagnostic suitable for a tool result."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), **({"details": self.details} if self.details else {})}


@dataclass(frozen=True)
class Owner:
    user_id: str
    agent_id: str
    session_id: str

    def __post_init__(self):
        if not all(isinstance(x, str) and x and len(x) <= 256 for x in asdict(self).values()):
            raise AnalysisError("INVALID_OWNER", "用户、智能体和会话身份必须完整。")

    @property
    def key(self) -> str:
        return digest(asdict(self))

    def fields(self) -> dict:
        return {"ownerUserId": self.user_id, "agentId": self.agent_id, "sessionId": self.session_id}


@dataclass(frozen=True)
class Limits:
    # Baseline: historical largest complete response is below 1 MiB UTF-8.
    max_receipt_bytes: int = 16 * 1024 * 1024
    max_store_bytes: int = 512 * 1024 * 1024
    retention_seconds: int = 7 * 24 * 3600
    max_facts: int = 50000
    max_selections: int = 1000
    max_operations: int = 100
    page_size: int = 100

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in asdict(self).values()):
            raise AnalysisError("INVALID_LIMIT", "分析容量配置必须是正整数。")
        if self.max_receipt_bytes > self.max_store_bytes:
            raise AnalysisError("INVALID_LIMIT", "单次输入上限不能超过存储配额。")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def exact_loads(text: str) -> Any:
    def reject(token):
        raise AnalysisError("NON_FINITE", "输入包含非有限数值。")
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=reject)
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise AnalysisError("INVALID_JSON", "来源内容不是完整的 JSON。") from exc


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise AnalysisError("PRECISION_UNPROVEN", "该输入不是可证明精度的原始十进制数值。")
    if isinstance(value, (Decimal, int)):
        number = Decimal(value)
    elif isinstance(value, str) and NUMBER.fullmatch(value) and len(value) <= 512:
        number = Decimal(value)
    else:
        raise AnalysisError("NOT_NUMERIC", "该单元格不是无歧义数值。")
    if not number.is_finite() or len(number.as_tuple().digits) > 128 or abs(number.as_tuple().exponent) > 128:
        raise AnalysisError("NUMBER_LIMIT", "数值超出支持的精度或数量级。")
    return number


def decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def precise_sum(values: list[Decimal]) -> Decimal:
    # 128 input digits and exponent ±128; room for 50,000 accumulated cells.
    with localcontext() as context:
        context.prec = 512
        return sum(values, Decimal(0))


def json_values(value: Any) -> Any:
    """Make parsed source metadata serializable without going through float."""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {k: json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_values(v) for v in value]
    return value


def bounded_page(items: list, offset: int, limit: int, maximum: int = 100) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise AnalysisError("INVALID_PAGE", "分页位置或大小超出允许范围。")
    end = min(len(items), offset + limit)
    return {"items": items[offset:end], "offset": offset, "total": len(items), "nextOffset": end if end < len(items) else None}
