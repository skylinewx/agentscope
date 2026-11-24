"""Business material shown to the model must describe the inputs Python actually receives."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from analysis_extension.formula_arithmetic import BUDGET_FORMULA
from analysis_extension.python_input_markdown import render_input_material
from analysis_extension.review_budget import ReviewBudgetConfig
from analysis_extension.tools import AnalysisBuild, AnalysisPython, AnalysisTask


class PythonInputMarkdownTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.records = []
        self.facts = []
        for ref, code, name, amount in [('a', '00000000', '全辖', '13.50'),
                                         ('b', '01', '合计', None), ('c', '02', '工程组', '0')]:
            self.records.append({'ref': ref, 'sourceMeasureCode': 'COST', 'measureCode': 'cell_column',
                'recordTable': 'COST_FORM', 'formName': '费用表', 'headers': ['费用'],
                'value': amount, 'valueType': 'DECIMAL_STRING', 'unit': '元',
                'valueState': 'PRESENT' if amount is not None else 'NO_VALUE',
                'dimensions': {'MD_TEAM': code}, 'dimensionNames': {'MD_TEAM': name}})
            self.facts.append({'factId': ref, 'captureRef': 'capture', 'formulaArithmetic': BUDGET_FORMULA})
        self.prepared = {'records': self.records, 'files': [], 'analysisRef': 'analysis:1'}
        self.artifact = {'facts': self.facts}
        self.proof = {'members': ['member-a', 'member-b'], 'factRefs': ['fact-a'],
                      'grain': ['MD_TEAM'], 'complete': False}
        self.result = {'analysisRef': 'analysis:1', 'derivedValues': [], 'inputSetRef': 'selected',
                       'scope': {'populationEvidence': [self.proof], 'pendingMembers': ['pending']},
                       'pythonInputCatalog': {'recordCount': 3, 'sampleRead': {'inputSetRef': 'selected'}}}
        receipt = {'data': {'question': '历史问题', 'dimensionCatalog': [{'dimCode': 'MD_TEAM', 'label': '团队'}],
                            'resultSets': [{'rows': [{'value': '未选入输入的来源行'}]}]}}
        self.service = SimpleNamespace(
            owner='owner', turn=SimpleNamespace(state={'request': '分析这次费用'}),
            build=AsyncMock(return_value=self.result), task=AsyncMock(return_value=self.result),
            review_budget=ReviewBudgetConfig(),
            store=SimpleNamespace(get_input_set=Mock(return_value=self.prepared),
                                  get_analysis=Mock(return_value=self.artifact),
                                  get_capture=Mock(return_value={'response': receipt})),
            sources=SimpleNamespace(for_capture=lambda capture: SimpleNamespace(adapter_id='budget-v2', raw_receipt=lambda r: r)))

    async def test_build_and_explicit_prepare_show_identical_frozen_values_and_code_selector(self):
        build = (await AnalysisBuild(self.service).call()).content[0].text
        prepared = (await AnalysisTask(self.service).call(action='prepare_inputs')).content[0].text
        material = build.split('\n\n分析构建结果')[0]
        self.assertTrue(prepared.startswith(material))
        for phrase in ('分析这次费用', '| 团队 | MD_TEAM | 00000000 | 全辖 |', '| 费用 | sourceMeasureCode | COST |',
                       '1 行、1 个指标值', '.get("MD_TEAM") == "00000000"', '| 13.50 |',
                       '| 合计 | 空值 |', '| 工程组 | 0 |'):
            self.assertIn(phrase, material)
        self.assertNotIn('未选入输入的来源行', material)
        self.assertNotIn('历史问题', material)
        self.assertNotIn('成员类型', material)
        self.assertNotIn('member-a', build)
        self.assertIn('"complete":false', build)
        self.assertIn('"factRefsCount":1', build)
        self.assertIn('"pendingMembers":["pending"]', build)
        self.assertEqual(self.proof['members'], ['member-a', 'member-b'])
        self.service.store.get_input_set.assert_called_with('owner', 'selected')

    def material(self, max_chars=12000):
        return render_input_material(self.service, self.prepared, self.artifact, 'selected', max_chars)

    def test_two_total_axes_or_missing_input_coordinate_keep_coordinates_together(self):
        for row, code in zip(self.records, ['00000000', '01', '02'], strict=True):
            row['dimensions']['MD_ALLOCATION'] = code
        self.assertIn('### 查询明细', self.material())
        self.assertNotIn('total_rows =', self.material())
        for row in self.records:
            del row['dimensions']['MD_ALLOCATION']
        self.records[1]['effectiveDimensions'] = self.records[1].pop('dimensions')
        self.records[1]['dimensions'] = {'DATATIME': '2026N0001'}
        self.assertIn('### 查询明细', self.material())
        self.assertNotIn('total_rows =', self.material())

    def test_mixed_inputs_show_their_own_scope_and_bounded_material_has_a_read(self):
        self.prepared['files'] = [{'ref': 'file'}]
        material = self.material()
        self.assertIn('另有 1 项输入', material)
        self.assertNotIn('total_rows =', material)
        self.assertIn('### 团队合计', material)
        self.assertIn('inputSetRef="selected"', self.material(max_chars=550))
        self.assertLessEqual(len(self.material(max_chars=550)), 550)
        self.records[0]['propertyDimensions'] = ['MD_TEAM']
        self.assertIn('### 查询明细', self.material())

    def test_reserved_code_uses_the_returned_dimension_even_for_an_organization_role(self):
        for row in self.records:
            row['dimensions'] = {'MD_ORG_1': row['dimensions']['MD_TEAM']}
            row['dimensionNames'] = {'MD_ORG_1': row['dimensionNames']['MD_TEAM']}
        self.assertIn('.get("MD_ORG_1") == "00000000"', self.material())

    async def test_python_handoff_registers_native_result_and_keeps_detailed_scope_readable(self):
        value = {**self.result, 'pythonRunRef': 'run', 'resultRefs': ['output']}
        self.artifact['facts'] = [
            {**self.records[0], 'factId': period, 'value': amount,
             'dimensions': {**self.records[0]['dimensions'], 'DATATIME': period}}
            for period, amount in [('2026J0001', '250'), ('2026J0002', '370')]]
        before = deepcopy(value)
        self.service.python = AsyncMock(return_value=value)
        response = await AnalysisPython(self.service).call()
        self.assertEqual(response.metadata['analysisNativeResult'],
                         {'analysisRef': 'analysis:1', 'pythonRunRef': 'run', 'resultRefs': ['output']})
        self.assertIn('"section":"summary"', response.content[0].text)
        body = response.content[0].text
        self.assertIn('AnalysisPublish 的 analysisRef=analysis:1', body)
        self.assertIn('本次返回的 inputSetRef=selected', body)
        self.assertIn('trend_', body)
        self.assertIn('2026年第2季度', body)
        self.assertNotIn('"points"', body)
        self.assertNotIn('member-a', response.content[0].text)
        self.assertEqual(value, before)
