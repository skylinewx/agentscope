"""Nuwa server verification must fail closed for forged and expired tokens."""

import base64
import json

import httpx
import pytest

from agentscope.app.auth._nvwa_ticket import NvwaTicketSettings, resolve_ticket


@pytest.mark.asyncio
async def test_verified_bearer_username_and_private_token():
    settings = NvwaTicketSettings(
        base_url="http://nvwa.test", auth_mode="bearer"
    )

    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "fixture-token"
        return httpx.Response(
            200,
            json={
                "code": "200",
                "datas": {
                    "userName": "alice",
                    "displayName": "Alice",
                    "id": "u1",
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        user = await resolve_ticket(settings, "fixture-token", client=client)
    assert (user.username, user.display_name, user.user_id) == (
        "alice",
        "Alice",
        "u1",
    )
    assert user.access_token.get_secret_value() == "fixture-token"
    assert "fixture-token" not in user.model_dump_json()


@pytest.mark.asyncio
async def test_ticket_exchange_does_not_forward_one_time_ticket_to_mcp():
    def handler(request):
        assert request.method == "POST"
        assert json.loads(request.content) == {"ticket": "one-time"}
        return httpx.Response(200, json={"userName": "alice"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        user = await resolve_ticket(
            NvwaTicketSettings(base_url="http://nvwa.test"),
            "one-time",
            client=client,
        )
    assert user.username == "alice"
    assert user.access_token is None


@pytest.mark.asyncio
async def test_ticket_exchange_accepts_explicit_delegated_token():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "contextUser": {"userName": "alice"},
                    "accessToken": "delegated",
                },
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        user = await resolve_ticket(
            NvwaTicketSettings(base_url="http://nvwa.test"),
            "ticket",
            client=client,
        )
    assert user.access_token.get_secret_value() == "delegated"


@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {"userName": "admin"}),
        (403, {}),
        (404, {}),
        (502, {}),
        (503, {}),
        (200, {"code": "401", "data": {"userName": "admin"}}),
        (200, {"code": "200", "data": {}}),
        (200, []),
        (302, {}),
    ],
)
@pytest.mark.asyncio
async def test_unverified_jwt_is_rejected_even_when_payload_claims_admin(
    status, payload
):
    claims = (
        base64.urlsafe_b64encode(
            json.dumps({"username": "admin", "exp": 1}).encode()
        )
        .decode()
        .rstrip("=")
    )
    token = f"unsigned.{claims}.invalid-signature"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, json=payload)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError):
            await resolve_ticket(
                NvwaTicketSettings(
                    base_url="http://nvwa.test", auth_mode="bearer"
                ),
                token,
                client=client,
            )


@pytest.mark.asyncio
async def test_invalid_json_does_not_trigger_jwt_fallback():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text="<html>gateway</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError):
            await resolve_ticket(
                NvwaTicketSettings(base_url="http://nvwa.test"),
                "a.eyJ1c2VybmFtZSI6ImFkbWluIn0.x",
                client=client,
            )


@pytest.mark.asyncio
async def test_transport_failure_is_sanitized():
    def fail(request):
        raise httpx.ConnectError("secret-upstream-body", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail)
    ) as client:
        with pytest.raises(ValueError) as caught:
            await resolve_ticket(
                NvwaTicketSettings(base_url="http://nvwa.test"),
                "ticket",
                client=client,
            )
    assert "secret-upstream-body" not in str(caught.value)


@pytest.mark.asyncio
async def test_explicit_auth_mode_cannot_be_overridden_by_query_kind():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"userName": "alice"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await resolve_ticket(
            NvwaTicketSettings(
                base_url="http://nvwa.test", auth_mode="ticket_post"
            ),
            "token",
            prefer="bearer",
            client=client,
        )
