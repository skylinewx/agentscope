"""动态读取参数与追问意图的通用回归，不依赖业务模型或字段编码。"""
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from jinja2 import Template
from jsonschema import Draft202012Validator

from agentscope.tool._builtin._skill import SkillViewer
from agentscope.tool._toolkit import DEFAULT_SKILL_INSTRUCTION
from analysis_extension.models import AnalysisError
from analysis_extension.query_results import compact_receipt, encode, read_options, read_receipt
from analysis_extension.tools import AnalysisInspect, QueryResultRead
from analysis_fixtures import saved_fixture
from analysis_presentation_fixtures import PresentationCase


class ReadGuidanceTests(TestCase):
    def test_generated_reads_use_actual_fields_and_keep_null_zero_and_precision(self):
        receipt = {'unusual/items': [{'customKey': 'one', 'amount': '123456789012345678.00001', 'remark': None},
                                     {'customKey': 'two', 'amount': '0', 'remark': None}]}
        original = deepcopy(receipt)
        options = read_options(receipt, '', 'capture')
        self.assertEqual(options[0]['pointer'], '/unusual~1items')
        self.assertEqual(options[0]['availableFields'], ['customKey', 'amount', 'remark'])
        request = options[0]['read']
        Draft202012Validator(QueryResultRead.input_schema).validate(request)
        page = read_receipt(receipt, request.pop('captureRef'), **request)
        self.assertEqual(page['items'], receipt['unusual/items'])
        self.assertEqual(receipt, original)

    def test_navigation_projection_does_not_claim_to_read_large_children(self):
        receipt = {'forms': [{'name': 'a', 'page': {'cells': [{'key': 'C2', 'raw': '7'}] * 1000}}]}
        option = read_options(receipt, '', 'capture')[0]
        self.assertEqual(option['read']['fields'], ['name'])
        self.assertIn('page', option['availableFields'])
        page = read_receipt(receipt, 'capture', '/forms/0', fields=['name'])
        child = next(option for option in page['readOptions'] if option['pointer'] == '/forms/0/page')
        self.assertEqual(child['availableFields'], ['cells'])
        self.assertEqual(page['value'], {'name': 'a'})

    def test_error_recovery_has_capture_and_executable_child_read(self):
        receipt = {'data': {'actualField': 'x' * 26000}}
        with self.assertRaises(AnalysisError) as raised:
            read_receipt(receipt, 'capture', '/data', fields=['inventedField'])
        details = raised.exception.details
        self.assertEqual(details['captureRef'], 'capture')
        self.assertEqual(details['availableFields'], ['actualField'])
        request = details['readOptions'][0]['read']
        Draft202012Validator(QueryResultRead.input_schema).validate(request)
        self.assertEqual(len(read_receipt(receipt, request.pop('captureRef'), **request)['value']), 1000)

    def test_continuation_keeps_search_fields_and_actual_offset_after_size_limit(self):
        receipt = {'records': [{'key': str(index), 'tag': 'chosen' if index % 2 else 'other', 'payload': 'x' * 2500}
                               for index in range(40)]}
        request = {'captureRef': 'capture', 'pointer': '/records', 'search': 'chosen',
                   'fields': ['key', 'payload'], 'limit': 100}
        actual = []
        while request is not None:
            Draft202012Validator(QueryResultRead.input_schema).validate(request)
            page = read_receipt(receipt, request['captureRef'], **{key: value for key, value in request.items() if key != 'captureRef'})
            self.assertLessEqual(len(encode(page)), 24000)
            actual.extend(item['key'] for item in page['items'])
            request = page.get('nextRead')
            if request:
                self.assertEqual(request['offset'], page['nextOffset'])
                self.assertEqual(request['search'], 'chosen')
                self.assertEqual(request['fields'], ['key', 'payload'])
        self.assertEqual(actual, [str(index) for index in range(1, 40, 2)])

    def test_compact_receipt_points_directly_to_current_capture_analysis(self):
        receipt = {'data': {'stepResults': [{'stepId': 'arbitrary-step', 'rows': [{'key': 'record'}]}]}, 'evidence': ['x' * 20000]}
        compact = compact_receipt(receipt, 'observed-capture', len(encode(receipt)))
        Draft202012Validator(AnalysisInspect.input_schema).validate(compact['analysisRead'])
        self.assertEqual(compact['analysisRead']['captureRef'], 'observed-capture')
        self.assertLessEqual(len(encode(compact)), 24000)

    def test_skill_calls_are_generated_from_registered_names_and_match_real_schema(self):
        names = ['arbitrary-skill', '含空格 "动态" 名称']
        prompt = Template(DEFAULT_SKILL_INSTRUCTION).render(skill_viewer='Skill', skills=[
            SimpleNamespace(name=name, description='说明', dir='/test') for name in names])
        calls = [json.loads(value) for value in re.findall(r'<read_arguments>(.*?)</read_arguments>', prompt)]
        self.assertEqual(calls, [{'skill': name} for name in names])
        for call in calls:
            Draft202012Validator(SkillViewer.input_schema).validate(call)
        self.assertTrue(list(Draft202012Validator(SkillViewer.input_schema).iter_errors({'name': names[0]})))


class TargetedInventoryTests(PresentationCase):
    async def test_next_read_does_not_expand_character_budget_or_remove_source_rows(self):
        source = {'items': [{'value': 'x' * 23900}], 'nextOffset': 1, 'characterBudget': 24000}
        with patch.object(self.service, '_inspect', new=AsyncMock(return_value=source)):
            result = await self.service.inspect(section='inventory', captureRef='c' * 100, limit=1)
        self.assertEqual(result, source)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), result['characterBudget'])

    async def test_optional_nulls_are_not_repeated_in_next_call(self):
        source = {'items': [], 'nextOffset': 1}
        with patch.object(self.service, '_inspect', new=AsyncMock(return_value=source)):
            result = await self.service.inspect(section='inventory', analysisRef=None, captureRef='capture', limit=1, search=None)
        Draft202012Validator(AnalysisInspect.input_schema).validate(result['nextRead'])
        self.assertEqual(result['nextRead'], {'section': 'inventory', 'captureRef': 'capture', 'limit': 1, 'offset': 1})

    async def test_known_capture_does_not_scan_unrelated_history_and_next_read_keeps_filter(self):
        capture, _, _ = saved_fixture(self.store, ('1', '2', '3'))
        saved_fixture(self.store, ('9', '8'))
        with patch.object(self.store, 'list_captures', side_effect=AssertionError('不应扫描会话全部历史')):
            page = await self.service.inspect('inventory', None, capture, limit=1, search=None)
            self.assertEqual(page['captureCount'], 1)
            self.assertEqual(page['scope'], 'selected_capture')
            request = page['nextRead']
            Draft202012Validator(AnalysisInspect.input_schema).validate(request)
            self.assertEqual(request['captureRef'], capture)
            self.assertEqual(request['limit'], 1)
            next_page = await self.service.inspect(**request)
            self.assertNotEqual(page['items'][0]['stepId'], next_page['items'][0]['stepId'])
