import unittest
from analysis_extension.budget_period_selection import contains, opening_matches, selected_period
from analysis_extension.formula_check_summary import summarize, normalize_review_issue
from analysis_extension.query_empty_result import empty_query_answer


class PeriodAndCheckStatusTest(unittest.TestCase):
    def test_query_adapter_checks_global_and_absolute_quarter_scope_on_selected_rows(self):
        from analysis_business_query_test import business_fixture
        from analysis_extension.adapters.budget_query import validate
        from analysis_extension.models import AnalysisError
        capture, receipt = business_fixture()
        for result in receipt['data']['resultSets']:
            for row in result['rows']:
                row['dataPeriod'] = '2026J0002'
                row['dimensions']['DATATIME'] = '2026J0002'
            result['context']['dataPeriodSelection'] = {'mode': 'CONTAINED_PREFERRED_GRAIN',
                'requestedPeriods': ['2026N0001'], 'preference': ['N', 'J', 'Y'],
                'groups': [{'requestedPeriod': '2026N0001', 'selectedPeriods': ['2026J0002'],
                            'selectedFactIds': [row['factId'] for row in result['rows']]}]}
        validate(capture, receipt)
        receipt['data']['resultSets'][0]['context']['dataPeriodSelection']['requestedPeriods'] = ['2025N0001']
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        capture['request']['scope'].pop('periodCode')
        capture['request']['scope']['periodRequest'] = {'kind': 'PERIOD', 'start': {'type': 'QUARTER', 'year': 2026, 'period': 2}}
        for result in receipt['data']['resultSets']:
            proof = result['context']['dataPeriodSelection']
            proof['requestedPeriods'] = ['2026J0002']
            proof['groups'][0]['requestedPeriod'] = '2026J0002'
        # Native contexts carry their own opening-period resolution in the fixture.
        for result in receipt['data']['resultSets']:
            result['context']['periodResolution'] = {'periodCodes': ['2026N0001'], 'resolutionId': 'opening', 'ruleVersion': 'v1'}
        for step in receipt['data']['stepResults']:
            for batch in step.get('batchResults', []):
                for metric in batch.get('metrics', []):
                    metric['context']['periodResolution'] = {'periodCodes': ['2026N0001'], 'resolutionId': 'opening', 'ruleVersion': 'v1'}
        validate(capture, receipt)
        capture['request']['scope']['periodRequest']['start']['period'] = 3
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_quarter_constrains_fallback_and_requires_server_selection_proof(self):
        self.assertTrue(contains('2026N0001', '2026J0002'))
        self.assertTrue(contains('2026J0002', '2026Y0004'))
        self.assertFalse(contains('2026J0002', '2026Y0007'))
        self.assertFalse(contains('2026J0002', '2026N0001'))
        self.assertTrue(opening_matches('2026J0002', '2026N0001'))
        row = {'factId': 'f1', 'dataPeriod': '2026Y0004', 'dimensions': {'DATATIME': '2026Y0004'}}
        context = {'period': '2026N0001', 'dataPeriodSelection': {'mode': 'CONTAINED_PREFERRED_GRAIN',
            'requestedPeriods': ['2026J0002'], 'preference': ['N', 'J', 'Y'],
            'groups': [{'requestedPeriod': '2026J0002', 'selectedFactIds': ['f1'], 'selectedPeriods': ['2026Y0004']}]}}
        self.assertTrue(selected_period(context, row, {'periodCode': '2026J0002'}))
        self.assertFalse(selected_period(context, row, {'periodCode': '2026J0001'}))
        self.assertFalse(selected_period(context, {**row, 'factId': 'other'}, {}))
        self.assertFalse(selected_period({'period': '2026N0001'}, row, {}))

    def test_root_match_does_not_hide_incomplete_dependency_check(self):
        trace = {'status': 'BLOCKED', 'targetLocalCheck': {'status': 'MATCH'}, 'nodes': [
            {'factRef': 'net', 'localCheck': {'status': 'MATCH'}},
            {'factRef': 'operating', 'localCheck': {'status': 'BLOCKED'}}]}
        summary = summarize(trace)
        self.assertEqual(summary['targetLocalCheck']['status'], 'MATCH')
        self.assertEqual(summary['capturedChainStatus'], 'INCOMPLETE')
        item = {'code': 'CHECK_STATUS_MISMATCH', 'statement': '整条路径复算一致',
                'evidenceRefs': ['operating'], 'severity': 'ADVISORY'}
        material = {'derived': [{'derivedId': 'check', 'formulaTrace': trace}]}
        self.assertEqual(normalize_review_issue(item, material)['severity'], 'BLOCKING')
        ordinary = {**item, 'code': 'UNSUPPORTED_CLAIM', 'statement': '建议补充经营说明'}
        self.assertEqual(normalize_review_issue(ordinary, material)['severity'], 'ADVISORY')
        self.assertEqual(normalize_review_issue({**item, 'evidenceRefs': ['unknown']}, material)['severity'], 'ADVISORY')

    def test_empty_result_is_not_a_computation_failure_and_incomplete_work_cannot_close(self):
        query = {'purpose': 'QUERY', 'complete': True, 'summary': {'candidateCount': 1, 'rowCount': 0},
                 'failureSummary': {'outcome': 'NO_MATCH', 'matchDiagnostics': [{'rejectionCounts': {'OUTSIDE_REQUESTED_PERIOD': 4}}]}}
        evidence = {'sources': [{'facts': [], 'source': {'businessQuery': query}}]}
        result = empty_query_answer(evidence)
        self.assertEqual(result['deliveryStatus'], 'NO_MATCH')
        self.assertIn('数据时期不在所问范围内', result['answer'])
        query['complete'] = False
        self.assertIsNone(empty_query_answer(evidence))
        query['complete'] = True
        evidence['sources'][0]['facts'] = [{'value': None}]
        self.assertIsNone(empty_query_answer(evidence))


if __name__ == '__main__':
    unittest.main()
