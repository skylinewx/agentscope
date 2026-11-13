from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import analysis_fixtures
from analysis_budget_batch_test import batch_fixture
from analysis_extension.budget_preparation import batch_targets, metric_target
from analysis_extension.capture import ReceiptCapture
from analysis_extension.dimension_scopes import inherit, remember
from analysis_extension.models import AnalysisError
from analysis_extension.python_reads import inspect_run
from analysis_extension.query_results import encode
from agentscope.app._service._errors import _classify_error


class RegressionReliabilityTests(TestCase):
    def test_property_group_analysis_does_not_request_a_sample_members_formula(self):
        capture, _, _, receipt = batch_fixture()
        capture['captureRef'] = 'cap'
        capture.update(originalToolName='execute_budget_query', request={'purpose': 'ANALYZE', 'groupBy': ['板块']})
        # The native table is already validated; automatic preparation must not
        # invent a representative member for a property aggregate.
        service = SimpleNamespace(sources=SimpleNamespace(for_capture=lambda item: SimpleNamespace(adapter_id='budget-v2')))
        with patch('analysis_extension.budget_preparation.receipt_from_capture', return_value=receipt), \
             patch('analysis_extension.budget_preparation.budget_batch.validate', return_value={}), \
             patch('analysis_extension.budget_preparation.metric_target', side_effect=AssertionError('group is not a member')):
            self.assertEqual(batch_targets(service, capture), [])

    def test_report_drafts_are_scoped_to_session_and_analysis_version(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from analysis_extension.delivery import AnalysisDelivery
        from analysis_extension.models import Owner
        first = AnalysisDelivery.__new__(AnalysisDelivery)
        first.owner = Owner('u', 'a', 'session-one')
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        first.root = Path(directory.name).resolve()
        one = first.document_draft('ana_1:1')['path']
        self.assertNotEqual(one, first.document_draft('ana_1:2')['path'])
        first.owner = Owner('u', 'a', 'session-two')
        self.assertNotEqual(one, first.document_draft('ana_1:1')['path'])
        self.assertNotIn(':', one)
        self.assertTrue(one.endswith('/report.html'))
        self.assertTrue((first.root / one).parent.is_dir())
    def test_effective_coordinates_do_not_become_cell_dimensions(self):
        capture, _, _, receipt = batch_fixture(False)
        capture['captureRef'] = 'cap'
        metric = receipt['data']['stepResults'][0]['batchResults'][0]['metrics'][0]
        metric['cellDimensions'] = deepcopy(metric['selectedDimensions'])
        metric['selectedDimensions'] = {**metric['context']['effectiveDimensions'], **metric['selectedDimensions']}
        target = metric_target(capture, receipt, metric, {'MD_ORG': 'child'})
        self.assertEqual(target['outer']['MD_ORG'], 'child')
        self.assertEqual(target['query']['dimensions']['MD_ORG'], 'child')
        self.assertNotIn('MD_ORG', target['query']['targetDimensions'])
        self.assertNotIn('MD_CURRENCY', target['query']['targetDimensions'])
        self.assertNotIn('observationId', target['query'])
        # A dimension really present on both axes still belongs to the cell;
        # changing its row does not change the opening context.
        metric['cellDimensions']['MD_ORG'] = 'seed'
        target = metric_target(capture, receipt, metric, {'MD_ORG': 'child'})
        self.assertEqual(target['outer']['MD_ORG'], 'seed')
        self.assertEqual(target['query']['targetDimensions']['MD_ORG'], 'child')
        self.assertIn('observationId', target['query'])

    def test_shared_value_keeps_raw_phrase_and_committed_reference(self):
        receipt = {'status': 'SUCCEEDED', 'executionId': 'execution',
            'context': {'contextId': 'original-context'}, 'querySessionId': 'original-session', 'data': {
            'dimensionCatalog': [{'dimCode': 'MD_SCENE', 'valueDomain': 'MD_SCENE', 'label': '情景'}],
            'resultSets': [{'resolvedTerms': [{'term': '预算', 'dimensionCode': 'MD_SCENE',
                'baseDataCode': 'MD_SCENE', 'objectCode': 'BUDGET'}]}]}}
        turn = SimpleNamespace(state={}, service=SimpleNamespace(sources=SimpleNamespace(
            for_capture=lambda capture: SimpleNamespace(raw_receipt=lambda response: receipt))))
        capture = {'originalToolName': 'execute_budget_query', 'captureRef': 'cap', 'response': {},
                   'request': {'intentDraft': {'sharedConditions': [{'text': '预算', 'sourceText': '预算'}]}}}
        remember(turn, capture)
        turn.state['inheritedDimensionScopes'] = turn.state['dimensionScopeCaptures']['cap']
        request = inherit(turn, {})
        scope = request['intentDraft']['dimensionScopes'][0]
        self.assertEqual(scope['selector'], {'type': 'VALUES', 'members': ['预算']})
        self.assertEqual(scope['confirmedSource'], {'executionId': 'execution', 'sharedTerm': '预算',
            'contextId': 'original-context', 'querySessionId': 'original-session'})
        self.assertEqual(turn.state['inheritedDimensionScopes'][0]['bindings'][0]['objectCode'], 'BUDGET')
        # Explicitly repeated same scope also receives the host's source proof.
        repeated = deepcopy(scope); repeated.pop('confirmedSource')
        inherited = inherit(turn, {'intentDraft': {'dimensionScopes': [repeated]}})
        self.assertEqual(inherited['intentDraft']['dimensionScopes'][0]['confirmedSource'], scope['confirmedSource'])

    def test_python_inspection_is_bounded_and_full_rows_are_pageable(self):
        rows = [[str(i), '内容' * 300] for i in range(2000)]
        record = {'runRef': 'run', 'status': 'COMPLETED', 'code': 'result = inputs',
                  'inputRecords': [{'ref': 'table', 'rows': rows}], 'outputs': [{'derivedId': 'answer', 'value': '1'}]}
        summary = inspect_run(record, 'run')
        self.assertLess(len(encode(summary)), 25000)
        page = inspect_run(record, 'run', '/inputRecords/0/rows', limit=5)
        self.assertEqual(page['items'], rows[:5])
        self.assertEqual(page['nextRead']['section'], 'python')
        self.assertEqual(page['nextRead']['pythonPath'], '/inputRecords/0/rows')
        self.assertEqual(page['nextRead']['pythonRunRef'], 'run')
        self.assertEqual(record['inputRecords'][0]['rows'], rows)

    def test_projection_failure_never_reinserts_full_receipt(self):
        middleware = ReceiptCapture.__new__(ReceiptCapture)
        middleware.sources = middleware.store = middleware.owner = None
        receipt = {'status': 'SUCCEEDED', 'data': {'rows': ['row' * 1000] * 1000}}
        adapter = SimpleNamespace(adapter_id='budget-v2')
        with patch.object(middleware, '_material_projection', side_effect=AnalysisError('BAD_SHAPE', 'bad')):
            result = middleware._model_projection(adapter, receipt, {'captureRef': 'cap'}, 3_000_000)
        self.assertLess(len(encode(result)), 25000)
        self.assertEqual(result['captureRef'], 'cap')
        self.assertFalse(result['projectionDiagnostic']['requiresSourceQuery'])
        # A malformed business-shaped receipt must not re-enter the same projector.
        receipt['data']['resultType'] = 'BUDGET_QUERY_RESULT'
        with patch.object(middleware, '_material_projection', side_effect=KeyError('purpose')), \
             patch('analysis_extension.query_results.compact_receipt', side_effect=AssertionError('same failed shape')):
            result = middleware._model_projection(adapter, receipt, {'captureRef': 'cap'}, 3_000_000)
        self.assertLess(len(encode(result)), 2000)
        self.assertEqual(result['projectionDiagnostic']['code'], 'MATERIAL_PROJECTION_FAILED')

    def test_redis_capacity_failure_has_specific_recovery(self):
        from redis.exceptions import OutOfMemoryError
        error = RuntimeError('wrapped')
        error.__cause__ = OutOfMemoryError('command not allowed when used memory > maxmemory')
        self.assertEqual(_classify_error(error).type.value, 'storage_capacity')
        self.assertIn('persisted', _classify_error(error).message)
