from copy import deepcopy

from analysis_presentation_fixtures import PresentationCase
from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_fact_view_fixtures import fact_view
from analysis_fixtures import OWNER
from analysis_extension.followup_scope import prepare_request
from analysis_extension.source_views import register, select, install
from analysis_extension.turn_completion import TurnCompletion
from analysis_extension.query_lineage import fact_draft
from unittest import TestCase


class FactDraftTests(TestCase):
    def test_sole_empty_alias_is_structural_and_keeps_predicate_values(self):
        request = {'intentDraft': {'targets': [{'id': 'old', 'businessPhrases': []}],
            'valueFilter': {'operator': 'AND', 'children': [
                {'operator': 'GT', 'targetId': 'old', 'value': '-10.01'},
                {'operator': 'LT', 'value': '0'}]}}}
        original = deepcopy(request)
        result = fact_draft(request)
        self.assertEqual(result['intentDraft']['targets'], [])
        self.assertEqual(result['intentDraft']['valueFilter']['children'], [
            {'operator': 'GT', 'value': '-10.01'}, {'operator': 'LT', 'value': '0'}])
        self.assertEqual(request, original)

    def test_substantive_or_ambiguous_target_is_not_silently_discarded(self):
        for target, predicate in [({'id': 'x', 'businessPhrases': [{'text': '收入'}]}, {}),
                                  ({'id': 'x', 'businessPhrases': []}, {'targetId': 'y'})]:
            request = {'intentDraft': {'targets': [target], 'valueFilter': predicate}}
            self.assertEqual(fact_draft(request), request)


