import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from analysis_extension.models import AnalysisError
from analysis_extension.receipt_references import expand_receipt, MARKER
from analysis_extension.value_references import reference_context, calculate_references
from analysis_extension.formula_tables import formula_view
from analysis_extension.business_tables import actual_headers
from numeric_tools import calculate_requests
from analysis_business_tables_test import fact, receipt


class ReceiptReferencesTest(unittest.TestCase):
    def test_chunked_pool_resolves_cross_chunk_refs_without_losing_identity(self):
        raw = {'data': {'receiptLayout': 'REFERENCE_POOL_V1',
                       'referencePool': [{'r1': {'task': 'A'}}, {'r2': [{'context': {MARKER: 'r1'}}]}],
                       'cellOrigins': {MARKER: 'r2'}, 'cellSelections': {MARKER: 'r2'}}}
        result = expand_receipt(raw)['data']
        self.assertEqual({'task': 'A'}, result['cellOrigins'][0]['context'])
        self.assertIs(result['cellOrigins'], result['cellSelections'])
        for chunks in ([{'r1': 1}, {'r1': 2}], [42]):
            with self.assertRaises(AnalysisError):
                expand_receipt({'data': {'receiptLayout': 'REFERENCE_POOL_V1', 'referencePool': chunks}})

    def test_shared_sources_resolve_once_and_keep_null(self):
        shared = [{'context': {MARKER: 'context'}, 'value': None}]
        raw = {'data': {'receiptLayout': 'REFERENCE_POOL_V1', 'referencePool': {'origin': shared, 'context': {'task': 'A'}},
                        'cellOrigins': {MARKER: 'origin'}, 'cellSelections': {MARKER: 'origin'}}}
        expanded = expand_receipt(raw)['data']
        self.assertIs(expanded['cellOrigins'], expanded['cellSelections'])
        self.assertEqual({'task': 'A'}, expanded['cellOrigins'][0]['context'])
        self.assertIsNone(expanded['cellOrigins'][0]['value'])
        self.assertIn('referencePool', raw['data'])

    def test_dangling_cycles_and_unknown_versions_rejected(self):
        for pool, ref in (({}, 'no'), ({'a': {MARKER: 'a'}}, 'a')):
            with self.assertRaises(AnalysisError):
                expand_receipt({'data': {'receiptLayout': 'REFERENCE_POOL_V1', 'referencePool': pool, 'context': {MARKER: ref}}})
        with self.assertRaises(AnalysisError):
            expand_receipt({'data': {'receiptLayout': 'UNKNOWN', 'referencePool': {}}})

    def test_actual_header_replacement_is_bound_not_global(self):
        cell = {'periodExpression': '-0N,1J', 'dimensions': {'DATATIME': '2026J0001'},
                'headers': ['自营渠道', '{-0N,1J}', '{其他}', '{-1N}']}
        self.assertEqual(['自营渠道', '2026年第1季度', '{其他}', '{-1N}'], actual_headers(cell))


class ValueReferenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_formula_table_calculation_uses_its_analysis_not_query_capture(self):
        facts = [fact('a', {'MD_BUDACCOUNT': 'a', 'MD_SCENE': 'BUDGET'}, '20', captureRef='cap1', observationId='one'),
                 fact('b', {'MD_BUDACCOUNT': 'b', 'MD_SCENE': 'BUDGET'}, '5', captureRef='cap1', observationId='one')]
        service, turn = self.service(receipt([facts[0]]))
        service.store.get_analysis = Mock(return_value={'facts': facts})
        trace = {'targetFactRef': 'a', 'nodes': [{'factRef': 'a', 'expression': 'E25',
                                               'dependencies': [{'address': 'E25', 'factRef': 'b'}]}]}
        view = formula_view(trace, facts, 'ana:2')
        self.assertIn('T1.R2[预算数]', view['markdown'])
        with reference_context(service, turn):
            result = await calculate_references({'expression': 'a-b', 'valueRefs': {
                'a': {'analysisRef': 'ana:2', 'tableRef': 'T1', 'rowRef': 'R1', 'columnRef': 'C1'},
                'b': {'analysisRef': 'ana:2', 'tableRef': 'T1', 'rowRef': 'R2', 'columnRef': 'C1'}}}, calculate_requests)
        self.assertEqual('15', result['value'])
        service.store.get_analysis.assert_called_once_with('owner', 'ana:2')
        service.store.get_capture.assert_not_called()
        self.assertEqual('ana:2', result['sourceReferences'][0]['inputs'][0]['analysisRef'])
        self.assertEqual(['cap1'], turn.state['queryCaptureRefs'])
        with reference_context(service, turn), self.assertRaises(AnalysisError):
            await calculate_references({'expression': 'a', 'valueRefs': {'a': {
                'analysisRef': 'ana:2', 'captureRef': 'cap1', 'tableRef': 'T1', 'rowRef': 'R1', 'columnRef': 'C1'}}}, calculate_requests)
        service.store.get_analysis.side_effect = AnalysisError('NOT_FOUND', '不同会话的分析不可访问')
        with reference_context(service, turn), self.assertRaises(AnalysisError) as denied:
            await calculate_references({'expression': 'a', 'valueRefs': {'a': {
                'analysisRef': 'other:1', 'tableRef': 'T1', 'rowRef': 'R1', 'columnRef': 'C1'}}}, calculate_requests)
        self.assertEqual('NOT_FOUND', denied.exception.code)

    def service(self, data):
        adapter = SimpleNamespace(catalog=Mock(return_value=[]), raw_receipt=lambda response: data)
        store = SimpleNamespace(get_capture=Mock(return_value={'response': {}, 'captureRef': 'cap1'}))
        service = SimpleNamespace(owner='owner', authorize=AsyncMock(), store=store,
                                  sources=SimpleNamespace(for_capture=lambda capture: adapter))
        turn = SimpleNamespace(state={})
        return service, turn

    async def test_reference_uses_exact_original_value_and_authorized_owner(self):
        service, turn = self.service(receipt([fact('1', {'MD_SCENE': 'BUDGET'}, '1.2345')]))
        with reference_context(service, turn):
            result = await calculate_references({'expression': 'x*100', 'valueRefs': {'x': {
                'captureRef': 'cap1', 'tableRef': 'T1', 'rowRef': 'R1', 'columnRef': 'C1'}}}, calculate_requests)
        self.assertEqual('123.45', result['value'])
        service.authorize.assert_awaited(); service.store.get_capture.assert_called_with('owner', 'cap1')
        self.assertIn('cap1', turn.state['queryCaptureRefs'])
        self.assertEqual(1, len(turn.state['referencedCalculations']))

    async def test_full_range_is_not_limited_to_display_page_or_200_inputs(self):
        service, turn = self.service(receipt([fact(str(n), {'科目': str(n), 'MD_SCENE': 'BUDGET'}, '0.01') for n in range(240)]))
        with reference_context(service, turn):
            result = await calculate_references({'expression': 'x', 'valueRefs': {'x': {
                'captureRef': 'cap1', 'tableRef': 'T1', 'columnRef': 'C1', 'aggregation': 'SUM'}}}, calculate_requests)
        self.assertEqual('2.4', result['value'])
        self.assertEqual(240, result['sourceReferences'][0]['inputs'][0]['rowCount'])

    async def test_total_and_details_are_not_implicitly_added(self):
        rows = [fact('1', {'地区': '00000000'}), fact('2', {'地区': '001'})]
        service, turn = self.service(receipt(rows))
        with reference_context(service, turn), self.assertRaises(AnalysisError) as error:
            await calculate_references({'expression': 'x', 'valueRefs': {'x': {
                'captureRef': 'cap1', 'tableRef': 'T1', 'columnRef': 'C1', 'aggregation': 'SUM'}}}, calculate_requests)
        self.assertEqual('TOTAL_DETAIL_OVERLAP', error.exception.code)

    async def test_sparse_column_range_uses_actual_facts_but_explicit_missing_and_null_still_fail(self):
        rows = [fact('a', {'科目': 'A'}, '1', measure='amount', title='指标甲'),
                fact('b', {'科目': 'B'}, '2', measure='amount', title='指标甲'),
                fact('c', {'科目': 'C'}, '10', measure='amount', title='指标乙')]
        service, turn = self.service(receipt(rows))
        ref = {'captureRef': 'cap1', 'tableRef': 'T1', 'columnRef': 'C1', 'aggregation': 'SUM'}
        with reference_context(service, turn):
            result = await calculate_references({'expression': 'x', 'valueRefs': {'x': ref}}, calculate_requests)
            self.assertEqual('3', result['value'])
            self.assertEqual(2, result['sourceReferences'][0]['inputs'][0]['rowCount'])
            with self.assertRaises(AnalysisError) as missing:
                await calculate_references({'expression': 'x', 'valueRefs': {'x': {**ref, 'rowRefs': ['R3']}}}, calculate_requests)
            self.assertEqual('TABLE_CELL_NOT_FOUND', missing.exception.code)
        rows[1].update(value=None, valueState='NULL')
        service, turn = self.service(receipt(rows))
        with reference_context(service, turn), self.assertRaises(AnalysisError) as null:
            await calculate_references({'expression': 'x', 'valueRefs': {'x': ref}}, calculate_requests)
        self.assertEqual('NON_NUMERIC_TABLE_CELL', null.exception.code)

    async def test_batch_keeps_other_success_when_a_reference_fails(self):
        service, turn = self.service(receipt([fact('1', {'MD_SCENE': 'BUDGET'}, '2')]))
        with reference_context(service, turn):
            result = await calculate_references({'requests': [
                {'id': 'bad', 'expression': 'x', 'valueRefs': {'x': {'captureRef': 'cap1', 'tableRef': 'T1', 'rowRef': 'R9', 'columnRef': 'C1'}}},
                {'id': 'ok', 'expression': '2+3'}]}, calculate_requests)
        self.assertEqual('PARTIAL', result['status']); self.assertEqual('5', result['results'][1]['value'])

    async def test_malformed_reference_is_a_structured_error(self):
        service, turn = self.service(receipt([fact('1', {'MD_SCENE': 'BUDGET'})]))
        for ref in ({'captureRef': [], 'tableRef': 'T1', 'columnRef': 'C1'},
                    {'captureRef': 'cap1', 'tableRef': 'T1', 'columnRef': 'C1', 'rowRefs': [{}], 'aggregation': 'SUM'}):
            with reference_context(service, turn), self.assertRaises(AnalysisError):
                await calculate_references({'expression': 'x', 'valueRefs': {'x': ref}}, calculate_requests)

    def test_selected_history_retains_calculation_proof_without_exposing_full_proof_in_context_summary(self):
        from analysis_extension.conversation_context import _snapshot, evidence_turn, selected_contexts
        proof = {'request': [{'expression': 'x*100'}], 'result': {'value': '123.45'}}
        snapshot = _snapshot({'turnKey': 'old', 'queryCaptureRefs': ['cap1'], 'referencedCalculations': {'p': proof}}, 'DIRECT')
        state = {'businessContexts': [snapshot], 'selectedContextRefs': [snapshot['contextRef']],
                 'followupContext': {'mode': 'CONTINUE'}}
        turn = SimpleNamespace(state=state, service=SimpleNamespace())
        self.assertNotIn('referencedCalculations', selected_contexts(state)[0])
        restored = evidence_turn(turn)
        self.assertEqual({'p': proof}, restored.state['referencedCalculations'])
        self.assertEqual(['cap1'], restored.state['queryCaptureRefs'])


