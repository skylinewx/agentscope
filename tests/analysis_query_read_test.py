from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from decimal import Decimal
import tempfile

from analysis_extension.query_results import compact_receipt, read_receipt, at_pointer, encode
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.store import AnalysisStore
from analysis_extension.service import AnalysisService


class QueryReadTests(TestCase):
    def test_generic_list_and_null_data_survive_compaction_and_reading(self):
        for data in (None, [{'value': '0'}, {'value': None}]):
            with self.subTest(data=data):
                receipt = {'contractVersion': '2.0', 'data': data, 'evidence': ['x' * 20000]}
                original = encode(receipt)
                compact = compact_receipt(receipt, 'cap', len(original))
                self.assertIsNotNone(compact)
                page = read_receipt(receipt, 'cap', '/data')
                self.assertEqual(page.get('items') if isinstance(data, list) else page.get('value'), data)
                self.assertEqual(encode(receipt), original)

    def test_capture_projection_does_not_dispatch_generic_data_as_a_budget_result(self):
        from analysis_extension.capture import ReceiptCapture
        capture = ReceiptCapture(None, None, frozenset(), None)
        for data in (None, [{'value': '0'}]):
            receipt = {'data': data}
            adapter = Mock(adapter_id='generic')
            adapter.compact.return_value = receipt
            with patch('analysis_extension.analysis_materials.response_materials', return_value=None):
                self.assertEqual(capture._model_projection(adapter, receipt, {'captureRef': 'cap'}, 100), receipt)
            adapter.compact.assert_called_once_with(receipt, 'cap', 100)

    def test_step_index_uses_real_array_positions_and_can_recover_unknown_step_pointer(self):
        receipt = {'data':{'stepResults':[
            {'stepId':'s4','rows':[{'value':'8'}]},
            {'stepId':'s2','rows':[{'value':'3'}]}]}, 'evidence':['x'*20000]}
        original = encode(receipt)
        compact = compact_receipt(receipt, 'cap', len(original))
        self.assertLessEqual(len(encode(compact)), 24000)
        steps = compact['stepReads']
        self.assertEqual([(s['stepId'],s['pointer']) for s in steps],
                         [('s4','/data/stepResults/0'),('s2','/data/stepResults/1')])
        path = next(p['pointer'] for p in steps[1]['readOptions'] if p['pointer'].endswith('/rows'))
        self.assertEqual(read_receipt(receipt,'cap',path)['items'], [{'value':'3'}])
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt,'cap','/data/stepResults/2/rows')
        self.assertEqual(error.exception.details['readOptions'][1]['stepId'], 's2')
        self.assertEqual(original, encode(receipt))

    def test_large_step_index_never_breaks_response_budget_or_hides_continuation(self):
        receipt = {'data':{'stepResults':[{'stepId':'step-'+str(i),'rows':[{'value':'1'}]} for i in range(200)]},
                   'evidence':['x'*20000]}
        compact = compact_receipt(receipt, 'cap', len(encode(receipt)))
        self.assertLessEqual(len(encode(compact)),24000)
        self.assertLess(len(compact['stepReads']),200)
        page = read_receipt(receipt,'cap','/data/stepResults',offset=180,fields=['stepId'],limit=20)
        self.assertEqual(len(page['items']),20)
        self.assertEqual(page['itemPointers'][-1],'/data/stepResults/199')
        self.assertIsNone(page['nextOffset'])

    def test_omitted_array_can_be_recovered_without_losing_precision_or_completion(self):
        rows = [{'name': '成员' + str(i), 'value': Decimal('123456789012345678.000001') + i, 'unit': None} for i in range(250)]
        receipt = {'contractVersion': '2.0', 'status': 'PARTIAL', 'resumeRef': 'resume-source',
                   'completeness': {'enumerationExhausted': False}, 'data': {'rows': rows}, 'evidence': ['a' * 20000]}
        text = encode(receipt)
        compact = compact_receipt(receipt, 'cap-test', len(text))
        self.assertLess(len(encode(compact)), 24000)
        self.assertEqual(compact['receipt']['resumeRef'], 'resume-source')
        self.assertIn('/data/rows', [o['pointer'] for o in compact['omitted']])
        found, offset = [], 0
        while offset is not None:
            page = read_receipt(receipt, 'cap-test', '/data/rows', offset=offset, limit=25)
            self.assertFalse(page['sourceState']['completeness']['enumerationExhausted'])
            found.extend(page['items'])
            offset = page['nextOffset']
        self.assertEqual(encode(found), encode(rows))
        self.assertEqual(receipt['data']['rows'], rows)

    def test_search_keeps_original_pointers_and_never_guesses_missing_fields(self):
        receipt = {'data': [{'label': '乙', 'value': None}, {'label': '甲', 'value': '0'}, {'label': '乙', 'value': '-1'}]}
        page = read_receipt(receipt, 'cap', '/data', search='乙', fields=['label', 'value'], limit=1)
        self.assertEqual(page['matchingTotal'], 2)
        self.assertEqual(page['itemPointers'], ['/data/0'])
        self.assertIsNone(page['items'][0]['value'])
        with self.assertRaises(AnalysisError):
            read_receipt(receipt, 'cap', '/data', fields=['unknown'])

    def test_pointer_is_data_access_not_path_or_negative_index(self):
        data = {'a/b': {'~': [None, '0', '-1']}}
        self.assertEqual(at_pointer(data, '/a~1b/~0/2'), '-1')
        for pointer in ('/a~1b/~0/-1', '/a~1b/~0/01', '/a~2b', 'C:/private', '/unknown'):
            with self.subTest(pointer=pointer), self.assertRaises(AnalysisError):
                at_pointer(data, pointer)

    def test_wide_read_returns_actionable_error_not_silent_truncation(self):
        receipt = {'data': [{'value': 'x' * 25000}]}
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt, 'cap', '/data')
        self.assertEqual(error.exception.code, 'QUERY_SELECTION_TOO_LARGE')
        page = read_receipt(receipt, 'cap', '/data/0/value', offset=0, limit=10)
        self.assertEqual(len(page['value']), 1000)
        self.assertEqual(page['nextOffset'], 1000)

    def test_large_failure_diagnostics_remain_readable(self):
        receipt = {'status': 'FAILED', 'error': {'message': 'x' * 50000}}
        page = read_receipt(receipt, 'cap', '/error/message', limit=5)
        self.assertEqual(page['sourceState']['status'], 'FAILED')
        self.assertEqual(page['sourceState']['error'], {'readAt': '/error'})
        self.assertEqual(page['nextOffset'], 500)
        self.assertEqual(page['value'], 'x' * 500)

    def test_small_rows_remain_readable_with_large_period_selection_context(self):
        groups = [{'factId': str(index), 'period': '2026J0001', 'proof': 'x' * 500} for index in range(100)]
        receipt = {'data': {'resultType': 'BUDGET_QUERY_RESULT', 'complete': True, 'purpose': 'QUERY',
            'resultSets': [{'context': {'period': '2026N0001', 'dataPeriodSelection': {
                'mode': 'CONTAINED_PREFERRED_GRAIN', 'groups': groups}},
                'rows': [{'value': '0', 'valueState': 'PRESENT'}, {'value': None, 'valueState': 'NULL'}]}]}}
        original = encode(receipt)
        page = read_receipt(receipt, 'cap', '/data/resultSets/0/rows', fields=['value', 'valueState'])
        self.assertEqual(page['items'], receipt['data']['resultSets'][0]['rows'])
        context = page['resultSetContext']['context']
        self.assertEqual(context['period'], '2026N0001')
        path = context['dataPeriodSelection']['groups']['readAt']
        self.assertEqual(at_pointer(receipt, path), groups)
        self.assertLess(len(encode(page)), 24000)
        self.assertEqual(encode(receipt), original)


class QueryReadAuthorizationTests(IsolatedAsyncioTestCase):
    async def test_capture_cannot_be_read_from_another_session(self):
        with tempfile.TemporaryDirectory() as path:
            store = AnalysisStore(path)
            owner = Owner('u', 'a', 'one')
            cap = store.capture(owner, {'sourceAdapter': {'id': 'budget-v2', 'version': '1', 'options': {}},
                'response': {'content': [{'type': 'text', 'text': '{"contractVersion":"2.0","data":{"value":"123"}}'}]}, 'captureState': 'success'})
            auth = AsyncMock()
            service = AnalysisService(store, owner, auth)
            self.assertEqual((await service.read_query_result(cap, '/data'))['value'], {'value': '123'})
            auth.assert_awaited()
            other = AnalysisService(store, Owner('u', 'a', 'two'), AsyncMock())
            with self.assertRaises(AnalysisError):
                await other.read_query_result(cap, '/data')
