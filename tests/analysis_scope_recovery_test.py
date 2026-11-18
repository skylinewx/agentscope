"""筛选背景与公式目标分离、摘要后续建及发布冲突的完整流程回归。"""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.completion import formula_target_refs
from analysis_extension.models import AnalysisError
from analysis_extension.store import AnalysisStore
from analysis_extension.turn_completion import TurnCompletion, current_turn
from agentscope.message import UserMsg
from agentscope.state import AgentState


class ScopeRecoveryTests(PresentationCase):
    async def build_population(self):
        capture, selections, scope = fixture(values=tuple(str(value) for value in range(1, 10)) + ('-3', '-5'))
        capture.update(serverName='fixture-budget', connectionFingerprint='authorized-fixture')
        receipt = json.loads(capture['response']['content'][0]['text'])
        for index, step in enumerate(receipt['data']['stepResults'][1:]):
            observation = step['formResults'][0]['runtime']['observation']
            observation['entryType'] = 'INPUT_VO' if index >= 9 else 'FOR_GRID'
            receipt['evidence'][index]['fact']['observation'] = deepcopy(observation)
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        cap = self.store.capture(OWNER, capture)
        for selection in selections:
            selection['captureRef'] = cap
        scope['population']['captureRef'] = cap
        self.inputs = ([cap], selections, scope)
        initial = await self.service.build(*self.inputs, [], 'population')
        facts = self.store.get_analysis(OWNER, initial['analysisRef'])['facts']
        self.refs = [fact['factId'] for fact in facts]
        operations = [{'id': 'formula-' + str(index), 'operator': 'FORMULA_CHECK', 'inputRefs': [ref]}
                      for index, ref in enumerate(self.refs[-2:])]
        self.work = [
            {'taskId': 'screen', 'kind': 'VALUES', 'title': '从全部对象中识别异常对象', 'refs': self.refs},
            {'taskId': 'explain', 'kind': 'COMPUTATION', 'operator': 'FORMULA_CHECK',
             'title': '核对选中对象的构成', 'refs': ['formula-0', 'formula-1']},
        ]
        built = await self.service.build(*self.inputs, operations, 'selected', workPlan=self.work,
                                         baseAnalysisRef=initial['analysisRef'])
        return self.store.get_analysis(OWNER, built['analysisRef'])

    def attach_turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '分析所选异常对象')))
        turn.task(mode='ANALYSIS', goal='分析所选异常对象', delivery='DOCUMENT', requiredEvidence='FORMULAS')
        self.service.turn = turn
        return turn

    def views(self, artifact):
        return [{'viewId': 'details', 'type': 'TABLE', 'scopeId': artifact['scope']['scopeId'],
                 'valueRefs': self.refs[-2:], 'showUnit': True}]

    async def preview(self, artifact, key='preview'):
        return await self.service.publish(artifact['analysisRef'], [], self.views(artifact), key,
                                           previewOnly=True)

    async def test_only_selected_two_require_formula_evidence_and_publish(self):
        artifact = await self.build_population()
        turn = self.attach_turn()
        _, issues, completion = turn.data_issues(artifact)
        self.assertEqual(issues, [])
        self.assertEqual(completion['state'], 'READY')
        self.assertEqual(formula_target_refs(artifact), set(self.refs[-2:]))
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        preview = await self.preview(artifact)
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        self.assertNotIn('hostSummary', self.store.get_analysis(OWNER, published['analysisRef']))

    async def test_explicit_unfinished_target_is_not_hidden_by_completed_subset(self):
        artifact = await self.build_population()
        turn = self.attach_turn()
        artifact['plan']['workPlan'][1]['targetRefs'] = [self.refs[0], *self.refs[-2:]]
        _, issues, completion = turn.data_issues(artifact)
        missing = next(issue for issue in issues if issue['code'] == 'FORMULA_DATA_REQUIRED')
        self.assertEqual(missing['missingFactRefs'], [self.refs[0]])
        self.assertTrue(missing['requiresBuild'])
        self.assertEqual(completion['items'][1]['missingTargetRefs'], [self.refs[0]])

    async def test_other_computation_targets_do_not_expand_formula_scope(self):
        artifact = await self.build_population()
        artifact['plan']['workPlan'].append({'taskId': 'ranking', 'kind': 'COMPUTATION',
            'operator': 'RANK', 'targetRefs': self.refs, 'refs': []})
        self.assertEqual(formula_target_refs(artifact), set(self.refs[-2:]))

    async def test_work_refs_and_downstream_calculation_use_actual_formula_inputs(self):
        artifact = await self.build_population()
        artifact['derivedValues'].append({'derivedId': 'derived_compare', 'operator': 'ARITHMETIC',
                                         'inputRefs': ['derived_formula-1']})
        artifact['workRefs'] = [{'taskId': 'explain', 'refs': ['derived_compare']}]
        self.assertEqual(formula_target_refs(artifact), {self.refs[-1]})
        # 显式声明的另一目标仍需完成，workRefs 不能缩掉它。
        artifact['plan']['workPlan'][1]['targetRefs'] = self.refs[-2:]
        self.assertEqual(formula_target_refs(artifact), set(self.refs[-2:]))

    async def test_true_missing_formula_stops_before_review_and_routes_to_build(self):
        artifact = await self.build_population()
        self.attach_turn()
        artifact['plan']['workPlan'][1]['targetRefs'] = [self.refs[0], *self.refs[-2:]]
        # 保存缺证据草稿，走真实发布入口，不只测试错误分类函数。
        artifact = self.store.build(OWNER, {'test': 'missing'}, artifact, 'missing', artifact['analysisRef'])
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        with self.assertRaises(AnalysisError) as caught:
            await self.preview(artifact)
        details = caught.exception.details
        self.assertEqual(caught.exception.code, 'FORMULA_DATA_REQUIRED')
        self.assertTrue(details['requiresBuild'])
        self.assertEqual(details['nextAction'], 'BUILD_WITH_REQUIRED_INPUTS')
        self.assertEqual(details['recoveryAction'], 'LOAD_FORM_DATA_AND_FORMULAS')
        self.assertTrue(self.store.get_publication_proposal(OWNER, details['repairRef'])['viewPlan'])
        self.service.preview_reviewer.assert_not_awaited()

    async def test_values_alone_cannot_satisfy_formula_analysis(self):
        artifact = await self.build_population()
        turn = self.attach_turn()
        artifact['plan']['workPlan'] = artifact['plan']['workPlan'][:1]
        # 本场景只有数值；保留已完成的公式检查会使它仍然具有构成证据。
        artifact['derivedValues'] = []
        _, issues, _ = turn.data_issues(artifact)
        # 概览并不要求全部对象补公式；先由模型明确需要解释的目标。
        self.assertEqual([issue['code'] for issue in issues], ['FORMULA_TARGETS_REQUIRED'])
        self.assertFalse(issues[0]['requiresSourceQuery'])

    async def test_rebuild_old_draft_after_fallback_uses_published_base(self):
        artifact = await self.build_population()
        fallback = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_LIMIT')
        built = await self.service.build(*self.inputs, [], 'continue', baseAnalysisRef=artifact['analysisRef'])
        self.assertGreater(built['revision'], fallback['revision'])
        self.attach_turn()
        continued = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(continued['facts'], artifact['facts'])
        self.assertEqual(continued['derivedValues'], artifact['derivedValues'])
        preview = await self.preview(continued)
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(published['publicationState'], 'PUBLISHED')

    async def test_saved_preview_resumes_existing_stale_draft_without_recalculation(self):
        artifact = await self.build_population()
        stale = self.store.build(OWNER, {'test': 'parallel-draft'}, deepcopy(artifact), 'stale', artifact['analysisRef'])
        preview = await self.preview(stale, 'before-summary')
        fallback = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_LIMIT')
        # 首次确认遇到摘要发布的并发变化；后续动作应是重试预览，不是改写报告。
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        details = caught.exception.details
        self.assertEqual(details['nextAction'], 'RETRY_PREVIEW')
        self.assertFalse(details['requiresBuild'])
        self.attach_turn()
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        resumed = await self.service.publish(**details['retryPreview'])
        draft = self.store.get_analysis(OWNER, resumed['analysisRef'])
        self.assertNotEqual(resumed['analysisRef'], stale['analysisRef'])
        self.assertEqual(draft['baseRevision'], fallback['revision'])
        self.assertEqual(draft['facts'], stale['facts'])
        self.assertEqual(draft['derivedValues'], stale['derivedValues'])
        saved = self.store.get_publication_proposal(OWNER, resumed['repairRef'])
        self.assertEqual(saved['viewPlan'], self.views(artifact))
        published = await self.service.publish(**resumed['confirmPublish'])
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        # 幂等重试不重复创建版本；重启存储实例后发布结果仍然存在。
        self.assertEqual(published, await self.service.publish(**resumed['confirmPublish']))
        reopened = AnalysisStore(self.store.root).get_analysis(OWNER, published['analysisRef'])
        self.assertEqual(reopened['publicationState'], 'PUBLISHED')

    async def test_new_formal_report_conflict_is_detected_before_review(self):
        artifact = await self.build_population()
        stale = self.store.build(OWNER, {'test': 'sibling'}, deepcopy(artifact), 'sibling', artifact['analysisRef'])
        published = await self.service.publish(artifact['analysisRef'], [], self.views(artifact), 'release')
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        with self.assertRaises(AnalysisError) as caught:
            await self.preview(stale)
        details = caught.exception.details
        self.assertEqual(caught.exception.code, 'VERSION_CONFLICT')
        self.assertEqual(details['latestPublishedAnalysisRef'], published['analysisRef'])
        self.assertTrue(details['requiresBuild'])
        self.assertEqual(details['nextAction'], 'REBUILD_FROM_LATEST_PUBLISHED')
        self.service.preview_reviewer.assert_not_awaited()
        self.assertEqual(self.store.get_publication_proposal(OWNER, details['repairRef'])['viewPlan'], self.views(artifact))

    async def test_another_branch_summary_cannot_be_silently_overwritten(self):
        artifact = await self.build_population()
        sibling = deepcopy(artifact)
        sibling['baseAnalysisRef'] = artifact['analysisRef']
        sibling = self.store.build(OWNER, {'test': 'sibling'}, sibling, 'sibling', artifact['analysisRef'])
        await self.service.publish_available_facts(sibling['analysisRef'], 'REPAIR_LIMIT')
        with self.assertRaises(AnalysisError) as caught:
            self.store.continuation_revision(OWNER, artifact['analysisRef'])
        self.assertEqual(caught.exception.details['nextAction'], 'REBUILD_FROM_LATEST_PUBLISHED')

    async def test_reviewed_analysis_fallback_is_not_treated_as_plain_data_summary(self):
        artifact = await self.build_population()
        summary = deepcopy(artifact)
        summary.update(baseAnalysisRef=artifact['analysisRef'], hostSummary={'content': 'REVIEWED_ANALYSIS'})
        summary = self.store.build(OWNER, {'test': 'reviewed'}, summary, 'reviewed', artifact['analysisRef'])
        self.store.publish(OWNER, summary['analysisRef'], summary, {'test': 'publish'}, 'reviewed-release', 0)
        with self.assertRaises(AnalysisError):
            self.store.continuation_revision(OWNER, artifact['analysisRef'])
