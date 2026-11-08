from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import json

from analysis_exact_cells_test import exact_fixture
from analysis_budget_batch_test import update
from analysis_extension.adapters.budget_v2 import catalog, receipt_from_capture
from analysis_extension.budget_preparation import batch_targets, metric_target
from analysis_extension.models import AnalysisError


def scenario_fixture(scene='ACTUAL', fallback_indices=(0, 1, 2, 3)):
    capture, receipt = exact_fixture()
    table = receipt['data']['stepResults'][0]['batchResults'][0]
    for index in fallback_indices:
        origin = table['cellOrigins'][index]
        anchor = deepcopy(origin['selectedDimensions'])
        origin['cellDimensions'] = anchor
        selected = {**anchor, 'MD_SCENE': scene}
        expression = 'MX_FINANCIAL_N[ZB_JE2' + ''.join(',' + key + '=' + json.dumps(value)
                       for key, value in sorted(selected.items())) + ']'
        proof = {'kind': 'BUDGET_CELL_SCENARIO_OVERRIDE', 'dimensionCode': 'MD_SCENE',
                 'fromValue': 'BUDGET', 'toValue': scene, 'anchorDimensions': deepcopy(anchor),
                 'anchorExpression': origin['runtimeCellExp'], 'queryExpression': expression,
                 'formulaStatus': 'NO_TARGET_CELL'}
        origin.update(selectedDimensions=selected, scenarioFallback=proof, headers=['净利润', '预算数'])
        table['request']['cellTargets'][index]['dimensions'] = selected
        row = table['data']['cellResults'][index]
        row['dimensions'] = selected
        row['dimensionNames']['MD_SCENE'] = '实际数' if scene == 'ACTUAL' else scene
        fact = receipt['data']['resultSets'][index // 2]['rows'][index % 2]
        fact.update(dimensions=selected, cellExp=expression, title='净利润_' + scene)
    update(capture, receipt)
    return capture, receipt


class ScenarioFallbackTests(TestCase):
    def test_exact_values_use_target_scenario_and_cannot_claim_budget_formula(self):
        for scene in ('ACTUAL', 'FORECAST', '1EMACTUAL'):
            capture, receipt = scenario_fixture(scene)
            receipt_from_capture(capture)
            facts = catalog(capture)
            self.assertEqual(len(facts), 4)
            for fact in facts:
                self.assertEqual(fact['dimensions']['MD_SCENE'], scene)
                self.assertEqual(fact['dimensions']['DATATIME'], '2026N0001')
                self.assertEqual(fact['formulaStatus'], 'NO_TARGET_CELL')
                self.assertNotIn('预算', fact['label'])
                self.assertIn('MD_SCENE="' + scene + '"', fact['expression'])
            origin = receipt['data']['stepResults'][0]['batchResults'][0]['cellOrigins'][0]
            with self.assertRaises(AnalysisError) as error:
                metric_target(capture, receipt, origin)
            self.assertEqual(error.exception.code, 'SCENARIO_FORMULA_UNAVAILABLE')

    def test_forged_period_anchor_or_unconfirmed_scene_is_rejected(self):
        for change in ('anchor', 'period', 'scene', 'formula'):
            capture, receipt = scenario_fixture()
            table = receipt['data']['stepResults'][0]['batchResults'][0]
            origin = table['cellOrigins'][0]
            if change == 'anchor': origin['cellDimensions']['MD_SCENE'] = 'FORECAST'
            if change == 'period': origin['scenarioFallback']['anchorDimensions']['DATATIME'] = '2025N0001'
            if change == 'scene': table['data']['cellResults'][0]['dimensions'] = {**table['data']['cellResults'][0]['dimensions'], 'MD_SCENE': 'BUDGET'}
            if change == 'formula': origin['scenarioFallback']['formulaStatus'] = 'COMPLETE'
            update(capture, receipt)
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                catalog(capture)

    def test_automatic_formula_preparation_skips_fallbacks_but_keeps_direct_cells(self):
        service = SimpleNamespace(sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(adapter_id='budget-v2'))))
        capture, _ = scenario_fixture()
        self.assertEqual(batch_targets(service, capture), [])
        capture, _ = scenario_fixture(fallback_indices=(0, 1))
        targets = batch_targets(service, capture)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['sourceSelection']['resultSetId'], 'r1')

    def test_formula_inspection_does_not_request_repeated_form_expansion_for_fallback(self):
        from analysis_extension.formulas import trace_formula
        capture, _ = scenario_fixture()
        fact = catalog(capture)[0]
        trace = trace_formula([fact], fact['factId'])
        self.assertEqual(trace['status'], 'NO_FORMULA')
        self.assertEqual(trace['applicabilityStatus'], 'NO_TARGET_SCENARIO_CELL')
        self.assertIsNone(trace['recovery'])

    def test_changed_phrase_is_not_reintroduced_when_model_put_scenario_in_task_field(self):
        from analysis_extension.followup_scope import _confirmed_business_terms, prepare_request
        capture, receipt = exact_fixture()
        capture['request']['question'] = '久其集团2026年净利润预算'
        capture['request']['intentDraft'] = {'sharedConditions': [], 'targets': [{'businessPhrases': [
            {'text': '净利润', 'sourceText': '净利润'}, {'text': '预算', 'sourceText': '预算'}]}]}
        receipt['data']['resultSets'][0]['resolvedTerms'] = [{'term': '预算', 'dimensionCode': 'MD_SCENE'}]
        turn = SimpleNamespace(state={'request': '改看实际数，其余条件不变',
            'previousQueryIntent': {'task': '预算', 'metric': '净利润', 'period': '2026年'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['task']},
            'inheritedScope': {'captureRefs': [capture['captureRef']]}},
            service=SimpleNamespace(owner=None, store=SimpleNamespace(get_capture=Mock(return_value=capture)),
                sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(raw_receipt=Mock(return_value=receipt))))))
        origin = {'originalToolName': 'execute_budget_query', 'resourceId': capture['resourceId']}
        with patch('analysis_extension.followup_scope._same_source', return_value=True), \
             patch('analysis_extension.followup_scope._inherited_tables', return_value=[]):
            self.assertEqual(_confirmed_business_terms(turn, origin, {'task'}), ['净利润'])
            request = prepare_request(turn, origin, {'purpose': 'QUERY', 'question': 'actual'})
            turn.state['followupContext']['changedFields'] = ['scenario']
            corrected_request = prepare_request(turn, origin, {'purpose': 'QUERY', 'question': 'actual'})
        self.assertNotIn('预算', request.get('contextText', ''))
        self.assertIn('净利润', request['contextText'])
        self.assertNotIn('预算', corrected_request.get('contextText', ''))
        capture['request']['intentDraft']['targets'][0]['businessPhrases'] = [
            {'text': '净利润预算', 'sourceText': '净利润预算', 'alternatives': [['净利润', '预算']]}]
        receipt['data']['resultSets'][0]['resolvedTerms'] = [
            {'term': '净利润', 'dimensionCode': 'MD_BUDACCOUNT'}, {'term': '预算', 'dimensionCode': 'MD_SCENE'}]
        turn.state['previousQueryIntent']['metric'] = '净利润预算'
        with patch('analysis_extension.followup_scope._same_source', return_value=True), \
             patch('analysis_extension.followup_scope._inherited_tables', return_value=[]):
            combined = prepare_request(turn, origin, {'purpose': 'QUERY'})
        self.assertIn('净利润', combined['contextText'])
        self.assertNotIn('预算', combined['contextText'])
        turn.state['previousQueryIntent']['metric'] = '净利润'
        capture['request']['intentDraft']['targets'][0]['businessPhrases'] = [
            {'text': '净利润', 'sourceText': '净利润'}, {'text': '预算', 'sourceText': '预算'}]
        capture['request']['question'] = '改看实际数，其余条件不变'
        capture['request']['contextText'] = '久其集团 净利润 2026年'
        capture['request']['intentDraft']['targets'][0]['businessPhrases'][1] = {'text': '实际数', 'sourceText': '实际数'}
        receipt['data']['resultSets'][0]['resolvedTerms'] = [{'term': '实际数', 'dimensionCode': 'MD_SCENE'}]
        turn.state['previousQueryIntent']['scenario'] = '实际数'
        turn.state['request'] = '分析这项实际净利润的构成'
        turn.state['followupContext']['changedFields'] = []
        with patch('analysis_extension.followup_scope._same_source', return_value=True), \
             patch('analysis_extension.followup_scope._inherited_tables', return_value=[]):
            third = prepare_request(turn, origin, {'purpose': 'ANALYZE'})
        self.assertNotIn('预算', third.get('contextText', ''))
        self.assertIn('实际数', third['contextText'])

    def test_misclassified_changed_field_releases_only_its_confirmed_dimension(self):
        from analysis_extension.dimension_scopes import changed_fields
        record = {'draft': {'dimensionHint': '情景', 'sourceText': '预算', 'selector': {'members': ['预算']}},
                  'bindings': [{'dimCode': 'MD_SCENE'}]}
        state = {'previousQueryIntent': {'task': '预算', 'metric': '净利润'},
                 'followupContext': {'changedFields': ['task']}, 'inheritedDimensionScopes': [record]}
        self.assertEqual(changed_fields(state), {'task', 'scenario'})
        state['previousQueryIntent']['task'] = '年度预算二上终版'
        self.assertEqual(changed_fields(state), {'task'})

    def test_empty_actual_query_does_not_call_it_a_budget_value_or_a_match_failure(self):
        from analysis_extension.query_empty_result import empty_query_answer
        evidence = {'sources': [{'source': {'businessQuery': {'purpose': 'QUERY', 'complete': True,
            'failureSummary': {'outcome': 'NO_DATA', 'matchDiagnostics': [{'rejectionCounts': {'DIFFERENT_MODEL': 10}}]}}},
            'facts': [{'valueState': 'NO_DATA'}]}]}
        answer = empty_query_answer(evidence)['answer']
        self.assertIn('完成取数', answer)
        self.assertNotIn('预算值', answer)
        self.assertNotIn('运行匹配记录', answer)

    def test_empty_scenario_analysis_is_terminal_only_for_complete_verified_fallbacks(self):
        from analysis_extension.query_empty_result import empty_scenario_analysis_answer
        evidence = {'sources': [{'source': {'businessQuery': {'purpose': 'ANALYZE', 'complete': True,
            'failureSummary': {'outcome': 'NO_DATA'}}}, 'facts': [{'valueState': 'NO_DATA',
            'formulaStatus': 'NO_TARGET_CELL', 'dimensions': {'MD_SCENE': 'ACTUAL'},
            'dimensionNames': {'MD_SCENE': '实际数'}}]}]}
        self.assertIn('实际数', empty_scenario_analysis_answer(evidence)['answer'])
        evidence['sources'][0]['source']['businessQuery']['purpose'] = 'QUERY'
        self.assertIsNotNone(empty_scenario_analysis_answer(evidence))
        for change in ('partial', 'present', 'formula', 'diagnostic'):
            altered = deepcopy(evidence); source = altered['sources'][0]
            if change == 'partial': source['source']['businessQuery']['complete'] = False
            if change == 'present': source['facts'][0]['valueState'] = 'PRESENT'
            if change == 'formula': source['facts'][0]['formulaStatus'] = 'COMPLETE'
            if change == 'diagnostic': source['diagnostic'] = {'code': 'INVALID_RECEIPT'}
            with self.subTest(change=change): self.assertIsNone(empty_scenario_analysis_answer(altered))

    def test_query_review_projection_preserves_fallback_formula_boundary_for_followups(self):
        from analysis_extension.review_materials import business_query_projection
        capture, receipt = scenario_fixture()
        for origin in receipt['data']['origins']:
            origin['scenarioFallback'] = {'kind': 'BUDGET_CELL_SCENARIO_OVERRIDE', 'formulaStatus': 'NO_TARGET_CELL'}
        _, facts, _ = business_query_projection(capture, receipt, {'businessQuery': {}})
        self.assertTrue(facts)
        self.assertTrue(all(fact.get('formulaStatus') == 'NO_TARGET_CELL' for fact in facts))
