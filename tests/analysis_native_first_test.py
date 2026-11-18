"""原生能力优先的结果契约，以及自定义代码选择的可追溯记录。"""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.document_data import compile_datasets
from analysis_extension.engine import calculate
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.native_analysis import describe, document_inputs
from analysis_extension.tools import AnalysisPython
from analysis_extension.validation import selected_value


class NativeAnalysisTests(PresentationCase):
    async def build_values(self, values, **operation):
        reference, selectors, scope = saved_fixture(self.store, values)
        result = await self.service.build([reference], selectors, scope,
            [{'id': 'distribution', 'operator': 'DESCRIBE', **operation}], 'describe',
            workPlan=[{'taskId': 'distribution', 'title': '来源值分布', 'kind': 'COMPUTATION',
                       'operator': 'DESCRIBE', 'refs': ['distribution']}])
        return result, self.store.get_analysis(OWNER, result['analysisRef'])

    async def test_distribution_preserves_sign_missing_and_exact_amount(self):
        built, artifact = await self.build_values(('0.1', '0.2', '-0.15', '0', None), top=2)
        value = artifact['derivedValues'][0]
        self.assertEqual(value['calculationValidation'], 'PASS', value['diagnostics'])
        self.assertEqual(value['counts'], dict(total=5, observed=4, unavailable=1, positive=2, negative=1, zero=1))
        self.assertEqual(value['statistics'], dict(sum='0.15', positiveSum='0.3', negativeSum='-0.15',
            minimum='-0.15', maximum='0.2', mean='0.04', topSum='0.3', topShare='200.00'))
        self.assertEqual([row[3] for row in value['rows']], ['0.2', '0.1', '0', '-0.15', None])
        self.assertEqual(built['completion']['pendingTaskIds'], [])
        self.assertEqual(built['documentInputs'][0]['dataset'], {'valueRef': 'derived_distribution'})
        self.assertIn('not_consolidated', value['meaning'])

    async def test_all_missing_retains_rows_and_null_statistics(self):
        _, artifact = await self.build_values((None, None), top=1)
        value = artifact['derivedValues'][0]
        self.assertTrue(all(number is None for number in value['statistics'].values()))
        self.assertEqual(value['counts']['unavailable'], 2)
        self.assertEqual(len(value['rows']), 2)
        self.assertEqual(selected_value(value, 'total'), '2')

    async def test_zero_denominator_has_no_share(self):
        _, artifact = await self.build_values(('10', '-10', '0'), top=1)
        value = artifact['derivedValues'][0]
        self.assertEqual(value['value'], '0')
        self.assertIsNone(value['statistics']['topShare'])
        self.assertTrue(all(row[5] is None for row in value['rows']))

    async def test_negative_total_uses_signed_amount_and_ordered_top(self):
        _, artifact = await self.build_values(('-2', '-1'), top=1, direction='asc')
        value = artifact['derivedValues'][0]
        self.assertEqual(value['statistics']['positiveSum'], '0')
        self.assertEqual(value['statistics']['negativeSum'], '-3')
        self.assertEqual(value['statistics']['topShare'], '66.67')
        self.assertEqual(value['rows'][0][3], '-2')

    async def test_huge_decimals_do_not_round_source_or_emit_exponents(self):
        _, artifact = await self.build_values(('123456789012345678.123456789', '0.000000001'))
        value = artifact['derivedValues'][0]
        self.assertEqual(Decimal(value['value']), Decimal('123456789012345678.123456790'))
        self.assertNotIn('E', value['value'])

    async def test_statistics_and_counts_bind_without_python(self):
        _, artifact = await self.build_values(('3', '-1', None), top=1)
        document = {'title': '来源分布', 'html': '<html><head></head><body>{{count}} {{amount}} {{share}}</body></html>',
            'bindings': [{'key': 'count', 'valueRef': 'derived_distribution', 'field': 'total'},
                         {'key': 'amount', 'valueRef': 'derived_distribution', 'field': 'negativeSum'},
                         {'key': 'share', 'valueRef': 'derived_distribution', 'field': 'topShare'}],
            'datasets': [{'key': 'distribution', 'valueRef': 'derived_distribution'}]}
        compiled = compile_document(artifact, document)
        dataset = compiled['datasets']['distribution']
        self.assertEqual(dataset['kind'], 'TABULAR')
        self.assertEqual(dataset['statistics']['negativeSum'], '-1')
        self.assertEqual(dataset['counts']['total'], 3)
        self.assertNotIn('pythonRunRef', dataset)
        self.assertEqual([row[3] for row in dataset['rows']], ['3', '-1', None])
        self.assertIn('"unit":"项"', compiled['html'])
        self.assertIn('"unit":"%"', compiled['html'])
        page = await self.service.inspect(section='products', analysisRef=artifact['analysisRef'],
                                          derivedId='derived_distribution', limit=1)
        self.assertEqual(page['rows']['items'][0][3], '3')
        self.assertEqual(page['rows']['total'], 3)

    async def test_select_batch_column_and_role_excludes_other_material(self):
        _, artifact = await self.build_values(('1', '2'))
        facts = artifact['facts']
        for fact in facts:
            fact.update(batchId='batch-one', columnName='amount', inputRole='PRIMARY')
        extra = deepcopy(facts[0])
        extra.update(factId='other-metric', columnName='other')
        results = calculate([*facts, extra], artifact['scope'], [{'id': 'filtered', 'operator': 'DESCRIBE',
            'select': {'batchId': 'batch-one', 'columnName': 'amount', 'inputRole': 'PRIMARY'}}], self.rules)
        self.assertEqual(results[0]['value'], '3')
        self.assertEqual(results[0]['counts']['total'], 2)

    async def test_distinct_grain_metric_period_unit_and_precision_do_not_silently_mix(self):
        _, artifact = await self.build_values(('1', '2'))
        for key, changed in [('grain', ['different']), ('period', '2025N0001'), ('unit', '万元'),
                             ('measureCode', 'another'), ('precision', 'display_only')]:
            facts = deepcopy(artifact['facts'])
            facts[0][key] = changed
            with self.subTest(key=key), self.assertRaises(AnalysisError):
                describe(facts, artifact['scope'], {})
        with self.assertRaises(AnalysisError):
            describe([artifact['facts'][0], artifact['facts'][0]], artifact['scope'], {})

    async def test_grouped_input_counts_groups_not_member_population(self):
        _, artifact = await self.build_values(('3', '4'))
        facts = artifact['facts']
        for index, fact in enumerate(facts):
            fact.update(grain=['group'], dimensions={'group': str(index)},
                        aggregation={'groupBy': [{'dimCode': 'MD_ORG', 'field': 'attribute'}]})
        result = describe(facts, {'memberDimension': 'PER_INPUT_GRAIN'}, {})
        self.assertEqual(result['counts']['total'], 2)
        self.assertNotIn('organizationCount', result['counts'])

    async def test_native_formula_directory_reuses_check_and_retains_missing(self):
        from analysis_closeout_test import mixed_fixture
        reference, selectors, scope = mixed_fixture(self.store, 1)
        capture = self.store.get_capture(OWNER, reference)
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['data']['stepResults'][1]['formResults'][0]['page']['cells'][1].update(rawValueJson='null', displayValue='')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        reference = self.store.capture(OWNER, capture)
        selectors[0]['captureRef'] = scope['population']['captureRef'] = reference
        built = await self.service.build([reference], selectors, scope,
            [{'id': 'formula', 'operator': 'FORMULA_CHECK', 'select': {'cellKey': '2:1', 'objectId': 'object-0'}}], 'formula')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        directory = document_inputs(artifact)
        self.assertEqual(directory[0]['localStatus'], 'BLOCKED')
        self.assertEqual(directory[0]['recursiveStatus'], 'BLOCKED')
        self.assertFalse(directory[0]['checkSummary']['allLocalChecksCompleted'])
        self.assertFalse(directory[0]['checkSummary']['allLocalChecksMatch'])
        self.assertGreater(directory[0]['checkSummary']['localUnavailableCount'], 0)
        datasets, _ = compile_datasets(artifact, [{'key': 'formula', **directory[0]['dataset']}])
        self.assertIsNone(datasets['formula']['nodes'][0]['values']['localValue']['value'])
        self.assertEqual(len(artifact['derivedValues']), 1)

    def test_formula_summary_distinguishes_root_match_from_incomplete_chain(self):
        from analysis_extension.native_analysis import formula_check_summary
        nodes = [{'factRef': 'target', 'expression': 'A1', 'localCheck': {'status': 'MATCH'}},
                 {'factRef': 'subtotal', 'expression': 'B1+C1', 'localCheck': {'status': 'BLOCKED'}},
                 {'factRef': 'input', 'expression': None, 'localCheck': {'status': 'NOT_APPLICABLE'}}]
        summary = formula_check_summary({'nodes': nodes})
        self.assertEqual(summary['formulaNodeCount'], 2)
        self.assertEqual(summary['localMatchedCount'], 1)
        self.assertEqual(summary['localUnavailableCount'], 1)
        self.assertFalse(summary['allLocalChecksMatch'])
        self.assertEqual(summary['exceptions'][0]['factRef'], 'subtotal')
        nodes[1]['localCheck'] = {'status': 'MISMATCH', 'difference': '1'}
        summary = formula_check_summary({'nodes': nodes})
        self.assertTrue(summary['allLocalChecksCompleted'])
        self.assertFalse(summary['allLocalChecksMatch'])
        self.assertEqual(summary['localMismatchedCount'], 1)


