"""Regression for source-scoped grouping, request identity, revisions and review partitions."""
from copy import deepcopy
import json
from unittest.mock import patch

from analysis_budget_batch_test import batch_fixture
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.capture import host_request
from analysis_extension.grouping_policy import grouping_issues
from analysis_extension.models import Owner
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn, analysis_review_payloads
from agentscope.message import UserMsg
from agentscope.state import AgentState


class GroupingRecoveryTests(PresentationCase):
    def test_no_grouping_does_not_load_unrelated_source_receipts(self):
        turn = self.turn()
        turn.state['queryCaptureRefs'] = ['unrelated-source']
        with patch.object(self.store, 'get_capture', side_effect=AssertionError('无需读取来源')):
            self.assertEqual(grouping_issues(turn), [])

    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '按口径分析')))
        turn.task(mode='ANALYSIS', goal='按已确认口径分析', delivery='DOCUMENT')
        self.service.turn = turn
        return turn

    def discovery(self, turn, keyword, field, binding='binding-A'):
        capture, _, _, receipt = batch_fixture()
        capture.update(originalToolName='discover_query_targets', request={'bindingIri': binding, 'groupingKeyword': keyword})
        receipt['data'] = {'resolutionKind': 'GROUPING', 'bindingIri': binding, 'status': 'SUPPORTED',
                           'metadataComplete': True, 'organizationType': 'category-A',
                           'candidates': [{'supported': True, 'groupBy': {'dimCode': 'MD_ORG', 'field': field}}]}
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        turn._add_query_capture(ref)
        return ref

    def test_unused_discovery_does_not_block_selected_native_aggregation(self):
        turn = self.turn()
        chosen = self.discovery(turn, '分类一', 'MD_YWBK')
        other = self.discovery(turn, '分类二', 'OTHER_CATEGORY')
        self.assertEqual(grouping_issues(turn), [])
        capture, _, _, _ = batch_fixture()
        turn._add_query_capture(self.store.capture(OWNER, capture))
        self.assertEqual(grouping_issues(turn), [])
        from analysis_extension.grouping_policy import grouping_requirements
        flags = {row['captureRef']: row['required'] for row in grouping_requirements(turn)}
        self.assertEqual(flags, {chosen: True, other: False})

    def test_two_required_groupings_are_not_satisfied_by_only_one(self):
        turn = self.turn()
        first = self.discovery(turn, '分类一', 'MD_YWBK')
        second = self.discovery(turn, '分类二', 'OTHER_CATEGORY')
        turn.task(mode='ANALYSIS', goal='分别按两个分类分析', groupingCaptureRefs=[first, second])
        capture, _, _, _ = batch_fixture()
        turn._add_query_capture(self.store.capture(OWNER, capture))
        self.assertEqual([item['discoveryCaptureRef'] for item in grouping_issues(turn)], [second])
        # 重复登记但未提供选择，不得丢弃已有的第二个要求。
        turn.task(mode='ANALYSIS', goal='分别按两个分类分析')
        self.assertEqual([item['discoveryCaptureRef'] for item in grouping_issues(turn)], [second])

    def test_explicit_selection_can_replace_explored_candidate_without_cross_binding(self):
        turn = self.turn()
        old = self.discovery(turn, '未采用分类', 'OTHER_CATEGORY')
        selected = self.discovery(turn, '当前分类', 'MD_YWBK')
        foreign = self.discovery(turn, '当前分类', 'MD_YWBK', binding='binding-B')
        turn.task(mode='ANALYSIS', goal='按当前分类分析', groupingCaptureRefs=[old])
        turn.task(mode='ANALYSIS', goal='按当前分类分析', groupingCaptureRefs=[selected, foreign])
        capture, _, _, _ = batch_fixture()
        turn._add_query_capture(self.store.capture(OWNER, capture))
        self.assertEqual([item['discoveryCaptureRef'] for item in grouping_issues(turn)], [foreign])
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError):
            turn.task(mode='ANALYSIS', goal='无效选择', groupingCaptureRefs=['unknown-capture'])
        self.assertEqual(turn.state, before)

    async def test_supported_grouping_needs_native_result_from_same_binding_and_source(self):
        turn = self.turn()
        capture, selection, scope, receipt = batch_fixture()
        discovery = deepcopy(capture)
        discovery.update(originalToolName='discover_query_targets', request={'bindingIri': 'binding-A', 'groupingKeyword': '类别'})
        data = {'resolutionKind': 'GROUPING', 'bindingIri': 'binding-A', 'status': 'SUPPORTED', 'metadataComplete': True,
                'organizationType': 'category-A', 'candidates': [{'supported': True, 'groupBy': {'dimCode': 'MD_ORG', 'field': 'MD_YWBK'}}]}
        discovery['response']['content'][0]['text'] = json.dumps({**receipt, 'data': data})
        discovery_ref = self.store.capture(OWNER, discovery)
        turn._add_query_capture(discovery_ref)
        turn.task(mode='ANALYSIS', goal='按已确认口径分析', groupingCaptureRefs=[discovery_ref])
        self.assertEqual(grouping_issues(turn)[0]['code'], 'GROUPING_RESULT_REQUIRED')
        unrelated = deepcopy(capture)
        unrelated['resourceId'] = 'other-source'
        turn._add_query_capture(self.store.capture(OWNER, unrelated))
        self.assertEqual(len(grouping_issues(turn)), 1)
        actual = self.store.capture(OWNER, capture)
        turn._add_query_capture(actual)
        self.assertEqual(grouping_issues(turn), [])
        # 真实会话及后台恢复会经历 JSON 序列化，来源身份仍应按值匹配。
        turn = TurnCompletion(self.service, json.loads(json.dumps(turn.state)))
        self.assertEqual(grouping_issues(turn), [])
        # 另建只含普通明细的产物，不能借用未选入的聚合回执宣布完成。
        self.assertEqual(len(grouping_issues(turn, {'captureRefs': [discovery_ref]})), 1)

    async def test_metadata_incomplete_can_finish_with_evidence_but_not_fake_success(self):
        turn = self.turn()
        capture, _, _, receipt = batch_fixture()
        capture.update(originalToolName='discover_query_targets', request={'bindingIri': 'binding-A', 'groupingKeyword': '类别'})
        receipt['data'] = {'resolutionKind': 'GROUPING', 'bindingIri': 'binding-A', 'status': 'INCOMPLETE',
                           'metadataComplete': False, 'diagnostics': ['当前表样结构缺失'], 'candidates': []}
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        turn._add_query_capture(ref)
        result = turn.task(action='blocked', reason='当前元数据未完整，无法确认分组', evidenceCaptureRefs=[ref])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(turn.check()['status'], 'BLOCKED')

    async def test_same_turn_rebuild_retains_goal_work_and_calculations(self):
        turn = self.turn()
        ref, selections, scope = saved_fixture(self.store, ('10', '20'))
        first = await self.service.build([ref], selections, scope, [{'id': 'n', 'operator': 'COUNT'}], 'first',
            workPlan=[{'taskId': 'distribution', 'kind': 'COMPUTATION', 'title': '分布统计', 'refs': ['derived_n']}])
        turn.state['analysisRef'] = first['analysisRef']
        again = await self.service.build([ref], selections, scope, [], 'again')
        artifact = self.store.get_analysis(OWNER, again['analysisRef'])
        self.assertEqual(artifact['baseAnalysisRef'], first['analysisRef'])
        self.assertEqual(again['completion']['state'], 'READY')
        self.assertEqual(again['derivedValues'][0]['value'], '2')
        reused = await self.service.build([ref], selections, scope, [], 'again')
        self.assertEqual(reused['analysisRef'], again['analysisRef'])

    def test_host_identity_is_stable_for_retries_and_isolated_by_turn_owner_and_content(self):
        origin = {'resourceId': 'source', 'originalToolName': 'execute_semantic_plan'}
        request = {'contractVersion': '2.0', 'requestId': 'model-key', 'year': 2026}
        first = host_request(OWNER, 'turn1', origin, request)['requestId']
        self.assertEqual(first, host_request(OWNER, 'turn1', origin, {**request, 'requestId': 'another-model-key'})['requestId'])
        for owner, turn, changed in [(OWNER, 'turn2', request), (OWNER, 'turn1', {**request, 'year': 2027}),
                                     (Owner(OWNER.user_id, OWNER.agent_id, 'other'), 'turn1', request)]:
            self.assertNotEqual(first, host_request(owner, turn, origin, changed)['requestId'])

    def test_invalid_intent_does_not_change_goal_and_source_failure_keeps_capture(self):
        turn = self.turn()
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError):
            turn.task(mode='ANALYSIS', goal='无效目标', intent={'unexpected': '字段'})
        self.assertEqual(turn.state, before)
        capture, _, _, _ = batch_fixture()
        capture['originalToolName'] = 'discover_query_targets'
        capture['response']['content'][0]['text'] = 'source connection failed'
        capture['captureState'] = 'error'
        ref = self.store.capture(OWNER, capture)
        turn._add_query_capture(ref)
        self.assertIn(ref, turn.state['queryCaptureRefs'])
        self.assertIn(ref, turn.state['groupingDiagnostics'])

    def test_analysis_partitions_preserve_every_fact_and_complete_dependencies(self):
        # 相同标题已可无损共享；用不同的真实背景强制覆盖容量分段分支。
        facts = [{'factId': 'f'+str(index), 'value': str(index), 'headers': ['x' * 300 + str(index)]} for index in range(30)]
        payload = {'userRequest': '分析全部', 'facts': facts, 'relations': [],
                   'derived': [{'derivedId': 'd1', 'inputRefs': ['f0', 'f29'], 'value': '29'}]}
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 2000):
            from analysis_extension.review_encoding import unpack_review_records
            encoded = analysis_review_payloads(payload)
            parts = [unpack_review_records(json.loads(text)) for text in encoded]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(text) <= 2000 for text in encoded))
        self.assertEqual({fact['factId'] for part in parts for fact in part['facts']}, {fact['factId'] for fact in facts})
        for part in parts:
            if part['derived']:
                self.assertTrue({'f0', 'f29'} <= {fact['factId'] for fact in part['facts']})