class FormulaTableTest(unittest.TestCase):
    def test_formula_numbers_and_arithmetic_are_stable_across_target_views(self):
        facts = [fact(str(n), {'MD_BUDACCOUNT': str(n), 'MD_SCENE': 'BUDGET'}, str(n)) for n in range(1, 5)]
        a = {'factRef': '4', 'expression': '(A1-A2)/A3*100',
             'dependencies': [{'address': 'A' + str(n), 'factRef': str(n)} for n in range(1, 4)]}
        b = {'factRef': '3', 'expression': 'A1+A2', 'dependencies': a['dependencies'][:2]}
        first = formula_view({'nodes': [a, b]}, facts, 'analysis1')
        second = formula_view({'nodes': [b]}, facts, 'analysis1')
        self.assertEqual(first['references'][1]['formulaRef'], second['references'][0]['formulaRef'])
        self.assertIn('((T1.R1[预算数] - T1.R2[预算数]) / T1.R3[预算数]) * 100', first['markdown'])

    def test_formula_uses_bound_fact_not_same_address_in_another_observation(self):
        facts = [fact('a', {'MD_SCENE': 'BUDGET'}, '10', observationId='one', headers=['利润总额']),
                 fact('b', {'MD_SCENE': 'BUDGET'}, '99', observationId='two', headers=['利润总额']),
                 fact('c', {'MD_SCENE': 'BUDGET'}, '10', observationId='one', headers=['净利润'])]
        trace = {'targetFactRef': 'c', 'status': 'BLOCKED', 'nodes': [
            {'factRef': 'c', 'expression': 'E25', 'dependencies': [{'address': 'E25', 'factRef': 'a'}],
             'localCheck': {'status': 'MATCH', 'value': '10', 'difference': '0'}, 'status': 'BLOCKED'}]}
        view = formula_view(trace, facts, 'analysis1')
        self.assertIn('T1.R1[预算数]', view['markdown'])
        self.assertNotIn('| 99 |', view['markdown'])
        self.assertIn('MATCH', view['markdown']); self.assertIn('BLOCKED', view['markdown'])

    def test_cross_period_dependencies_are_allowed_and_repeated_terms_preserved(self):
        facts = [fact('a', {'MD_SCENE': 'BUDGET', 'DATATIME': '2025N0001'}, '2'),
                 fact('c', {'MD_SCENE': 'BUDGET', 'DATATIME': '2026N0001'}, '4')]
        trace = {'targetFactRef': 'c', 'nodes': [{'factRef': 'c', 'expression': 'E25+E25', 'dependencies': [{'address': 'E25', 'factRef': 'a'}]}]}
        body = formula_view(trace, facts, 'analysis1')['markdown']
        self.assertIn('(T1.R1[预算数] + T1.R1[预算数])', body)
        self.assertIn('2025年', body); self.assertIn('2026年', body)

    def test_missing_dependency_does_not_invent_a_formula(self):
        facts = [fact('c', {'MD_SCENE': 'BUDGET'})]
        trace = {'targetFactRef': 'c', 'nodes': [{'factRef': 'c', 'expression': 'E25', 'dependencies': []}]}
        self.assertIn('FORMULA_DEPENDENCY_MISSING', formula_view(trace, facts, 'a')['markdown'])


if __name__ == '__main__': unittest.main()
