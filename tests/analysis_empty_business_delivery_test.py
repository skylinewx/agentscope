from copy import deepcopy
from unittest import TestCase
from analysis_empty_delivery_fixtures import attach_empty_business_view
from analysis_extension.query_empty_result import empty_query_answer
from analysis_extension.business_tables import business_view
from analysis_extension.review_materials import business_query_projection
from analysis_business_query_test import business_fixture


class EmptyBusinessDeliveryTests(TestCase):
    def test_missing_or_incomplete_presentation_never_closes_with_generic_sentence(self):
        evidence = {'sources': [{'source': {'businessQuery': {'purpose': 'QUERY', 'complete': True,
                    'failureSummary': {'outcome': 'NO_DATA'}}}, 'facts': [{'valueState': 'NO_DATA'}]}]}
        self.assertIsNone(empty_query_answer(evidence))
        attach_empty_business_view(evidence)
        answer = empty_query_answer(evidence)['answer']
        for word in ['合成任务', '合成利润表', '2026年', '合成机构', '实际数', '无数据']:
            self.assertIn(word, answer)
        evidence['sources'][0]['source']['businessTableView']['allFactsIncluded'] = False
        self.assertIsNone(empty_query_answer(evidence))

    def test_zero_rows_keep_existing_context_and_columns_without_fabricating_facts(self):
        capture, receipt = business_fixture()
        for result in receipt['data']['resultSets']:
            result['rows'] = []
        data = receipt['data']
        data['summary']['rowCount'] = 0
        data['failureSummary'] = {'outcome': 'NO_DATA'}
        data['valueFilter'] = {'basis': 'BUDGET_NATIVE_FILTER', 'executionStatus': 'EXECUTED',
                               'condition': {'targetId': 't0', 'operator': 'LT', 'value': '0'}}
        before = deepcopy(receipt)
        view = business_view(receipt, 'capture')
        self.assertEqual(view['tableCount'], 2)
        self.assertTrue(view['allRowsIncluded'])
        self.assertTrue(all(t['rowCount'] == t['factCount'] == t['returnedRows'] == 0 for t in view['tables']))
        self.assertIn('利润表', view['markdown'])
        self.assertIn('| 行号 |', view['markdown'])
        self.assertNotIn('| R1 |', view['markdown'])
        self.assertEqual(receipt, before)
        query = {key: data[key] for key in ('purpose', 'complete', 'summary', 'failureSummary', 'valueFilter')}
        contexts, facts, source = business_query_projection(capture, receipt, {'businessQuery': query})
        self.assertEqual(facts, [])
        self.assertTrue(source['businessTableView']['allFactsIncluded'])
        self.assertIn('利润表', source['businessTableView']['markdown'])
        closed = empty_query_answer({'sources': [{'source': source, 'facts': facts}]})
        self.assertEqual(closed['deliveryStatus'], 'NO_DATA')
        self.assertEqual(closed['answer'], source['businessTableView']['markdown'])
        for expected in ('利润表', '2026年', '数值筛选：科目0 &lt; 0', '本次展示：0行', '| 行号 |'):
            self.assertIn(expected, closed['answer'])

    def test_empty_table_and_unmatched_target_are_delivered_in_either_source_order(self):
        empty = {'source': {'businessQuery': {'purpose': 'QUERY', 'complete': True,
                            'failureSummary': {'outcome': 'NO_DATA'}}}, 'facts': []}
        attach_empty_business_view({'sources': [empty]})
        unmatched = {'source': {'businessQuery': {
            'purpose': 'QUERY', 'complete': True, 'requestedTargets': [{'label': '运输收入'}],
            'summary': {'candidateCount': 3}, 'failureSummary': {'outcome': 'NO_MATCH',
                'matchDiagnostics': [{'rejectionCounts': {'OUTSIDE_REQUESTED_PERIOD': 2}}]}}}, 'facts': []}
        first = empty_query_answer({'sources': [empty, unmatched]})
        self.assertEqual(empty_query_answer({'sources': [unmatched, empty]}), first)
        self.assertEqual(first['deliveryStatus'], 'NO_DATA')
        for text in ('合成利润表', '本次展示：0行', '运输收入', '3 个任务表样候选', '数据时期不在所问范围内'):
            self.assertIn(text, first['answer'])

        empty['source']['businessTableView']['allFactsIncluded'] = False
        for sources in ([empty, unmatched], [unmatched, empty]):
            self.assertIsNone(empty_query_answer({'sources': sources}))

    def test_saved_no_match_requires_a_successfully_executed_empty_filter_to_use_the_table(self):
        query = {'purpose': 'QUERY', 'complete': True, 'failureSummary': {'outcome': 'NO_MATCH'},
                 'summary': {'rowCount': 0, 'resultSetCount': 1}, 'valueFilter': {
                     'basis': 'BUDGET_NATIVE_FILTER', 'executionStatus': 'EXECUTED',
                     'batches': [{'status': 'SUCCEEDED', 'rowCount': 0}]}}
        source = {'source': {'status': 'SUCCEEDED', 'businessQuery': query}, 'facts': []}
        evidence = attach_empty_business_view({'sources': [source]})
        original = deepcopy(evidence)
        closed = empty_query_answer(evidence)
        self.assertEqual(closed['deliveryStatus'], 'NO_DATA')
        self.assertIn('合成利润表', closed['answer'])
        self.assertEqual(evidence, original)

        query['valueFilter']['batches'][0]['rowCount'] = 1
        self.assertEqual(empty_query_answer(evidence)['deliveryStatus'], 'NO_MATCH')
        query['valueFilter']['batches'][0]['rowCount'] = 0
        query['summary']['resultSetCount'] = 0
        self.assertEqual(empty_query_answer(evidence)['deliveryStatus'], 'NO_MATCH')
        source['source']['status'] = 'FAILED'
        self.assertIsNone(empty_query_answer(evidence))
