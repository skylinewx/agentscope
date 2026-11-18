"""Authentication boundary and user-management HTTP endpoints."""

import hmac
from typing import Annotated, Literal, Any
from starlette.types import ASGIApp, Scope, Receive, Send, Message

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, RedirectResponse

from ._branding import BrandingSettings
from ._service import public_user, AuthService
from ..._logging import logger

Username = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[\w.@-]+$")
]
Password = Annotated[SecretStr, Field(min_length=8, max_length=128)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: Username
    password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


class NewUser(Input):
    username: Username
    password: Password
    display_name: str = Field(default="", max_length=100)
    legacy_user_id: (
        Annotated[
            str, Field(min_length=1, max_length=128, pattern=r"^[\w.@-]+$")
        ]
        | None
    ) = None


class EditUser(Input):
    username: Username | None = None
    display_name: Annotated[str, Field(max_length=100)] | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("username", "display_name", "status")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError(
                "Omit an unchanged field instead of sending null."
            )
        return value


class ResetPassword(Input):
    password: Password


class ChangePassword(ResetPassword):
    current_password: Annotated[SecretStr, Field(min_length=1, max_length=128)]


def current(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "auth_user", None)
    if user is None:
        raise HTTPException(401, "请先登录。")
    return user


def administrator(user: dict[str, Any] = Depends(current)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(403, "需要系统管理员权限。")
    return user


def set_session_cookie(
    response: Response, service: AuthService, token: str
) -> None:
    """Login and sliding renewal use the same cookie policy."""
    response.set_cookie(
        service.cookie_name,
        token,
        httponly=True,
        secure=service.settings.cookie_secure,
        samesite="lax",
        path=service.settings.cookie_path or "/",
        max_age=service.settings.session_ttl_seconds,
    )


def safe_next_path(next_path: str | None, default: str = "/chat") -> str:
    """Allow only same-origin relative redirects."""
    if not next_path:
        return default
    value = next_path.strip()
    if not value.startswith("/") or value.startswith("//") or "://" in value:
        return default
    return value


def auth_router(service: AuthService) -> APIRouter:
    router = APIRouter(tags=["authentication"])

    @router.get("/branding", response_model=BrandingSettings)
    async def branding() -> BrandingSettings:
        return BrandingSettings.model_validate(await service.store.branding())

    @router.put(
        "/admin/branding",
        response_model=BrandingSettings,
        dependencies=[Depends(administrator)],
    )
    async def update_branding(body: BrandingSettings) -> BrandingSettings:
        await service.store.save_branding(body.model_dump())
        return body

    @router.post("/auth/login")
    async def login(
        body: Login, request: Request, response: Response
    ) -> dict[str, Any]:
        token, session, user = await service.login(
            body.username,
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
        )
        set_session_cookie(response, service, token)
        return {"user": public_user(user), "csrf_token": session["csrf_token"]}

    @router.get("/sso/nvwa")
    async def sso_nvwa(
        request: Request,
        ticket: str | None = Query(default=None),
        nv_t: str | None = Query(default=None),
        token: str | None = Query(default=None),
        next: str | None = Query(default=None),  # noqa: A002
    ) -> RedirectResponse:
        if service.nvwa_settings is None:
            raise HTTPException(503, "未配置女娲换票。")
        # URL App：ticket=短期票；token/nv_t=会话令牌。身份只来自女娲验票。
        session_token = None
        for value in (token, nv_t):
            if isinstance(value, str) and value.strip():
                session_token = value.strip()
                break
        short_ticket = (
            ticket.strip() if isinstance(ticket, str) and ticket.strip() else None
        )
        if not session_token and not short_ticket:
            raise HTTPException(400, "缺少 ticket / token / nv_t。")
        from ._nvwa_ticket import resolve_ticket

        nvwa_user = None
        resolve_error = None
        if session_token:
            try:
                nvwa_user = await resolve_ticket(
                    service.nvwa_settings,
                    session_token,
                    prefer="bearer",
                )
            except ValueError as exc:
                resolve_error = str(exc)
        if nvwa_user is None and short_ticket and short_ticket != session_token:
            try:
                nvwa_user = await resolve_ticket(
                    service.nvwa_settings,
                    short_ticket,
                    prefer="auto",
                )
                resolve_error = None
            except ValueError as exc:
                resolve_error = str(exc)
        if nvwa_user is None:
            raise HTTPException(401, resolve_error or "验票失败。")
        user = await service.ensure_sso_user(
            nvwa_user.username, nvwa_user.display_name
        )
        # JIT: copy usable model credentials + create an isolated default
        # agent under this user only (other Nuwa users cannot see it).
        storage = getattr(request.app.state, "storage", None)
        if storage is not None:
            try:
                from ._sso_bootstrap import provision_sso_user

                await provision_sso_user(storage, user)
            except Exception:  # noqa: BLE001 — never block SSO on provision failure
                pass
        session_token, _session, _user = await service.create_session_for_user(
            user
        )
        target = safe_next_path(next)
        # Cross-site iframe blocks Set-Cookie. Do NOT 302 to /sso/nvwa/handoff:
        # under Vite `/api` proxy that Location drops the `/api` prefix and the
        # SPA swallows the page. Return handoff HTML on this same response.
        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>SSO</title></head>
<body><p>正在进入…</p>
<script>
(function () {{
  try {{
    sessionStorage.setItem("agentscope_session", {session_token!r});
    sessionStorage.setItem("agentscope_embed", "1");
  }} catch (e) {{}}
  location.replace({target!r});
}})();
</script></body></html>"""
        response = Response(content=html, media_type="text/html; charset=utf-8")
        set_session_cookie(response, service, session_token)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/auth/me")
    async def me(
        request: Request, user: dict[str, Any] = Depends(current)
    ) -> dict[str, Any]:
        return {
            "user": public_user(user),
            "csrf_token": request.state.auth_session["csrf_token"],
        }

    @router.post("/auth/logout", status_code=204)
    async def logout(
        request: Request,
        response: Response,
        user: dict[str, Any] = Depends(current),
    ) -> None:
        await service.store.delete_session(
            request.cookies[service.cookie_name]
        )
        logger.info("Login revoked: reason=logout user_id=%s", user["id"])
        response.delete_cookie(
            service.cookie_name,
            path="/",
            httponly=True,
            secure=service.settings.cookie_secure,
            samesite="lax",
        )

    @router.post("/auth/password", status_code=204)
    async def password(
        body: ChangePassword, user: dict[str, Any] = Depends(current)
    ) -> None:
        if not await service.verify(
            user["password_hash"], body.current_password.get_secret_value()
        ):
            raise HTTPException(400, "当前密码不正确。")
        await service.edit_user(
            user["id"],
            {
                "password_hash": await service.hash_password(
                    body.password.get_secret_value()
                )
            },
            expected_version=user["auth_version"],
        )

    @router.get("/admin/users", dependencies=[Depends(administrator)])
    async def users(
        q: str = Query(default="", max_length=100),
        status: Literal["active", "disabled", "deleted"] | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        rows = [
            u
            for u in (await service.store.users()).values()
            if (u["status"] == status if status else u["status"] != "deleted")
            and (
                q.casefold() in u["username"].casefold()
                or q.casefold() in u["display_name"].casefold()
            )
        ]
        rows.sort(key=lambda u: (u["id"] != "admin", u["username"].casefold()))
        return {
            "items": [public_user(u) for u in rows[offset : offset + limit]],
            "total": len(rows),
        }

    @router.get("/admin/users/{uid}", dependencies=[Depends(administrator)])
    async def detail(uid: str) -> dict[str, Any]:
        user = await service.store.user(uid)
        if not user:
            raise HTTPException(404, "用户不存在。")
        return public_user(user)

    @router.post(
        "/admin/users", status_code=201, dependencies=[Depends(administrator)]
    )
    async def create(body: NewUser) -> dict[str, Any]:
        return public_user(await service.create_user(body))

    @router.patch("/admin/users/{uid}", dependencies=[Depends(administrator)])
    async def edit(uid: str, body: EditUser) -> dict[str, Any]:
        return public_user(
            await service.edit_user(uid, body.model_dump(exclude_unset=True))
        )

    @router.post(
        "/admin/users/{uid}/reset-password",
        status_code=204,
        dependencies=[Depends(administrator)],
    )
    async def reset(uid: str, body: ResetPassword) -> None:
        await service.edit_user(
            uid,
            {
                "password_hash": await service.hash_password(
                    body.password.get_secret_value()
                )
            },
        )

    @router.delete(
        "/admin/users/{uid}",
        status_code=204,
        dependencies=[Depends(administrator)],
    )
    async def delete(uid: str) -> None:
        await service.edit_user(uid, {"status": "deleted"})

    return router


class AuthMiddleware:
    """Replace caller-supplied identity at the ASGI boundary."""

    def __init__(self, app: ASGIApp, service: AuthService) -> None:
        self.app, self.service = app, service

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            # The current realtime transport is authenticated HTTP + WebRTC.
            return await send({"type": "websocket.close", "code": 1008})
        request = StarletteRequest(scope)
        started = False
        completed = False
        renewal_cookie: bytes | None = None

        async def outgoing(message: Message) -> None:
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"cache-control"
                ]
                message["headers"] = headers + [
                    (b"cache-control", b"no-store")
                ]
                if renewal_cookie is not None and message["status"] != 401:
                    message["headers"].append((b"set-cookie", renewal_cookie))
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                completed = True
            await send(message)

        try:
            # Frontend and API share an origin. An explicit allowlist supports
            # deployments whose proxy preserves a different internal Host.
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                origins = self.service.settings.allowed_origins or [
                    str(request.base_url).rstrip("/")
                ]
                if request.headers.get("origin") not in origins:
                    raise HTTPException(403, "请求来源校验失败。")
            path = scope["path"].removeprefix(scope.get("root_path", ""))
            # The login screen needs these two public display values.
            # Only this exact read endpoint bypasses session authentication.
            if path == "/branding" and scope["method"] == "GET":
                return await self.app(scope, receive, outgoing)
            if path == "/auth/login" and scope["method"] == "POST":
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != "application/json"
                ):
                    raise HTTPException(415, "登录请求必须使用 JSON。")
                return await self.app(scope, receive, outgoing)
            if path == "/sso/nvwa" and scope["method"] == "GET":
                return await self.app(scope, receive, outgoing)
            token = request.cookies.get(self.service.cookie_name)
            if not token:
                # Third-party iframe fallback: frontend sends session from sessionStorage.
                header = request.headers.get("x-agentscope-session", "").strip()
                token = header or None
            identity = await self.service.authenticate(token)
            if identity is None:
                raise HTTPException(401, "登录已失效，请重新登录。")
            session, user = identity
            if scope["method"] not in {
                "GET",
                "HEAD",
                "OPTIONS",
            } and not hmac.compare_digest(
                request.headers.get("x-csrf-token", "").encode("utf-8"),
                session["csrf_token"].encode("utf-8"),
            ):
                raise HTTPException(403, "请求校验失败，请刷新页面。")
            # Only incoming, authenticated requests count as activity. Keep
            # guard_request's periodic checks read-only, and never overwrite
            # logout's cookie deletion or renew credentials being replaced.
            if path not in {"/auth/logout", "/auth/password"}:
                identity = await self.service.renew_session(token)
                if identity is None:
                    raise HTTPException(401, "登录已失效，请重新登录。")
                session, user = identity
                cookie_response = Response()
                set_session_cookie(cookie_response, self.service, token)
                renewal_cookie = next(
                    value
                    for name, value in cookie_response.raw_headers
                    if name == b"set-cookie"
                )
            scope.setdefault("state", {}).update(
                auth_user=user, auth_session=session
            )
            scope["headers"] = [
                (k, v)
                for k, v in scope["headers"]
                if k.lower() != b"x-user-id"
            ]
            scope["headers"].append((b"x-user-id", user["id"].encode("utf-8")))
            await self.service.guard_request(
                token, self.app(scope, receive, outgoing)
            )
        except HTTPException as exc:
            if not started:
                await JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            elif not completed:
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                )
        except Exception as exc:
            # Auth reads also use Redis, but a storage outage does not revoke the cookie.
            from ..storage._errors import is_storage_unavailable, unavailable_detail
            if not is_storage_unavailable(exc):
                raise
            if not started:
                await JSONResponse({'detail': unavailable_detail()}, status_code=503,
                                   headers={'Cache-Control': 'no-store', 'Retry-After': '1'})(scope, receive, send)
            elif not completed:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
