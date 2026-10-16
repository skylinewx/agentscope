import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import mcp.types

from fastapi import FastAPI
import httpx

from analysis_action_fixtures import ActionCase
from analysis_runtime_fixtures import resources_for
from analysis_fixtures import OWNER
from analysis_extension.actions import followup_text
from analysis_extension.middleware import AnalysisMiddleware
from analysis_extension.models import AnalysisError
from analysis_extension.router import create_analysis_router
from agentscope.message import Msg, TextBlock
from agentscope.tool._adapters import MCPTool
from agentscope.app._service._resources import _LeasedTool
from analysis_extension.actions import readonly_capability
from analysis_extension.capture import leased_mcp_origin


class FollowupRouteTests(ActionCase):
    async def asyncSetUp(self):
        await self.initial()
        self.config = self.root / "analysis-config.local.json"
        self.config.write_text(json.dumps({"schemaVersion": 1, "rules": self.rules.rules}), "utf-8")
        app = FastAPI()
        async def session(user, agent, session):
            return SimpleNamespace(user_id=user, agent_id=agent) if (user, agent, session) == (OWNER.user_id, OWNER.agent_id, OWNER.session_id) else None
        app.state.storage = SimpleNamespace(get_session=session)
        app.state.resource_access_service = SimpleNamespace(get_resource=AsyncMock())
        app.state.workspace_service = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(workdir=str(self.root / "workspace"))))
        resources_for(app)
        app.include_router(create_analysis_router(self.config))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)
        self.params = {"agent_id": OWNER.agent_id, "session_id": OWNER.session_id}
        self.headers = {"X-User-ID": OWNER.user_id}

    async def post(self, body=None, params=None, headers=None):
        return await self.client.post("/workspace/analyses/resolve-action", json=body or {"actionRef": self.action["actionRef"]}, params=params or self.params, headers=headers or self.headers)

    async def test_resolve_is_readonly_and_returns_exact_normal_message(self):
        before = self.store.list_captures(OWNER)["total"]
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], followup_text(self.action))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.store.list_captures(OWNER)["total"], before)
        self.assertEqual(self.store.list_action_runs(OWNER), [])

    async def test_access_forgery_and_extra_scope_parameters_fail(self):
        for changed in ({"session_id": "other"}, {"agent_id": "other"}):
            response = await self.post(params={**self.params, **changed})
            self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.post(headers={"X-User-ID": "other"})).status_code, 404)
        self.assertEqual((await self.post(body={"actionRef": self.action["actionRef"][:-1] + "x"})).status_code, 409)
        for field in ("sourceId", "scope", "completed", "goal"):
            self.assertEqual((await self.post(body={"actionRef": self.action["actionRef"], field: "override"})).status_code, 422)

    async def test_global_disable_blocks_action_resolution_and_view(self):
        config = json.loads(self.config.read_text("utf-8"))
        config["activation"] = "disabled"
        self.config.write_text(json.dumps(config), "utf-8")
        self.assertEqual((await self.post()).status_code, 409)
        view = await self.client.get("/workspace/analyses/view", params={**self.params, "path": self.path}, headers=self.headers)
        self.assertEqual(view.status_code, 409)
        self.assertEqual(view.json()["detail"]["code"], "ANALYSIS_DISABLED")


class MiddlewareFollowupTests(ActionCase):
    async def test_real_mcp_business_readonly_metadata_without_overriding_permission(self):
        definition = mcp.types.Tool(name="execute_semantic_plan", inputSchema={"properties": {"contractVersion": {"const": "2.0"}}},
            annotations=mcp.types.ToolAnnotations(readOnlyHint=False),
            _meta={"version": "2.0", "exactContractResource": "semantic://contracts/external-query", "risk": "L1",
                   "dataBoundary": "PINNED_SOURCE_SNAPSHOT_ASOF", "evidencePolicy": "EVIDENCE_REQUIRED"})
        tool = _LeasedTool(MCPTool("budget", definition, session=SimpleNamespace()), SimpleNamespace(resource_id="fixture-resource", fingerprint="fixture-connection"))
        origin = leased_mcp_origin(tool, frozenset({"fixture-resource"}))
        capability = readonly_capability(tool, origin)
        self.assertEqual(capability["readOnlyBasis"], "budget-v2-business-readonly-contract")
        self.assertFalse(tool.is_read_only)  # Normal MCP permission check remains intact.
        self.assertIsNone(readonly_capability(tool, leased_mcp_origin(tool, frozenset({"other"}))))
        definition.meta["dataBoundary"] = "ARBITRARY_WRITE"
        self.assertIsNone(readonly_capability(tool, origin))

    async def test_denied_tool_records_failure(self):
        await self.initial()
        await self.start()
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        decision = SimpleNamespace(behavior="deny")
        returned = await middleware.on_check_permission(None, {}, AsyncMock(return_value=decision))
        self.assertIs(returned, decision)
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "FAILED")

    async def test_real_user_turn_binding_and_cancel_lifecycle(self):
        await self.initial()
        middleware = AnalysisMiddleware(self.store, OWNER, ["fixture-resource"], self.service.authorize, actions_enabled=True)
        middleware.service = self.service
        agent = SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[], _get_available_tools=AsyncMock(return_value={})))
        user = Msg(name="user", role="user", content=[TextBlock(text=followup_text(self.action))])
        async def next_handler(**kwargs):
            # No discovered source is available: host must leave this awaiting input.
            response = await self.service.follow_up(self.action["actionRef"], "user-run")
            self.assertEqual(response["actionRun"]["status"], "AWAITING_INPUT")
            yield response
        _ = [event async for event in middleware.on_reply(agent, {"inputs": user}, next_handler)]
        self.assertIsNotNone(self.service.actions.turn_ref)
        self.assertEqual(self.service.actions.user_text, followup_text(self.action))

    async def test_no_user_message_cannot_be_replaced_by_model_memory(self):
        await self.initial()
        middleware = AnalysisMiddleware(self.store, OWNER, ["fixture-resource"], self.service.authorize, actions_enabled=True)
        middleware.service = self.service
        agent = SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[], _get_available_tools=AsyncMock(return_value={})))
        assistant = Msg(name="assistant", role="assistant", content=[TextBlock(text=followup_text(self.action))])
        async def next_handler(**kwargs):
            with self.assertRaises(AnalysisError) as failure:
                await self.service.follow_up(self.action["actionRef"], "fake")
            self.assertEqual(failure.exception.code, "ACTION_INTENT_CHANGED")
            yield None
        _ = [e async for e in middleware.on_reply(agent, {"inputs": assistant}, next_handler)]
        self.assertEqual(self.store.list_action_runs(OWNER), [])

    async def test_host_cancellation_is_not_a_model_completed_parameter(self):
        await self.initial()
        await self.start()
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, actions_enabled=False)
        middleware.service = self.service
        self.service.actions_enabled = False  # run already exists; exercise finally/exception handling
        async def interrupted(**kwargs):
            raise asyncio.CancelledError()
            yield
        with self.assertRaises(asyncio.CancelledError):
            _ = [e async for e in middleware.on_reply(None, {}, interrupted)]
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "CANCELLED")
