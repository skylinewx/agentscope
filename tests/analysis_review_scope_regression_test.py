"""整体审查复现：多指标追问按真实定位承接，不能按位置借用时期。"""
from copy import deepcopy
from unittest import TestCase

import analysis_fixtures
from analysis_extension.followup_scope import _inherit_query
from analysis_extension.models import AnalysisError
from analysis_extension.tools import OPERATION
from analysis_extension.operation_contracts import validate_operation
from numeric_tools import Calculate, calculate
from jsonschema import Draft202012Validator


def metric(account, period=None, binding='binding-A', name=None):
    value = {'bindingIri': binding, 'selection': {'measureCode': 'amount',
             'targetDimensions': {'ACCOUNT': account}}}
    if period is not None:
        value['selection']['periodCode'] = period
    if name:
        value['name'] = name
    return value


class MetricPeriodInheritanceTests(TestCase):
    def setUp(self):
        self.previous = {'metrics': [metric('PROFIT', '2025N0001'), metric('COST', '2026N0001')]}

    def inherit(self, metrics, changes=('metric',)):
        proposed = {'metrics': metrics}
        original = deepcopy(proposed)
        result = _inherit_query(self.previous, proposed, set(changes))
        self.assertEqual(proposed, original)
        return result['metrics']

    def test_subset_and_reordered_metrics_inherit_their_own_period(self):
        selected = self.inherit([metric('COST', name='new-alias')])
        self.assertEqual(selected[0]['selection']['periodCode'], '2026N0001')
        reordered = self.inherit([metric('COST'), metric('PROFIT')])
        self.assertEqual([item['selection']['periodCode'] for item in reordered], ['2026N0001', '2025N0001'])

    def test_new_metric_does_not_choose_from_multiple_periods(self):
        self.assertNotIn('periodCode', self.inherit([metric('REVENUE')])[0]['selection'])

    def test_common_period_can_be_used_for_new_metric_within_same_binding(self):
        self.previous['metrics'][0]['selection']['periodCode'] = '2026N0001'
        self.assertEqual(self.inherit([metric('REVENUE')])[0]['selection']['periodCode'], '2026N0001')
        self.assertNotIn('periodCode', self.inherit([metric('REVENUE', binding='binding-B')])[0]['selection'])

    def test_same_metric_with_two_periods_stays_unresolved(self):
        self.previous['metrics'].append(metric('COST', '2024N0001'))
        self.assertNotIn('periodCode', self.inherit([metric('COST')])[0]['selection'])

    def test_missing_old_period_is_not_a_proven_common_period(self):
        self.previous['metrics'][0]['selection'].pop('periodCode')
        self.assertNotIn('periodCode', self.inherit([metric('REVENUE')])[0]['selection'])

    def test_explicit_period_and_changed_task_or_period_are_preserved(self):
        self.assertEqual(self.inherit([metric('COST', '2027N0001')])[0]['selection']['periodCode'], '2027N0001')
        for changes in [('task',), ('period',)]:
            self.assertNotIn('periodCode', self.inherit([metric('COST')], changes)[0]['selection'])


class ArithmeticContractTests(TestCase):
    def test_two_hundred_values_pass_schema_precheck_and_execution(self):
        refs = ['fact-' + str(index) for index in range(200)]
        names = ['v' + str(index) for index in range(200)]
        for expression in ['sum(' + ','.join(names) + ')', 'sum([' + ','.join(names) + '])']:
            operation = {'id': 'total', 'operator': 'ARITHMETIC', 'inputRefs': refs, 'expression': expression}
            Draft202012Validator(OPERATION).validate(operation)
            validate_operation(operation)
            self.assertEqual(calculate(expression, dict.fromkeys(names, '0.01'))['value'], '2')

    def test_201_values_are_rejected_by_both_entry_contracts(self):
        operation = {'id': 'total', 'operator': 'ARITHMETIC', 'inputRefs': ['fact-' + str(i) for i in range(201)], 'expression': 'v0'}
        self.assertTrue(list(Draft202012Validator(OPERATION).iter_errors(operation)))
        with self.assertRaises(AnalysisError) as caught:
            validate_operation(operation)
        self.assertEqual(caught.exception.code, 'INVALID_OPERATION_INPUTS')
        self.assertEqual(Calculate.input_schema['properties']['variables']['maxProperties'], 200)
        operation['operator'] = 'SUM'
        operation.pop('expression')
        Draft202012Validator(OPERATION).validate(operation)
        validate_operation(operation)

    def test_list_aggregates_preserve_decimal_precision_and_reject_arbitrary_python(self):
        self.assertEqual(calculate('sum([0.1, 0.2]) + max([1,2]) - min([1,2])')['value'], '1.3')
        for expression in ['sum([x for x in v0])', '[v0]+[v1]', 'sum([])', 'abs([v0])',
                           'sum([v0], v1)', '__import__("os")', 'v0.real', 'sum(*[v0])']:
            with self.subTest(expression=expression):
                with self.assertRaises(AnalysisError):
                    calculate(expression, {'v0': '1', 'v1': '2'})
                with self.assertRaises(AnalysisError):
                    validate_operation({'id': 'bad', 'operator': 'ARITHMETIC', 'inputRefs': ['f0', 'f1'], 'expression': expression})
