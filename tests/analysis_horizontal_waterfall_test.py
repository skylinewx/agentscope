"""加减公式的精确累计区间、原值边界与横向图形的真实浏览器回归。"""
from decimal import Decimal
import os
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless

from analysis_depth_test import chain
from analysis_extension.document_checks import evaluate
from analysis_extension.document_data import formula_dataset
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.free_document import compile_document
from analysis_extension.local_render import LocalRenderChecker


def waterfall_artifact(expression='B1-C1+2', first='10', second='3', source='5'):
    facts = chain()[:3]
    facts[0].update(formula=expression, value=source)
    facts[1].update(formula=None, value=first)
    facts[2].update(formula=None, value=second)
    for fact in facts:
        fact['unit'] = '元'
        if fact['value'] is None:
            fact['valueState'] = 'NO_VALUE'
    scope = {'scopeId': 'scope', 'coverage': 'UNKNOWN'}
    values = calculate(facts, scope, [{'id': 'formula', 'operator': 'FORMULA_CHECK',
                                     'inputRefs': ['root'], 'checkScope': 'LOCAL'}], RuleRegistry())
    return {'facts': facts, 'derivedValues': values, 'scope': scope}


def formula_node(artifact):
    facts = {fact['factId']: fact for fact in artifact['facts']}
    return formula_dataset(artifact['derivedValues'][0], facts, ['root'],
                           {'decimalPlaces': 2, 'grouping': True})['nodes'][0]


def waterfall_document(artifact=None, extra_script=''):
    artifact = artifact or waterfall_artifact()
    html = '''<html><head><meta charset="utf-8"><style>
body{margin:0;padding:24px;font:14px/1.6 sans-serif;color:#253750}
h1{font-size:20px}.step{display:grid;grid-template-columns:minmax(100px,180px) minmax(0,1fr) minmax(80px,160px);gap:16px;align-items:center;margin:18px 0}
.amount{text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;min-width:0}.track{height:28px;position:relative}
.bar{height:20px;top:4px;background:#498b75}.negative{background:#c45e70}.total{background:#547ce7}
.zero{position:absolute;top:0;bottom:0;width:1px;background:#d9e0eb}
@media(max-width:560px){.step{grid-template-columns:1fr auto;gap:4px 12px}.track{grid-row:2;grid-column:1/-1}.amount{grid-column:2;grid-row:1}}
</style></head><body><h1>加减公式构成 · 元</h1>
<p>来源原值 {{source}} · 本层计算 {{computed}} · 差额 {{difference}}</p><main id="chart"></main><script>
const node=analysisSets.formula.nodes.find(item=>item.factRef==='root');
const rows=node.waterfall.steps.map((step,index)=>{
 const row=document.createElement('div');row.className='step';
 const title=document.createElement('span');
 title.textContent=step.kind==='TOTAL'?'本层计算结果':step.kind==='CONSTANT'?'公式常数':node.operands.find(item=>item.factRef===step.factRef).headers.join(' / ');
 const track=document.createElement('div');track.className='track';
 const zero=document.createElement('span');zero.className='zero';zero.style.left=node.waterfall.zero*100+'%';track.append(zero);
 const bar=document.createElement('div');bar.className='bar '+(step.kind==='TOTAL'?'total':Number(step.value)<0?'negative':'');track.append(bar);
 const amount=document.createElement('span');amount.className='amount';amount.textContent=step.display;
 row.append(title,track,amount);document.getElementById('chart').append(row);
 return {element:bar,track,stepIndex:index};
});
analysisCharts.bindHorizontalWaterfall(rows,{setKey:'formula',nodeRef:'root'});
''' + extra_script + '''</script></body></html>'''
    return compile_document(artifact, {
        'title': '加减公式构成', 'html': html,
        'datasets': [{'key': 'formula', 'valueRef': 'derived_formula', 'nodeRefs': ['root'],
                      'format': {'decimalPlaces': 2, 'grouping': True}}],
        'bindings': [{'key': key, 'valueRef': 'derived_formula', 'nodeRef': 'root', 'field': field,
                      'format': {'decimalPlaces': 2, 'grouping': True}}
                     for key, field in [('source', 'sourceValue'), ('computed', 'localValue'),
                                        ('difference', 'localDifference')]],
    })


