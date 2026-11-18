# -*- coding: utf-8 -*-
"""Resolve a Nuwa URL App ticket into a portal user.

URL App appends a one-time ``ticket`` to the embedded page. ``auto`` mode
sends that value in the ``authorization-ticket`` header to
``GET {base_url}/nvwa/getLoginContext``. The username is
``context.contextUser.name``. ``bearer`` mode is only for an existing Nuwa
session token. Do not send a URL App ticket to ``/nvwa-user/v1/user/current``.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class NvwaUser(BaseModel):
    """Identity returned after a successful ticket/token exchange."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    display_name: str = ""
    user_id: str | None = None


class NvwaTicketSettings(BaseModel):
    """HTTP settings for resolving a Nuwa URL-App ticket / session token."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1)
    # auto: URL App ticket → authorization-ticket on user_info_path.
    # bearer: existing Nuwa session token. ticket_post: POST validate_path.
    auth_mode: Literal["auto", "bearer", "ticket_post"] = "auto"
    validate_path: str = "/nvwa-certification/v1/ticket/resolve"
    user_info_path: str = "/nvwa/getLoginContext"
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("validate_path", "user_info_path")
    @classmethod
    def ensure_leading_slash(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            value = "/" + value
        return value


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None and payload[key] != "":
            return payload[key]
    return None


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Nuwa APIs often wrap the body as {code, datas|data, message}."""
    for key in ("datas", "data", "result"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
    return payload


def _map_user(payload: dict[str, Any]) -> NvwaUser:
    body = _unwrap(payload)
    # GET /nvwa/getLoginContext returns {context: {id, contextUser: {name, fullname}}}.
    login_context = body.get("context")
    if isinstance(login_context, dict):
        body = login_context
    context = body.get("contextUser") or body.get("conetxtUser")
    if isinstance(context, dict):
        username = _pick(context, "username", "userName", "name") or _pick(
            body, "username", "userName", "name", "loginName"
        )
        display = _pick(context, "fullname", "fullName", "displayName", "display_name", "name") or _pick(
            body, "fullname", "fullName", "displayName", "display_name", "name"
        )
        user_id = _pick(context, "id", "userId", "user_id") or _pick(
            body, "id", "userId", "user_id"
        )
    else:
        username = _pick(body, "username", "userName", "name", "loginName")
        display = _pick(body, "displayName", "display_name", "name")
        user_id = _pick(body, "id", "userId", "user_id")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("换票响应缺少 username。")
    return NvwaUser(
        username=username.strip(),
        display_name=str(display).strip() if display is not None else "",
        user_id=str(user_id) if user_id is not None else None,
    )


def _error_detail(response: httpx.Response) -> str:
    snippet = (response.text or "").strip().replace("\n", " ")
    if len(snippet) > 180:
        snippet = snippet[:180] + "…"
    if snippet:
        return f"验票失败（HTTP {response.status_code}）：{snippet}"
    return f"验票失败（HTTP {response.status_code}）。"


async def _bearer_user_info(
    http: httpx.AsyncClient,
    settings: NvwaTicketSettings,
    token: str,
) -> httpx.Response:
    url = f"{settings.base_url}{settings.user_info_path}"
    # 女娲默认把会话令牌放在查询参数 JTOKENID，请求头名是 Authorization。
    response = await http.get(
        url, params={"JTOKENID": token}, headers={"Authorization": token}
    )
    if response.status_code in {401, 403}:
        response = await http.get(url, headers={"Authorization": token})
    return response


async def _ticket_post(
    http: httpx.AsyncClient,
    settings: NvwaTicketSettings,
    ticket: str,
) -> httpx.Response:
    return await http.post(
        f"{settings.base_url}{settings.validate_path}",
        json={"ticket": ticket},
    )


def _parse_user_payload(payload: dict[str, Any]) -> NvwaUser:
    code = payload.get("code")
    if code is not None and str(code) not in {"0", "0000", "200", "OK", "ok"}:
        msg = payload.get("message") or payload.get("msg") or str(code)
        raise ValueError(f"验票失败（业务码 {code}）：{msg}")
    return _map_user(payload)


async def resolve_ticket(
    settings: NvwaTicketSettings,
    ticket: str,
    *,
    client: httpx.AsyncClient | None = None,
    prefer: Literal["auto", "bearer", "ticket_post"] | None = None,
) -> NvwaUser:
    """Resolve a Nuwa credential into a portal user.

    Args:
        settings: Base URL and which Nuwa endpoint to call.
        ticket: URL App ticket, or a Nuwa session token in bearer mode.
        client: Optional HTTP client. A client is created when omitted.
        prefer: Overrides ``settings.auth_mode`` for this call.

    Returns:
        The portal user from ``context.contextUser``.

    Raises:
        ValueError: The credential is empty, Nuwa rejects it, or the
            response has no username.
    """
    if not isinstance(ticket, str) or not ticket.strip():
        raise ValueError("ticket 不能为空。")
    token = ticket.strip()
    mode = prefer or settings.auth_mode

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
    try:
        if mode == "bearer":
            response = await _bearer_user_info(http, settings, token)
        elif mode == "ticket_post":
            response = await _ticket_post(http, settings, token)
        else:
            # URL App 的 ticket 放在请求头或参数 authorization-ticket。401 不再改走会话令牌。
            response = await http.get(
                f"{settings.base_url}{settings.user_info_path}",
                params={"authorization-ticket": token},
                headers={"authorization-ticket": token},
            )
            if response.status_code in {404, 405}:
                response = await _bearer_user_info(http, settings, token)
    finally:
        if owns_client:
            await http.aclose()

    if response.status_code >= 400:
        raise ValueError(_error_detail(response))

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("验票响应不是合法 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("验票响应必须是 JSON 对象。")
    return _parse_user_payload(payload)

