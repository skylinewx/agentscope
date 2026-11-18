"""等价别名和冻结输入续建：修复技术往返，不改变业务范围与计算依赖。"""
from copy import deepcopy
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from analysis_fixtures import OWNER, saved_fixture
from analysis_budget_batch_test import batch_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.python_analysis import execute_python
from analysis_extension.python_worker import validate_code
from analysis_extension.tools import AnalysisBuild


class IncrementalOptimizationTests(PresentationCase):
    async def test_python_output_work_is_checked_before_build_or_execution(self):
        capture, selections, scope = saved_fixture(self.store, ('1', '2'))
        with patch.object(self.service, 'build', side_effect=AssertionError('不能先构建错误工作项')):
            with self.assertRaises(AnalysisError) as caught:
                await self.service.python(captureRefs=[capture], selections=selections, scope=scope,
                    code='result=[]', purpose='测试工作项契约', requestKey='invalid-work',
                    workPlan=[{'taskId': 'ratio', 'title': '占比', 'kind': 'COMPUTATION', 'operator': 'RATIO', 'refs': ['scriptRatio']}])
        self.assertEqual(caught.exception.code, 'PYTHON_WORK_PLAN_OPERATOR')
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    def test_native_work_checks_are_not_relabelled_as_python(self):
        from analysis_extension.python_analysis import validate_python_work_plan
        operations = [{'id': 'formula', 'operator': 'FORMULA_CHECK'}]
        work = [{'taskId': 'formula', 'kind': 'COMPUTATION', 'operator': 'FORMULA_CHECK', 'refs': ['formula']}]
        before = deepcopy(work)
        validate_python_work_plan(work, operations)
        self.assertEqual(work, before)

    async def test_decimal_alias_preserves_exact_runtime_precision(self):
        for code in ('D = Decimal', 'Decimal = D', 'D = D', 'from decimal import Decimal\nD = Decimal'):
            with self.subTest(code=code):
                result = await execute_python(code + '\nresult=[{"value":D("9007199254740993.11") + Decimal("0.02")}]', [], 10)
                self.assertEqual(result, [{'value': '9007199254740993.13'}])

    def test_non_equivalent_or_indirect_overwrites_remain_rejected(self):
        for code in ('D = 1', 'Decimal = sum', 'D += 1', 'D, x = [Decimal, 1]',
                     'for D in inputs:\n pass', 'def f(D):\n return D', 'def Decimal(x):\n return x',
                     'D = Decimal("1")', 'sum = D', 'from collections import Counter as D'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_code(code)

    def test_incremental_schema_requires_explicit_base_and_complete_replacements(self):
        validator = Draft202012Validator(AnalysisBuild.input_schema)
        validator.validate({'baseAnalysisRef': 'base:1', 'operations': [], 'requestKey': 'next'})
        for request in ({'operations': [], 'requestKey': 'missing-base'},
                        {'baseAnalysisRef': 'base:1', 'captureRefs': ['capture'], 'operations': [], 'requestKey': 'partial-input'}):
            with self.assertRaises(ValidationError):
                validator.validate(request)

    async def test_append_preserves_typed_grain_null_and_python_without_readaptation(self):
        capture, selection, scope, _ = batch_fixture(null_value=True)
        ref = self.store.capture(OWNER, capture)
        selection = {**selection, 'captureRef': ref, 'role': 'PRIMARY', 'grain': ['MD_ORG__MD_YWBK']}
        first = await self.service.python(captureRefs=[ref], selections=[selection], scope=scope,
            code='result=[{"name":"kept","label":"输入快照","kind":"DIAGNOSTIC","inputRefs":[x["ref"] for x in inputs],"data":[x["value"] for x in inputs]}]',
            purpose='保存实际输入', requestKey='python-base')
        base = self.store.get_analysis(OWNER, first['analysisRef'])
        before = deepcopy(base)
        operation = {'id': 'count', 'operator': 'COUNT', 'inputRefs': [fact['factId'] for fact in base['facts']]}
        request = {'baseAnalysisRef': first['analysisRef'], 'operations': [operation], 'requestKey': 'append',
                   'workPlan': [{'taskId': 'count', 'title': '统计所选事实', 'kind': 'COMPUTATION', 'refs': ['count']}]}
        with patch('analysis_extension.analysis_inputs.adapt_inputs', side_effect=AssertionError('不能重适配')):
            built = await self.service.build(**request)
        actual = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(actual['facts'], base['facts'])
        self.assertEqual(actual['scope'], base['scope'])
        self.assertEqual(actual['pythonRuns'], base['pythonRuns'])
        self.assertEqual(actual['derivedValues'][-1], base['derivedValues'][0])
        self.assertEqual(self.store.get_analysis(OWNER, first['analysisRef']), before)
        self.assertTrue(any(fact['value'] is None for fact in actual['facts']))
        self.assertEqual((await self.service.build(**request))['analysisRef'], built['analysisRef'])

    async def test_incremental_service_rejects_partial_input_and_keeps_version_checks(self):
        ref, selections, scope = saved_fixture(self.store, ('2', '3'))
        first = await self.service.build([ref], selections, scope, [], 'base')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.build(baseAnalysisRef=first['analysisRef'], scope=scope, operations=[], requestKey='partial')
        self.assertEqual(caught.exception.code, 'INVALID_BUILD_INPUT')
        second = await self.service.build(baseAnalysisRef=first['analysisRef'], operations=[], requestKey='next')
        self.assertNotEqual(first['analysisRef'], second['analysisRef'])
        # 未发布草稿可分支续建；已发布头冲突仍由存储层判定，不能跳过已有检查。
        with patch.object(self.store, 'continuation_revision', side_effect=AnalysisError('VERSION_CONFLICT', '已有更新的发布结果')):
            with self.assertRaises(AnalysisError) as conflict:
                await self.service.build(baseAnalysisRef=first['analysisRef'], operations=[], requestKey='stale')
        self.assertEqual(conflict.exception.code, 'VERSION_CONFLICT')
