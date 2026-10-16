import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER
from analysis_runtime_fixtures import resources_for
from analysis_extension.bootstrap import create_analysis_middlewares
from analysis_extension.adapters.budget_v2 import declares_v2
from analysis_extension.models import AnalysisError


class BootstrapTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "analysis-config.local.json"
        self.workspace = SimpleNamespace(workdir=str(Path(self.temp.name) / "workspace"))
        self.app = SimpleNamespace(state=SimpleNamespace(
            resource_access_service=SimpleNamespace(get_resource=AsyncMock()),
            storage=SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id=OWNER.user_id, agent_id=OWNER.agent_id)))))

    async def create(self):
        return await create_analysis_middlewares(self.app, OWNER.user_id, OWNER.agent_id, OWNER.session_id, self.workspace, self.path)

    async def test_host_capabilities_are_available_without_selected_skills(self):
        result = await self.create()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].service.skill_versions, {})

    async def test_revoked_or_wrong_agent_session(self):
        resources_for(self.app)
        self.app.state.storage.get_session.return_value.agent_id = "different"
        with self.assertRaisesRegex(AnalysisError, "访问"):
            await self.create()

    async def test_private_store_outside_workspace(self):
        resources_for(self.app)
        self.workspace.workdir = self.temp.name
        with self.assertRaisesRegex(AnalysisError, "工作区"):
            await self.create()

    async def test_unknown_config_mode_fails_closed(self):
        self.path.write_text(json.dumps({"schemaVersion": 1, "activation": "typo"}), "utf-8")
        with self.assertRaises(AnalysisError) as failure:
            await self.create()
        self.assertEqual(failure.exception.code, "INVALID_MODE")

    async def test_oneof_current_mcp_schema_and_mixed_version(self):
        v2 = {"properties": {"contractVersion": {"const": "2.0"}}}
        self.assertTrue(declares_v2({"oneOf": [v2, v2]}))
        self.assertFalse(declares_v2({"oneOf": [v2, {"properties": {"contractVersion": {"const": "3.0"}}}]}))
        self.assertFalse(declares_v2({"properties": {"name": {"const": "budget"}}}))
