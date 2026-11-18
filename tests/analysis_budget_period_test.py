from copy import deepcopy
import json
from unittest import TestCase

from analysis_fixtures import fixture
from analysis_extension.adapters.budget_v2 import adapt, catalog
from analysis_extension.models import AnalysisError


def period_fixture():
    first, selectors, scope = fixture(values=('100',))
    scope['expectedDimensions'] = {}
    intent = {'kind': 'RANGE', 'start': {'type': 'Y', 'year': 2026, 'period': 1},
              'end': {'type': 'Y', 'year': 2026, 'period': 2}}
    first['request']['plan']['steps'][1]['formQuery'].pop('periodCode')
    first['request']['plan']['steps'][1]['formQuery']['periodRequest'] = intent
    receipt = json.loads(first['response']['content'][0]['text'])
    resolution = {'resolutionId': 'resolution', 'ruleVersion': 'system-period-v1', 'taskPeriodType': 'Y',
                  'referenceSource': 'SERVER_TIME', 'referenceDate': '2026-12-31', 'referencePeriod': None,
                  'referenceInstant': '2026-12-31T23:59:59Z', 'timeZone': 'UTC',
                  'periodCodes': ['2026Y0001', '2026Y0002']}
    captures, selected = {}, []
    for index, ref in enumerate(('replace', 'february')):
        capture, data = deepcopy(first), deepcopy(receipt)
        step = data['data']['stepResults'][1]
        form = step['formResults'][0]
        context, observation = form['runtime']['context'], form['runtime']['observation']
        period = resolution['periodCodes'][index]
        context.update(period=period, periodResolution=deepcopy(resolution))
        context['effectiveDimensions']['DATATIME'] = period
        observation['observationId'] = 'period-observation-' + str(index)
        form['page']['observationId'] = observation['observationId']
        form['page'].update(startCell=0, endCellExclusive=1, totalCells=1)
        form['page']['cells'][0]['dimensions']['DATATIME'] = period
        data['evidence'][0]['fact'] = deepcopy(form['runtime'])
        step['periodExecution'] = {'planId': 'period-plan', 'resolution': deepcopy(resolution), 'periodIndex': index,
                                   'executionPeriod': period, 'periodCount': 2, 'workIndex': 0, 'workCount': 1,
                                   'periodFinished': True, 'planFinished': index == 1, 'bindingIris': ['binding-A'],
                                   'periodRequest': deepcopy(intent)}
        data.update(executionId='period-execution-' + str(index), status='SUCCEEDED' if index else 'PARTIAL')
        data['completeness']['enumerationExhausted'] = index == 1
        if index == 0:
            data['resumeRef'] = data['executionId']
        else:
            data['continuationOf'] = 'period-execution-0'
            capture['request'] = {'resumeRef': 'period-execution-0'}
        capture['response']['content'][0]['text'] = json.dumps(data)
        capture['captureRef'] = ref
        captures[ref] = capture
        selected.append({**selectors[0], 'captureRef': ref, 'executionPeriod': period, 'observationId': observation['observationId']})
    return captures, selected, scope