class HorizontalWaterfallTests(TestCase):
    def test_complete_formula_keeps_constant_and_does_not_balance_to_source(self):
        node = formula_node(waterfall_artifact())
        waterfall = node['waterfall']
        self.assertEqual(waterfall['basis'], 'LOCAL')
        self.assertEqual([(step['kind'], step['value'], step['start'], step['end'])
                          for step in waterfall['steps']], [
            ('CONTRIBUTION', '10', '0', '10'), ('CONTRIBUTION', '-3', '10', '7'),
            ('CONSTANT', '2', '7', '9'), ('TOTAL', '9', '0', '9')])
        self.assertEqual(node['values']['sourceValue']['value'], '5')
        self.assertEqual(node['values']['localDifference']['value'], '-4')
        self.assertEqual(waterfall['minimum'], '0')
        self.assertEqual(waterfall['maximum'], '10')
        self.assertEqual(waterfall['steps'][1]['geometry'], {'left': 0.7, 'width': 0.3})

    def test_negative_input_subtracted_increases_result_and_crosses_zero(self):
        node = formula_node(waterfall_artifact('B1-C1', first='-10', second='-15'))
        waterfall = node['waterfall']
        self.assertEqual([step['value'] for step in waterfall['steps']], ['-10', '15', '5'])
        self.assertEqual(waterfall['steps'][1]['coefficient'], '-1')
        self.assertAlmostEqual(waterfall['zero'], 2 / 3)
        self.assertEqual(waterfall['steps'][1]['geometry'], {'left': 0.0, 'width': 1.0})

    def test_parentheses_repeated_operands_and_coefficients_preserve_operations(self):
        node = formula_node(waterfall_artifact('B1-(C1-2)+B1*0.5'))
        self.assertEqual([Decimal(step['value']) for step in node['waterfall']['steps']],
                         [Decimal('15'), Decimal('-3'), Decimal('2'), Decimal('14')])
        self.assertEqual(node['waterfall']['steps'][0]['coefficient'], '1.5')

    def test_zero_is_not_faked_as_minimum_width(self):
        waterfall = formula_node(waterfall_artifact('B1-C1', '0', '0', '0'))['waterfall']
        self.assertEqual(len(waterfall['steps']), 3)
        self.assertTrue(all(step['geometry'] == {'left': 0.0, 'width': 0.0} for step in waterfall['steps']))
        self.assertEqual(waterfall['zero'], 0)

    def test_missing_and_nonlinear_inputs_keep_tree_without_fabricated_steps(self):
        for artifact in [waterfall_artifact(first=None), waterfall_artifact('B1*C1')]:
            with self.subTest(artifact=artifact):
                node = formula_node(artifact)
                self.assertNotIn('waterfall', node)
                self.assertIn('expressionTree', node)
                self.assertEqual(len(node['operands']), 2)

    def test_high_precision_endpoints_survive_display_rounding(self):
        node = formula_node(waterfall_artifact('B1-C1', '123456789012345678.123456789',
                                             '123456789012345678.123456788', '0.000000001'))
        steps = node['waterfall']['steps']
        self.assertEqual(steps[1]['end'], '0.000000001')
        self.assertEqual(steps[-1]['value'], '0.000000001')
        self.assertEqual(steps[-1]['display'], '0.00')
        self.assertGreater(steps[-1]['geometry']['width'], 0)

    def test_deeper_missing_dependency_does_not_remove_complete_local_chart(self):
        artifact = waterfall_artifact('B1-C1')
        artifact['facts'] = chain()
        artifact['facts'][4].update(value=None, valueState='NO_VALUE')
        artifact['derivedValues'] = calculate(artifact['facts'], artifact['scope'], [
            {'id': 'formula', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}], RuleRegistry())
        node = formula_node(artifact)
        self.assertEqual(node['recursiveStatus'], 'BLOCKED')
        self.assertEqual(node['waterfall']['steps'][-1]['value'], '-8')


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1', '显式启用本地无头浏览器回归')
class HorizontalWaterfallBrowserTests(IsolatedAsyncioTestCase):
    async def test_positive_negative_zero_and_high_precision_at_two_widths(self):
        for artifact in [waterfall_artifact(), waterfall_artifact('B1-C1', '-10', '-15'),
                         waterfall_artifact('B1-C1', '0', '0', '0'),
                         waterfall_artifact('B1-C1', '123456789012345678.123456789',
                                            '123456789012345678.123456788', '0.000000001')]:
            document = waterfall_document(artifact)
            for observation in await LocalRenderChecker(widths=(420, 1280)).check(document):
                result = evaluate(document, {key: value for key, value in observation.items() if key != 'hostRenderedText'})
                self.assertFalse(result['blocking'], result)
                self.assertFalse(result['pageOverflow'], result)
                self.assertEqual(result['smallText'], [])
                self.assertEqual(result['clippedText'], [])
                self.assertEqual(result['numericChecks']['linearBars']['status'], 'PASS')

    async def test_wrong_cumulative_position_blocks_even_when_length_is_correct(self):
        document = waterfall_document(extra_script="rows[1].element.style.left='0%';")
        for observation in await LocalRenderChecker(widths=(420, 1280)).check(document):
            result = evaluate(document, {key: value for key, value in observation.items() if key != 'hostRenderedText'})
            self.assertTrue(result['blocking'])
            self.assertTrue(any('累计位置或长度' in error for error in result['errors']))

    async def test_incomplete_steps_and_wrong_node_fail_without_changing_business_values(self):
        for script in ["analysisCharts.bindHorizontalWaterfall(rows.slice(1),{setKey:'formula',nodeRef:'root'});",
                       "analysisCharts.bindHorizontalWaterfall(rows,{setKey:'formula',nodeRef:'missing'});",
                       "analysisCharts.bindHorizontalWaterfall(rows.map(row=>({...row,element:rows[0].element,track:rows[0].track})),{setKey:'formula',nodeRef:'root'});"]:
            document = waterfall_document(extra_script=script)
            observation = (await LocalRenderChecker(widths=(420,)).check(document))[0]
            self.assertTrue(evaluate(document, {key: value for key, value in observation.items() if key != 'hostRenderedText'})['blocking'])

    async def test_switching_an_element_to_linear_binding_discards_waterfall_geometry(self):
        document = waterfall_document(extra_script="""
analysisCharts.bindLinearBars([{element:rows[0].element,valueKey:'difference'}],
                             {extent:rows[0].track,maximum:10});
""")
        for observation in await LocalRenderChecker(widths=(420, 1280)).check(document):
            result = evaluate(document, {key: value for key, value in observation.items() if key != 'hostRenderedText'})
            self.assertFalse(result['blocking'], result)
            self.assertEqual(result['numericChecks']['linearBars']['measured'], 4)
