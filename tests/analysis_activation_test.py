"""Host capability assembly and HTTP routes use the same access decision."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
import mcp.types

from analysis_fixtures import OWNER
from analysis_action_fixtures import ActionCase
from analysis_extension.bootstrap import create_analysis_middlewares
from analysis_extension.models import AnalysisError
from analysis_extension.router import create_analysis_router
from analysis_extension.capture import leased_mcp_origin
from analysis_extension.adapters import default_sources
from agentscope.app._service._resources import _LeasedTool
from agentscope.tool._adapters import MCPTool


from analysis_runtime_fixtures import resources_for


class AutoActivationTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "analysis-config.local.json"
        self.workspace = SimpleNamespace(workdir=str(self.root / "workspace"))
        self.app = SimpleNamespace(state=SimpleNamespace(
            resource_access_service=SimpleNamespace(get_resource=AsyncMock()),
            storage=SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id=OWNER.user_id, agent_id=OWNER.agent_id)))))
        self.resources, self.package, self.skills, self.bindings = resources_for(self.app)

    async def create(self):
        return await create_analysis_middlewares(self.app, OWNER.user_id, OWNER.agent_id, OWNER.session_id, self.workspace, self.config)

    def configure(self, **kwargs):
        self.config.write_text(json.dumps({"schemaVersion": 1, **kwargs}), "utf-8")

    async def test_host_exposes_analysis_and_reports_independently_of_skill_names(self):
        result = await self.create()
        self.assertEqual(len(result), 1, "A selected analysis Skill should assemble without an agent whitelist")
        middleware = result[0]
        self.assertEqual([t.name for t in await middleware.list_tools()],
            ["QueryResultRead", "AnalysisBuild", "AnalysisInspect", "AnalysisPublish", "AnalysisFollowUp", "AnalysisRenderReport"])
        self.assertTrue(middleware.service.delivery.cards_enabled)
        self.assertTrue(middleware.service.delivery.allow_reports)
        self.assertEqual(middleware.service.skill_versions, {"data-analysis": "installed-v1"})
        self.assertEqual(middleware.capture.resource_ids, frozenset())
        self.assertFalse(self.config.exists())

    async def test_registered_skill_upgrade_needs_no_config_hash_change(self):
        self.configure()
        self.package.artifact_id = "installed-v2"
        self.bindings[self.skills[0].dir].artifact_id = "installed-v2"
        self.resources._skill_hash.return_value = "installed-v2"
        result = await self.create()
        self.assertEqual(len(result), 1, "A newly mounted package should not require copying its hash into config")
        self.assertEqual(result[0].service.skill_versions, {"data-analysis": "installed-v2"})
        self.assertNotIn("skillVersions", json.loads(self.config.read_text()))

    async def test_unselected_disabled_unbound_or_foreign_skill_is_not_recorded(self):
        self.bindings[self.skills[0].dir].bound = False
        self.assertEqual((await self.create())[0].service.skill_versions, {})
        self.bindings[self.skills[0].dir].bound = True
        self.bindings[self.skills[0].dir].enabled = False
        self.assertEqual((await self.create())[0].service.skill_versions, {})
        self.bindings[self.skills[0].dir].enabled = True
        self.package.user_id = "different-user"
        self.assertEqual((await self.create())[0].service.skill_versions, {})
        self.package.user_id = OWNER.user_id
        self.bindings.clear()
        self.assertEqual((await self.create())[0].service.skill_versions, {})
        self.assertTrue((self.root / "analysis-data").exists())

    async def test_global_disable_stops_assembly(self):
        self.configure(activation="disabled")
        self.assertEqual(await self.create(), [])
        self.resources.effective.assert_not_awaited()

    async def test_removed_config_fields_are_rejected_instead_of_ignored(self):
        for config in ({"activation": "manual"}, {"activation": "read-only"}, {"agents": {}},
                       {"defaults": {}}, {"resourceIds": []}, {"skillVersions": {}}, {"capture": True}):
            with self.subTest(config=config):
                self.configure(**config)
                with self.assertRaises(AnalysisError):
                    await self.create()
        self.resources.effective.assert_not_awaited()

    async def test_changed_or_duplicate_package_fails_closed(self):
        self.resources._skill_hash.return_value = "unregistered-edit"
        with self.assertRaisesRegex(AnalysisError, "不一致"):
            await self.create()
        self.resources._skill_hash.return_value = "installed-v1"
        self.configure()
        self.skills.append(SimpleNamespace(name="data-analysis (6)", dir="duplicate"))
        self.bindings["duplicate"] = self.bindings[self.skills[0].dir]
        with self.assertRaisesRegex(AnalysisError, "重复"):
            await self.create()

    async def test_missing_installed_hash_cannot_fall_back_to_library_hash(self):
        self.bindings[self.skills[0].dir].artifact_id = None
        with self.assertRaises(AnalysisError) as failure:
            await self.create()
        self.assertEqual(failure.exception.code, "SKILL_VERSION_MISMATCH")

    async def test_renamed_skill_retains_capabilities_and_integrity_checks(self):
        before = {tool.name for tool in await (await self.create())[0].list_tools()}
        self.package.name = "custom-analysis"
        after = {tool.name for tool in await (await self.create())[0].list_tools()}
        self.assertEqual(before, after)
        self.resources._skill_hash.return_value = "unregistered-change"
        with self.assertRaises(AnalysisError):
            await self.create()

    async def test_auto_capture_requires_real_leased_tool_and_supported_contract(self):
        definition = mcp.types.Tool(name="execute_semantic_plan", inputSchema={"properties": {"contractVersion": {"const": "2.0"}}})
        inner = MCPTool("configured-service", definition, session=SimpleNamespace())
        tool = _LeasedTool(inner, SimpleNamespace(resource_id="selected", fingerprint="connection"))
        allowed = frozenset({"selected"})
        self.assertIsNone(default_sources().for_tool(tool, leased_mcp_origin(tool, allowed)))
        definition.meta = {"version": "2.0", "exactContractResource": "semantic://contracts/external-query"}
        self.assertEqual(leased_mcp_origin(tool, allowed)["resourceId"], "selected")
        self.assertEqual(default_sources().for_tool(tool, leased_mcp_origin(tool, allowed)).adapter_id, "budget-v2")
        self.assertIsNone(leased_mcp_origin(inner, allowed))
        self.assertIsNone(leased_mcp_origin(tool, frozenset({"other"})))
        definition.meta["exactContractResource"] = "unrelated://v2"
        self.assertIsNone(default_sources().for_tool(tool, leased_mcp_origin(tool, allowed)))

    async def test_only_bound_enabled_owned_resources_are_selected(self):
        self.resources.mcps = [
            (SimpleNamespace(kind="mcp", resource_id=name + "-id", enabled=name != "disabled"),
             SimpleNamespace(user_id="other" if name == "foreign" else OWNER.user_id)
             if name != "deleted" else None)
            for name in ("available", "disabled", "foreign", "deleted")
        ]
        result = (await self.create())[0]
        self.assertEqual(result.capture.resource_ids, frozenset({"available-id"}))
        self.app.state.storage.get_mcp.assert_not_awaited()
        for call in self.resources.effective.await_args_list:
            self.assertEqual(call.args, (OWNER.user_id, OWNER.agent_id, OWNER.session_id))

    async def test_access_and_session_checks_remain_required(self):
        self.app.state.resource_access_service.get_resource.side_effect = HTTPException(403, "revoked")
        with self.assertRaises(HTTPException):
            await self.create()
        self.app.state.resource_access_service.get_resource.side_effect = None
        self.app.state.storage.get_session.return_value.user_id = "other"
        with self.assertRaises(AnalysisError):
            await self.create()
        self.resources.effective.assert_not_awaited()


class AutoDeliveryTests(ActionCase):
    async def asyncSetUp(self):
        await self.initial()
        self.config = self.root / "analysis-config.local.json"
        self.config.write_text(json.dumps({"schemaVersion": 1, "rules": self.rules.rules}), "utf-8")
        self.app = FastAPI()
        async def session(user, agent, sid):
            return SimpleNamespace(user_id=user, agent_id=agent) if (user, agent, sid) == (OWNER.user_id, OWNER.agent_id, OWNER.session_id) else None
        self.app.state.storage = SimpleNamespace(get_session=session)
        self.app.state.resource_access_service = SimpleNamespace(get_resource=AsyncMock())
        self.app.state.workspace_service = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(workdir=str(self.root / "workspace"))))
        self.resources, self.package, self.skills, self.bindings = resources_for(self.app, ("data-analysis", "report-generation"))
        self.app.include_router(create_analysis_router(self.config))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://fixture")
        self.addAsyncCleanup(self.client.aclose)
        self.params = {"agent_id": OWNER.agent_id, "session_id": OWNER.session_id}
        self.headers = {"X-User-ID": OWNER.user_id}

    async def view(self):
        return await self.client.get("/workspace/analyses/view", params={**self.params, "path": self.path}, headers=self.headers)

    async def followup(self):
        return await self.client.post("/workspace/analyses/resolve-action", params=self.params, headers=self.headers, json={"actionRef": self.action["actionRef"]})

    async def test_auto_ui_report_and_action_match_runtime_without_whitelist(self):
        view = await self.view()
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["capabilities"], {"cards": True, "generateReports": True, "followUp": True})
        self.assertEqual((await self.followup()).status_code, 200)
        rendered = await self.client.post("/workspace/analyses/render-report", params=self.params, headers=self.headers,
            json={"analysisRef": self.ref, "requestKey": "automatic-report"})
        self.assertEqual(rendered.status_code, 200)
        self.assertEqual(self.store.list_action_runs(OWNER), [])

    async def test_skill_removal_does_not_revoke_host_but_selected_package_edit_is_rejected(self):
        report = self.delivery.render_report(self.ref, "current-report")
        for state in ("removed", "disabled", "edited"):
            with self.subTest(state=state):
                for binding in self.bindings.values():
                    binding.bound = state != "removed"
                    binding.enabled = state != "disabled"
                self.resources._skill_hash.return_value = "changed" if state == "edited" else "installed-v1"
                view = await self.view()
                expected_status = 409 if state == "edited" else 200
                self.assertEqual(view.status_code, expected_status)
                if state == "edited":
                    self.assertEqual(view.json()["detail"]["code"], "SKILL_VERSION_MISMATCH")
                self.assertEqual((await self.followup()).status_code, expected_status)
                download = await self.client.get("/workspace/analyses/report", headers=self.headers,
                    params={**self.params, "analysis_ref": self.ref, "export_ref": report["exportRef"]})
                self.assertEqual(download.status_code, expected_status)
        self.resources._skill_hash.return_value = "installed-v1"
        self.assertEqual((await self.followup()).status_code, 200)

    async def test_report_only_http_can_generate_without_followup(self):
        self.config.write_text(json.dumps({"schemaVersion": 1, "rules": self.rules.rules, "capabilities": {"analysis": False}}))
        self.bindings[self.skills[0].dir].bound = False
        view = await self.view()
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["capabilities"], {"cards": True, "generateReports": True, "followUp": False})
        rendered = await self.client.post("/workspace/analyses/render-report", params=self.params, headers=self.headers,
            json={"analysisRef": self.ref, "requestKey": "report-only"})
        self.assertEqual(rendered.status_code, 200)
        self.assertEqual((await self.followup()).json()["detail"]["code"], "ACTIONS_DISABLED")

    async def test_host_report_disable_keeps_analysis(self):
        self.config.write_text(json.dumps({"schemaVersion": 1, "rules": self.rules.rules, "capabilities": {"reports": False}}))
        self.bindings[self.skills[1].dir].bound = False
        view = await self.view()
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["capabilities"], {"cards": True, "generateReports": False, "followUp": True})
        rendered = await self.client.post("/workspace/analyses/render-report", params=self.params, headers=self.headers,
            json={"analysisRef": self.ref, "requestKey": "disabled-report"})
        self.assertEqual(rendered.json()["detail"]["code"], "REPORTS_DISABLED")
        self.assertEqual((await self.followup()).status_code, 200)

    async def test_auto_mode_does_not_allow_cross_session_or_revoked_access(self):
        response = await self.client.get("/workspace/analyses/view", params={**self.params, "session_id": "other", "path": self.path}, headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.app.state.resource_access_service.get_resource.side_effect = HTTPException(403, "revoked")
        self.assertEqual((await self.view()).status_code, 403)
        self.resources.effective.assert_not_awaited()