class QueryLineageTest(PresentationCase):
    def test_answer_candidates_exclude_successful_probe_with_no_final_origins(self):
        from types import SimpleNamespace
        from analysis_extension.query_subject import candidates, freeze_subject
        capture, receipt = business_fixture()
        # One table/column was executed, but only one row of the other column
        # contributed to the native business result. No valueFilter is present.
        receipt['data']['resultSets'] = receipt['data']['resultSets'][:1]
        receipt['data']['resultSets'][0]['rows'] = receipt['data']['resultSets'][0]['rows'][:1]
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        turn = SimpleNamespace(service=self.service, state={'queryCaptureRefs': [ref]})
        choices = candidates(turn)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['columnNames'], ['metric1'])
        self.assertEqual(choices[0]['rowIndices'], [0])
        freeze_subject(turn, choices, [{'candidateRef': choices[0]['candidateRef']}])
        self.assertEqual(turn.state['queryAnswerSubject']['selections'][0]['rowIndices'], [0])
        freeze_subject(turn, choices, [{'candidateRef': choices[0]['candidateRef'], 'rowIndices': [1]}])
        self.assertTrue(turn.state['queryAnswerSubject']['invalidSelection'])
        receipt['data']['resultSets'] = []
        update(capture, receipt)
        capture.pop('captureRef', None)
        turn.state['queryCaptureRefs'] = [self.store.capture(OWNER, capture)]
        self.assertEqual(candidates(turn), [])

    def test_answer_candidates_keep_distinct_final_row_sets_separate(self):
        from types import SimpleNamespace
        from analysis_extension.query_subject import candidates, freeze_subject
        from analysis_extension.adapters.budget_batch import catalog
        capture, receipt = business_fixture()
        for index, result in enumerate(receipt['data']['resultSets']):
            result['rows'] = [result['rows'][index]]
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        turn = SimpleNamespace(service=self.service, state={'queryCaptureRefs': [ref]})
        choices = candidates(turn)
        self.assertEqual([(item['columnNames'], item['rowIndices']) for item in choices],
                         [(['metric1'], [0]), (['metric2'], [1])])
        freeze_subject(turn, choices, [{'candidateRef': item['candidateRef']} for item in choices])
        selected = turn.state['queryAnswerSubject']['selections']
        facts = catalog(self.store.get_capture(OWNER, ref), selected)
        self.assertEqual({(fact['columnName'], fact['rowIndex']) for fact in facts},
                         {('metric1', 0), ('metric2', 1)})
        self.assertEqual(len(facts), 2)

    def source(self, question=None, native_names_only=False, scenario_in_targets=False):
        capture, receipt = business_fixture()
        capture['request']['question'] = question or '查询2026年科目0和科目1预算'
        draft = {'sharedConditions': [{'text': '预算', 'sourceText': '预算'}],
                 'targets': [{'id': 't' + str(i), 'periodText': '2026年',
                              'businessPhrases': [{'text': '科目' + str(i), 'sourceText': '科目' + str(i)}]}
                             for i in range(2)]}
        if scenario_in_targets:
            scenario = draft.pop('sharedConditions')[0]
            for target in draft['targets']:
                target['businessPhrases'].append(deepcopy(scenario))
        for result in receipt['data']['resultSets']:
            result.setdefault('resolvedTerms', []).append({'term': '预算', 'dimensionCode': 'MD_SCENE'})
        capture['request']['intentDraft'] = draft
        receipt['data'].update(question=capture['request']['question'])
        receipt['data']['resolution']['intentDraft'] = deepcopy(draft)
        if native_names_only:
            for metric in receipt['data']['stepResults'][0]['batchResults'][0]['metrics']:
                metric['context'].pop('taskName', None)
                metric['context'].pop('formName', None)
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'lineage', 'request': '按板块分析这些数据',
            'followupContext': {'mode': 'CONTINUE'}, 'previousQueryIntent': {'metric': '科目0', 'period': '2026年', 'scenario': '预算'},
            'queryIntent': {'metric': '科目0', 'period': '2026年', 'scenario': '预算', 'grouping': '板块'},
            'mode': 'ANALYSIS', 'goal': '按板块分析', 'requiredEvidence': 'VALUES', 'failures': {}, 'buildRefs': []})
        source = register(self.service.turn, {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']})
        install(self.service.turn, select(self.service.turn, [{'sourceRef': source['sourceRef']}]))
        return source, self.store.get_capture(OWNER, ref)

    def test_runtime_names_are_valid_provenance_for_explicit_continue_scope(self):
        _, capture = self.source()
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'scope': {'taskNames': ['年度预算'], 'formNames': ['利润表']},
            'intentDraft': {'targets': [{'id': 't0', 'businessPhrases': [{'text': '科目0'}]}]}})
        self.assertIn('年度预算', result['contextText'])
        self.assertIn('利润表', result['contextText'])
        self.assertNotIn('valueFilter', result['intentDraft'])

    def test_continue_uses_native_result_context_when_technical_metrics_omit_names(self):
        _, capture = self.source(native_names_only=True)
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'scope': {'taskNames': ['年度预算'], 'formNames': ['利润表']}})
        self.assertIn('年度预算', result['contextText'])
        self.assertIn('利润表', result['contextText'])
        other = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'scope': {'formNames': ['另一张未选择的表']}})
        self.assertNotIn('另一张未选择的表', other.get('contextText', ''))

    def test_members_grouping_preserves_same_metric_and_does_not_mutate_task(self):
        source, capture = self.source()
        before = deepcopy(self.service.turn.state)
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY', 'groupBy': ['板块'],
            'basis': {'kind': 'MEMBERS', 'sourceRef': source['sourceRef']},
            'intentDraft': {'targets': [{'id': 't0', 'businessPhrases': [{'text': '科目0'}]}]}})
        self.assertIn('科目0', result['contextText'])
        self.assertEqual(result['intentDraft']['targets'][0]['businessPhrases'][0]['sourceText'], '科目0')
        self.assertEqual(result['basis']['memberDimensions'], ['MD_ORG'])
        self.assertEqual({k: v for k, v in self.service.turn.state.items() if k != 'businessEvidence'}, before)

    def test_members_grouping_can_omit_unchanged_target_without_selecting_other_metric(self):
        source, capture = self.source()
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY', 'groupBy': ['板块'],
            'basis': {'kind': 'MEMBERS', 'sourceRef': source['sourceRef']}})
        self.assertEqual([target['id'] for target in result['intentDraft']['targets']], ['t0'])
        self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '2026年')
        self.assertNotIn('valueFilter', result['intentDraft'])

    def test_continue_omitted_target_reuses_selected_metric_only(self):
        _, capture = self.source()
        self.service.turn.state.update(request='按金额降序排列', queryIntent={'order': '降序'})
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'presentation': {'valueOrder': 'DESC'}}})
        self.assertEqual([target['id'] for target in result['intentDraft']['targets']], ['t0'])
        self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '2026年')
        self.assertNotIn('valueFilter', result['intentDraft'])

    def test_confirmed_normalized_period_survives_followup_with_or_without_targets(self):
        _, capture = self.source('查询26年科目0和科目1预算')
        self.service.turn.state.update(request='按金额降序排列', queryIntent={'order': '降序'})
        for targets in ([], [{'id': 't0', 'periodText': '2026年',
                              'businessPhrases': [{'text': '科目0'}]}]):
            with self.subTest(targets=bool(targets)):
                result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
                    'intentDraft': {'targets': targets, 'presentation': {'valueOrder': 'DESC'}}})
                self.assertEqual(result['question'], '按金额降序排列')
                self.assertIn('2026年', result['contextText'])
                self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '2026年')
                self.assertEqual(result['scope']['periodCode'], '2026N0001')

    def test_repeated_or_changed_target_keeps_omitted_confirmed_shared_condition(self):
        _, capture = self.source()
        for changed, metric in (([], '科目0'), (['metric'], '收入')):
            with self.subTest(changed=changed):
                self.service.turn.state.update(request='查询收入' if changed else '按金额降序排列',
                    followupContext={'mode': 'CONTINUE', 'changedFields': changed})
                result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
                    'intentDraft': {'targets': [{'id': 'target', 'businessPhrases': [{'text': metric}]}]}})
                self.assertEqual([p['text'] for p in result['intentDraft']['sharedConditions']], ['预算'])
                self.assertEqual(result['intentDraft']['targets'][0]['businessPhrases'][0]['text'], metric)
                self.assertNotIn('valueFilter', result['intentDraft'])

    def test_target_embedded_scenario_is_retained_when_model_repeats_metric_only(self):
        _, capture = self.source(scenario_in_targets=True)
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'targets': [{'id': 'target', 'businessPhrases': [{'text': '科目0'}]}]}})
        self.assertEqual([p['text'] for p in result['intentDraft']['sharedConditions']], ['预算'])
        self.assertNotIn('valueFilter', result['intentDraft'])
        explicit = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'sharedConditions': [], 'targets': [{'id': 'target', 'businessPhrases': [{'text': '科目0'}]}]}})
        self.assertEqual(explicit['intentDraft']['sharedConditions'], [])
        self.service.turn.state.update(request='改查实际',
            followupContext={'mode': 'CONTINUE', 'changedFields': ['scenario']})
        changed = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'targets': [{'id': 'target', 'businessPhrases': [{'text': '科目0'}, {'text': '实际'}]}]}})
        self.assertEqual(changed['intentDraft']['sharedConditions'], [])

    def test_shared_condition_clear_and_scenario_switch_do_not_reintroduce_budget(self):
        _, capture = self.source()
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'sharedConditions': [], 'targets': [{'id': 't0', 'businessPhrases': [{'text': '科目0'}]}]}})
        self.assertEqual(result['intentDraft']['sharedConditions'], [])
        self.service.turn.state.update(request='改查实际',
            followupContext={'mode': 'CONTINUE', 'changedFields': ['scenario']})
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'intentDraft': {'targets': [{'id': 't0', 'businessPhrases': [{'text': '科目0'}, {'text': '实际'}]}]}})
        self.assertEqual(result['intentDraft']['sharedConditions'], [])
        self.assertNotIn('预算', result.get('contextText', ''))

    def test_period_and_scenario_changes_release_old_parts_but_keep_metric(self):
        source, capture = self.source()
        self.service.turn.state.update(request='改查2027年实际，其他不变',
            followupContext={'mode': 'CONTINUE', 'changedFields': ['period', 'scenario']},
            queryIntent={'metric': '科目0', 'period': '2027年', 'scenario': '实际'})
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'basis': {'kind': 'MEMBERS', 'sourceRef': source['sourceRef']},
            'intentDraft': {'sharedConditions': [{'text': '实际'}]},
            'scope': {'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'YEAR', 'year': 2027}}}})
        self.assertEqual([target['id'] for target in result['intentDraft']['targets']], ['t0'])
        self.assertNotIn('periodText', result['intentDraft']['targets'][0])
        self.assertNotIn('2026', result.get('contextText', ''))
        self.assertNotIn('预算', result.get('contextText', ''))
        self.assertEqual(result['intentDraft']['sharedConditions'][0]['text'], '实际')

    def test_repeated_facts_keep_metric_and_explicit_form_for_next_independent_query(self):
        source, capture = self.source()
        first, _ = fact_view(self.service.turn, source['sourceRef'], {'operator': 'GT', 'value': '0'}, reopened=True)
        second, _ = fact_view(self.service.turn, first['sourceRef'], reopened=True)
        install(self.service.turn, select(self.service.turn, [{'sourceRef': second['sourceRef']}]))
        self.service.turn.state['request'] = '查询所有单位的科目0预算'
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY', 'scope': {'formNames': ['利润表']},
            'intentDraft': {'targets': [{'id': 't0', 'businessPhrases': [{'text': '科目0'}, {'text': '预算'}]}],
                'dimensionScopes': [{'id': 'org', 'dimensionHint': '组织机构', 'selector': {'type': 'ALL'}}]}})
        self.assertIn('利润表', result['contextText'])
        self.assertNotIn('valueFilter', result['intentDraft'])
        self.assertEqual(result['scope']['periodCode'], '2026N0001')

    def test_changed_metric_is_not_restored_and_unselected_names_gain_no_evidence(self):
        source, capture = self.source()
        self.service.turn.state.update(request='查询这些单位的收入',
            followupContext={'mode': 'CONTINUE', 'changedFields': ['metric']}, queryIntent={'metric': '收入'})
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'basis': {'kind': 'MEMBERS', 'sourceRef': source['sourceRef']},
            'scope': {'formNames': ['无关表']},
            'intentDraft': {'targets': [{'id': 'income', 'businessPhrases': [{'text': '收入'}]}]}})
        self.assertNotIn('科目0', result.get('contextText', ''))
        self.assertNotIn('无关表', result.get('contextText', ''))
        self.assertNotIn('formCodes', result['scope'])
        self.assertEqual(result['scope']['taskCodes'], ['TASK'])

    def test_new_query_does_not_borrow_previous_runtime_name(self):
        _, capture = self.source()
        self.service.turn.state.update(request='查询收入', followupContext={'mode': 'NEW'}, queryIntent={'metric': '收入'})
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'scope': {'taskNames': ['年度预算']},
            'intentDraft': {'targets': [{'id': 'income', 'businessPhrases': [{'text': '收入'}]}]}})
        self.assertNotIn('年度预算', result.get('contextText', ''))

    def test_pending_metric_change_releases_source_metric_after_repeated_resume(self):
        source, capture = self.source()
        source, _ = fact_view(self.service.turn, source['sourceRef'])
        install(self.service.turn, select(self.service.turn, [{'sourceRef': source['sourceRef']}]))
        state = self.service.turn.state
        state.update(request='继续查询这些单位的收入', previousQueryIntent={'metric': '收入'},
            queryIntent={'metric': '收入'}, basePendingChanges=['metric'],
            selectedContextRefs=['pending-2'], followupContext={'mode': 'CONTINUE'},
            businessContexts=[
                {'contextRef': 'delivered', 'kind': 'DELIVERED', 'intent': {'metric': '科目0'}},
                {'contextRef': 'pending-1', 'kind': 'PENDING', 'intent': {'metric': '收入'},
                 'baseContextRefs': ['delivered']},
                {'contextRef': 'pending-2', 'kind': 'PENDING', 'intent': {'metric': '收入'},
                 'baseContextRefs': ['pending-1']}])
        before = deepcopy(state)
        result = prepare_request(self.service.turn, capture, {'purpose': 'QUERY',
            'basis': {'kind': 'MEMBERS', 'sourceRef': source['sourceRef']},
            'intentDraft': {'targets': [{'id': 'income', 'businessPhrases': [{'text': '收入'}]}]}})
        self.assertNotIn('科目0', result.get('contextText', ''))
        self.assertIn('2026年', result.get('contextText', ''))
        self.assertEqual(result['basis']['memberDimensions'], ['MD_ORG'])
        self.assertEqual([target['id'] for target in result['intentDraft']['targets']], ['income'])
        self.assertNotIn('valueFilter', result['intentDraft'])
        self.assertEqual({k: v for k, v in state.items() if k != 'businessEvidence'},
                         {k: v for k, v in before.items() if k != 'businessEvidence'})
