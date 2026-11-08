from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_extension.adapters import default_sources
from analysis_extension.adapters.budget_query import validate
from analysis_extension.adapters.budget_batch import catalog, adapt
from analysis_extension.capture import ReceiptCapture
from analysis_extension.models import AnalysisError
from analysis_extension.query_results import encode, budget_query_view
from analysis_extension.review_materials import business_query_projection


class QueryEfficiencyTest(TestCase):
    def test_mixed_diagnostic_then_business_batch_does_not_bypass_validation(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from analysis_extension.service import AnalysisService
        capture, receipt = business_fixture()
        receipt['data']['resultSets'][0]['rows'][0]['value'] = '999999'
        update(capture, receipt)
        service = SimpleNamespace(authorize=AsyncMock(), owner='test', sources=default_sources(),
                                  store=SimpleNamespace(get_capture=lambda owner, ref: capture))
        result = asyncio.run(AnalysisService.read_query_result(service, captureRef=capture['captureRef'], requests=[
            {'pointer': '/data/question'}, {}]))
        self.assertEqual(result['results'][0]['status'], 'COMPLETE')
        self.assertEqual(result['results'][1]['status'], 'ERROR')
        self.assertEqual(result['results'][1]['error']['code'], 'BUDGET_QUERY_MISMATCH')

    def test_batch_does_not_claim_a_capacity_fallback_complete(self):
        import asyncio
        from analysis_extension.batch_reads import read_batch
        async def reader(**kwargs):
            return {'readComplete': False, 'detailRead': {'captureRef': 'cap1', 'pointer': '/data'}}
        result = asyncio.run(read_batch([{'captureRef': 'cap1'}], True, reader, allowed_fields={'captureRef'}))
        self.assertFalse(result['readComplete'])
        self.assertEqual('PARTIAL', result['results'][0]['status'])

    def test_runtime_text_does_not_turn_numeric_looking_content_into_a_number(self):
        from analysis_extension.adapters.budget_v2 import cell_value
        self.assertEqual(cell_value({'rawValueJson': '"001"'}, value_type='STRING'), ('001', 'PRESENT', 'source_typed'))
        self.assertEqual(cell_value({'rawValueJson': 'false'}, value_type='BOOLEAN'), (False, 'PRESENT', 'source_typed'))
        self.assertEqual(cell_value({'rawValueJson': '"2026-11-06"'}, value_type='DATE')[0], '2026-11-06')

    def typed(self, value_type, value):
        capture, receipt = business_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['columns'][2]['type'] = value_type
        table['data']['rows'][0][2] = value
        for index, row in enumerate(receipt['data']['resultSets'][0]['rows']):
            row['valueType'] = value_type
            if index == 0:
                row['value'] = value
        update(capture, receipt)
        return capture, receipt

    def test_typed_values_retain_leading_zero_false_and_dates(self):
        for value_type, value in [('STRING', '001'), ('BOOLEAN', False), ('DATE', '2026-11-06'),
                                  ('DATETIME', '2026-11-06T08:00:00')]:
            with self.subTest(value_type=value_type):
                capture, receipt = self.typed(value_type, value)
                validate(capture, receipt)
                facts = catalog(capture)
                self.assertEqual(facts[0]['valueType'], value_type)
                self.assertIs(type(facts[0]['value']), type(value))
                self.assertEqual(facts[0]['value'], value)
                self.assertIsNone(facts[0]['unit'])

    def test_typed_values_cannot_disguise_native_decimal_or_boolean(self):
        capture, receipt = self.typed('BOOLEAN', False)
        receipt['data']['resultSets'][0]['rows'][0]['value'] = 'false'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        capture, receipt = self.typed('STRING', '001')
        receipt['data']['resultSets'][0]['rows'][0]['valueType'] = 'DECIMAL_STRING'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_numeric_looking_text_is_not_accepted_as_amount_analysis(self):
        capture, receipt = self.typed('STRING', '001')
        ref = capture['captureRef']
        with self.assertRaises(AnalysisError) as error:
            adapt({ref: capture}, [{'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}],
                  {'memberDimension': 'MD_ORG', 'members': [r[0] for r in receipt['data']['stepResults'][0]['batchResults'][0]['data']['rows']]}, 100)
        self.assertEqual(error.exception.code, 'NON_NUMERIC_MEASURE')

    def test_query_projection_does_not_build_duplicate_analysis_material(self):
        capture, receipt = business_fixture()
        sources = default_sources()
        host = ReceiptCapture(None, None, frozenset(), None, sources=sources)
        with patch('analysis_extension.analysis_materials.response_materials', side_effect=AssertionError('duplicate material')):
            view = host._model_projection(sources.for_capture(capture), receipt, capture, len(encode(receipt)))
        self.assertNotIn('analysisMaterials', view)
        self.assertTrue(view['allRowsIncluded'])
        self.assertEqual(view['tables'][0]['factCount'], 2)
        self.assertEqual(view['readTool'], 'QueryResultRead')

    def test_all_task_tables_are_retained_without_row_preview(self):
        capture, receipt = business_fixture()
        data = receipt['data']
        original = data['resultSets'][0]
        data['resultSets'] = []
        for index in range(12):
            result = deepcopy(original)
            result['context']['taskName'] = '任务' + str(index)
            result['context']['target']['taskCode'] = 'T' + str(index)
            result['rows'][0]['value'] = str(index)
            data['resultSets'].append(result)
        view = budget_query_view(receipt, capture['captureRef'], len(encode(receipt)))
        self.assertTrue(view['allRowsIncluded'])
        self.assertEqual(len(view['tables']), 12)
        self.assertTrue(all(table['factCount'] == 2 for table in view['tables']))
        self.assertIn('任务11', view['markdown'])

    def test_business_projection_rejects_tampered_fact_before_model_display(self):
        capture, receipt = business_fixture()
        receipt['data']['resultSets'][0]['rows'][0]['value'] = '999999'
        update(capture, receipt)
        sources = default_sources()
        host = ReceiptCapture(None, None, frozenset(), None, sources=sources)
        view = host._model_projection(sources.for_capture(capture), receipt, capture, len(encode(receipt)))
        self.assertEqual(view['viewType'], 'captured_query_result')
        self.assertIn('projectionDiagnostic', view)
        self.assertNotIn('999999', encode(view))
        self.assertEqual(view['read']['pointer'], '/data/summary')
        self.assertTrue(view['projectionDiagnostic']['diagnosticOnly'])

    def test_review_wire_uses_same_complete_markdown_and_preserves_scope(self):
        from analysis_extension.turn_completion import _wire_review_json, review_model_text
        from analysis_extension.business_tables import build_tables
        capture, receipt = business_fixture()
        adapter = default_sources().for_capture(capture)
        contexts, facts, source = business_query_projection(capture, receipt, adapter.receipt_info(capture))
        envelope = {'evidence': {'sources': [{'captureRef': capture['captureRef'], 'contexts': contexts, 'facts': facts, 'source': source}]}}
        before = deepcopy(envelope)
        wire = _wire_review_json(envelope)
        model = review_model_text(wire)
        for table in build_tables(receipt):
            self.assertIn(table.markdown(), model)
        self.assertEqual(envelope, before)
        self.assertNotIn('"factRows"', model)
        self.assertIn('"executionScopes"', model)

    def test_review_projection_retains_every_value_state_scope_and_failure(self):
        capture, receipt = business_fixture()
        data = receipt['data']
        data['unresolvedGroups'] = [{'code': 'SOURCE_TIMEOUT', 'taskCode': 'OTHER', 'message': '未完成'}]
        data['complete'] = False
        row = deepcopy(data['resultSets'][0]['rows'][1])
        row.update(factId='not-loaded', valueState='NOT_LOADED')
        data['resultSets'][0]['rows'].append(row)
        update(capture, receipt)
        source = default_sources().for_capture(capture).receipt_info(capture)
        before = deepcopy(source)
        contexts, facts, projected = business_query_projection(capture, receipt, source)
        expected = [r for result in data['resultSets'] for r in result['rows']]
        self.assertEqual([(f['factId'], f['value'], f['valueState']) for f in facts],
                         [(r['factId'], r['value'], r['valueState']) for r in expected])
        self.assertTrue(all(f['contextRef'] in contexts for f in facts))
        self.assertEqual(projected['businessQuery']['unresolvedGroups'], data['unresolvedGroups'])
        self.assertEqual(projected['businessQuery']['requestedTargets'], data['targets'])
        self.assertEqual(source, before)
