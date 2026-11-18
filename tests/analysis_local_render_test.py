"""可选真实浏览器回归：冻结页面与聊天 iframe 使用同一检查协议。"""
import os
from unittest import skipUnless

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.local_render import LocalRenderChecker
from analysis_extension.models import AnalysisError


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1',
            '安装 requirements-analysis-render.txt 和 Chromium 后显式启用浏览器回归')
class LocalRenderTests(PresentationCase):
    async def prepare(self, script='', values=('12345.67', '-98.76')):
        capture, selectors, scope = saved_fixture(self.store, values)
        built = await self.service.build([capture], selectors, scope, [], 'browser-build')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.service.render_checker = LocalRenderChecker()
        self.service.web_ui_url = 'http://localhost:5173'
        return {'analysisRef': built['analysisRef'], 'requestKey': 'browser-preview',
                'document': {'title': '浏览器回归样例',
                             'html': '''<html><head><meta charset="utf-8"><style>
body{margin:0;padding:20px;font:16px/1.6 sans-serif}table{width:100%;border-collapse:collapse}
th,td{padding:12px;text-align:left}th.number,td.number{text-align:right}
</style></head><body><h1>预算比较样例</h1><table><thead><tr><th>机构</th><th class="number">金额（元）</th></tr></thead>
<tbody><tr><td>样例机构</td><td class="number">{{amount}}</td></tr></tbody></table>'''
                             + script + '</body></html>',
                             'bindings': [{'key': 'amount', 'valueRef': artifact['facts'][0]['factId'],
                                           'format': {'grouping': True, 'decimalPlaces': 2}}]}}

    async def test_two_real_widths_publish_and_reuse_without_another_browser(self):
        request = await self.prepare()
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED', preview['diagnostics'])
        self.assertTrue(preview['diagnostics']['renderChecks']['measurementRequirementMet'])
        self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM')
        self.assertTrue(preview['previewPage'].startswith('http://localhost:5173/analysis-preview/'))
        from unittest.mock import AsyncMock
        self.service.render_checker.check = AsyncMock(side_effect=AssertionError('重复启动浏览器'))
        again = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(again['diagnostics']['renderService']['status'], 'REUSED')
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])
        self.service.render_checker.check.assert_not_awaited()

    async def test_dynamic_values_are_observed_without_exposing_full_text_in_tool_result(self):
        request = await self.prepare('<p id="dynamic">—</p><script>document.getElementById("dynamic").textContent="脚本填充值："+analysisData.amount.display;</script>')
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED')
        digest = preview['diagnostics']['renderChecks']['viewports'][0]['contentDigest']
        records = self.store.document_checks(OWNER, request['analysisRef'], digest)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIn('脚本填充值：12,345.67', record['hostRenderedText']['text'])
            self.assertTrue(record['hostRenderedText']['complete'])
        self.assertTrue(all('hostRenderedText' not in record for record in preview['diagnostics']['renderChecks']['viewports']))

    async def test_full_chart_and_local_zoom_use_independent_default_scales(self):
        request = await self.prepare('''<div id="all" style="width:100%">
<div id="a1" style="height:12px;background:teal"></div>
<div id="a2" style="height:12px;background:teal"></div>
<div id="a3" style="height:12px;background:teal"></div></div>
<div id="zoom" style="width:100%">
<div id="z2" style="height:12px;background:orange"></div>
<div id="z3" style="height:12px;background:orange"></div></div>
<script>
analysisCharts.bindLinearBars(['amount','second','third'].map((key,index)=>
({element:document.getElementById('a'+(index+1)),valueKey:key})),{extent:document.getElementById('all')});
analysisCharts.bindLinearBars(['second','third'].map((key,index)=>
({element:document.getElementById('z'+(index+2)),valueKey:key})),{extent:document.getElementById('zoom')});
</script>''', values=('12345.67', '20', '10'))
        artifact = self.store.get_analysis(OWNER, request['analysisRef'])
        request['document']['bindings'] += [{'key': name, 'valueRef': fact['factId']}
                                          for name, fact in zip(('second', 'third'), artifact['facts'][1:])]
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM', preview['diagnostics'])
        for sample in preview['diagnostics']['renderChecks']['viewports']:
            bars = sample['numericChecks']['linearBars']
            self.assertEqual(bars['scaleGroups'], 2)
            self.assertEqual(bars['measured'], 5)
            self.assertEqual(bars['status'], 'PASS')
        # 显式把不同刻度命名为同组仍要阻断；不能靠自动拆组放过错误。
        request['document']['html'] = request['document']['html'].replace('{extent:', "{scale:'shared',extent:")
        request['requestKey'] = 'explicit-wrong-scale'
        invalid = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(invalid['nextAction'], 'REPAIR_DOCUMENT')
        messages = [error for sample in invalid['diagnostics']['renderChecks']['viewports'] for error in sample['errors']]
        self.assertTrue(any('局部放大图' in error and '不同组名' in error for error in messages))

    async def test_real_script_error_blocks_publication(self):
        request = await self.prepare('<script>throw new Error("fixture-script-error")</script>')
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED', preview['diagnostics'])
        self.assertEqual(preview['nextAction'], 'REPAIR_DOCUMENT')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_RENDER_ERRORS')

    async def test_linear_bars_in_auto_layout_table_do_not_loop_in_resize_observer(self):
        request = await self.prepare('''<style>
.comparison{width:100%;table-layout:auto}.track{height:12px;min-width:110px;background:#eee}
.fill{display:block;height:100%;width:0;background:teal}
</style><table class="comparison"><tbody>
<tr><td>较长的组织名称与口径</td><td>{{amount}}</td><td><div class="track" id="track"><i id="large" class="fill"></i></div></td></tr>
<tr><td>第二个对象</td><td>{{other}}</td><td><div class="track"><i id="small" class="fill"></i></div></td></tr>
</tbody></table><script>
analysisCharts.bindLinearBars([
{element:document.getElementById('large'),valueKey:'amount'},
{element:document.getElementById('small'),valueKey:'other'}],
{extent:document.getElementById('track')});
</script>''')
        artifact = self.store.get_analysis(OWNER, request['analysisRef'])
        request['document']['bindings'].append({'key': 'other', 'valueRef': artifact['facts'][1]['factId']})
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED', preview['diagnostics'])
        self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM', preview['diagnostics'])
        for sample in preview['diagnostics']['renderChecks']['viewports']:
            self.assertEqual(sample['errors'], [])
            self.assertEqual(sample['numericChecks']['linearBars']['status'], 'PASS')

    async def test_bound_linear_component_keeps_geometry_and_markers_at_two_widths(self):
        request = await self.prepare('''<div id="track" style="width:100%">
<div id="first" style="height:12px;background:teal"></div>
<div id="second" style="height:12px;background:orange"></div></div>
<script>window.analysisCharts.bindLinearBars([
{element:document.getElementById('first'),valueKey:'amount'},
{element:document.getElementById('second'),valueKey:'other'}],
{scale:'budget',extent:document.getElementById('track')});</script>''')
        artifact = self.store.get_analysis(OWNER, request['analysisRef'])
        request['document']['bindings'].append({'key': 'other', 'valueRef': artifact['facts'][1]['factId']})
        preview = await self.service.publish(**request, previewOnly=True)
        checks = preview['diagnostics']['renderChecks']
        self.assertTrue(checks['measurementRequirementMet'], preview)
        for sample in checks['viewports']:
            self.assertEqual(sample['numericChecks']['linearBars']['status'], 'PASS', sample)
            self.assertEqual(sample['numericChecks']['linearBars']['measured'], 2)
            self.assertFalse(sample['pageOverflow'])
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])

    async def test_bound_subpixel_amount_is_not_mistaken_for_a_missing_bar(self):
        request = await self.prepare('''<div id="track" style="width:100%">
<div id="large" style="height:14px;background:teal"></div>
<div id="small" style="height:14px;background:orange"></div></div><p>{{tiny}}</p>
<script>analysisCharts.bindLinearBars([
{element:document.getElementById('large'),valueKey:'amount'},
{element:document.getElementById('small'),valueKey:'tiny'}],
{extent:document.getElementById('track')});</script>''', values=('100000000', '-1'))
        artifact = self.store.get_analysis(OWNER, request['analysisRef'])
        request['document']['bindings'].append({'key': 'tiny', 'valueRef': artifact['facts'][1]['factId']})
        preview = await self.service.publish(**request, previewOnly=True)
        for sample in preview['diagnostics']['renderChecks']['viewports']:
            self.assertEqual(sample['numericChecks']['linearBars']['status'], 'PASS', sample)
        # 大值条形真的被设为零宽时，仍然拦下；没有放宽一般绘图错误。
        request['document']['html'] += '<style>#large{width:0px!important}</style>'
        request['requestKey'] = 'bad-real-zero'
        invalid = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(invalid['nextAction'], 'REPAIR_DOCUMENT')
