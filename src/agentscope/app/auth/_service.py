"""Password validation, revocable sessions, and account administration."""

import asyncio
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Coroutine, NoReturn, Self, TYPE_CHECKING

from ._store import RedisAuthStore
from ..._logging import logger

if TYPE_CHECKING:
    from ._http import NewUser

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AuthSettings(BaseModel):
    """Secrets stay in server configuration and never appear in API output."""

    model_config = ConfigDict(extra="forbid")
    admin_password: SecretStr
    session_ttl_seconds: int = Field(default=43200, ge=60, le=604800)
    cookie_secure: bool = False
    allowed_origins: list[str] = Field(default_factory=list)
    cookie_path: str = "/"
    public_base_path: str = "/"

    @field_validator("cookie_path", "public_base_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or any(c in value for c in "\\?#;%")
            or any(ord(c) < 33 for c in value)
            or any(p in {".", ".."} for p in value.split("/"))
        ):
            raise ValueError("Use an absolute, same-origin path.")
        return value.rstrip("/") or "/"

    def validate_password(self) -> None:
        value = self.admin_password.get_secret_value()
        if not 8 <= len(value) <= 128 or value in {
            "<部署时设置>",
            "CHANGE_ME",
        }:
            raise ValueError(
                "Configure an admin password of 8–128 characters."
            )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in user.items()
        if key not in {"password_hash", "auth_version"}
    }


def reject(status: int, detail: str) -> NoReturn:
    raise HTTPException(status, detail)


class _BootstrapConflict(Exception):
    """Retry a bootstrap whose password changed during hash verification."""


