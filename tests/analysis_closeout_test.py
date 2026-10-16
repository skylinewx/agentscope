import hashlib
import json
from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from analysis_fixtures import OWNER, fixture
from analysis_extension.service import AnalysisService
from analysis_extension.store import AnalysisStore
from analysis_extension.delivery import AnalysisDelivery
from analysis_extension.engine import RuleRegistry
from analysis_extension.models import AnalysisError
from analysis_extension.formulas import Expression, trace_formula
from analysis_extension.presentation import project, page
from analysis_extension.validation import validate_claims
from analysis_extension.service import compact_derived
from analysis_fixtures import saved_fixture, rules


async def authorized(): pass


def mixed_fixture(store, count=2):
    capture, selectors, scope = fixture(tuple('-5' if i % 2 == 0 else '20' for i in range(count)), unit=None)
    receipt = json.loads(capture['response']['content'][0]['text'])
    for i, step in enumerate(receipt['data']['stepResults'][1:]):
        grid = step['formResults'][0]['page']
        prototype = grid['cells'][0]
        values = ('-5', '10', '15') if i % 2 == 0 else ('20', '50', '30')
        cells = []
        for column, (name, value) in enumerate(zip(('结果', '收入', '成本'), values), 1):
            cell = {**deepcopy(prototype), 'key': f'2:{column}', 'column': column, 'headers': [name],
                    'measureCode': name, 'rawValueJson': json.dumps(value), 'displayValue': value,
                    'dimensions': {**prototype['dimensions'], 'ACCOUNT': name},
                    'formula': 'B1-C1' if column == 1 else None}
            cells.append(cell)
        grid['cells'] = cells + [{'row': 0, 'column': c, 'mapType': 'COL_INDEX', 'displayValue': label}
                                 for c, label in enumerate(('A','B','C'), 1)] + [{'row': 2, 'column': 0, 'mapType': 'ROW_INDEX', 'displayValue': '1'}]
        grid['totalColumns'] = 4
        selectors[i]['cellKeys'] = [f'2:{c}' for c in (1, 2, 3)]
    capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
    ref = store.capture(OWNER, capture)
    for selector in selectors: selector['captureRef'] = ref
    scope['population']['captureRef'] = ref
    return ref, selectors, scope


class CloseoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AnalysisStore(Path(self.temp.name) / 'private')
        self.rules = RuleRegistry()
        self.delivery = AnalysisDelivery(self.store, OWNER, Path(self.temp.name) / 'workspace', self.rules)
        self.service = AnalysisService(self.store, OWNER, authorized, self.rules, delivery=self.delivery)

    def tearDown(self): self.temp.cleanup()

    async def build(self, count=2):
        ref, selectors, scope = mixed_fixture(self.store, count)
        built = await self.service.build([ref], selectors, scope,
            [{'id': 'formula', 'operator': 'FORMULA_CHECK', 'select': {'cellKey': '2:1', 'objectId': 'object-0'}}], 'mixed')
        self.ref = built['analysisRef']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        return built

    async def published(self, count=2, chart=False):
        built = await self.build(count)
        facts = self.artifact['facts']
        scope = self.artifact['scope']['scopeId']
        plans = [{'viewId':'result', 'scopeId':scope, 'type':'TABLE', 'title':'成员结果', 'valueRefs':[f['factId'] for f in facts if f['cellKey']=='2:1']},
                 {'viewId':'components', 'scopeId':scope, 'type':'TABLE', 'title':'构成来源', 'valueRefs':[f['factId'] for f in facts if f['cellKey']!='2:1']},
                 {'viewId':'formula', 'scopeId':scope, 'type':'TABLE', 'valueRefs':['derived_formula']}]
        if chart:
            plans.append({'viewId': 'values', 'scopeId': scope, 'type': 'BAR_VALUES',
                          'valueRefs': [f['factId'] for f in facts if f['cellKey'] == '2:1']})
        published = await self.service.publish(self.ref, [{'claimId':'calculation','scopeId':scope,'kind':'computed','parts':[{'valueRef':'derived_formula'}]}], plans, 'publish', built['baseRevision'])
        if not published['delivery']['registered']: self.delivery.ensure(self.ref)
        self.assertTrue(published['delivery']['registered'], published['delivery'])
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.path = published['delivery']['path']
        return self.delivery.view(self.path)

    async def test_member_filter_topn_and_page_retain_positive_components(self):
        view = await self.published()
        column = next(c['key'] for c in view['columns'] if c['label']=='结果')
        filtered = self.delivery.project_view(self.ref, {'filter':'negative','columnKey':column})
        self.assertEqual((filtered['objectCount'], filtered['total']), (1, 3))
        self.assertEqual([r['exact'] for r in filtered['rows']], ['-5','10','15'])
        self.assertEqual([len(t['rows']) for t in filtered['tables']], [1,1,3])
        first = self.delivery.view(self.path, 0, 1)
        self.assertEqual(len(first['rows']), 3)
        self.assertEqual(first['page']['nextOffset'], 1)
        top = self.delivery.project_view(self.ref, {'topN':1})
        self.assertEqual(len(top['rows']), 3)
        self.assertEqual(view['scopeLabel'], self.artifact['scope']['label'])

    async def test_mixed_columns_sort_explicitly_and_do_not_mix_metrics(self):
        view = await self.published()
        with self.assertRaises(AnalysisError) as failure:
            self.delivery.project_view(self.ref, {'sort':'desc'})
        self.assertEqual(failure.exception.code, 'VIEW_COLUMN_REQUIRED')
        column = next(c['key'] for c in view['columns'] if c['label']=='结果')
        sorted_view = self.delivery.project_view(self.ref, {'sort':'desc','columnKey':column})
        self.assertEqual([r['exact'] for r in sorted_view['rows']], ['20','50','30','-5','10','15'])

    async def test_formula_checks_source_coordinates_and_complete_dependencies(self):
        await self.build()
        derived = self.artifact['derivedValues'][0]
        self.assertEqual(derived['value'], '-5')
        self.assertEqual(derived['formulaTrace']['status'], 'MATCH')
        self.assertEqual(len(derived['sourceFactIds']), 3)
        inspected = await self.service.inspect('formulas', analysisRef=self.ref, factRef=self.artifact['facts'][0]['factId'])
        self.assertEqual(inspected['status'], 'MATCH')
        self.assertEqual(inspected['nodes']['total'], 3)
        self.assertEqual(self.artifact['facts'][0]['cellAddress'], 'A1')

    async def test_formula_missing_cycle_external_and_value_mismatch_are_distinct(self):
        await self.build()
        facts = deepcopy(self.artifact['facts'])
        target = facts[0]['factId']
        missing = trace_formula([f for f in facts if f['cellAddress']!='C1'], target)
        self.assertEqual(missing['status'], 'BLOCKED')
        self.assertIn('C1', [d['address'] for d in missing['pendingDependencies']])
        facts[1]['formula'] = 'A1'
        cyclic = trace_formula(facts, target)
        self.assertTrue(any(d['code']=='FORMULA_CYCLE' for node in cyclic['nodes'] for d in node['diagnostics']))
        facts = deepcopy(self.artifact['facts']); facts[0]['formula'] = 'Other!B1'
        external = trace_formula(facts, target)
        self.assertEqual(external['status'], 'BLOCKED')
        facts[0]['formula'] = 'B1-C1'; facts[0]['value'] = '-6'
        mismatch = trace_formula(facts, target)
        self.assertEqual(mismatch['status'], 'MISMATCH')
        self.assertEqual(mismatch['nodes'][0]['difference'], '-1')

    async def test_claim_narrative_binds_only_model_selected_fields(self):
        await self.build()
        scope = self.artifact['scope']['scopeId']
        ref = self.artifact['facts'][1]['factId']
        bad = [{'claimId':'bad','scopeId':scope,'kind':'fact','parts':[{'text':'结果为：'},{'valueRef':ref}]}]
        narrative = validate_claims(self.artifact, bad)
        self.assertEqual(narrative[0]['rendered'], '结果为：10')
        good = validate_claims(self.artifact, [{**bad[0], 'parts':[{'valueRef':ref}]}])
        self.assertEqual(good[0]['rendered'], '10')
        computed = validate_claims(self.artifact, [{'claimId':'formula-name','scopeId':scope,'kind':'computed','parts':[{'valueRef':'derived_formula'}]}])[0]
        self.assertEqual(computed['rendered'], '-5')

    async def test_formula_gap_keeps_independent_available_branches_and_bounded_summary(self):
        await self.build()
        facts = deepcopy(self.artifact['facts'])
        trace = trace_formula([f for f in facts if f['cellAddress'] != 'C1'], facts[0]['factId'])
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertIn(facts[1]['factId'], [n['factRef'] for n in trace['nodes']])
        compact = compact_derived([self.artifact['derivedValues'][0]])[0]['formulaTrace']
        self.assertEqual(compact['nodesTotal'], 3)
        self.assertNotIn('dependencies', compact['nodes'][0])
        self.assertEqual(compact['detailsSection'], 'formulas')

    async def test_repeated_failed_dependencies_cannot_exceed_trace_capacity(self):
        await self.build()
        facts = deepcopy(self.artifact['facts'][:3])
        facts[0]['formula'] = 'B1+C1+E1'
        facts[1]['formula'] = facts[2]['formula'] = 'D1'
        failed = {**facts[1], 'factId':'fact_failed', 'cellAddress':'D1', 'formula':None, 'valueState':'FAILED', 'value':None}
        other = {**facts[1], 'factId':'fact_other', 'cellAddress':'E1', 'formula':'D1'}
        with patch('analysis_extension.formulas.MAX_NODES', 6):
            trace = trace_formula([*facts, failed, other], facts[0]['factId'])
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertLessEqual(len(trace['nodes']), 6)
        self.assertTrue(any(d['code']=='FORMULA_LIMIT' for node in trace['nodes'] for d in node['diagnostics']))

    async def test_free_explanations_require_specific_missing_evidence(self):
        await self.build()
        claim = {'claimId': 'why', 'scopeId': self.artifact['scope']['scopeId'], 'kind': 'hypothesis',
                 'parts': [{'text': '可能与尚未取得的构成明细有关'}]}
        for missing in (None, [], ['  ']):
            with self.subTest(missing=missing), self.assertRaises(AnalysisError):
                validate_claims(self.artifact, [{**claim, 'missingEvidence': missing}])
        valid = validate_claims(self.artifact, [{**claim, 'missingEvidence': ['同期构成明细及适用规则']}])[0]
        self.assertEqual(valid['rendered'], '可能与尚未取得的构成明细有关')
        self.assertNotIn('同期构成明细及适用规则', valid['rendered'])

    async def test_missing_source_name_cannot_be_published_as_object_code(self):
        capture, selectors, scope = fixture()
        receipt = json.loads(capture['response']['content'][0]['text'])
        for member in receipt['data']['stepResults'][0]['rows']: member.pop('label')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        for selector in selectors: selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        built = await self.service.build([ref], selectors, scope, [], 'missing-names')
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(built['analysisRef'], [], [], 'missing-publish', 0)
        self.assertEqual(error.exception.code, 'OBJECT_LABEL_REQUIRED')

    async def test_parent_child_sum_and_difference_require_rule_and_keep_input_scope(self):
        ref, selectors, scope = saved_fixture(self.store, ('100', '30', '60'))
        self.rules = rules()
        self.delivery = AnalysisDelivery(self.store, OWNER, Path(self.temp.name) / 'workspace', self.rules)
        self.service = AnalysisService(self.store, OWNER, authorized, self.rules, delivery=self.delivery)
        draft = await self.service.build([ref], selectors, scope, [], 'locate')
        facts = self.store.get_analysis(OWNER, draft['analysisRef'])['facts']
        children = [f['factId'] for f in facts[1:]]
        built = await self.service.build([ref], selectors, scope, [
            {'id': 'blocked', 'operator': 'SUM', 'inputRefs': children},
            {'id': 'children', 'operator': 'SUM', 'inputRefs': children, 'ruleRef': 'fixture-additive'},
            {'id': 'gap', 'operator': 'DIFFERENCE', 'inputRefs': [facts[0]['factId'], 'derived_children']},
        ], 'hierarchy')
        values = built['derivedValues']
        self.assertEqual(values[0]['calculationValidation'], 'BLOCKED')
        self.assertEqual([v['value'] for v in values[1:]], ['90', '10'])
        plan = [{'viewId': 'reconciliation', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
                 'title': '一级汇总核对', 'valueRefs': ['derived_children', 'derived_gap']}]
        published = await self.service.publish(built['analysisRef'], [], plan, 'reconciliation', 0)
        self.assertTrue(published['delivery']['registered'])
        view = self.delivery.view(published['delivery']['path'])
        self.assertEqual([t['columns'][0]['label'] for t in view['tables'] if t['kind'] == 'calculation'], ['合计', '差额'])
        filtered = self.delivery.project_view(built['analysisRef'], {'topN': 1})
        self.assertFalse(any(t['kind'] == 'calculation' for t in filtered['tables']))

    async def test_excess_group_count_is_refused_before_publication(self):
        ref, selectors, scope = saved_fixture(self.store)
        built = await self.service.build([ref], selectors, scope,
            [{'id': f'count{i}', 'operator': 'COUNT'} for i in range(100)], 'many-groups')
        refs = [value['derivedId'] for value in built['derivedValues']]
        plan = [{'viewId': f'table{i}', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': refs} for i in range(2)]
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(built['analysisRef'], [], plan, 'too-many-groups', 0)
        self.assertEqual(error.exception.code, 'TABLE_LIMIT')
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef'])['publicationState'], 'DRAFT')

    async def test_html_keeps_approved_css_and_renders_grouped_business_columns(self):
        await self.published()
        export = self.delivery.render_report(self.ref, 'report')
        html = self.delivery.report(self.ref, export['exportRef'])['html']
        self.assertEqual(html.count('<table>'), 3)
        self.assertIn('构成来源', html)
        self.assertIn('逐层展开计算', html)
        self.assertNotIn('已取得', html)
        self.assertNotIn('object-0', html)
        self.assertNotIn('未注明', html)
        current = Path(__file__).parents[1] / 'examples/agent_service/analysis_extension/report.css'
        self.assertEqual(hashlib.sha256((current.read_text('utf-8').split('/* Authorized scoped changes:')[0].rstrip()+'\n').replace('.summary p { margin-top: 12px; }','.summary p { margin-top: 12px; max-width: 80ch; }').replace('\n','\r\n').encode()).hexdigest(), '7992f9a9cf9c145b3c2171606d621c62c64161d9ec483dea55d40c7f2396a7c7')


class FormulaParserTests(unittest.TestCase):
    def test_report_tool_schema_accepts_host_default_view_and_rejects_bad_column(self):
        from jsonschema import Draft202012Validator, ValidationError
        from analysis_extension.presentation import parameters
        from analysis_extension.tools import AnalysisRenderReport
        validator = Draft202012Validator(AnalysisRenderReport.input_schema)
        request = {'analysisRef':'current-ref','requestKey':'current-key','viewParameters':parameters()}
        validator.validate(request)
        with self.assertRaises(ValidationError):
            validator.validate({**request, 'viewParameters':{**parameters(), 'columnKey':42}})

    def test_bounded_sum_and_precise_decimal_expression(self):
        expression = Expression('SUM($A$1:A2) - 0.1 * 3')
        self.assertEqual(str(expression.evaluate({'A1':__import__('decimal').Decimal('0.1'), 'A2':__import__('decimal').Decimal('0.2')}.__getitem__)), '0.0')
        with self.assertRaises(AnalysisError): Expression('__import__("os")')
        with self.assertRaises(AnalysisError): Expression('SUM(A1:A99999)')
        with self.assertRaises(AnalysisError): Expression('SUM(A1:A1000,A1:A1000)')
        with self.assertRaises(AnalysisError): Expression('+'.join(['1'] * 800)).evaluate(lambda ref: None)
        with self.assertRaises(AnalysisError): Expression('9' * 128 + '*9').evaluate(lambda ref: None)
