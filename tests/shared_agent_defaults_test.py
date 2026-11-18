"""Shared defaults through both real storage backends, HTTP and ChatService."""
from functools import partial
import tempfile
from unittest import TestCase
from unittest.mock import patch

import fakeredis.aioredis
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agentscope.agent import Agent
from agentscope.app import create_app
from agentscope.app.access import ResourceAccessPolicyBase, ResourceKind, ResourceRef
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentData, AgentRecord, AsyncSQLAlchemyStorage, ChatModelConfig,
    EmbeddingModelConfig, KnowledgeBaseData, KnowledgeBaseRecord,
    RedisStorage, SessionConfig, SessionKnowledgeConfig,
)
from agentscope.app.storage._model._agent_settings import AgentDefaults
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.permission import PermissionMode
from utils import MockModel


class SharingPolicy(ResourceAccessPolicyBase):
    def __init__(self):
        self.agent_shared = True
        self.credential_shared = True
        self.knowledge_shared = False

    async def list_accessible(self, viewer_id, kind, storage):
        if viewer_id != "viewer":
            return []
        if kind == ResourceKind.AGENT and self.agent_shared:
            return [ResourceRef(kind=kind, owner_id="owner", resource_id="shared")]
        if kind == ResourceKind.CREDENTIAL and self.credential_shared:
            return [ResourceRef(kind=kind, owner_id="owner", resource_id="credential")]
        if kind == ResourceKind.KNOWLEDGE_BASE and self.knowledge_shared:
            return [ResourceRef(kind=kind, owner_id="owner", resource_id="knowledge")]
        return []


class FakeRedisStorage(RedisStorage):
    async def __aenter__(self):
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        return self


