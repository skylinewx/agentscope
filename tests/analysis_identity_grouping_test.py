"""Native identity grouping must survive host Python, review and publication gates."""
from copy import deepcopy
import json
from unittest.mock import patch

from agentscope.message import UserMsg
from agentscope.state import AgentState
from analysis_budget_batch_test import batch_fixture, update
from analysis_business_query_test import business_fixture
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters import budget_batch
from analysis_extension.grouping_policy import grouping_issues
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn


class IdentityGroupingTests(PresentationCase):
    def fixture(self, fixed=True):
        _, selection, scope, _ = batch_fixture(aggregate=False)
        capture, receipt = business_fixture()
        group = {'dimCode': 'MD_BUDACCOUNT' if fixed else 'MD_ORG'}
        steps = receipt['data']['runtimeRequests']
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        if fixed:
            # Two fixed in-cell members, with no varying dimension outside them.
            ranges = [{'dimCode': 'MD_ORG', 'values': ['07']}]
            steps[-1]['batchQuery']['batchDimensions'] = deepcopy(ranges)
            table['request']['batchDimensions'] = ranges
            table['data']['rows'] = table['data']['rows'][:1]
            table['data']['rowCount'] = 1
            scope['members'] = ['07']
            for result_set in receipt['data']['resultSets']:
                result_set['rows'] = result_set['rows'][:1]
            receipt['data']['origins'] = [origin for origin in receipt['data']['origins'] if origin['rowIndex'] == 0]
        capture['request']['groupBy'] = ['分类']
        receipt['data'].update(complete=True, unresolvedGroups=[], runtimeRequests=steps,
            failureSummary={'issueCount': 0}, grouping=[{'bindingRef': 'binding-A',
            'groupingKeyword': '分类', 'status': 'SUPPORTED', 'metadataComplete': True,
            'candidates': [{'supported': True, 'groupBy': group}]}])
        update(capture, receipt)
        return capture, selection, scope, receipt

    def prepare(self, capture):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '按分类分析数据')))
        self.service.turn = turn
        turn.task(mode='ANALYSIS', goal='按分类分析', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}], intent={'grouping': '分类'})
        ref = self.store.capture(OWNER, capture)
        turn._add_query_capture(ref)
        frozen = self.store.get_capture(OWNER, ref)
        artifact = {'captureRefs': [ref], 'facts': budget_batch.catalog(frozen)}
        return turn, artifact, ref

    def test_fixed_cell_dimension_and_identity_range_are_both_native_results(self):
        for fixed in (True, False):
            with self.subTest(fixed=fixed):
                capture, _, _, _ = self.fixture(fixed)
                turn, artifact, _ = self.prepare(capture)
                self.assertEqual(grouping_issues(turn, artifact), [])
                self.assertEqual(grouping_issues(turn), [])
                restored = TurnCompletion(self.service, json.loads(json.dumps(turn.state)))
                self.assertEqual(grouping_issues(restored, artifact), [])

    def test_selected_subset_is_allowed_without_certifying_business_coverage(self):
        capture, _, _, _ = self.fixture()
        turn, artifact, _ = self.prepare(capture)
        artifact['facts'] = artifact['facts'][:-1]
        self.assertEqual(grouping_issues(turn, artifact), [])
        self.assertNotIn('calculationReview', artifact)

    def test_property_grouping_still_requires_native_aggregation(self):
        capture, _, _, receipt = self.fixture(False)
        receipt['data']['grouping'][0]['candidates'][0]['groupBy']['field'] = 'MD_YWBK'
        update(capture, receipt)
        turn, artifact, _ = self.prepare(capture)
        self.assertTrue(grouping_issues(turn, artifact))

    def test_other_real_dimensions_remain_available_for_code_to_filter(self):
        capture, _, _, receipt = self.fixture()
        ranges = [{'dimCode': 'MD_ORG', 'values': ['07', '05']}]
        receipt['data']['runtimeRequests'][-1]['batchQuery']['batchDimensions'] = deepcopy(ranges)
        receipt['data']['stepResults'][0]['batchResults'][0]['request']['batchDimensions'] = ranges
        update(capture, receipt)
        turn, artifact, _ = self.prepare(capture)
        self.assertEqual(grouping_issues(turn, artifact), [])

    def test_source_context_and_binding_cannot_cross_match(self):
        capture, _, _, _ = self.fixture()
        turn, artifact, _ = self.prepare(capture)
        original = deepcopy(turn.state['groupingRequirements'])
        for field, value in [('bindingRef', 'other-binding'), ('source', ['other-source']),
                             ('context', {'snapshotId': 'other-snapshot'})]:
            with self.subTest(field=field):
                turn.state['groupingRequirements'] = deepcopy(original)
                next(iter(turn.state['groupingRequirements'].values()))[field] = value
                self.assertTrue(grouping_issues(turn, artifact))

    def test_partial_or_failed_discovery_cannot_use_identity_path(self):
        for change in ('partial', 'incomplete', 'unresolved', 'failed_group', 'missing_dimension'):
            with self.subTest(change=change):
                capture, _, _, receipt = self.fixture()
                if change == 'partial': receipt['status'] = 'PARTIAL'
                elif change == 'incomplete': receipt['data']['complete'] = False
                elif change == 'unresolved':
                    receipt['data']['complete'] = False
                    receipt['data']['unresolvedGroups'] = [{'reason': 'incomplete'}]
                elif change == 'failed_group': receipt['data']['grouping'][0]['status'] = 'UNRESOLVED'
                else:
                    receipt['data']['grouping'][0]['candidates'][0]['groupBy'] = {'dimCode': 'MISSING'}
                update(capture, receipt)
                turn, artifact, _ = self.prepare(capture)
                self.assertTrue(grouping_issues(turn, artifact))

    def test_complete_receipt_alone_does_not_invent_a_grouping_decision(self):
        capture, _, _, _ = self.fixture()
        turn, artifact, _ = self.prepare(capture)
        next(iter(turn.state['groupingRequirements'].values()))['candidates'] = [{'dimCode': 'OTHER'}]
        self.assertTrue(grouping_issues(turn, artifact))

    def test_source_timeout_is_reported_as_fetch_failure_not_unknown_dimension(self):
        capture, _, _, receipt = self.fixture()
        receipt['status'] = 'PARTIAL'
        receipt['data'].update(complete=False,
            unresolvedGroups=[{'code': 'SOURCE_UNAVAILABLE', 'message': 'budget request timed out'}],
            failureSummary={'issueCount': 1})
        update(capture, receipt)
        turn, artifact, _ = self.prepare(capture)
        problem = grouping_issues(turn, artifact)[0]
        self.assertEqual(problem['groupingStatus'], 'SUPPORTED')
        self.assertEqual(problem['recoveryAction'], 'RETRY_INCOMPLETE_SOURCE')
        self.assertEqual(problem['sourceIssues'][0]['code'], 'SOURCE_UNAVAILABLE')
        self.assertIn('不要修改', problem['message'])

    async def test_python_uses_complete_identity_data_without_changing_native_facts(self):
        capture, selection, scope, _ = self.fixture()
        turn, _, ref = self.prepare(capture)
        selection['captureRef'] = ref
        built = await self.service.build([ref], [selection], scope, [], 'identity-base')
        before = self.store.get_analysis(OWNER, built['analysisRef'])
        result = await self.service.python(analysisRef=built['analysisRef'], purpose='读取分组明细',
            requestKey='identity-python', code='result=[{"name":"rows","label":"实际行数",'
            '"value":str(len(inputs)),"unit":null,"inputRefs":[r["ref"] for r in inputs]}]'.replace('null', 'None'))
        after = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(after['facts'], before['facts'])
        self.assertEqual(result['derivedValues'][-1]['value'], '2')
        _, issues, _ = turn.data_issues(after)
        self.assertFalse([item for item in issues if item.get('code') == 'GROUPING_RESULT_REQUIRED'])

    async def test_python_still_stops_before_execution_for_missing_native_property_result(self):
        capture, selection, scope, receipt = self.fixture(False)
        receipt['data']['grouping'][0]['candidates'][0]['groupBy']['field'] = 'MD_YWBK'
        update(capture, receipt)
        _, _, ref = self.prepare(capture)
        selection['captureRef'] = ref
        built = await self.service.build([ref], [selection], scope, [], 'property-base')
        with patch('analysis_extension.python_analysis.execute_python', side_effect=AssertionError('must not execute')):
            with self.assertRaises(AnalysisError) as error:
                await self.service.python(analysisRef=built['analysisRef'], purpose='尝试分组', requestKey='property', code='result=[]')
        self.assertEqual(error.exception.code, 'GROUPING_RESULT_REQUIRED')