class BudgetPeriodTest(TestCase):
    def _multi_work_capture(self):
        captures, selected, scope = period_fixture()
        # Both source work items completed, but only one contributes selected
        # facts (e.g. the other object's values now come from a newer observation).
        captures['february']['request'] = deepcopy(captures['replace']['request'])
        for capture in list(captures.values()):
            data = json.loads(capture['response']['content'][0]['text'])
            data['data']['stepResults'][1]['periodExecution']['workCount'] = 2
            capture['response']['content'][0]['text'] = json.dumps(data)
        for ref, capture in list(captures.items()):
            other = deepcopy(capture)
            data = json.loads(other['response']['content'][0]['text'])
            step = data['data']['stepResults'][1]
            step['periodExecution']['workIndex'] = 1
            observation = step['formResults'][0]['runtime']['observation']
            observation['observationId'] += '-other'
            step['formResults'][0]['page']['observationId'] = observation['observationId']
            data['executionId'] += '-other'
            data.pop('continuationOf', None)
            data.pop('resumeRef', None)
            other['response']['content'][0]['text'] = json.dumps(data)
            captures[ref + '-other'] = other
        return captures, selected, scope

    def test_completed_unselected_work_still_proves_source_plan_completion(self):
        captures, selected, scope = self._multi_work_capture()
        result = adapt(captures, selected, scope, 50000)
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')
        self.assertEqual(len(result['facts']), 2)
        self.assertEqual(result['scope']['periodCoverage'][0]['pendingWorkIndexes'], [])

    def test_unselected_work_missing_period_is_retained_without_blocking_selected_facts(self):
        captures, selected, scope = self._multi_work_capture()
        captures.pop('replace-other')
        result = adapt(captures, selected, scope, 50000)
        self.assertTrue(result['scope']['selectedDataComplete'])
        self.assertFalse(result['scope']['sourcePagesComplete'])
        self.assertFalse(result['scope']['periodCoverage'][0]['complete'])
        self.assertEqual(result['scope']['periodCoverage'][0]['pendingWorkIndexes'], [1])

    def test_completed_capture_does_not_replace_an_unselected_required_period(self):
        captures, selected, scope = self._multi_work_capture()
        result = adapt(captures, selected[:1], scope, 50000)
        self.assertEqual(result['scope']['coverage'], 'PARTIAL')
        self.assertEqual(result['scope']['periodCoverage'][0]['missingMemberPeriods'],
                         [{'executionPeriod': '2026Y0002', 'objectId': scope['members'][0]}])

    def test_host_source_registry_keeps_period_coverage_after_member_normalization(self):
        from analysis_extension.adapters import default_sources
        captures, selected, scope = period_fixture()
        result = default_sources().adapt(captures, selected, scope, 50000)
        self.assertTrue(result['scope']['periodCoverage'][0]['complete'])
        self.assertEqual(result['facts'][0]['executionPeriod'], '2026Y0001')

    def test_typed_inputs_combine_period_coverage_without_selecting_extra_page_facts(self):
        from analysis_extension.adapters import default_sources
        from analysis_extension.analysis_inputs import adapt_inputs
        captures, selected, scope = period_fixture()
        selected = [{**selection, 'role': 'PRIMARY', 'grain': ['MD_ORG']} for selection in selected]
        result = adapt_inputs(default_sources(), captures, selected, scope, 50000)
        self.assertTrue(result['scope']['selectedDataComplete'])
        incomplete = adapt_inputs(default_sources(), captures, selected[:1], scope, 50000)
        self.assertFalse(incomplete['scope']['selectedDataComplete'])

    def test_same_row_missing_middle_cell_page_cannot_prove_complete(self):
        captures, selected, scope = period_fixture()
        data = json.loads(captures['replace']['response']['content'][0]['text'])
        step = data['data']['stepResults'][1]
        page = step['formResults'][0]['page']
        page.update(startCell=0, endCellExclusive=1, totalCells=3, observationExhausted=False, nextCursor='grid-next')
        step['periodExecution']['periodFinished'] = False
        captures['replace']['response']['content'][0]['text'] = json.dumps(data)
        tail = deepcopy(captures['replace'])
        tail_data = deepcopy(data)
        tail_data['data']['stepResults'][1]['formResults'][0]['page'].update(startCell=2, endCellExclusive=3, observationExhausted=True, nextCursor=None)
        tail_data['data']['stepResults'][1]['periodExecution']['periodFinished'] = True
        tail_data.update(executionId='same-row-last', resumeRef='same-row-last')
        tail['response']['content'][0]['text'] = json.dumps(tail_data)
        captures['same-row-last'] = tail
        # Both captured ranges have identical row bounds, but cell offset 1 is absent.
        result = adapt(captures, selected, scope, 50000)
        self.assertFalse(result['scope']['sourcePagesComplete'])

    def test_all_periods_and_native_resolution_survive_resume(self):
        captures, selected, scope = period_fixture()
        result = adapt(captures, selected, scope, 50000)
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')
        self.assertEqual([f['executionPeriod'] for f in result['facts']], ['2026Y0001', '2026Y0002'])
        self.assertTrue(result['scope']['periodCoverage'][0]['complete'])
        self.assertEqual(result['facts'][1]['periodResolution']['referenceDate'], '2026-12-31')

    def test_missing_final_period_is_not_complete_or_zero_filled(self):
        captures, selected, scope = period_fixture()
        result = adapt({'replace': captures['replace']}, selected[:1], scope, 50000)
        self.assertFalse(result['scope']['selectedDataComplete'])
        self.assertEqual(result['scope']['periodCoverage'][0]['pendingPeriods'], ['2026Y0002'])
        self.assertEqual(len(result['facts']), 1)

    def test_last_success_cannot_hide_uncaptured_earlier_period(self):
        captures, selected, scope = period_fixture()
        tail = captures['february']
        tail['request'] = deepcopy(captures['replace']['request'])
        scope['population']['captureRef'] = 'february'
        result = adapt({'february': tail}, selected[1:], scope, 50000)
        self.assertEqual(result['scope']['coverage'], 'PARTIAL')
        self.assertEqual(result['scope']['periodCoverage'][0]['pendingPeriods'], ['2026Y0001'])

    def test_new_year_reference_drift_rejected_even_if_context_and_progress_agree(self):
        captures, selected, scope = period_fixture()
        data = json.loads(captures['february']['response']['content'][0]['text'])
        step = data['data']['stepResults'][1]
        for resolution in (step['periodExecution']['resolution'], step['formResults'][0]['runtime']['context']['periodResolution']):
            resolution.update(referenceDate='2027-01-01', referenceInstant='2027-01-01T00:00:01Z')
        captures['february']['response']['content'][0]['text'] = json.dumps(data)
        with self.assertRaisesRegex(AnalysisError, '改变了参照'):
            adapt(captures, selected, scope, 50000)

    def test_missing_period_receipt_rejected(self):
        captures, selected, scope = period_fixture()
        data = json.loads(captures['replace']['response']['content'][0]['text'])
        data['data']['stepResults'][1].pop('periodExecution')
        captures['replace']['response']['content'][0]['text'] = json.dumps(data)
        with self.assertRaisesRegex(AnalysisError, '缺少服务端'):
            adapt(captures, selected, scope, 50000)

    def test_wrong_execution_selector_rejected(self):
        captures, selected, scope = period_fixture()
        selected[0]['executionPeriod'] = '2025Y0001'
        with self.assertRaisesRegex(AnalysisError, '无法唯一定位'):
            adapt(captures, selected, scope, 50000)

    def test_actual_cell_period_and_execution_period_are_distinct(self):
        captures, selected, scope = period_fixture()
        data = json.loads(captures['replace']['response']['content'][0]['text'])
        data['data']['stepResults'][1]['formResults'][0]['page']['cells'][0]['dimensions']['DATATIME'] = '2025Y0012'
        captures['replace']['response']['content'][0]['text'] = json.dumps(data)
        items = catalog({**captures['replace'], 'captureRef': 'replace'})
        self.assertEqual(items[0]['period'], '2025Y0012')
        self.assertEqual(items[0]['executionPeriod'], '2026Y0001')
        result = adapt(captures, selected, scope, 50000)
        self.assertEqual(result['facts'][0]['period'], '2025Y0012')

    def test_equivalent_expression_requires_source_match_receipt(self):
        capture, selected, scope = fixture(values=('100',), page=False)
        expression = 'M_N[AMOUNT,-0N,ACCOUNT="profit",SCENE="budget"]'
        capture['request']['plan']['steps'][1]['formQuery']['expression'] = expression
        data = json.loads(capture['response']['content'][0]['text'])
        form = data['data']['stepResults'][1]['formResults'][0]
        form['cells'][0]['expression'] = 'M_N[AMOUNT,DATATIME="2026N0001",ACCOUNT="profit",SCENE="budget"]'
        capture['response']['content'][0]['text'] = json.dumps(data)
        with self.assertRaisesRegex(AnalysisError, '目标匹配证明'):
            adapt({'replace': capture}, selected, scope, 50000)
        form['matchedExpression'] = expression
        capture['response']['content'][0]['text'] = json.dumps(data)
        self.assertEqual(adapt({'replace': capture}, selected, scope, 50000)['facts'][0]['value'], '100')
