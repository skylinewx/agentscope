"""结果字段贯通、参数纠错、完成覆盖与模型材料体积的回归测试。"""
from copy import deepcopy
import json
from unittest import TestCase

from jsonschema import Draft202012Validator

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.completion import completion_summary, continue_plan
from analysis_extension.engine import calculate
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.native_analysis import document_inputs
from analysis_extension.operation_contracts import validate_operation
from analysis_extension.operation_plan import merge_operations
from analysis_extension.result_projection import project_results
from analysis_extension.scalar_references import field_catalog, scalar_coverage
from analysis_extension.service import compact_derived
from analysis_extension.tools import AnalysisBuild, AnalysisPython, AnalysisPublish


class ResultReuseTests(PresentationCase):
    async def prepare(self, values=('10', '-3', '-2', None)):
        capture, selections, scope = saved_fixture(self.store, values)
        built = await self.service.build([capture], selections, scope,
            [{'id': 'distribution', 'operator': 'DESCRIBE', 'top': 3}], 'first')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        return built, artifact

    async def test_statistical_fields_continue_arithmetic_and_bind_without_copying(self):
        built, artifact = await self.prepare()
        output = await self.service.build(baseAnalysisRef=built['analysisRef'], requestKey='ratio', operations=[{
            'id': 'ratio', 'operator': 'ARITHMETIC', 'inputRefs': [
                {'valueRef': 'distribution', 'field': 'negativeSum'},
                {'valueRef': 'distribution', 'field': 'sum'}], 'expression': 'v0/v1*100', 'decimalPlaces': 2}])
        result = next(value for value in output['derivedValues'] if value['derivedId'] == 'derived_ratio')
        self.assertEqual(result['value'], '-100.00', result)
        current = self.store.get_analysis(OWNER, output['analysisRef'])
        expression = next(value for value in current['derivedValues'] if value['derivedId'] == 'derived_ratio')
        self.assertEqual(expression['inputRefs'], ['derived_distribution'])
        self.assertEqual(expression['inputBindings'][0]['field'], 'negativeSum')
        page = compile_document(current, {'title': '统计', 'html': '<html><head></head><body>{{negative}}</body></html>',
            'bindings': [{'key': 'negative', 'valueRef': 'derived_distribution', 'field': 'negativeSum'}]})
        self.assertIn('-5', page['html'])
        self.assertEqual(output['resultChanges']['unchanged'], ['derived_distribution'])
        unchanged = next(value for value in output['derivedValues'] if value['derivedId'] == 'derived_distribution')
        self.assertNotIn('rows', unchanged)
        restored = await self.service.inspect(**unchanged['detailRead'])
        self.assertEqual(restored['rowCount'], 4)

    async def test_null_sum_cannot_become_zero_but_count_is_usable(self):
        _, artifact = await self.prepare((None, None))
        operations = artifact['plan']['operations'] + [
            {'id': 'count', 'operator': 'ARITHMETIC', 'inputRefs': [{'valueRef': 'distribution', 'field': 'total'}], 'expression': 'v0'},
            {'id': 'missing', 'operator': 'ARITHMETIC', 'inputRefs': [{'valueRef': 'distribution', 'field': 'sum'}], 'expression': 'v0'}]
        output = calculate(artifact['facts'], artifact['scope'], operations, self.rules)
        self.assertEqual(output[1]['value'], '2')
        self.assertEqual(output[2]['diagnostics'][0]['code'], 'SCALAR_VALUE_UNAVAILABLE')
        self.assertIsNone(output[2]['value'])

    async def test_errors_are_local_and_corrected_inputs_execute(self):
        _, artifact = await self.prepare()
        references = [fact['factId'] for fact in artifact['facts'][:2]]
        bad = {'id': 'addition', 'operator': 'ARITHMETIC', 'inputRefs': references, 'expression': 'a+b'}
        cache = {}
        first = calculate(artifact['facts'], artifact['scope'], [bad, *artifact['plan']['operations']], self.rules, failure_cache=cache)
        self.assertEqual(first[0]['diagnostics'][0]['code'], 'UNKNOWN_ARITHMETIC_VARIABLE')
        self.assertEqual(first[0]['diagnostics'][0]['details']['variableMap']['v0'], references[0])
        self.assertEqual(first[1]['calculationValidation'], 'PASS')
        repeated = calculate(artifact['facts'], artifact['scope'], [bad], self.rules, failure_cache=cache)
        self.assertTrue(repeated[0]['diagnostics'][0]['details']['repeatedRequest'])
        corrected = calculate(artifact['facts'], artifact['scope'], [{**bad, 'expression': 'v0+v1'}], self.rules, failure_cache=cache)
        self.assertEqual(corrected[0]['value'], '7')
        changed = deepcopy(artifact['facts'])
        changed[0]['value'] = '20'
        retried = calculate(changed, artifact['scope'], [bad], self.rules, failure_cache=cache)
        self.assertNotIn('repeatedRequest', retried[0]['diagnostics'][0]['details'])

    async def test_work_field_can_be_rebound_without_rebuilding_or_expanding_targets(self):
        _, artifact = await self.prepare()
        negative = [fact['factId'] for fact in artifact['facts'] if fact['value'] in ('-3', '-2')]
        artifact['plan']['workPlan'] = [{'taskId': 'negative', 'title': '负值合计', 'kind': 'COMPUTATION',
            'operator': 'ARITHMETIC', 'targetRefs': negative, 'refs': []}]
        artifact['workRefs'] = [{'taskId': 'negative', 'operator': 'DESCRIBE',
            'refs': [{'valueRef': 'derived_distribution', 'field': 'negativeSum'}]}]
        complete = completion_summary(artifact)
        self.assertEqual(complete['state'], 'READY', complete)
        _, work = continue_plan(artifact, artifact['scope'], None)
        self.assertEqual(work[0]['operator'], 'DESCRIBE')
        artifact['workRefs'][0]['refs'][0]['field'] = 'sum'
        wrong = completion_summary(artifact)
        self.assertEqual(wrong['state'], 'INCOMPLETE')
        self.assertTrue(wrong['items'][0]['unexpectedTargetRefs'])

    async def test_formula_requirement_cannot_be_replaced_by_statistics(self):
        _, artifact = await self.prepare()
        task = {'taskId': 'formula', 'title': '来源构成', 'kind': 'COMPUTATION',
                'operator': 'FORMULA_CHECK', 'refs': [], 'targetRefs': [artifact['facts'][0]['factId']]}
        artifact['plan']['workPlan'] = [task]
        artifact['workRefs'] = [{'taskId': 'formula', 'operator': 'DESCRIBE', 'refs': ['derived_distribution']}]
        with self.assertRaises(AnalysisError):
            completion_summary(artifact)
        artifact.pop('workRefs')
        with self.assertRaises(AnalysisError):
            continue_plan(artifact, artifact['scope'], [{**task, 'operator': 'DESCRIBE'}])

    async def test_business_sum_rule_cannot_be_bypassed_by_rebinding(self):
        _, artifact = await self.prepare()
        task = {'taskId': 'sum', 'title': '业务合计', 'kind': 'COMPUTATION',
                'operator': 'SUM', 'refs': [], 'targetRefs': [artifact['facts'][0]['factId']]}
        artifact['plan']['workPlan'] = [task]
        artifact['workRefs'] = [{'taskId': 'sum', 'operator': 'DESCRIBE',
                                 'refs': [{'valueRef': 'derived_distribution', 'field': 'sum'}]}]
        with self.assertRaises(AnalysisError):
            completion_summary(artifact)

    async def test_narrowed_arithmetic_field_retains_selected_subjects(self):
        built, artifact = await self.prepare()
        output = await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[{
            'id': 'negative', 'operator': 'ARITHMETIC',
            'inputRefs': [{'valueRef': 'distribution', 'field': 'negativeSum'}],
            'expression': 'v0'}], requestKey='selected-subjects')
        current = self.store.get_analysis(OWNER, output['analysisRef'])
        value = next(value for value in current['derivedValues'] if value['derivedId'] == 'derived_negative')
        expected = {fact['factId'] for fact in artifact['facts'] if fact['value'] in ('-3', '-2')}
        self.assertEqual(scalar_coverage(value, {'valueRef': 'derived_negative'}), expected)

    async def test_ratio_denominator_does_not_expand_declared_subjects(self):
        built, artifact = await self.prepare()
        targets = [fact['factId'] for fact in artifact['facts'] if fact['value'] in ('-3', '-2')]
        result = await self.service.build(baseAnalysisRef=built['analysisRef'], requestKey='ratio-subjects', operations=[{
            'id': 'share', 'operator': 'ARITHMETIC', 'inputRefs': [
                {'valueRef': 'distribution', 'field': 'negativeSum'},
                {'valueRef': 'distribution', 'field': 'sum'}], 'expression': 'v0/v1*100', 'decimalPlaces': 2}],
            workPlan=[{'taskId': 'loss-share', 'title': '负值占全体的比例', 'kind': 'COMPUTATION',
                       'operator': 'ARITHMETIC', 'targetRefs': targets,
                       'refs': [{'valueRef': 'share', 'field': 'value'}]}])
        self.assertEqual(result['completion']['state'], 'READY', result['completion'])

    async def test_field_catalog_retains_scope_not_organization_count(self):
        _, artifact = await self.prepare(('10', '-3'))
        value = artifact['derivedValues'][0]
        catalog = {item['field']: item for item in field_catalog(value)}
        self.assertEqual(catalog['topShare']['memberCount'], 2)
        self.assertEqual(catalog['topShare']['top'], 3)
        self.assertEqual(catalog['topShare']['denominatorField'], 'sum')
        self.assertEqual(catalog['negativeSum']['memberCount'], 1)
        self.assertNotIn('organizationCount', json.dumps(catalog))
        directory = document_inputs(artifact)[0]
        self.assertNotIn('statistics', directory)
        self.assertNotIn('counts', directory)

    async def test_field_dependencies_survive_incremental_merge(self):
        built, _ = await self.prepare()
        ratio = await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[{
            'id': 'single', 'operator': 'ARITHMETIC', 'inputRefs': [{'valueRef': 'distribution', 'field': 'negativeSum'}],
            'expression': 'v0'}], requestKey='single')
        later = await self.service.build(baseAnalysisRef=ratio['analysisRef'], operations=[], requestKey='later')
        value = next(value for value in later['derivedValues'] if value['derivedId'] == 'derived_single')
        self.assertEqual(value['value'], '-5')

    async def test_count_field_reuses_actual_negative_subset(self):
        _, artifact = await self.prepare()
        operations = [{'id': 'count', 'operator': 'COUNT'}, {'id': 'negativeCount', 'operator': 'ARITHMETIC',
            'inputRefs': [{'valueRef': 'count', 'field': 'negative'}], 'expression': 'v0'}]
        output = calculate(artifact['facts'], artifact['scope'], operations, self.rules)
        expected = [fact['factId'] for fact in artifact['facts'] if fact['value'] in ('-3', '-2')]
        self.assertEqual(output[1]['value'], '2')
        self.assertEqual(output[1]['selectedCoverageRefs'], sorted(expected))


