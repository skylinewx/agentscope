"""Report-only assembly and publication with synthetic captured values."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER, saved_fixture
from analysis_runtime_fixtures import resources_for
from analysis_extension.bootstrap import create_analysis_middlewares
from analysis_extension.models import AnalysisError


class ReportSkillTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = SimpleNamespace(workdir=str(self.root / 'workspace'))
        self.config = self.root / 'analysis-config.local.json'
        self.app = SimpleNamespace(state=SimpleNamespace(
            resource_access_service=SimpleNamespace(get_resource=AsyncMock()),
            storage=SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(
                user_id=OWNER.user_id, agent_id=OWNER.agent_id)))))

    async def create(self):
        return await create_analysis_middlewares(self.app, OWNER.user_id, OWNER.agent_id,
                                                OWNER.session_id, self.workspace, self.config)

    async def test_skill_capability_matrix(self):
        shared = {'QueryResultRead', 'AnalysisBuild', 'AnalysisInspect', 'AnalysisPublish'}
        all_tools = shared | {'AnalysisFollowUp', 'AnalysisRenderReport'}
        cases = [(names, all_tools) for names in ((), ('budget-ontology-mcp-v2',), ('data-analysis',),
                                                 ('report-generation',), ('renamed-method', 'renamed-report'))]
        for names, expected in cases:
            with self.subTest(skills=names):
                resources_for(self.app, names)
                middlewares = await self.create()
                actual = {tool.name for middleware in middlewares for tool in await middleware.list_tools()}
                self.assertEqual(actual, expected)
                if middlewares:
                    capabilities = (await middlewares[0].service.inspect(section='protocol'))['deliveryCapabilities']
                    self.assertEqual(capabilities, {'cards': True, 'generateReports': True, 'followUp': True})

    async def test_report_only_build_publish_render_without_analysis_operations(self):
        resources_for(self.app, ('report-generation',))
        self.config.write_text(json.dumps({'schemaVersion': 1, 'capabilities': {'analysis': False}}))
        middleware = (await self.create())[0]
        service = middleware.service
        capture, selectors, scope = saved_fixture(service.store, values=('12', '-3', '0'), unit='')
        result = await service.build([capture], selectors, scope, [], 'report-input')
        self.assertEqual(result['derivedValues'], [])
        artifact = service.store.get_analysis(OWNER, result['analysisRef'])
        refs = [fact['factId'] for fact in artifact['facts']]
        scope_id = result['scope']['scopeId']
        views = [
            {'viewId': 'values', 'scopeId': scope_id, 'type': 'BAR_VALUES', 'valueRefs': refs},
            {'viewId': 'details', 'scopeId': scope_id, 'type': 'TABLE', 'valueRefs': refs, 'collapsed': True},
        ]
        published = await service.publish(result['analysisRef'], [], views, 'report-publish', result['baseRevision'])
        exported = await service.render_report(result['analysisRef'], 'report-html')
        self.assertTrue(published['delivery']['registered'])
        self.assertTrue(exported['registered'])
        self.assertEqual(exported, await service.render_report(result['analysisRef'], 'report-html'))
        saved = service.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(saved['plan']['operations'], [])
        self.assertEqual(saved['findings'], [])
        self.assertEqual(saved['actions'], [])
        html = service.delivery.report(result['analysisRef'], exported['exportRef'])['html']
        self.assertIn('<svg', html)
        self.assertIn('合成成员 1', html)
        self.assertIn('-3', html)
        self.assertNotIn('未注明', html)
        self.assertEqual(service.store.list_action_runs(OWNER), [])
        self.assertEqual(self.app.state.resource_service.effective.await_count, 2)
        with self.assertRaises(AnalysisError) as denied:
            await service.follow_up('action-unavailable', 'followup')
        self.assertEqual(denied.exception.code, 'ACTIONS_DISABLED')

    async def test_report_selection_and_integrity_checks(self):
        for state in ('unselected', 'disabled', 'foreign', 'unbound', 'edited', 'no-hash'):
            with self.subTest(state=state):
                resources, package, mounted, bindings = resources_for(self.app, ('report-generation',))
                binding = bindings[mounted[0].dir]
                if state == 'unselected': binding.bound = False
                elif state == 'disabled': binding.enabled = False
                elif state == 'foreign': package.user_id = 'other'
                elif state == 'unbound': bindings.clear()
                elif state == 'edited': resources._skill_hash.return_value = 'changed'
                elif state == 'no-hash': binding.artifact_id = None
                if state in ('edited', 'no-hash'):
                    with self.assertRaises(AnalysisError) as error:
                        await self.create()
                    self.assertEqual(error.exception.code, 'SKILL_VERSION_MISMATCH')
                else:
                    self.assertEqual((await self.create())[0].service.skill_versions, {})

    async def test_global_disable_also_disables_report_only_assembly(self):
        resources, *_ = resources_for(self.app, ('report-generation',))
        self.config.write_text(json.dumps({'schemaVersion': 1, 'activation': 'disabled'}), 'utf-8')
        self.assertEqual(await self.create(), [])
        resources.effective.assert_not_awaited()
