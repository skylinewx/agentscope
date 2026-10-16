# -*- coding: utf-8 -*-
"""Agent and conversation resources through HTTP, storage and runtime."""

import asyncio
from pathlib import Path
from unittest import TestCase

from tests import resource_router_test as fixtures
from tests.service_resources_test import FakeMCP
from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourceRef,
)

HEADERS = fixtures.HEADERS


class ResourceScopesTest(TestCase):
    setUp = fixtures.ResourceRouterTest.setUp
    start = fixtures.ResourceRouterTest.start
    seed_library = fixtures.ResourceRouterTest.seed_library
    listing = fixtures.ResourceRouterTest.listing

    def setup_scopes(self, client):
        mcp = client.get("/mcp", headers=HEADERS).json()[0]
        skill = client.get("/skill", headers=HEADERS).json()[0]
        config = {
            "mcps": {mcp["id"]: {"enabled": True}},
            "skills": {skill["id"]: {"enabled": True}},
        }
        response = client.post(
            "/agent/",
            headers=HEADERS,
            json={"name": "agent", "resource_config": config},
        )
        self.assertEqual(response.status_code, 201, response.text)
        aid = response.json()["agent_id"]
        scopes = []
        for _ in range(2):
            response = client.post(
                "/sessions/", headers=HEADERS, json={"agent_id": aid}
            )
            self.assertEqual(response.status_code, 201, response.text)
            scopes.append(
                {"agent_id": aid, "session_id": response.json()["session_id"]}
            )
        return mcp, skill, scopes

    def switch(self, client, scope, kind, rid, enabled):
        response = client.patch(
            f"/workspace/{kind}/{rid}",
            params=scope,
            headers=HEADERS,
            json={"enabled": enabled},
        )
        self.assertEqual(response.status_code, 204, response.text)

    def agent_config(self, client, aid):
        return next(
            a
            for a in client.get("/agent/", headers=HEADERS).json()["agents"]
            if a["id"] == aid
        )["data"]["resource_config"]

    def save_agent(self, client, aid, config):
        response = client.patch(
            f"/agent/{aid}", headers=HEADERS, json={"resource_config": config}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_dynamic_defaults_overrides_reset_and_rebind(self):
        with self.start() as client:
            mcp, skill, (a, b) = self.setup_scopes(client)
            for kind, record in [("mcp", mcp), ("skill", skill)]:
                self.assertNotIn("enabled", record)
                self.assertEqual(
                    self.listing(client, kind, a)[0]["binding_sources"],
                    ["agent"],
                )
                self.switch(client, a, kind, record["id"], False)
                self.assertFalse(self.listing(client, kind, a)[0]["enabled"])
                self.assertTrue(self.listing(client, kind, b)[0]["enabled"])
            config = self.agent_config(client, a["agent_id"])
            for field in ["mcps", "skills"]:
                next(iter(config[field].values()))["enabled"] = False
            self.save_agent(client, a["agent_id"], config)
            for kind, record in [("mcp", mcp), ("skill", skill)]:
                self.assertFalse(self.listing(client, kind, b)[0]["enabled"])
                self.switch(client, a, kind, record["id"], True)
                self.assertTrue(self.listing(client, kind, a)[0]["enabled"])
                self.switch(client, a, kind, record["id"], None)
                self.assertFalse(self.listing(client, kind, a)[0]["enabled"])
                self.switch(client, a, kind, record["id"], True)
            self.save_agent(client, a["agent_id"], {"mcps": {}, "skills": {}})
            self.assertEqual(self.listing(client, "mcp", a), [])
            self.save_agent(client, a["agent_id"], config)
            self.assertFalse(self.listing(client, "mcp", a)[0]["enabled"])
            self.assertIsNone(
                self.listing(client, "mcp", a)[0]["session_override"]
            )

    def test_local_addition_dedup_remove_and_user_isolation(self):
        with self.start() as client:
            mcp, _, (a, b) = self.setup_scopes(client)
            self.switch(client, a, "mcp", mcp["id"], False)
            response = client.post(
                "/workspace/mcp/from-library",
                params=a,
                headers=HEADERS,
                json={"mcp_ids": [mcp["id"]]},
            )
            self.assertFalse(response.json()["failed"])
            rows = self.listing(client, "mcp", a)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["binding_sources"], ["agent", "session"])
            self.assertTrue(rows[0]["enabled"])
            self.assertEqual(
                client.delete(
                    f"/workspace/mcp/{mcp['id']}", params=a, headers=HEADERS
                ).status_code,
                204,
            )
            self.assertEqual(
                self.listing(client, "mcp", a)[0]["binding_sources"], ["agent"]
            )
            response = client.patch(
                f"/workspace/mcp/{mcp['id']}",
                params=b,
                headers={"X-User-ID": "bob"},
                json={"enabled": False},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                client.patch(
                    f"/mcp/{mcp['id']}",
                    headers=HEADERS,
                    json={"enabled": False},
                ).status_code,
                422,
            )

    def test_runtime_isolation_skill_copies_and_leased_mcp(self):
        with self.start() as client:
            mcp, skill, (a, b) = self.setup_scopes(client)

            async def check():
                service = client.app.state.resource_service
                workspace_service = client.app.state.workspace_service
                ws = await workspace_service.resolve(
                    "alice", a["agent_id"], a["session_id"]
                )
                ws.create_mcp_client = (
                    lambda spec, **kw: FakeMCP.model_validate(
                        spec.model_dump()
                    )
                )
                ta, sa, ea = await service.snapshot(
                    "alice", ws, a["agent_id"], a["session_id"]
                )
                tb, sb, eb = await service.snapshot(
                    "alice", ws, b["agent_id"], b["session_id"]
                )
                self.assertNotEqual(sa[0].dir, sb[0].dir)
                self.assertIsNot(ea[0], eb[0])
                Path(sa[0].dir, "scripts/run.py").write_text(
                    "local edit", encoding="utf-8"
                )
                self.assertEqual(
                    Path(sb[0].dir, "scripts/run.py").read_text("utf-8"),
                    "version one",
                )
                for kind, record in [("mcp", mcp), ("skill", skill)]:
                    await service.change_session_resource(
                        "alice",
                        a["agent_id"],
                        a["session_id"],
                        kind,
                        record["id"],
                        False,
                    )
                tools, skills, entries = await service.snapshot(
                    "alice", ws, a["agent_id"], a["session_id"]
                )
                self.assertEqual((tools, skills, entries), ([], [], []))
                self.assertTrue(ea[0].client.is_connected)
                self.assertTrue(Path(sa[0].dir).exists())
                await ea[0].release()
                self.assertFalse(ea[0].client.is_connected)
                await eb[0].release()

            client.portal.call(check)

    def test_concurrent_config_and_state_updates_are_preserved(self):
        with self.start() as client:
            mcp, skill, (a, _) = self.setup_scopes(client)

            async def check():
                storage = client.app.state.storage
                service = client.app.state.resource_service

                def rename(session):
                    session.config.name = "renamed"

                await asyncio.gather(
                    storage.mutate_session(
                        "alice", a["agent_id"], a["session_id"], rename
                    ),
                    service.change_session_resource(
                        "alice",
                        a["agent_id"],
                        a["session_id"],
                        "mcp",
                        mcp["id"],
                        False,
                    ),
                    service.change_session_resource(
                        "alice",
                        a["agent_id"],
                        a["session_id"],
                        "skill",
                        skill["id"],
                        False,
                    ),
                )
                saved = await storage.get_session(
                    "alice", a["agent_id"], a["session_id"]
                )
                self.assertEqual(saved.config.name, "renamed")
                self.assertFalse(
                    saved.config.resource_config.mcp_overrides[
                        mcp["id"]
                    ].enabled
                )
                self.assertFalse(
                    saved.config.resource_config.skill_overrides[
                        skill["id"]
                    ].enabled
                )

            client.portal.call(check)

    def test_shared_agent_requires_explicit_resource_use_grants(self):
        with self.start() as client:
            mcp, skill, (a, _) = self.setup_scopes(client)

            class Policy(ResourceAccessPolicyBase):
                allow_resources = False

                async def list_accessible(self, viewer_id, kind, storage):
                    if viewer_id != "bob":
                        return []
                    ids = {ResourceKind.AGENT: a["agent_id"]}
                    if self.allow_resources:
                        ids.update(
                            {
                                ResourceKind.MCP: mcp["id"],
                                ResourceKind.SKILL: skill["id"],
                            }
                        )
                    return (
                        [
                            ResourceRef(
                                kind=kind,
                                owner_id="alice",
                                resource_id=ids[kind],
                            )
                        ]
                        if kind in ids
                        else []
                    )

            policy = Policy()
            client.app.state.resource_access_service._policy = policy
            bob = {"X-User-ID": "bob"}
            response = client.post(
                "/sessions/", headers=bob, json={"agent_id": a["agent_id"]}
            )
            self.assertEqual(response.status_code, 201, response.text)
            scope = {
                "agent_id": a["agent_id"],
                "session_id": response.json()["session_id"],
            }
            for kind in ("mcp", "skill"):
                row = client.get(
                    f"/workspace/{kind}", headers=bob, params=scope
                ).json()[0]
                self.assertFalse(row["available"])
                self.assertFalse(row["effective_enabled"])
            self.assertEqual(
                client.patch(
                    f"/agent/{a['agent_id']}",
                    headers=bob,
                    json={"resource_config": {}},
                ).status_code,
                403,
            )
            policy.allow_resources = True
            for kind in ("mcp", "skill"):
                row = client.get(
                    f"/workspace/{kind}", headers=bob, params=scope
                ).json()[0]
                self.assertTrue(row["available"])
                self.assertTrue(row["effective_enabled"])
                self.assertNotIn("private-key", str(row))

    def test_missing_resource_invalid_binding_and_active_run_update(self):
        with self.start() as client:
            mcp, _, (a, b) = self.setup_scopes(client)
            response = client.post(
                "/agent/",
                headers=HEADERS,
                json={
                    "name": "invalid",
                    "resource_config": {
                        "mcps": {"missing": {"enabled": True}}
                    },
                },
            )
            self.assertEqual(response.status_code, 404)
            from unittest.mock import AsyncMock, patch

            with patch.object(
                client.app.state.message_bus,
                "is_locked",
                new=AsyncMock(return_value=True),
            ):
                self.switch(client, a, "mcp", mcp["id"], False)
                response = client.patch(
                    f"/sessions/{a['session_id']}",
                    params={"agent_id": a["agent_id"]},
                    headers=HEADERS,
                    json={"resource_config": {}},
                )
                self.assertEqual(response.status_code, 200, response.text)
            client.delete(f"/mcp/{mcp['id']}", headers=HEADERS)
            for scope in (a, b):
                row = self.listing(client, "mcp", scope)[0]
                self.assertFalse(row["available"])
                self.assertFalse(row["effective_enabled"])

    def test_delete_session_purges_only_its_resource_copies(self):
        with self.start() as client:
            _, skill, (a, b) = self.setup_scopes(client)
            paths = []
            for scope in (a, b):
                response = client.post(
                    "/workspace/skill/from-library",
                    headers=HEADERS,
                    params=scope,
                    json={"skill_ids": [skill["id"]]},
                )
                self.assertFalse(response.json()["failed"])
                paths.append(
                    Path(self.listing(client, "skill", scope)[0]["dir"])
                )
            response = client.delete(
                f"/sessions/{a['session_id']}",
                headers=HEADERS,
                params={"agent_id": a["agent_id"]},
            )
            self.assertEqual(response.status_code, 204, response.text)
            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())

    def test_skill_switches_change_guidance_without_disabling_host_capabilities(self):
        """Session switches still control packages and MCPs, not host tools."""
        from examples.agent_service.analysis_extension.activation import (
            resolve_activation,
            selected_resource_ids,
        )

        with self.start() as client:
            mcp, _, (a, b) = self.setup_scopes(client)
            response = client.post(
                "/skill",
                headers=HEADERS,
                json={
                    "files": {
                        "SKILL.md": "---\nname: data-analysis\n"
                        "description: Analysis fixture\n---\nBody\n"
                    }
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            record = response.json()
            config = self.agent_config(client, a["agent_id"])
            config["skills"][record["id"]] = {"enabled": True}
            self.save_agent(client, a["agent_id"], config)

            async def current(scope):
                ws = await client.app.state.workspace_service.resolve(
                    "alice", scope["agent_id"], scope["session_id"]
                )
                active = await resolve_activation(
                    client.app,
                    "alice",
                    scope["agent_id"],
                    scope["session_id"],
                    ws,
                    {"schemaVersion": 1},
                )
                ids = await selected_resource_ids(
                    client.app,
                    "alice",
                    scope["agent_id"],
                    scope["session_id"],
                    ws,
                )
                return active, ids

            self.assertTrue(client.portal.call(current, a)[0].actions)
            self.assertTrue(client.portal.call(current, b)[0].actions)
            self.switch(client, a, "skill", record["id"], False)
            self.switch(client, a, "mcp", mcp["id"], False)
            self.assertTrue(client.portal.call(current, a)[0].actions)
            self.assertNotIn(record['id'], client.portal.call(current, a)[0].skill_versions)
            self.assertIn(record['id'], client.portal.call(current, b)[0].skill_versions)
            self.assertEqual(client.portal.call(current, a)[1], [])
            self.assertTrue(client.portal.call(current, b)[0].actions)
            self.assertEqual(client.portal.call(current, b)[1], [mcp["id"]])
            config = self.agent_config(client, a["agent_id"])
            config["skills"][record["id"]]["enabled"] = False
            self.save_agent(client, a["agent_id"], config)
            self.assertTrue(client.portal.call(current, b)[0].actions)
            self.assertNotIn(record['id'], client.portal.call(current, b)[0].skill_versions)
            self.switch(client, a, "skill", record["id"], True)
            self.assertTrue(client.portal.call(current, a)[0].actions)
            self.assertIn(record['id'], client.portal.call(current, a)[0].skill_versions)