class CustomCodeDecisionTests(PresentationCase):
    async def test_declared_reason_code_inputs_status_and_export_survive_execution(self):
        reference, selectors, scope = saved_fixture(self.store, ('3',))
        code = 'result=[{"name":"custom","label":"条件结果","value":D(inputs[0]["value"])*2,"unit":None,"inputRefs":[inputs[0]["ref"]]}]'
        result = await self.service.python(captureRefs=[reference], selections=selectors, scope=scope,
            code=code, purpose='执行自定义条件处理', fallbackReason='需要未登记的特定变换与输出形式', requestKey='custom')
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertEqual(run['capabilityDecision']['reason'], '需要未登记的特定变换与输出形式')
        self.assertEqual(run['capabilityDecision']['processing'], '执行自定义条件处理')
        self.assertEqual(run['capabilityDecision']['codeChars'], len(code))
        self.assertEqual(run['code'], code)
        self.assertEqual(run['status'], 'COMPLETED')
        self.assertTrue(run['inputRecords'])
        specification = importlib.util.spec_from_file_location('usage_export', Path(__file__).parents[1] / 'scripts/export_analysis_capability_usage.py')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        report = module.export_usage(self.root / 'analysis-data', OWNER.agent_id, OWNER.session_id, True)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]['code'], code)
        self.assertEqual(report[0]['capabilityDecision'], run['capabilityDecision'])
        self.assertEqual(module.export_usage(self.root / 'analysis-data', 'another-agent'), [])

    async def test_failed_execution_keeps_reason_and_attempted_code(self):
        reference, selectors, scope = saved_fixture(self.store, ('3',))
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(captureRefs=[reference], selections=selectors, scope=scope,
                code='result=[{"value":"5"}]', purpose='诊断特定输出', fallbackReason='现有算子不能表达所需诊断', requestKey='bad')
        run = self.store.get_python_run(OWNER, caught.exception.details['pythonRunRef'])
        self.assertEqual(run['status'], 'FAILED')
        self.assertEqual(run['capabilityDecision']['declaration'], 'DECLARED')
        self.assertEqual(run['capabilityDecision']['reason'], '现有算子不能表达所需诊断')

    def test_public_tool_requires_reason_without_guessing_legacy_reason(self):
        payload = dict(code='result=[]', purpose='处理', requestKey='test')
        validator = Draft202012Validator(AnalysisPython.input_schema)
        self.assertTrue(any('fallbackReason' in error.message for error in validator.iter_errors(payload)))
        payload['fallbackReason'] = '需要现有算子不支持的文件透视'
        self.assertEqual(list(validator.iter_errors(payload)), [])
        from analysis_extension.code_decisions import decision
        self.assertEqual(decision(None, '旧调用', 'result=[]')['declaration'], 'UNDECLARED')
        for invalid in ('', ' ', 3):
            with self.assertRaises(AnalysisError):
                decision(invalid, '处理', 'result=[]')
