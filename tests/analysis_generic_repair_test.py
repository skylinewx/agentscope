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
    def test_group_coverage_includes_separately_captured_sources_and_unknown_coordinates(self):
        def source(name, coordinate):
            return {'factId': name, 'captureRef': name, 'batchId': name, 'formName': name,
                    'measureCode': '数量', 'dimensions': coordinate, 'aggregation': {'groupBy': [{'dimCode': 'D'}]}}

        catalog = {name: [source(name, coordinate)] for name, coordinate in [('一表', {'D': 'a'}), ('二表', {})]}
        service = SimpleNamespace(owner=None, store=SimpleNamespace(get_capture=lambda owner, ref: ref),
                                  sources=SimpleNamespace(catalog=lambda ref, dim: catalog[ref]))
        turn = SimpleNamespace(service=service)
        artifact = {'facts': [items[0] for items in catalog.values()], 'scope': {'memberDimension': 'D'}}
        result = supplement(turn, artifact, '比较两个表样。')
        self.assertEqual(result['groupedSources'], [
            {'业务表': '一表', '返回分组数': 1, '材料已选分组数': 1},
            {'业务表': '二表', '返回分组数': None, '材料已选分组数': None}])
        unknown = supplement(turn, {**artifact, 'facts': catalog['二表']}, '分析二表。')
        self.assertIsNone(unknown['groupedSources'][0]['返回分组数'])

    def test_multi_axis_groups_keep_tuples_typed_nulls_and_missing_evidence(self):
        def fact(ref, site, kind, measure):
            return {'factId':ref, 'batchId':'b', 'formName':'产出', 'captureRef':'cap', 'measureCode':measure,
                    'dimensions':{'SITE':site, 'D__K':kind}, 'aggregation':{'groupBy':[{'dimCode':'SITE'}, {'dimCode':'D','field':'K'}]}}
        base = [fact('f1','A',None,'数量'),fact('f2','A',None,'成本'),fact('f3','A','None','数量'),fact('f4','B',None,'数量')]
        service = SimpleNamespace(owner=None, store=SimpleNamespace(get_capture=lambda *_:{}),
                                  sources=SimpleNamespace(catalog=lambda *_:base))
        turn = SimpleNamespace(service=service)
        def summary(selection):
            packet = supplement(turn,{'scope':{'memberDimension':'SITE'},'facts':selection},'本次分析')
            return packet['groupedSources'][0]
        overview = summary([base[0],base[2]])
        self.assertEqual((overview['返回分组数'],overview['材料已选分组数']), (3,2))
        self.assertEqual(overview['指标采用'], [{'指标':'数量','返回':3,'已选':2},{'指标':'成本','返回':1,'已选':0}])
        extra = fact('f5','A',None,'成本')
        del extra['dimensions']['D__K']
        base.append(extra)
        overview = summary([*base[:2],extra])
        self.assertIsNone(overview['返回分组数'])
        self.assertIsNone(overview['材料已选分组数'])
        self.assertEqual(overview['指标采用'][0]['返回'],3)

    def test_same_capture_grouping_supports_named_other_form_without_expanding_analysis(self):
        primary = {'factId': 'p', 'batchId': 'b1', 'captureRefs': ['cap'], 'formName': '主表',
                   'dimensions': {'ORG__TYPE': 'A'}, 'measureCode': 'quantity', 'label': '数量',
                   'period': '2026N0001', 'taskName': '任务', 'dataContext': {'snapshotId': 's'},
                   'aggregation': {'groupBy': [{'dimCode': 'ORG', 'field': 'TYPE'}]}}
        other = {**primary, 'factId': 'o', 'batchId': 'b2', 'formName': '对照表',
                 'valueState': 'PRESENT', 'value': '12', 'dimensions': {'ORG__TYPE': None}}
        artifact = {'facts': [primary], 'scope': {'memberDimension': 'ORG__TYPE'}}
        service = SimpleNamespace(owner=None,
            store=SimpleNamespace(get_capture=lambda *_: {'captureRef': 'cap'}),
            sources=SimpleNamespace(catalog=lambda *_: [primary, other]))
        turn = SimpleNamespace(service=service)
        before = deepcopy(artifact)
        supported = supplement(turn, artifact, '对照表也返回空属性组。')
        self.assertEqual([f['factId'] for f in supported['facts']], ['o'])
        self.assertEqual(supported['facts'][0]['inputRole'], 'REVIEW_SUPPORT')
        self.assertEqual(artifact, before)
        unnamed = supplement(turn, artifact, '只分析主表。')
        self.assertEqual(unnamed['facts'], [])
        self.assertEqual(unnamed['groupedSources'], [
            {'业务表': '主表', '返回分组数': 1, '材料已选分组数': 1},
            {'业务表': '对照表', '返回分组数': 1, '材料已选分组数': 0}])
        from analysis_extension.review_business_markdown import render_business_markdown
        packet = render_business_markdown({'userRequest': '延续前面的分组', 'supportingEvidence': unnamed}, {})
        self.assertLess(packet.index('延续前面的分组'), packet.index('同次分组查询各来源的采用情况'))
        self.assertLess(packet.index('| 对照表 | 1 | 0 |'), packet.index('二、查询结果'))
        self.assertNotIn('同次查询取得的补充业务数据', packet)
        extra = {**other, 'factId': 'o2', 'measureCode': 'cost', 'label': '成本'}
        service.sources.catalog = lambda *_: [primary, other, extra]
        all_sources = {**artifact, 'facts': [primary, other]}
        partial = supplement(turn, all_sources, '比较所选范围。')
        summary = partial['groupedSources'][1]
        self.assertEqual((summary['返回分组数'], summary['材料已选分组数']), (1,1))
        self.assertEqual(summary['指标采用'], [{'指标':'数量','返回':1,'已选':1},{'指标':'成本','返回':1,'已选':0}])
        complete = supplement(turn, {**all_sources, 'facts': [primary, other, extra]}, '比较所选范围。')
        self.assertEqual([row['材料已选分组数'] for row in complete['groupedSources']], [1,1])
        for key in ('period', 'taskName', 'dataContext'):
            with self.subTest(field=key):
                changed = {**other, key: 'different'}
                service._review_evidence_cache = {}
                service.sources.catalog = lambda *_: [primary, changed]
                self.assertEqual(supplement(turn, artifact, '对照表')['facts'], [])

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
        for index in range(3, self.store.limits.max_analysis_repairs):
            self.assertIsNone(closeout_reason(turn))
            record_repair(turn, f'param-{index}', 'INVALID_SELECTOR')
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
