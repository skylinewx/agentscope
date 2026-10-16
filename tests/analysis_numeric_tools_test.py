import ast
import json
from pathlib import Path
import unittest

from analysis_extension.models import AnalysisError
from numeric_tools import calculate, sort_records, NumericToolsMiddleware, Calculate, SortRecords


class NumericTests(unittest.TestCase):
    def test_arithmetic_preserves_precision_and_uses_business_variables(self):
        self.assertEqual(calculate('revenue - cost', {'revenue': '9007199254740993.1', 'cost': '0.2'})['value'], '9007199254740992.9')
        self.assertEqual(calculate('0.1 + 0.2 - 0.3')['value'], '0')
        self.assertEqual(calculate('sum(1, 2, -3) + abs(-4) + max(2, 5) - min(1, 8)')['value'], '8')
        self.assertEqual(calculate('(1 / 3) * 3')['value'], '1')
        self.assertFalse(calculate('1 / 8')['rounded'])

    def test_rounding_is_explicit_and_handles_negative_ties(self):
        with self.assertRaisesRegex(AnalysisError, '无限小数'): calculate('1 / 3')
        self.assertEqual(calculate('1 / 3', decimalPlaces=4), {'value': '0.3333', 'rounded': True, 'decimalPlaces': 4, 'rounding': 'HALF_UP'})
        self.assertEqual(calculate('-1.005', decimalPlaces=2)['value'], '-1.01')
        self.assertEqual(calculate('9.995', decimalPlaces=2)['value'], '10.00')
        self.assertEqual(calculate('1 / 10000000000000000000000000000000000000000')['value'], '0.' + '0' * 39 + '1')

    def test_calculator_refuses_executable_syntax_bad_values_and_capacity(self):
        for expression in ('1 / 0', '__import__("os")', '1 ** 1000', 'x.real', '[1,2]', 'True', 'abs(1, 2)', '1e9999', 'min(a=1)', '+'.join(['1'] * 500)):
            with self.subTest(expression=expression), self.assertRaises(AnalysisError): calculate(expression)
        for value in ('NaN', '1,000', 'Infinity', True, 0.1):
            with self.subTest(value=value), self.assertRaises(AnalysisError): calculate('x', {'x': value})

    def test_stable_numeric_sort_precedes_pagination_and_null_is_not_zero(self):
        records = [{'id': str(i), 'values': {'amount': value, 'name': '保留名称'}}
                   for i, value in enumerate(('-8300', '-1033.14', '-1033.14', None, '0', '9007199254740993', '9007199254740992'))]
        keys = [{'field': 'amount', 'type': 'number', 'direction': 'desc', 'nulls': 'last'}]
        first = sort_records(records, keys, limit=3)
        self.assertEqual([row['id'] for row in first['records']], ['5', '6', '4'])
        self.assertEqual(first['nextOffset'], 3)
        next_page = sort_records(records, keys, offset=3, limit=10)
        self.assertEqual(next_page['records'], [records[i] for i in (1, 2, 0, 3)])
        self.assertEqual(next_page['total'], 7)
        self.assertIsNone(next_page['nextOffset'])
        for direction in ('asc', 'desc'):
            key = {**keys[0], 'direction': direction, 'nulls': 'first'}
            self.assertEqual(sort_records(records, [key])['records'][0]['id'], '3')

    def test_multiple_sort_keys_and_input_records_are_unchanged(self):
        records = [{'id': str(i), 'values': {'group': group, 'amount': amount}} for i, (group, amount) in enumerate((('乙','3'), ('甲','1'), ('甲','2'), ('甲','2')))]
        before = json.dumps(records)
        keys = [{'field':'group','type':'text','direction':'desc','nulls':'last'}, {'field':'amount','type':'number','direction':'desc','nulls':'last'}]
        self.assertEqual([r['id'] for r in sort_records(records, keys)['records']], ['2', '3', '1', '0'])
        self.assertEqual(json.dumps(records), before)
        for bad in ([records[0], records[0]], [{'id':'a','values':{}}], [{'id':'a','values':{'amount':'unknown'}}]):
            with self.subTest(bad=bad), self.assertRaises(AnalysisError): sort_records(bad, [keys[1]])


class NumericRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_available_without_analysis_or_a_selected_skill(self):
        middleware = NumericToolsMiddleware()
        tools = await middleware.list_tools()
        self.assertEqual([tool.name for tool in tools], ['Calculate', 'SortRecords'])
        for tool in tools:
            self.assertTrue(tool.is_read_only)
        tree = ast.parse((Path(__file__).parents[1] / 'examples/agent_service/main.py').read_text('utf-8'))
        factory = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'longterm_memory_factory')
        returned = next(node for node in factory.body if isinstance(node, ast.Return))
        self.assertEqual(returned.value.elts[0].func.id, 'NumericToolsMiddleware')
        from jsonschema import Draft202012Validator, ValidationError
        for tool in tools: Draft202012Validator.check_schema(tool.input_schema)
        with self.assertRaises(ValidationError): Draft202012Validator(Calculate.input_schema).validate({'expression':'1', 'script':'bad'})
        response = await tools[0].call(expression='0.1 + 0.2')
        self.assertEqual(json.loads(response.content[0].text)['value'], '0.3')
        error = await tools[0].call(expression='1 / 0')
        self.assertEqual(json.loads(error.content[0].text)['error']['code'], 'ZERO_DENOMINATOR')
