from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
import re

from analysis_presentation_fixtures import PresentationCase
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.models import AnalysisError
from analysis_extension.validation import validate_views


class ModelPresentationTests(PresentationCase):
    def test_business_formula_preserves_sum_precedence(self):
        from analysis_extension.formatting import formula_text
        from analysis_extension.formulas import Expression
        names = {'A1':'收入', 'B1':'补贴', 'C1':'成本'}
        cases = {
            'SUM(A1,B1)*C1': '(收入 + 补贴) × 成本',
            'C1-SUM(A1,B1)': '成本 − (收入 + 补贴)',
            'C1/SUM(A1:B1)': '成本 ÷ (收入 + 补贴)',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(formula_text(Expression(source).tree, names), expected)

    async def build(self, values=('100', '-1033.14', '-8300'), unit='元', formula=False):
        if formula:
            ref, selections, scope = mixed_fixture(self.store)
            operations = [{'id':'formula','operator':'FORMULA_CHECK','select':{'cellKey':'2:1','objectId':'object-0'}}]
        else:
            ref, selections, scope = saved_fixture(self.store, values, unit=unit)
            operations = [{'id':'sum','operator':'SUM','ruleRef':'fixture-additive'}]
        result = await self.service.build([ref], selections, scope, operations, 'build')
        self.ref = result['analysisRef']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.sid = self.artifact['scope']['scopeId']
        self.refs = [f['factId'] for f in self.artifact['facts']]
        return result

    def plan(self, kind='TABLE', **options):
        return {'viewId':kind, 'scopeId':self.sid, 'type':kind, 'valueRefs':self.refs, **options}

    async def release(self, plans, claims=None, technical=False):
        self.result = await self.service.publish(self.ref, claims or [], plans, 'publish', 0, technicalDetails=technical)
        self.assertTrue(self.result['delivery']['registered'])
        self.view = self.delivery.view(self.result['delivery']['path'])
        export = self.delivery.render_report(self.ref, 'report')
        self.html = self.delivery.report(self.ref, export['exportRef'])['html']
        return self.view

    async def test_no_plan_adds_no_table_statistics_or_prose_and_preserves_facts(self):
        await self.build()
        original = deepcopy(self.artifact['facts'])
        view = await self.release([])
        for field in ('tables', 'rows', 'metrics', 'claims', 'sources', 'limitations'): self.assertEqual(view[field], [])
        self.assertIsNone(comparison(view))
        self.assertNotIn('markdown', self.result)
        self.assertEqual(self.result['summary']['factCount'], 3)
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['facts'], original)

    async def test_model_selects_units_titles_and_details_without_changing_calculations(self):
        await self.build()
        plans = [self.plan(title='模型选择的标题', valueRefs=self.refs[:1], showUnit=False),
                 self.plan('METRIC', title='总额（元）', valueRefs=['derived_sum'])]
        view = await self.release(plans)
        table = view['tables'][0]
        self.assertEqual(table['title'], '模型选择的标题')
        self.assertEqual(len(table['rows']), 1)
        self.assertEqual(table['columns'][0]['unit'], '')
        self.assertEqual(view['metrics'][0]['label'], '总额（元）')
        self.assertEqual(view['metrics'][0]['unit'], '')
        self.assertEqual(view['metrics'][0]['display'], '-9,233.14')
        self.assertNotIn('单位：', self.html)
        self.assertEqual({f['unit'] for f in self.store.get_analysis(OWNER, self.ref)['facts']}, {'元'})

    async def test_explicit_unit_display_is_respected_in_metric_table_and_chart(self):
        await self.build()
        view = await self.release([self.plan(showUnit=True), self.plan('METRIC', valueRefs=self.refs[:1], showUnit=True), self.plan('BAR_VALUES', showUnit=True)])
        self.assertEqual(view['metrics'][0]['unit'], '元')
        self.assertEqual(view['tables'][0]['columns'][0]['unit'], '元')
        self.assertTrue(any('元' in p.get('text', '') for p in comparison(view)['primitives']))

    async def test_empty_units_never_produce_unit_warning_or_column(self):
        await self.build(unit=None)
        view = await self.release([self.plan(showUnit=True), self.plan('METRIC', valueRefs=self.refs[:1])])
        self.assertEqual(view['tables'][0]['columns'][0]['unit'], '')
        for unwanted in ('未注明', '单位：', '>单位<', '待补证据', '依据：', '证据：'):
            self.assertNotIn(unwanted, self.html)

    async def test_claims_preserve_model_emphasis_and_exact_unit_position(self):
        await self.build()
        claims = [{'claimId':'value','scopeId':self.sid,'kind':'fact','parts':[
            {'text':'**该指标为 '}, {'valueRef':self.refs[0]}, {'valueRef':self.refs[0], 'field':'unit'}, {'text':'**。<script>不执行</script>'}]},
            {'claimId':'possibility','scopeId':self.sid,'kind':'hypothesis','parts':[{'text':'可能与项目安排有关，目前尚不能确定。'}], 'missingEvidence':['同期项目安排']}]
        view = await self.release([], claims)
        self.assertEqual(view['claims'][0], '**该指标为 100元**。<script>不执行</script>')
        self.assertEqual(view['claims'][1], '可能与项目安排有关，目前尚不能确定。')
        self.assertIn('<strong>该指标为 100元</strong>', self.html)
        self.assertNotIn('<script>', self.html)
        for unwanted in ('待验证假设', '同期项目安排', '需补证据'): self.assertNotIn(unwanted, self.html)

    async def test_long_table_starts_with_model_page_size_and_retains_all_rows(self):
        await self.build(values=tuple(str(i) for i in range(35)))
        view = await self.release([self.plan(pageSize=3, collapsed=False)])
        self.assertEqual(view['tables'][0]['pageSize'], 3)
        bodies = re.findall(r'<tbody>(.*?)</tbody>', self.html, re.S)
        self.assertEqual([body.count('<tr ') for body in bodies], [3, 32])
        self.assertIn('查看其余 32 项（共 35 项）', self.html)
        self.assertEqual(len(self.store.get_analysis(OWNER, self.ref)['facts']), 35)

    async def test_collapsed_table_is_explicit_not_chosen_by_length(self):
        await self.build(values=('5',))
        await self.release([self.plan(collapsed=True)])
        self.assertIn('<details><summary>查看明细（1 项）</summary>', self.html)

    async def test_formula_graphs_do_not_auto_append_detail_tables(self):
        await self.build(formula=True)
        view = await self.release([self.plan('FORMULA', valueRefs=['derived_formula'])])
        self.assertGreater(len(diagrams(view)), 0)
        self.assertEqual(view['tables'], [])
        self.assertNotIn('B1-C1', self.html)

    async def test_formula_details_use_business_names_and_focused_nodes(self):
        await self.build(formula=True)
        plan = self.plan('FORMULA', valueRefs=['derived_formula'], includeDetails=True, nodeRefs=self.refs[:1], title='计算关系')
        view = await self.release([plan])
        self.assertEqual(len(view['tables'][0]['rows']), 1)
        self.assertEqual(view['tables'][0]['rows'][0]['note'], '结果 = 收入 − 成本')
        self.assertNotIn('B1-C1', self.html)
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['derivedValues'][0]['value'], '-5')

    async def test_technical_trace_requires_explicit_opt_in(self):
        await self.build(formula=True)
        view = await self.release([self.plan('TABLE', valueRefs=['derived_formula'])], technical=True)
        self.assertTrue(view['technicalDetails'])
        self.assertGreater(len(view['sources']), 0)
        self.assertIn('B1-C1', self.html)
        with self.assertRaises(AnalysisError):
            await self.service.publish(self.ref, [], [self.plan('TABLE', valueRefs=['derived_formula'])], 'publish', 0, technicalDetails=False)

    async def test_invalid_presentation_options_fail_before_publication(self):
        await self.build(formula=True)
        for options in ({'showUnit':'yes'}, {'pageSize':0}, {'pageSize':True}, {'collapsed':1}, {'includeDetails':True}, {'nodeRefs':['not-a-node']}):
            with self.subTest(options=options), self.assertRaises(AnalysisError): validate_views(self.artifact, [self.plan(**options)])
        with self.assertRaises(AnalysisError):
            validate_views(self.artifact, [self.plan('FORMULA', valueRefs=[{}], nodeRefs=['invalid'])])

    async def test_comparison_preserves_selected_heading_and_caption_without_details(self):
        await self.build()
        view = await self.release([self.plan('BAR_VALUES', title='模型选择的比较角度', description='本轮所选对象', showUnit=False)])
        self.assertEqual(comparison(view)['title'], '模型选择的比较角度')
        self.assertEqual(view['sections'][0]['blocks'][0]['description'], '本轮所选对象')
        self.assertEqual(view['tables'], [])
        self.assertIn('<h3>模型选择的比较角度</h3>', self.html)

    async def test_same_formula_can_be_explicitly_presented_with_different_titles(self):
        await self.build(formula=True)
        selected = self.plan('FORMULA', valueRefs=['derived_formula'], nodeRefs=self.refs[:1], title='整体计算')
        view = await self.release([selected, {**selected, 'viewId':'detail', 'title':'关注的计算环节'}])
        self.assertEqual([scene['title'] for scene in diagrams(view)], ['整体计算', '关注的计算环节'])

    async def test_long_names_and_units_remain_readable_without_assuming_a_table(self):
        from analysis_extension.presentation import scene
        label, unit = '长名称业务项目' * 12, '来源确认的单位' * 8
        chart = scene([{'rowKey':'example', 'label':label, 'exact':'-1', 'display':'-1', 'unit':unit}], 'BAR_VALUES', '')
        labels = ''.join(p['text'] for p in chart['primitives'] if p['kind'] == 'text' and p['tone'] == 'label')
        values = ''.join(p['text'] for p in chart['primitives'] if p['kind'] == 'text' and p['tone'] == 'value')
        self.assertEqual(labels, label)
        self.assertEqual(values, '-1 ' + unit)

    async def test_long_business_formula_does_not_promise_unselected_details(self):
        from analysis_extension.diagrams import relation_scenes
        await self.build(formula=True)
        for index, fact in enumerate(self.artifact['facts']):
            fact['headers'] = ['业务项目名称' * 80 + str(index)]
        self.artifact['viewPlan'] = [self.plan('FORMULA', valueRefs=['derived_formula'], nodeRefs=self.refs[:1])]
        graph = relation_scenes(self.artifact)[0]
        displayed = graph['relationship']['equation']
        self.assertNotIn('完整公式见核对明细', displayed)
        self.assertIn(self.artifact['facts'][0]['headers'][0], displayed)
        self.assertIn('−', displayed)
