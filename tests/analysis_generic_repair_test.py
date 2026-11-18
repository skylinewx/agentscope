"""通用修复边界：显示名称、有限补做、捕获补证及空网格导航。"""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.identity import display_names
from analysis_extension.models import AnalysisError
from analysis_extension.presentation_options import prepare_views
from analysis_extension.query_results import read_receipt
from analysis_extension.review_control import record_repair, record_review_issue, closeout_reason, progress
from analysis_extension.review_evidence import supplement


class GenericDisplayTests(unittest.TestCase):
    def test_code_fallback_uses_resolved_name_without_mutating_facts(self):
        base = dict(objectId='member-1', sourceObjectId='A', objectModelCode='model-1',
                    sourceOrigins=['source-1'], period='period-1', objectLabelState='RESOLVED')
        facts = [{**base, 'factId': 'a', 'objectLabel': 'A'},
                 {**base, 'factId': 'b', 'objectLabel': '测试成员'}]
        before = deepcopy(facts)
        self.assertEqual(display_names(facts, 'ORG'), {'a': '测试成员', 'b': '测试成员'})
        self.assertEqual(facts, before)
        for field in ('objectId', 'objectModelCode', 'sourceOrigins', 'period'):
            other = deepcopy(facts)
            other[1][field] = ['other'] if field == 'sourceOrigins' else 'other'
            self.assertNotIn('a', display_names(other, 'ORG'))

    def test_conflicting_explicit_aliases_remain_errors(self):
        facts = [dict(factId=ref, objectId='a', sourceOrigins=['s']) for ref in ('f1', 'f2')]
        artifact = dict(facts=facts, scope={'memberDimension': 'ORG'}, viewPlan=[],
                        presentation={'labels': [dict(valueRef=ref, role='object', label=ref) for ref in ('f1', 'f2')]})
        with self.assertRaises(AnalysisError):
            prepare_views(artifact)

    def test_empty_cells_preserves_empty_result_and_guides_to_real_page(self):
        _, selectors, _ = fixture()
        capture, _, _ = fixture()
        receipt = json.loads(capture['response']['content'][0]['text'])
        path = '/data/stepResults/1/formResults/0/cells'
        result = read_receipt(receipt, 'capture', pointer=path)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['relatedRead']['pointer'], path[:-6] + '/page/cells')


class GenericRepairTests(PresentationCase):
    def turn(self):
        return SimpleNamespace(service=self.service, state={'turnKey': 'generic-repair', 'request': '分析', 'goal': '分析'})

    def test_semantic_repair_has_own_quota_and_shared_clock(self):
        turn = self.turn()
        for index in range(3):
            record_repair(turn, str(index), 'INVALID_WORK_REF')
        record_repair(turn, 'review-1', 'PUBLICATION_REVISE')
        self.assertIsNone(closeout_reason(turn))
        record_repair(turn, 'review-1', 'PUBLICATION_REVISE')
        self.assertEqual(progress(turn)['repairCounts'], {'parameter': 3, 'semantic': 1})
        record_repair(turn, 'last-param', 'INVALID_SELECTOR')
        self.assertEqual(closeout_reason(turn), 'REPAIR_LIMIT')

    def test_repair_time_budget_survives_a_new_turn_instance(self):
        with patch('analysis_extension.review_control.time.time', return_value=1000):
            record_repair(self.turn(), 'parameter-1', 'INVALID_SELECTOR')
        expired = 1000 + self.store.limits.repair_budget_seconds + 1
        with patch('analysis_extension.review_control.time.time', return_value=expired):
            self.assertEqual(closeout_reason(self.turn()), 'REPAIR_TIME_LIMIT')

    def test_issue_ledger_uses_existing_evidence_without_locking_severity(self):
        turn = self.turn()
        material = {'documentText': '来源值与重算值不一致。', 'candidateAnswer': '',
                    'facts': [{'factId': 'source'}], 'derived': [{'derivedId': 'calculation'}],
                    'supportingEvidence': {'facts': [{'factId': 'support'}]}}
        issue = {'code': 'CONTRADICTION', 'message': '请说明差额', 'severity': 'ERROR',
                 'statement': '来源值与重算值不一致。',
                 'evidenceRefs': ['source', 'support', 'calculation', 'invented']}
        record_review_issue(turn, issue, 'first-material', material)
        record_review_issue(turn, issue, 'first-material', material)
        record_review_issue(self.turn(), {**issue, 'severity': 'ADVISORY', 'message': '当前已说明差额'},
                            'second-material', material)
        ledger = list(progress(self.turn())['reviewIssueHistory'].values())
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['evidenceRefs'], ['calculation', 'source', 'support'])
        self.assertEqual([item['severity'] for item in ledger[0]['observations']], ['ERROR', 'ADVISORY'])

    async def test_supplement_is_same_observation_and_does_not_change_scope(self):
        capture, selectors, scope = fixture()
        receipt = json.loads(capture['response']['content'][0]['text'])
        for step in receipt['data']['stepResults'][1:]:
            cell = deepcopy(step['formResults'][0]['page']['cells'][0])
            cell.update(key='3:1', row=3, headers=['测试成本'], rawValueJson='"25"', displayValue='25')
            step['formResults'][0]['page']['cells'].append(cell)
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        ref = self.store.capture(OWNER, capture)
        for selector in selectors:
            selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        result = await self.service.build([ref], selectors, scope, [], 'supplement')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        artifact['facts'] = artifact['facts'][:1]
        before = deepcopy(artifact)
        evidence = supplement(self.turn(), artifact, '测试成本为25元。')
        self.assertEqual(len(evidence['facts']), 1)
        self.assertEqual(evidence['facts'][0]['value'], '25')
        self.assertEqual(evidence['facts'][0]['observationId'], artifact['facts'][0]['observationId'])
        self.assertEqual(artifact, before)
        self.assertEqual(supplement(self.turn(), artifact, '其他业务')['facts'], [])
