"""SSO verification, password compatibility, prefix and revocation checks."""

import asyncio
import tempfile
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.auth import AuthService, AuthSettings, RedisAuthStore
from agentscope.app.auth._http import safe_next_path
from agentscope.app.auth._nvwa_ticket import NvwaTicketSettings, NvwaUser
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.resources import LocalSkillStore
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

ADMIN_PASSWORD = "Admin-test-password-42"


class FakeAuthStore(RedisAuthStore):
    async def __aenter__(self):
        self.client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return self


class FakeStorage(RedisStorage):
    async def __aenter__(self):
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return self


def make_sso_app(workdir, nvwa=None, storage=None):
    service = AuthService(
        FakeAuthStore(),
        AuthSettings(
            admin_password=ADMIN_PASSWORD,
            cookie_path="/agentscope",
            public_base_path="/agentscope",
        ),
        nvwa_settings=nvwa,
    )
    return (
        create_app(
            storage=storage or FakeStorage(),
            message_bus=InMemoryMessageBus(),
            workspace_manager=LocalWorkspaceManager(str(workdir)),
            skill_store=LocalSkillStore(str(workdir) + "/skill-artifacts"),
            enable_index_worker=False,
            enable_scheduler=False,
            auth_service=service,
        ),
        service,
    )


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test",
        "//evil.test",
        "/\\evil.test",
        "/%5cevil.test",
        "/</script><script>alert(1)</script>",
        "/../outside",
        "/%2e%2e/out",
        "/\n/evil.test",
    ],
)
def test_safe_next_rejects_url_aliases_and_injection(target):
    assert safe_next_path(target) == "/chat"


def test_sso_disabled_and_username_only_are_rejected():
    for config, path, expected in [
        (None, "/sso/nvwa?ticket=x", 503),
        (
            NvwaTicketSettings(base_url="http://nvwa.test"),
            "/sso/nvwa?username=admin",
            400,
        ),
    ]:
        with tempfile.TemporaryDirectory() as root:
            app, _ = make_sso_app(root, config)
            with TestClient(app) as client:
                assert client.get(path).status_code == expected


def test_verified_username_login_redirect_and_header_logout():
    with tempfile.TemporaryDirectory() as root:
        app, auth = make_sso_app(
            root, NvwaTicketSettings(base_url="http://nvwa.test")
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            with patch(
                "agentscope.app.auth._nvwa_ticket.resolve_ticket",
                new=AsyncMock(
                    return_value=NvwaUser(
                        username="alice", display_name="Alice"
                    )
                ),
            ):
                response = client.get(
                    "/sso/nvwa?ticket=verified&username=admin&next=/agents",
                    follow_redirects=False,
                )
            assert response.status_code == 303
            assert response.headers["location"] == "/agentscope/agents"
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "Path=/agentscope" in response.headers["set-cookie"]
            assert "sessionStorage" not in response.text
            headers = {
                "X-Agentscope-Session": response.cookies.get(auth.cookie_name)
            }
            client.cookies.clear()
            identity = client.get("/auth/me", headers=headers).json()
            assert identity["user"]["username"] == "alice"
            assert identity["user"]["role"] == "user"
            assert (
                client.get("/admin/users", headers=headers).status_code == 403
            )
            response = client.post(
                "/auth/logout",
                headers={
                    **headers,
                    "Origin": "http://testserver",
                    "X-CSRF-Token": identity["csrf_token"],
                },
            )
            assert response.status_code == 204
            assert "Path=/agentscope" in response.headers["set-cookie"]
            assert client.get("/auth/me", headers=headers).status_code == 401


def test_admin_sso_blocked_and_password_login_remains_available():
    with tempfile.TemporaryDirectory() as root:
        app, _ = make_sso_app(
            root, NvwaTicketSettings(base_url="http://nvwa.test")
        )
        with TestClient(app) as client:
            with patch(
                "agentscope.app.auth._nvwa_ticket.resolve_ticket",
                new=AsyncMock(return_value=NvwaUser(username="ADMIN")),
            ):
                assert (
                    client.get("/sso/nvwa?ticket=verified").status_code == 403
                )
            response = client.post(
                "/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200
            assert response.json()["user"]["role"] == "admin"


def test_rejected_ticket_never_falls_back_to_supplied_username():
    with tempfile.TemporaryDirectory() as root:
        app, _ = make_sso_app(
            root, NvwaTicketSettings(base_url="http://nvwa.test")
        )
        with TestClient(app) as client:
            with patch(
                "agentscope.app.auth._nvwa_ticket.resolve_ticket",
                new=AsyncMock(side_effect=ValueError("rejected")),
            ):
                assert (
                    client.get(
                        "/sso/nvwa?ticket=bad&username=alice"
                    ).status_code
                    == 401
                )


def test_concurrent_username_mapping_preserves_password_and_disabled_state():
    from agentscope.app.auth._http import NewUser

    with tempfile.TemporaryDirectory() as root:
        app, auth = make_sso_app(
            root, NvwaTicketSettings(base_url="http://nvwa.test")
        )
        with TestClient(app) as client:
            original = client.portal.call(
                auth.create_user,
                NewUser(username="alice", password="Local-password-42"),
            )

            async def race():
                return await asyncio.gather(
                    *(
                        auth.ensure_sso_user("Alice", "New display")
                        for _ in range(4)
                    )
                )

            users = client.portal.call(race)
            assert {u["id"] for u in users} == {original["id"]}
            assert users[0]["password_hash"] == original["password_hash"]
            response = client.post(
                "/auth/login",
                json={"username": "alice", "password": "Local-password-42"},
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200
            client.portal.call(
                auth.edit_user, original["id"], {"status": "disabled"}
            )
            with patch(
                "agentscope.app.auth._nvwa_ticket.resolve_ticket",
                new=AsyncMock(return_value=NvwaUser(username="alice")),
            ):
                assert (
                    client.get("/sso/nvwa?ticket=verified").status_code == 403
                )
