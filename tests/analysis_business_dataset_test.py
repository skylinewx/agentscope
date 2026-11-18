"""Regression examples for named rows, exact values and independent consumers."""
import unittest
from copy import deepcopy

import analysis_fixtures  # Install the service import root.
from analysis_extension.business_dataset import table, rows, row_values, project
from analysis_extension.models import AnalysisError
from analysis_extension.python_products import products
from analysis_extension.python_analysis import input_record
from analysis_extension.document_data import compile_datasets
from analysis_extension.document_coverage import reference


COLUMNS = [{'name': 'organization', 'label': '同名', 'type': 'STRING'},
           {'name': 'amount', 'label': '同名', 'type': 'DECIMAL_STRING'}]


class BusinessDatasetTests(unittest.TestCase):
    def test_reserved_column_names_do_not_capture_business_values(self):
        columns = [{'name':name,'label':name,'type':'STRING'} for name in ('values','rowRef','metadata')]
        original = {'values': {'nested':'value'}, 'rowRef':'业务行名','metadata':'业务元数据'}
        converted = table(columns, [original])['rows'][0]
        self.assertEqual(converted['values'], original)
        self.assertNotEqual(converted['rowRef'], original['rowRef'])
        self.assertEqual(table(columns,[converted])['rows'][0], converted)

    def test_column_order_never_changes_named_value_and_preserves_decimal(self):
        raw = [{'values': {'amount': '9007199254740993.005', 'organization': '甲'}}]
        original = deepcopy(raw)
        a = table(COLUMNS, raw)
        b = table(list(reversed(COLUMNS)), raw)
        self.assertEqual(a['rows'][0]['values'], b['rows'][0]['values'])
        self.assertEqual(a['rows'][0]['values']['amount'], '9007199254740993.005')
        self.assertEqual(raw, original)

    def test_null_missing_failure_and_zero_remain_distinct(self):
        data = rows(COLUMNS, [
            {'values': {'organization': '空', 'amount': None}},
            {'values': {'organization': '缺'}},
            {'values': {'organization': '失败', 'amount': None}, 'valueStates': {'amount': 'FAILED'}},
            {'values': {'organization': '零', 'amount': '0'}}])
        self.assertEqual([r['valueStates']['amount'] for r in data], ['NO_VALUE', 'MISSING', 'FAILED', 'PRESENT'])
        self.assertNotIn('amount', data[1]['values'])
        with self.assertRaises(AnalysisError):
            rows(COLUMNS, [{'values': {}, 'valueStates': {'amount': 'NO_VALUE'}}])

    def test_reading_native_and_historical_arrays_does_not_mutate_them(self):
        original = {'outputKind': 'TABLE', 'columns': COLUMNS, 'rows': [['甲', '1.005']]}
        frozen = deepcopy(original)
        self.assertEqual(project(original)['rows'][0]['values']['amount'], '1.005')
        self.assertEqual(original, frozen)
        self.assertEqual(input_record('d', original)['rows'][0]['values']['amount'], '1.005')

    def test_unknown_or_duplicate_keys_are_not_silently_dropped(self):
        with self.assertRaises(AnalysisError): rows(COLUMNS, [{'amount': '2', 'hidden': '3'}], named_only=True)
        with self.assertRaises(AnalysisError): table([COLUMNS[0], COLUMNS[0]], [])
        with self.assertRaises(AnalysisError): rows(COLUMNS, [['甲', '2']], named_only=True)
        with self.assertRaises(AnalysisError): rows(COLUMNS, [
            {'rowRef': 'same', 'values': {}}, {'rowRef': 'same', 'values': {}}])

    def test_python_product_rounding_and_document_share_named_rows(self):
        columns = deepcopy(COLUMNS)
        columns[1]['decimalPlaces'] = 2
        values = products([{'name': 'comparison', 'label': '比较', 'kind': 'TABLE', 'inputRefs': ['f'],
                            'columns': columns, 'rows': [{'values': {'organization': '甲', 'amount': '1.005'}}]}],
                          {'f': {'sourceFactIds': ['f']}}, ['f'], 'scope', 'python_test')
        value = values[0]
        self.assertEqual(value['rows'][0]['values']['amount'], '1.01')
        datasets, _ = compile_datasets({'facts': [], 'derivedValues': values},
                                      [{'key': 'comparison', 'valueRef': value['derivedId']}])
        self.assertEqual(datasets['comparison']['rows'], value['rows'])

    def test_sort_and_statistics_binding_use_the_same_named_fields(self):
        facts = [{'factId': 'f1', 'objectId': 'o1', 'objectLabel': '甲', 'valueState': 'PRESENT', 'value': '-2', 'unit': '元'},
                 {'factId': 'f2', 'objectId': 'o2', 'objectLabel': '乙', 'valueState': 'NO_VALUE', 'value': None, 'unit': '元'}]
        sort = {'derivedId': 's', 'operator': 'SORT', 'calculationValidation': 'PASS',
                'orderedFactIds': ['f1'], 'unavailableFactIds': ['f2']}
        ds, _ = compile_datasets({'facts': facts, 'derivedValues': [sort]}, [{'key': 'order', 'valueRef': 's'}])
        self.assertEqual(ds['order']['rows'][0]['values']['value'], '-2')
        self.assertEqual(ds['order']['rows'][0]['metadata']['objectId'], 'o1')
        self.assertIsNone(ds['order']['unavailable'][0]['values']['value'])
        self.assertIn('f1', reference({'datasets': ds}, {'setKey': 'order', 'factRef': 'f1'}))

    def test_formula_and_diagnostic_are_not_flattened(self):
        for original in ({'formulaTrace': {'nodes': [{'factRef': 'x', 'dependencies': []}]}},
                         {'outputKind': 'DIAGNOSTIC', 'data': {'source': None, 'difference': '2'}}):
            self.assertEqual(project(original), original)

    def test_positional_python_output_converts_only_with_unambiguous_columns(self):
        value = products([{'name': 'table', 'label': '表', 'kind': 'TABLE', 'inputRefs': ['f'],
                           'columns': COLUMNS, 'rows': [['甲', '1.005']]}],
                         {'f': {'sourceFactIds': ['f']}}, ['f'], 'scope', 'run')[0]
        self.assertEqual(value['rows'][0]['values'], {'organization': '甲', 'amount': '1.005'})
        with self.assertRaises(AnalysisError):
            products([{'name': 'table', 'label': '表', 'kind': 'TABLE', 'inputRefs': ['f'],
                       'columns': COLUMNS, 'rows': [['甲']]}], {'f': {}}, ['f'], 'scope', 'run')