class AuthService:
    """The example service enables this explicitly; SDK embedders opt in."""

    cookie_name = "agentscope_session"

    def __init__(
        self,
        store: RedisAuthStore,
        settings: AuthSettings,
        nvwa_settings: Any | None = None,
    ) -> None:
        settings.validate_password()
        from argon2 import PasswordHasher

        self.store = store
        self.settings = settings
        self.nvwa_settings = nvwa_settings
        self.hasher = PasswordHasher(
            time_cost=2, memory_cost=19456, parallelism=1
        )
        self.dummy_hash = ""

    async def __aenter__(self) -> Self:
        await self.store.__aenter__()
        try:
            password = self.settings.admin_password.get_secret_value()
            encoded = await self.hash_password(password)
            self.dummy_hash = encoded
            for _ in range(20):
                current = await self.store.user("admin")
                expected = current["password_hash"] if current else None
                unchanged = bool(
                    current and await self.verify(expected or "", password)
                )

                def bootstrap(
                    users: dict[str, dict[str, Any]],
                ) -> dict[str, Any]:
                    record = users.get("admin")
                    if (
                        record["password_hash"] if record else None
                    ) != expected:
                        raise _BootstrapConflict()
                    if record is None:
                        return self.new_user(
                            "admin", "admin", "系统管理员", encoded, "admin"
                        )
                    if not unchanged:
                        record["password_hash"] = encoded
                        record["auth_version"] += 1
                        record["updated_at"] = now()
                    record.update(
                        username="admin", role="admin", status="active"
                    )
                    return record

                try:
                    await self.store.mutate(bootstrap)
                    break
                except _BootstrapConflict:
                    continue
            else:
                raise RuntimeError(
                    "Concurrent admin configuration changes; retry startup."
                )
        except BaseException:
            await self.store.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.store.__aexit__(*exc)

    @staticmethod
    def new_user(
        uid: str,
        username: str,
        display_name: str,
        encoded: str,
        role: str = "user",
    ) -> dict[str, Any]:
        return dict(
            id=uid,
            username=username,
            display_name=display_name,
            password_hash=encoded,
            role=role,
            status="active",
            auth_version=1,
            created_at=now(),
            updated_at=now(),
            last_login_at=None,
        )

    async def hash_password(self, password: str) -> str:
        return await asyncio.to_thread(self.hasher.hash, password)

    async def verify(self, encoded: str, password: str) -> bool:
        from argon2.exceptions import VerificationError, InvalidHashError

        try:
            return await asyncio.to_thread(
                self.hasher.verify, encoded, password
            )
        except (VerificationError, InvalidHashError):
            return False

    async def login(
        self, username: str, password: str, address: str
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if not await self.store.allow_login(username, address):
            reject(429, "登录尝试过于频繁，请 5 分钟后重试。")
        user = next(
            (
                u
                for u in (await self.store.users()).values()
                if u["username"].casefold() == username.casefold()
                and u["status"] != "deleted"
            ),
            None,
        )
        valid = await self.verify(
            user["password_hash"] if user else self.dummy_hash, password
        )
        if not valid or user is None or user["status"] != "active":
            reject(401, "用户名或密码错误，或账号已停用。")
        verified_user = user

        def touch(users: dict[str, dict[str, Any]]) -> dict[str, Any]:
            latest = users[verified_user["id"]]
            if (
                latest["status"] != "active"
                or latest["auth_version"] != verified_user["auth_version"]
            ):
                reject(401, "账号状态已变化，请重新登录。")
            latest["last_login_at"] = now()
            return latest

        user = await self.store.mutate(touch)
        token = secrets.token_urlsafe(32)
        session = dict(
            user_id=user["id"],
            auth_version=user["auth_version"],
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self.settings.session_ttl_seconds,
        )
        await self.store.save_session(
            token, session, self.settings.session_ttl_seconds
        )
        logger.info(
            "Login accepted: user_id=%s idle_timeout_seconds=%s",
            user["id"],
            self.settings.session_ttl_seconds,
        )
        return token, session, user

    async def create_session_for_user(
        self, user: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Create a session for an already-authenticated account (SSO)."""
        verified_user = user

        def touch(users: dict[str, dict[str, Any]]) -> dict[str, Any]:
            latest = users[verified_user["id"]]
            if (
                latest["status"] != "active"
                or latest["auth_version"] != verified_user["auth_version"]
            ):
                reject(401, "账号状态已变化，请重新登录。")
            latest["last_login_at"] = now()
            return latest

        user = await self.store.mutate(touch)
        token = secrets.token_urlsafe(32)
        session = dict(
            user_id=user["id"],
            auth_version=user["auth_version"],
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self.settings.session_ttl_seconds,
        )
        await self.store.save_session(
            token, session, self.settings.session_ttl_seconds
        )
        logger.info(
            "SSO session created: user_id=%s idle_timeout_seconds=%s",
            user["id"],
            self.settings.session_ttl_seconds,
        )
        return token, session, user

    async def ensure_sso_user(
        self, username: str, display_name: str = ""
    ) -> dict[str, Any]:
        """Atomically match a verified Nuwa username, preserving local passwords."""
        from ._http import NewUser

        body = NewUser(
            username=username,
            password=secrets.token_urlsafe(24),
            display_name=display_name or username,
        )
        if username.casefold() == "admin":
            reject(403, "内置 admin 请使用用户名密码登录。")
        encoded = await self.hash_password(body.password.get_secret_value())
        uid = uuid.uuid4().hex

        def provision(users):
            existing = next(
                (
                    u
                    for u in users.values()
                    if u["username"].casefold() == username.casefold()
                ),
                None,
            )
            if existing is not None:
                if (
                    existing["status"] != "active"
                    or existing["role"] != "user"
                ):
                    reject(403, "账号已停用或不允许通过女娲登录。")
                if display_name:
                    existing["display_name"] = body.display_name
                existing.update(sso_provider="nvwa", updated_at=now())
                return existing
            user = self.new_user(
                uid,
                body.username,
                body.display_name,
                encoded,
            )
            user["sso_provider"] = "nvwa"
            return user

        return await self.store.mutate(provision)

    async def authenticate(
        self, token: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Validate only; server-side stream monitors must not renew sessions."""
        if not token or len(token) > 256:
            logger.info(
                "Authentication rejected: reason=%s",
                "missing_cookie" if not token else "invalid_cookie",
            )
            return None
        session = await self.store.session(token)
        if not session:
            logger.info("Authentication rejected: reason=missing_session")
            return None
        if session["expires_at"] <= time.time():
            logger.info(
                "Authentication rejected: reason=expired user_id=%s",
                session["user_id"],
            )
            return None
        user = await self.store.user(session["user_id"])
        if (
            not user
            or user["status"] != "active"
            or user["auth_version"] != session["auth_version"]
        ):
            logger.info(
                "Authentication rejected: reason=account_changed user_id=%s",
                session["user_id"],
            )
            return None
        return session, user

    async def renew_session(
        self, token: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Renew only after the incoming request passes origin/CSRF checks."""
        identity = await self.store.renew_session(
            token, self.settings.session_ttl_seconds
        )
        if identity is None:
            logger.info(
                "Authentication rejected: reason=session_changed_before_renewal"
            )
        return identity

    async def is_active(self, user_id: str) -> bool:
        user = await self.store.user(user_id)
        return bool(user and user["status"] == "active")

    async def create_user(self, body: "NewUser") -> dict[str, Any]:
        encoded = await self.hash_password(body.password.get_secret_value())
        uid = body.legacy_user_id or uuid.uuid4().hex

        def create(users: dict[str, dict[str, Any]]) -> dict[str, Any]:
            if uid in users or uid.casefold() == "admin":
                reject(409, "此历史用户 ID 已绑定或被保留。")
            self.check_username(users, body.username)
            return self.new_user(
                uid, body.username, body.display_name, encoded
            )

        return await self.store.mutate(create)

    @staticmethod
    def check_username(
        users: dict[str, dict[str, Any]],
        username: str,
        exclude: str | None = None,
    ) -> None:
        if username.casefold() == "admin" or any(
            u["id"] != exclude
            and u["status"] != "deleted"
            and u["username"].casefold() == username.casefold()
            for u in users.values()
        ):
            reject(409, "用户名已存在或被系统保留。")

    async def edit_user(
        self,
        uid: str,
        changes: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        def edit(users: dict[str, dict[str, Any]]) -> dict[str, Any]:
            user = users.get(uid)
            if not user or user["status"] == "deleted":
                reject(404, "用户不存在。")
            if uid == "admin":
                reject(403, "内置 admin 由服务端配置管理。")
            if (
                expected_version is not None
                and user["auth_version"] != expected_version
            ):
                reject(409, "账号已发生变化，请重新登录。")
            if "username" in changes:
                self.check_username(users, changes["username"], uid)
            if "password_hash" in changes or (
                "status" in changes and changes["status"] != user["status"]
            ):
                user["auth_version"] += 1
            user.update(changes, updated_at=now())
            return user

        return await self.store.mutate(edit)

    async def guard_request(
        self, token: str, awaitable: Coroutine[Any, Any, Any]
    ) -> Any:
        """Stop long requests when their session expires or is revoked."""
        task = asyncio.create_task(awaitable)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=2)
                if not done and not await self.authenticate(token):
                    reject(401, "登录已失效，请重新登录。")
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def monitor_runtime(self, app: Any) -> None:
        """Stop this worker's running work for disabled accounts."""
        while True:
            await asyncio.sleep(1)
            registry = app.state.chat_run_registry
            bg = app.state.background_task_manager
            connections = app.state.realtime_connections
            owners = set(registry.owners.values()) | {
                t.user_id for t in bg.tasks.values()
            }
            owners |= {key[0] for key in connections}
            for uid in owners:
                try:
                    active = await self.is_active(uid)
                except Exception:
                    # Fail closed during an authentication-store outage.
                    active = False
                if active:
                    continue
                for sid, owner in list(registry.owners.items()):
                    task = registry.get(sid)
                    if owner == uid and task:
                        task.cancel()
                for task in list(bg.tasks.values()):
                    if task.user_id == uid:
                        task.asyncio_task.cancel()
                for key, connection in list(connections.items()):
                    if key[0] == uid:
                        # One broken transport must not stop the monitor.
                        await asyncio.gather(
                            connection.close(),
                            return_exceptions=True,
                        )
