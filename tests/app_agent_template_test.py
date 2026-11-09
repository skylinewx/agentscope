"""SSO template isolation, least-privilege resources and explicit updates."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app_auth_sso_nvwa_test import make_sso_app
from agentscope.app.auth._nvwa_ticket import NvwaTicketSettings, NvwaUser
from agentscope.app._service._agent_template import AgentTemplateService
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    MCPRecord,
    SkillRecord,
)
from agentscope.app.storage._model._agent_settings import AgentDefaults
from agentscope.app.storage._model._resource_config import (
    AgentResourceConfig,
    ResourceBinding,
    SkillResourceBinding,
)
from agentscope.credential import OpenAICredential
from agentscope.mcp import MCPClient, HttpMCPConfig


@pytest.fixture(params=["redis", "sql"])
def fixture(tmp_path, request):
    app, auth = make_sso_app(
        tmp_path,
        NvwaTicketSettings(
            base_url="http://nvwa.test",
            template_owner_id="wx",
            template_agent_id="source",
        ),
        storage=(
            AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:")
            if request.param == "sql"
            else None
        ),
    )
    with TestClient(app) as client:
        storage = app.state.storage
        portal = client.portal
        portal.call(
            storage.upsert_credential,
            "wx",
            OpenAICredential(
                id="source-key",
                name="Template model",
                api_key="source-secret-never-export",
            ),
        )
        portal.call(
            storage.upsert_credential,
            "wx",
            OpenAICredential(
                id="other-key",
                name="Unrelated model",
                api_key="unrelated-secret",
            ),
        )
        package = portal.call(
            app.state.resource_service.skill_store.save_files,
            {
                "SKILL.md": "---\nname: ontology\ndescription: Ontology fixture\n---\nUse the fixture tools.",
                "scripts/helper.py": "print('fixture helper')",
            },
        )
        portal.call(
            storage.upsert_skill,
            "wx",
            SkillRecord(
                id="source-skill",
                user_id="wx",
                name="ontology",
                artifact_id=package.artifact_id,
                markdown=package.markdown,
                description=package.description,
                version="v1",
            ),
        )
        portal.call(
            storage.upsert_mcp,
            "wx",
            MCPRecord(
                id="source-mcp",
                user_id="wx",
                client=MCPClient(
                    name="ontology",
                    is_stateful=False,
                    mcp_config=HttpMCPConfig(
                        url="http://ontology.test/mcp",
                        headers={"Authorization": "admin-business-secret"},
                    ),
                ),
                values={"token": "admin-business-secret"},
            ),
        )
        source = AgentRecord(
            id="source",
            user_id="wx",
            source="user",
            data=AgentData(
                id="source",
                name="Ontology verifier",
                system_prompt="Original business instructions",
                description="Original description",
                defaults=AgentDefaults(
                    chat_model_config=ChatModelConfig(
                        type="openai_credential",
                        credential_id="source-key",
                        model="fixture-model",
                        parameters={},
                    )
                ),
                resource_config=AgentResourceConfig(
                    skills={
                        "source-skill": SkillResourceBinding(
                            artifact_id=package.artifact_id, version="v1"
                        )
                    },
                    mcps={"source-mcp": ResourceBinding()},
                ),
            ),
        )
        portal.call(storage.upsert_agent, "wx", source)

        def sso(name):
            with patch(
                "agentscope.app.auth._nvwa_ticket.resolve_ticket",
                new=AsyncMock(
                    return_value=NvwaUser(
                        username=name,
                        access_token="user-token-" + name,
                    )
                ),
            ):
                response = client.get(
                    "/sso/nvwa?ticket=verified", follow_redirects=False
                )
            assert response.status_code == 303, response.text
            headers = {
                "X-Agentscope-Session": response.cookies.get(auth.cookie_name)
            }
            client.cookies.clear()
            identity = client.get("/auth/me", headers=headers).json()
            headers.update(
                {
                    "Origin": "http://testserver",
                    "X-CSRF-Token": identity["csrf_token"],
                }
            )
            return identity["user"]["id"], headers

        yield SimpleNamespace(
            app=app,
            auth=auth,
            client=client,
            portal=portal,
            storage=storage,
            source=source,
            sso=sso,
            templates=AgentTemplateService(app.state),
        )


def owned(f, uid):
    rows = f.portal.call(f.storage.list_agents, uid)
    assert len(rows) == 1
    return rows[0]


def test_first_login_clones_exact_template_without_exporting_admin_keys(
    fixture,
):
    f = fixture
    uid, headers = f.sso("alice")
    a = owned(f, uid)
    assert a.id != "source" and a.user_id == uid
    assert a.data.system_prompt == f.source.data.system_prompt
    assert a.data.defaults.chat_model_config.model == "fixture-model"
    assert f.portal.call(f.storage.list_credentials, uid) == []
    credentials = f.client.get("/credential/", headers=headers).json()[
        "credentials"
    ]
    assert [c["id"] for c in credentials] == ["source-key"]
    assert "source-secret-never-export" not in str(credentials)
    assert credentials[0]["editable"] is False
    assert f.client.delete(
        "/credential/source-key", headers=headers
    ).status_code in {403, 404}
    assert (
        f.client.get(
            "/agent-management/source",
            params={"owner_id": "wx"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        f.client.post(
            "/sessions/", json={"agent_id": a.id}, headers=headers
        ).status_code
        == 201
    )
    mcp = f.portal.call(f.storage.list_mcps, uid)[0]
    assert "admin-business-secret" not in mcp.model_dump_json()
    assert f.portal.call(f.templates.mcp_headers, uid, a.id, mcp) == {
        "Authorization": "user-token-alice"
    }
    skill = f.portal.call(f.storage.list_skills, uid)[0]
    helper = f.portal.call(
        f.app.state.resource_service.skill_store.read_file,
        skill.artifact_id,
        "scripts/helper.py",
    )
    assert helper == "print('fixture helper')"


def test_repeated_and_concurrent_login_create_one_instance_and_keep_user_edits(
    fixture,
):
    f = fixture
    uid, _ = f.sso("alice")
    a = owned(f, uid)
    a.data.name = "Personal name"
    f.portal.call(f.storage.upsert_agent, uid, a)

    async def race():
        return await asyncio.gather(
            *(f.templates.ensure(uid) for _ in range(4))
        )

    assert {r.id for r in f.portal.call(race)} == {a.id}
    f.sso("alice")
    assert owned(f, uid).data.name == "Personal name"
    assert len(f.portal.call(f.storage.list_mcps, uid)) == 1
    assert len(f.portal.call(f.storage.list_skills, uid)) == 1


def test_users_have_distinct_workspaces_resources_and_mcp_tokens(fixture):
    f = fixture
    alice, ha = f.sso("alice")
    bob, hb = f.sso("bob")
    a, b = owned(f, alice), owned(f, bob)
    assert a.id != b.id and a.data.workspace_id != b.data.workspace_id
    assert set(a.data.resource_config.mcps).isdisjoint(
        b.data.resource_config.mcps
    )
    assert (
        f.client.get(
            f"/agent-management/{a.id}", params={"owner_id": alice}, headers=hb
        ).status_code
        == 403
    )
    assert (
        f.client.get(
            f"/agent-management/{b.id}/template-preview", headers=ha
        ).status_code
        == 404
    )
    for uid, agent, name in ((alice, a, "alice"), (bob, b, "bob")):
        mcp = f.portal.call(f.storage.list_mcps, uid)[0]
        assert (
            f.portal.call(f.templates.mcp_headers, uid, agent.id, mcp)[
                "Authorization"
            ]
            == "user-token-" + name
        )


def test_updates_are_explicit_and_conflicts_default_to_personal_value(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    workspace = a.data.workspace_id
    a.data.system_prompt = "My personal prompt"
    f.portal.call(f.storage.upsert_agent, uid, a)
    f.source.data.system_prompt = "New business prompt"
    f.source.data.description = "Updated source description"
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    f.sso("alice")
    assert owned(f, uid).data.description == "Original description"
    preview = f.client.get(
        f"/agent-management/{a.id}/template-preview", headers=h
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert next(
        c for c in data["changes"] if c["path"] == "/agent/system_prompt"
    )["conflict"]
    update = {
        "source_revision": data["source_revision"],
        "target_revision": data["target_revision"],
        "take_source": [],
    }
    response = f.client.post(
        f"/agent-management/{a.id}/template-update", json=update, headers=h
    )
    assert response.status_code == 200, response.text
    saved = owned(f, uid)
    assert saved.data.system_prompt == "My personal prompt"
    assert saved.data.description == "Updated source description"
    assert saved.data.workspace_id == workspace
    assert (
        f.client.get(
            f"/agent-management/{a.id}/template-preview", headers=h
        ).json()["changes"]
        == []
    )
    assert len(f.portal.call(f.storage.list_mcps, uid)) == 1


def test_explicit_conflict_override_and_stale_preview(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    a.data.system_prompt = "Personal"
    f.portal.call(f.storage.upsert_agent, uid, a)
    f.source.data.system_prompt = "Source v2"
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    data = f.client.get(
        f"/agent-management/{a.id}/template-preview", headers=h
    ).json()
    body = {k: data[k] for k in ("source_revision", "target_revision")}
    body["take_source"] = ["/agent/system_prompt"]
    assert (
        f.client.post(
            f"/agent-management/{a.id}/template-update", json=body, headers=h
        ).status_code
        == 200
    )
    assert owned(f, uid).data.system_prompt == "Source v2"
    assert (
        f.client.post(
            f"/agent-management/{a.id}/template-update", json=body, headers=h
        ).status_code
        == 409
    )


def test_failed_install_rolls_back_resources_and_can_be_retried(fixture):
    f = fixture
    user = f.portal.call(f.auth.ensure_sso_user, "alice")
    with patch.object(
        f.storage,
        "upsert_agent",
        new=AsyncMock(side_effect=RuntimeError("fixture failure")),
    ):
        with pytest.raises(RuntimeError):
            f.portal.call(f.templates.ensure, user["id"])
    assert f.portal.call(f.storage.list_agents, user["id"]) == []
    assert f.portal.call(f.storage.list_mcps, user["id"]) == []
    assert f.portal.call(f.storage.list_skills, user["id"]) == []
    f.portal.call(f.templates.ensure, user["id"])
    assert owned(f, user["id"]).data.name == f.source.data.name


def test_model_revocation_and_mcp_endpoint_changes_fail_closed(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    mcp = f.portal.call(f.storage.list_mcps, uid)[0]
    mcp.client.mcp_config.url = "http://attacker.test/mcp"
    with pytest.raises(HTTPException) as denied:
        f.portal.call(f.templates.mcp_headers, uid, a.id, mcp)
    assert denied.value.status_code == 409
    f.portal.call(f.storage.delete_credential, "wx", "source-key")
    assert f.client.get("/credential/", headers=h).json()["credentials"] == []
    assert (
        f.client.post(
            "/sessions/", json={"agent_id": a.id}, headers=h
        ).status_code
        == 404
    )


def test_logout_removes_delegated_business_token(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    assert f.client.post("/auth/logout", headers=h).status_code == 204
    mcp = f.portal.call(f.storage.list_mcps, uid)[0]
    with pytest.raises(HTTPException) as denied:
        f.portal.call(f.templates.mcp_headers, uid, a.id, mcp)
    assert denied.value.status_code == 401


def test_concurrent_first_initialization_is_idempotent(fixture):
    f = fixture
    user = f.portal.call(f.auth.ensure_sso_user, "alice")

    async def race():
        return await asyncio.gather(
            *(f.templates.ensure(user["id"]) for _ in range(4))
        )

    assert len({a.id for a in f.portal.call(race)}) == 1
    assert len(f.portal.call(f.storage.list_agents, user["id"])) == 1
    assert len(f.portal.call(f.storage.list_skills, user["id"])) == 1


def test_update_keeps_session_model_overrides_and_rejects_source_race(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    inherited = f.client.post(
        "/sessions/", headers=h, json={"agent_id": a.id}
    ).json()["session_id"]
    config = a.data.defaults.chat_model_config.model_dump()
    config["model"] = "personal-model"
    personal = f.client.post(
        "/sessions/",
        headers=h,
        json={"agent_id": a.id, "chat_model_config": config},
    ).json()["session_id"]
    f.source.data.defaults.chat_model_config.model = "source-v2"
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    data = f.client.get(
        f"/agent-management/{a.id}/template-preview", headers=h
    ).json()
    body = {k: data[k] for k in ("source_revision", "target_revision")}
    f.source.data.description = "Changed while preview was open"
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    assert (
        f.client.post(
            f"/agent-management/{a.id}/template-update", headers=h, json=body
        ).status_code
        == 409
    )
    data = f.client.get(
        f"/agent-management/{a.id}/template-preview", headers=h
    ).json()
    body = {k: data[k] for k in ("source_revision", "target_revision")}
    assert (
        f.client.post(
            f"/agent-management/{a.id}/template-update", headers=h, json=body
        ).status_code
        == 200
    )
    assert (
        f.portal.call(
            f.storage.get_session, uid, a.id, personal
        ).config.chat_model_config.model
        == "personal-model"
    )
    assert (
        f.portal.call(
            f.storage.get_session, uid, a.id, inherited
        ).config.chat_model_config.model
        == "source-v2"
    )


def test_update_waits_until_running_session_finishes(fixture):
    from agentscope.app.message_bus import MessageBusKeys

    f = fixture
    uid, _ = f.sso("alice")
    a = owned(f, uid)
    from agentscope.app.storage import SessionConfig

    session = f.portal.call(
        f.storage.upsert_session,
        uid,
        a.id,
        SessionConfig(workspace_id=a.data.workspace_id),
    )
    f.source.data.description = "v2"
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    preview = f.portal.call(f.templates.preview, a)

    async def while_running():
        async with f.app.state.message_bus.acquire_lock(
            MessageBusKeys.session_lock(session.id)
        ):
            with pytest.raises(HTTPException) as denied:
                await f.templates.update(
                    a,
                    preview["source_revision"],
                    preview["target_revision"],
                    [],
                )
            assert denied.value.status_code == 409

    f.portal.call(while_running)
    assert owned(f, uid).data.description == "Original description"


def test_existing_login_does_not_require_source_to_still_exist(fixture):
    f = fixture
    uid, _ = f.sso("alice")
    a = owned(f, uid)
    f.portal.call(f.storage.delete_agent, "wx", "source")
    assert f.sso("alice")[0] == uid
    assert owned(f, uid).id == a.id


def test_update_preview_keeps_personal_mcp_secrets_write_only(fixture):
    f = fixture
    uid, h = f.sso("alice")
    a = owned(f, uid)
    mcp = f.portal.call(f.storage.list_mcps, uid)[0]
    mcp.client.mcp_config.headers = {"Authorization": "personal-secret"}
    f.portal.call(f.storage.upsert_mcp, uid, mcp)
    f.source.data.resource_config.mcps.clear()
    f.portal.call(f.storage.upsert_agent, "wx", f.source)
    response = f.client.get(
        f"/agent-management/{a.id}/template-preview", headers=h
    )
    assert response.status_code == 200
    assert "personal-secret" not in response.text
    assert "KEEP_EXISTING_SECRET" in response.text


def test_runtime_mcp_token_rotation_and_expiry(fixture, tmp_path):
    from service_resources_test import FakeMCP
    from agentscope.app._service._resources import ResourceToolkit
    from agentscope.app.storage import SessionConfig
    from agentscope.workspace import LocalWorkspace

    f = fixture
    uid, _ = f.sso("alice")
    a = owned(f, uid)

    async def exercise():
        workspace = LocalWorkspace(workdir=str(tmp_path / "runtime"))
        await workspace.initialize()
        clients = []

        def create(spec, **_kwargs):
            client = FakeMCP.model_validate(spec.model_dump(mode="json"))
            clients.append(client)
            return client

        workspace.create_mcp_client = create
        session = await f.storage.upsert_session(
            uid, a.id, SessionConfig(workspace_id=a.data.workspace_id)
        )

        async def snapshot():
            tools, skills, entries = (
                await f.app.state.resource_service.snapshot(
                    uid, workspace, a.id, session.id
                )
            )
            toolkit = ResourceToolkit(
                entries=entries, tools=tools, skills_or_loaders=skills
            )
            await toolkit.aclose()
            return tools

        try:
            assert len(await snapshot()) == 1
            assert clients[0].name == "ontology"
            assert clients[0]._runtime_headers == {
                "Authorization": "user-token-alice"
            }
            assert "user-token-alice" not in clients[0].model_dump_json()
            user = await f.auth.store.user(uid)
            await f.auth.store.save_nvwa_token(user, "rotated-token", 60)
            assert len(await snapshot()) == 1
            assert len(clients) == 2
            assert clients[1]._runtime_headers == {
                "Authorization": "rotated-token"
            }
            await f.auth.store.delete_nvwa_token(uid)
            assert await snapshot() == []
            assert len(clients) == 2
        finally:
            await f.app.state.resource_service.close()
            await workspace.close()

    f.portal.call(exercise)


def test_skill_names_and_full_packages_remain_usable_by_source_prompt(fixture):
    f = fixture
    uid, _ = f.sso("alice")
    skill = f.portal.call(f.storage.list_skills, uid)[0]
    source = f.portal.call(f.storage.get_skill, "wx", "source-skill")
    assert skill.name == source.name == "ontology"
    assert skill.artifact_id == source.artifact_id
    assert skill.id != source.id


def test_name_conflicts_do_not_overwrite_personal_resources(fixture):
    f = fixture
    user = f.portal.call(f.auth.ensure_sso_user, "alice")
    own = MCPRecord(
        id="personal-mcp",
        user_id=user["id"],
        client=MCPClient(
            name="ontology",
            is_stateful=False,
            mcp_config=HttpMCPConfig(url="http://personal.test/mcp"),
        ),
    )
    f.portal.call(f.storage.upsert_mcp, user["id"], own)
    with pytest.raises(HTTPException) as conflict:
        f.portal.call(f.templates.ensure, user["id"])
    assert conflict.value.status_code == 409
    assert (
        f.portal.call(
            f.storage.get_mcp, user["id"], own.id
        ).client.mcp_config.url
        == "http://personal.test/mcp"
    )
    assert f.portal.call(f.storage.list_agents, user["id"]) == []
    assert f.portal.call(f.storage.list_skills, user["id"]) == []
