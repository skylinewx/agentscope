"""Nuwa identities are trusted only after a successful server-side exchange."""

from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class NvwaUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64, pattern=r"^[\w.@-]+$")
    display_name: str = Field(default="", max_length=100)
    user_id: str | None = None
    access_token: SecretStr | None = Field(default=None, exclude=True)


class NvwaTicketSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1)
    auth_mode: Literal["auto", "bearer", "ticket_post"] = "auto"
    validate_path: str = "/nvwa-certification/v1/ticket/resolve"
    user_info_path: str = "/nvwa-user/v1/user/current"
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    template_owner_id: str = Field(
        default="admin", min_length=1, max_length=128, pattern=r"^[\w.@-]+$"
    )
    template_agent_id: str | None = Field(default=None, min_length=1)
    # This token stays in the private auth store, never agent/MCP data.
    mcp_token_ttl_seconds: int = Field(default=3600, ge=60, le=43200)
    mcp_authorization_prefix: Literal["", "Bearer "] = ""

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use an HTTP(S) service URL without credentials.")
        return value.rstrip("/")

    @field_validator("validate_path", "user_info_path")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = "/" + value.strip().lstrip("/")
        if any(c in value for c in "\\?#") or any(ord(c) < 33 for c in value):
            raise ValueError("Use a service-relative endpoint path.")
        return value


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    return next(
        (payload[k] for k in keys if payload.get(k) not in (None, "")), None
    )


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    return next(
        (
            payload[k]
            for k in ("datas", "data", "result")
            if isinstance(payload.get(k), dict)
        ),
        payload,
    )


def _parse_user_payload(payload: Any) -> NvwaUser:
    if not isinstance(payload, dict):
        raise ValueError("验票响应必须是 JSON 对象。")
    code = payload.get("code")
    if code is not None and str(code) not in {"0", "0000", "200", "OK", "ok"}:
        raise ValueError("女娲拒绝了登录凭证。")
    body = _unwrap(payload)
    context = body.get("contextUser")
    identity = context if isinstance(context, dict) else body
    username = _pick(identity, "username", "userName", "loginName")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("换票响应缺少 userName。")
    user_id = _pick(identity, "id", "userId", "user_id")
    token = _pick(body, "access_token", "accessToken")
    return NvwaUser(
        username=username.strip(),
        display_name=str(
            _pick(identity, "displayName", "display_name", "name") or ""
        ).strip(),
        user_id=str(user_id) if user_id is not None else None,
        access_token=token if isinstance(token, str) and token else None,
    )


async def resolve_ticket(
    settings: NvwaTicketSettings,
    ticket: str,
    *,
    client: httpx.AsyncClient | None = None,
    prefer: Literal["auto", "bearer", "ticket_post"] | None = None,
) -> NvwaUser:
    """Validate Nuwa's response; upstream errors never authorize a login."""
    if not isinstance(ticket, str) or not ticket.strip():
        raise ValueError("ticket 不能为空。")
    token = ticket.strip()
    mode = (
        (prefer or "ticket_post")
        if settings.auth_mode == "auto"
        else settings.auth_mode
    )
    http = client or httpx.AsyncClient(
        timeout=settings.timeout_seconds, follow_redirects=False
    )
    try:
        if mode == "bearer":
            response = await http.get(
                settings.base_url + settings.user_info_path,
                headers={"Authorization": token},
            )
            if response.status_code in {
                401,
                403,
            } and not token.lower().startswith("bearer "):
                token = "Bearer " + token
                response = await http.get(
                    settings.base_url + settings.user_info_path,
                    headers={"Authorization": token},
                )
        else:
            response = await http.post(
                settings.base_url + settings.validate_path,
                json={"ticket": token},
            )
        if not 200 <= response.status_code < 300:
            raise ValueError(f"验票失败（HTTP {response.status_code}）。")
        try:
            user = _parse_user_payload(response.json())
        except (ValueError, TypeError):
            raise ValueError("女娲未返回有效的已认证用户。") from None
        if mode == "bearer":
            user.access_token = SecretStr(token)
        return user
    except httpx.HTTPError:
        raise ValueError("无法连接女娲认证服务，请稍后重试。") from None
    finally:
        if client is None:
            await http.aclose()