class SharedDefaultsCases:
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.policy = SharingPolicy()
        self.observed = []
        observed = self.observed

        class ObservedAgent(Agent):
            def __init__(self, **kwargs):
                observed.append(kwargs["state"].permission_context.mode)
                super().__init__(**kwargs)

        self.app = create_app(
            storage=self.make_storage(), message_bus=InMemoryMessageBus(),
            workspace_manager=LocalWorkspaceManager(root),
            resource_access_policy=self.policy, custom_agent_cls=ObservedAgent,
            enable_index_worker=False, enable_scheduler=False,
        )
        self.client = self.enterContext(TestClient(self.app))
        self.storage = self.app.state.storage
        self.portal = self.client.portal
        self.headers = {"X-User-ID": "viewer"}
        self.model = ChatModelConfig(
            type="openai_credential", credential_id="credential",
            model="shared-model", parameters={},
        )
        self.agent = AgentRecord(
            id="shared", user_id="owner",
            data=AgentData(name="Shared test agent", defaults=AgentDefaults(
                chat_model_config=self.model, permission_mode="bypass",
            )),
        )
        self.portal.call(self.storage.upsert_credential, "owner",
                         OpenAICredential(id="credential", name="Fixture", api_key="test-only-key"))
        self.portal.call(self.storage.upsert_agent, "owner", self.agent)

    def create(self, **fields):
        response = self.client.post("/sessions/", headers=self.headers,
                                    json={"agent_id": "shared", **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session_id"]

    def read(self, sid):
        return self.portal.call(self.storage.get_session, "viewer", "shared", sid)

    def update(self, sid, **fields):
        return self.client.patch(f"/sessions/{sid}", params={"agent_id": "shared"},
                                 headers=self.headers, json=fields)

    def test_create_list_read_and_runtime_use_shared_defaults(self):
        response = self.client.post(
            "/sessions/", headers=self.headers,
            json={"agent_id": "shared", "name": "Stable test name"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        sid = created["session_id"]
        initial = created["view"]
        self.assertEqual(initial["session"]["id"], sid)
        self.assertEqual(initial["status"], "idle")
        session = self.read(sid)
        self.assertEqual(session.user_id, "viewer")
        self.assertEqual(session.config.chat_model_config, self.model)
        self.assertEqual(session.state.permission_context.mode, PermissionMode.BYPASS)
        self.assertIsNone(self.portal.call(self.storage.get_agent, "viewer", "shared"))
        self.assertIsNone(self.portal.call(self.storage.get_session, "owner", "shared", sid))
        listing = self.client.get("/sessions/", params={"agent_id": "shared"}, headers=self.headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        listed = listing.json()["sessions"][0]["session"]
        self.assertEqual(initial["session"]["config"], listed["config"])
        self.assertEqual(initial["session"]["state"]["permission_context"]["mode"], "bypass")
        self.assertEqual(listed["config"]["chat_model_config"]["model"], "shared-model")
        self.assertEqual(listed["state"]["permission_context"]["mode"], "bypass")

        # Exercise real AgentScope assembly/reply/persistence with a deterministic
        # model transport. Credentials still go through the actual access policy.
        async def model_for(viewer, config, access):
            await access.resolve_credential(viewer, config.credential_id)
            self.assertEqual((viewer, config.model), ("viewer", "shared-model"))
            model = MockModel(context_size=1000000)
            model.set_responses([ChatResponse(content=[TextBlock(text="shared runtime ok")], is_last=True)])
            return model

        with patch("agentscope.app._service._chat.get_model", side_effect=model_for):
            self.portal.call(partial(self.app.state.chat_service.run, user_id="viewer",
                                     agent_id="shared", session_id=sid, input_msg=UserMsg("viewer", "hello")))
        self.assertEqual(self.observed, [PermissionMode.BYPASS])
        messages, _ = self.portal.call(self.storage.list_messages, "viewer", sid)
        reply = messages[-1]
        self.assertEqual(reply.finished_reason.value, "completed")
        self.assertIn("shared runtime ok", reply.get_text_content())

    def test_default_changes_apply_but_overrides_and_nulls_survive(self):
        inherited = self.create()
        overridden = self.create(chat_model_config={**self.model.model_dump(), "model": "local-choice"})
        cleared = self.create(chat_model_config=None)
        self.assertEqual(self.update(overridden, permission_mode="explore").status_code, 200)
        snapshot = self.read(inherited)
        self.agent.data.defaults.chat_model_config.model = "updated-model"
        self.agent.data.defaults.permission_mode = PermissionMode.ACCEPT_EDITS
        self.portal.call(self.storage.upsert_agent, "owner", self.agent)
        current = self.read(inherited)
        self.assertEqual(current.config.chat_model_config.model, "updated-model")
        self.assertEqual(current.state.permission_context.mode, PermissionMode.ACCEPT_EDITS)
        self.assertEqual(snapshot.config.chat_model_config.model, "shared-model")
        self.assertEqual(snapshot.state.permission_context.mode, PermissionMode.BYPASS)
        self.assertEqual(self.read(overridden).config.chat_model_config.model, "local-choice")
        self.assertEqual(self.read(overridden).state.permission_context.mode, PermissionMode.EXPLORE)
        self.assertIsNone(self.read(cleared).config.chat_model_config)
        reset = self.update(overridden, inherit_defaults=["chat_model_config", "permission_mode"])
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["config"]["chat_model_config"]["model"], "updated-model")
        self.assertEqual(reset.json()["state"]["permission_context"]["mode"], "accept_edits")

    def test_agent_sharing_does_not_share_credentials(self):
        self.policy.credential_shared = False
        rejected = self.client.post("/sessions/", headers=self.headers, json={"agent_id": "shared"})
        self.assertEqual(rejected.status_code, 404, rejected.text)
        self.assertEqual(self.portal.call(self.storage.list_sessions, "viewer", "shared"), [])
        sid = self.create(chat_model_config=None)
        reset = self.update(sid, inherit_defaults=["chat_model_config"])
        self.assertEqual(reset.status_code, 404, reset.text)
        self.assertNotIn("chat_model_config", self.read(sid).config.inherited_fields)

    def test_revocation_never_resolves_latest_private_defaults_or_credentials(self):
        sid = self.create()
        self.policy.agent_shared = False
        self.agent.data.defaults.chat_model_config.model = "private-change"
        self.portal.call(self.storage.upsert_agent, "owner", self.agent)
        self.assertIsNone(self.portal.call(self.storage.resolve_session_agent, "viewer", "shared"))
        self.assertEqual(self.read(sid).config.chat_model_config.model, "shared-model")
        self.assertEqual(self.client.get("/sessions/", params={"agent_id": "shared"}, headers=self.headers).status_code, 404)
        self.policy.credential_shared = False
        with self.assertRaises(HTTPException):
            self.portal.call(self.app.state.resource_access_service.resolve_credential, "viewer", "credential")

    def test_knowledge_defaults_require_independent_sharing_grant(self):
        self.portal.call(self.storage.upsert_knowledge_base, "owner", KnowledgeBaseRecord(
            id="knowledge", user_id="owner", data=KnowledgeBaseData(
                name="Fixture knowledge", collection_name="fixture",
                embedding_model_config=EmbeddingModelConfig(type="openai_credential",
                    credential_id="credential", model="fixture-embedding", dimensions=3),
            ),
        ))
        self.agent.data.defaults.knowledge_config = SessionKnowledgeConfig(knowledge_base_ids=["knowledge"])
        self.portal.call(self.storage.upsert_agent, "owner", self.agent)
        rejected = self.client.post("/sessions/", headers=self.headers, json={"agent_id": "shared"})
        self.assertEqual(rejected.status_code, 404, rejected.text)
        sid = self.create(knowledge_config=None)
        self.assertEqual(self.update(sid, inherit_defaults=["knowledge_config"]).status_code, 404)
        self.assertIsNone(self.read(sid).config.knowledge_config)
        self.policy.knowledge_shared = True
        self.assertEqual(self.update(sid, inherit_defaults=["knowledge_config"]).status_code, 200)
        for session_id in (sid, self.create()):
            self.assertEqual(self.read(session_id).config.knowledge_config.knowledge_base_ids, ["knowledge"])

    def test_storage_entry_points_inherit_and_legacy_sessions_stay_explicit(self):
        background = self.portal.call(self.storage.upsert_session, "viewer", "shared", SessionConfig(workspace_id="background"))
        self.assertEqual(background.config.chat_model_config.model, "shared-model")
        self.assertEqual(background.state.permission_context.mode, PermissionMode.BYPASS)
        legacy = self.portal.call(self.storage.upsert_session, "viewer", "shared",
                                  SessionConfig(workspace_id="legacy", inherited_fields=[], chat_model_config=None))
        self.assertIsNone(self.read(legacy.id).config.chat_model_config)
        self.assertEqual(self.read(legacy.id).state.permission_context.mode, PermissionMode.DEFAULT)


class SharedDefaultsRedisTest(SharedDefaultsCases, TestCase):
    @staticmethod
    def make_storage():
        return FakeRedisStorage()


class SharedDefaultsSQLTest(SharedDefaultsCases, TestCase):
    @staticmethod
    def make_storage():
        return AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:")