class ContractTests(TestCase):
    def test_formula_fields_keep_local_and_recursive_meaning_in_arithmetic(self):
        from analysis_formula_binding_test import FormulaBindingTests
        from analysis_extension.engine import RuleRegistry
        artifact = FormulaBindingTests().artifact()
        operations = [{'id': 'formula', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']},
                      {'id': 'local', 'operator': 'ARITHMETIC', 'inputRefs': [
                          {'valueRef': 'formula', 'field': 'localDifference'}], 'expression': 'abs(v0)'},
                      {'id': 'recursive', 'operator': 'ARITHMETIC', 'inputRefs': [
                          {'valueRef': 'formula', 'field': 'difference'}], 'expression': 'abs(v0)'}]
        output = calculate(artifact['facts'], artifact['scope'], operations, RuleRegistry())
        self.assertEqual(output[1]['value'], '1322980.43')
        self.assertEqual(output[1]['selectedCoverageRefs'], ['root'])
        self.assertEqual(output[2]['diagnostics'][0]['code'], 'UPSTREAM_CALCULATION_BLOCKED')
        self.assertEqual(output[2]['diagnostics'][0]['details']['suggestedField'], 'localDifference')

    def test_large_formula_preview_retains_checks_and_frozen_artifact(self):
        from analysis_formula_binding_test import FormulaBindingTests
        artifact = FormulaBindingTests().artifact()
        value = artifact['derivedValues'][0]
        value['formulaTrace']['nodes'][0]['expression'] = 'x' * 18000
        before = deepcopy(artifact)
        projected, _, _ = project_results([value], compact_derived([value], analysis_ref='an_test'), 'an_test')
        result = projected[0]
        self.assertTrue(result['previewOmitted'])
        self.assertEqual(result['formulaTrace']['targetLocalCheck'], value['formulaTrace']['targetLocalCheck'])
        self.assertEqual(result['formulaTrace']['status'], 'BLOCKED')
        self.assertEqual(result['value'], value['value'])
        self.assertEqual(result['detailRead'], {'section': 'formulas', 'analysisRef': 'an_test', 'factRef': 'root'})
        self.assertEqual(artifact, before)
        repeated, _, _ = project_results([value], compact_derived([value], analysis_ref='an_test'), 'an_test', [value])
        self.assertEqual(repeated[0]['formulaTrace']['targetLocalCheck'], value['formulaTrace']['targetLocalCheck'])
        self.assertEqual(repeated[0]['formulaTrace']['status'], 'BLOCKED')

    def test_operation_parameters_are_checked_before_reading_values(self):
        cases = [({'expression': 'v0'}, 'ARITHMETIC_INPUTS'),
                 ({'inputRefs': ['a'], 'expression': 'v0', 'variables': {'a': '1'}}, 'INVALID_OPERATION_ARGUMENT'),
                 ({'inputRefs': 1, 'expression': 'v0'}, 'INVALID_OPERATION_INPUTS'),
                 ({'inputRefs': ['a'], 'expression': '2'}, 'UNUSED_ARITHMETIC_INPUT'),
                 ({'inputRefs': ['a', {'valueRef': 'a', 'field': 'value'}], 'expression': 'v0+v1'}, 'DUPLICATE_INPUT')]
        for supplied, expected in cases:
            with self.subTest(supplied=supplied), self.assertRaises(AnalysisError) as raised:
                validate_operation({'id': 'test', 'operator': 'ARITHMETIC', **supplied})
            self.assertEqual(raised.exception.code, expected)

    def test_shared_tool_schema_advertises_actual_field_inputs(self):
        operation = {'id': 'sum', 'operator': 'ARITHMETIC',
                     'inputRefs': [{'valueRef': 'derived_stats', 'field': 'negativeSum'}], 'expression': 'v0'}
        build = {'baseAnalysisRef': 'an_test', 'operations': [operation], 'requestKey': 'build'}
        self.assertFalse(list(Draft202012Validator(AnalysisBuild.input_schema).iter_errors(build)))
        invalid = deepcopy(build)
        invalid['operations'][0]['variables'] = {'a': '2'}
        self.assertTrue(list(Draft202012Validator(AnalysisBuild.input_schema).iter_errors(invalid)))
        python_schema = AnalysisPython.input_schema['properties']['operations']['items']
        self.assertFalse(list(Draft202012Validator(python_schema).iter_errors(operation)))
        correction = {'analysisRef': 'an_test', 'requestKey': 'publish', 'workRefs': [
            {'taskId': 'sum', 'operator': 'DESCRIBE', 'refs': [{'valueRef': 'derived_stats', 'field': 'negativeSum'}]}]}
        self.assertFalse(list(Draft202012Validator(AnalysisPublish.input_schema['properties']['workRefs']).iter_errors(correction['workRefs'])))
