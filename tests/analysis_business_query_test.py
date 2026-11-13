from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch
from analysis_budget_batch_test import batch_fixture, update
from analysis_extension.adapters.budget_query import validate
from analysis_extension.adapters.budget_v2 import receipt_from_capture, _step_request
from analysis_extension.adapters.budget_batch import validate as validate_batch
from analysis_extension.query_results import compact_receipt, read_receipt, encode
from analysis_extension.models import AnalysisError


def business_fixture():
    capture, selection, scope, receipt = batch_fixture(False, True)
    original = capture['request']['plan']['steps'][-1]
    original['resourceRefs'] = ['binding-A']
    original['inputs'] = []
    capture['originalToolName'] = 'execute_budget_query'
    targets = [{'id': f't{i}', 'label': f'科目{i}', 'allOf': [
        {'term': f'科目{i}', 'anyOf': [{'baseDataCode': 'ACCOUNTS', 'objectCode': code}]}]}
        for i, code in enumerate(('0205', '0206'))]
    capture['request'] = {'purpose': 'QUERY', 'question': '2026年科目0和科目1',
                          'scope': {'periodCode': '2026N0001', 'taskCodes': ['TASK'], 'formCodes': ['LRB']},
                          'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId']}
    data = receipt['data']
    data.update(resultType='BUDGET_QUERY_RESULT', purpose='QUERY', selection='CELLS', complete=True,
                targets=targets, resultSets=[], origins=[], unresolvedGroups=[],
                runtimeRequests=[original], grouping=[], summary={'rowCount': 4, 'resultSetCount': 2})
    data.update(question=capture['request']['question'], resolution={'exactRecallComplete': True,
                'interpretations': deepcopy(targets)})
    table = data['stepResults'][0]['batchResults'][0]
    for index, metric in enumerate(table['metrics']):
        context = metric['context']
        context['target'].update(taskCode='TASK', formCode='LRB')
        context.update(taskName='年度预算', formName='利润表')
        account = metric['selectedDimensions']['MD_BUDACCOUNT']
        result_set = {'resultSetId': f'r{index}', 'targetId': f't{index}', 'interpretationId': f'i{index}',
                      'context': deepcopy(context), 'resolvedTerms': [{'term': f'科目{index}', 'baseDataCode': 'ACCOUNTS',
                          'objectCode': account, 'dimensionCode': 'MD_BUDACCOUNT'}], 'grain': ['MD_ORG'], 'rows': []}
        for row_index, row in enumerate(table['data']['rows']):
            ref = f'o{index}-{row_index}'
            value = row[index + 2]
            result_set['rows'].append({'factId': f'f{index}-{row_index}', 'title': '科目_预算数',
                'dimensions': {'MD_BUDACCOUNT': account, 'MD_ORG': row[0]}, 'dataPeriod': '2026N0001',
                'value': value, 'valueState': 'PRESENT' if value is not None else 'NULL', 'unit': '元', 'originRefs': [ref]})
            data['origins'].append({'originId': ref, 'sourceType': 'BATCH_QUERY', 'taskCode': 'TASK', 'formCode': 'LRB',
                                   'stepId': 'batch', 'batchId': 'batch-A', 'rowIndex': row_index, 'sourceColumn': f'metric{index + 1}'})
        data['resultSets'].append(result_set)
    update(capture, receipt)
    return capture, receipt


class BusinessQueryTest(TestCase):
    def test_independent_query_does_not_inherit_or_conflict_with_previous_value_filter(self):
        from types import SimpleNamespace
        from analysis_extension.followup_scope import prepare_request
        turn = SimpleNamespace(state={'request': '查询净利润小于0', 'followupContext': {'mode': 'NEW'}})
        origin = {'originalToolName': 'execute_budget_query'}
        targets = [{'id': 't', 'businessPhrases': [{'text': '净利润'}]}]
        first = prepare_request(turn, origin, {'purpose': 'QUERY', 'intentDraft': {'targets': targets, 'valueFilter': {'operator': 'LT', 'value': '0'}}})
        self.assertEqual(first['intentDraft']['valueFilter'], {'operator': 'LT', 'value': '0'})
        independent = prepare_request(turn, origin, {'purpose': 'QUERY', 'intentDraft': {'targets': targets}})
        self.assertNotIn('valueFilter', independent['intentDraft'])
        changed = prepare_request(turn, origin, {'purpose': 'QUERY', 'intentDraft': {'targets': targets, 'valueFilter': {'operator': 'GT', 'value': '0'}}})
        self.assertEqual(changed['intentDraft']['valueFilter'], {'operator': 'GT', 'value': '0'})
        self.assertNotIn('budgetValueFilter', turn.state)

    def test_changed_population_keeps_proven_hierarchy_root_even_when_intent_omits_its_name(self):
        from types import SimpleNamespace
        from analysis_extension.followup_scope import prepare_request
        turn = SimpleNamespace(state={'request': '看下二级单位的净利润，从小到大',
            'previousQueryIntent': {'metric': '净利润', 'population': '久其集团', 'scenario': '预算', 'period': '2026年'},
            'queryIntent': {'population': '二级单位'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['population', 'order']}})
        request = {'purpose': 'QUERY', 'intentDraft': {'relations': [{'type': 'HIERARCHY',
                    'targetIds': ['t'], 'sourceText': '二级单位', 'hierarchy': {'rootText': '久其集团', 'level': 2}}]}}
        result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertIn('久其集团', result['contextText'])
        request['intentDraft']['relations'][0]['hierarchy']['rootText'] = '别的集团'
        with self.assertRaises(AnalysisError) as invalid:
            prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertEqual(invalid.exception.code, 'QUERY_DRAFT_EVIDENCE_INVALID')
    def test_names_fact_references_and_verified_sort_do_not_require_raw_row_reads(self):
        from analysis_extension.adapters.budget_query import present_value_groups
        capture, receipt = business_fixture()
        data = receipt['data']
        capture['request']['intentDraft'] = {'sharedConditions': [], 'targets': [], 'presentation': {'valueOrder': 'DESC'}}
        data['resolution']['intentDraft'] = deepcopy(capture['request']['intentDraft'])
        proofs = []
        for result in data['resultSets']:
            for row in result['rows']:
                row['dimensionNames'] = {'MD_ORG': '机构' + row['dimensions']['MD_ORG']}
            proofs.append({'resultSetId': result['resultSetId'], 'rowCount': len(result['rows']),
                           'orderedFactIds': [row['factId'] for row in result['rows']]})
        data['presentation'] = {'valueOrder': 'DESC', 'nulls': 'LAST', 'resultSets': proofs}
        validate(capture, receipt)
        group = present_value_groups(data)[0]
        self.assertEqual(group['dimensionNames']['MD_ORG'], '机构07')
        self.assertEqual(group['factRefs'], [{'resultSetId': 'r0', 'factId': 'f0-0'}])
        data['resultSets'][1]['rows'].reverse()
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_host_does_not_force_one_year_on_two_semantic_targets(self):
        from types import SimpleNamespace
        from analysis_extension.followup_scope import prepare_request
        turn = SimpleNamespace(state={'request': '2026年预算和2025年实际对比',
                                      'queryIntent': {'formExecutionYear': 2026}})
        draft = {'sharedConditions': [], 'targets': [
            {'id': 'a', 'periodText': '2026年', 'businessPhrases': [{'text': '预算', 'sourceText': '预算'}]},
            {'id': 'b', 'periodText': '2025年', 'businessPhrases': [{'text': '实际', 'sourceText': '实际'}]}]}
        result = prepare_request(turn, {'originalToolName': 'execute_budget_query'},
                                 {'question': '改写', 'purpose': 'QUERY', 'intentDraft': draft})
        self.assertEqual(result['question'], turn.state['request'])
        self.assertEqual(result['intentDraft'], draft)
        self.assertNotIn('periodCode', result.get('scope', {}))

    def test_returned_draft_and_target_period_cannot_drift(self):
        capture, receipt = business_fixture()
        draft = {'sharedConditions': [], 'targets': [{'id': 't0', 'periodText': '2026年', 'businessPhrases': []}]}
        capture['request']['intentDraft'] = deepcopy(draft)
        receipt['data']['resolution']['intentDraft'] = deepcopy(draft)
        validate(capture, receipt)
        receipt['data']['resolution']['intentDraft']['targets'][0]['periodText'] = '2025年'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        receipt['data']['resolution']['intentDraft'] = deepcopy(draft)
        for group in (receipt['data']['targets'], receipt['data']['resolution']['interpretations']):
            group[0]['scope'] = {'periodCode': '2025N0001'}
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_title_fallback_never_overrides_a_conflicting_runtime_dimension(self):
        capture, receipt = business_fixture()
        result = receipt['data']['resultSets'][0]
        member = result['resolvedTerms'][0]
        member['titleFallback'] = True
        row = result['rows'][0]
        row['dimensions'][member['dimensionCode']] = 'OTHER'
        row['conditionEvidence'] = [{**member, 'verifiedBy': 'RUNTIME_TITLE', 'runtimeHeaders': ['科目0']}]
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_terminal_receipt_supersedes_waits_only_for_same_request_and_execution(self):
        from analysis_extension.query_waiting import superseded_status_reads
        capture={'captureRef':'waiting','originalToolName':'execute_budget_query','resourceId':'source','request':{'requestId':'r','question':'净利润'}}
        receipt={'executionId':'e','status':'FAILED','executionState':'UNKNOWN','error':{'recoveryAction':'READ_STATUS'}}
        final={**capture,'captureRef':'final'}
        terminal={'executionId':'e','status':'PARTIAL'}
        self.assertEqual(superseded_status_reads([(capture,receipt),(final,terminal)]),{'waiting'})
        self.assertEqual(superseded_status_reads([(capture,receipt),(final,{**terminal,'executionId':'other'})]),set())
        self.assertEqual(superseded_status_reads([(capture,receipt),({**final,'request':{'requestId':'r','question':'收入'}},terminal)]),set())
        self.assertEqual(superseded_status_reads([(capture,receipt)]),set())

    def test_initial_unknown_without_execution_id_uses_exact_idempotent_request(self):
        from analysis_extension.query_waiting import superseded_status_reads
        capture={'captureRef':'waiting','originalToolName':'execute_budget_query','resourceId':'source',
                 'serverName':'budget','request':{'requestId':'r','question':'净利润'}}
        receipt={'status':'FAILED','executionState':'UNKNOWN','error':{'recoveryAction':'READ_STATUS'}}
        final={**capture,'captureRef':'final'}
        terminal={'executionId':'e','status':'PARTIAL'}
        self.assertEqual(superseded_status_reads([(capture,receipt),(final,terminal)]),{'waiting'})
        self.assertEqual(superseded_status_reads([(capture,receipt),({**final,'serverName':'other'},terminal)]),set())
        self.assertEqual(superseded_status_reads([(capture,receipt),
            ({**final,'request':{'requestId':'r','question':'收入'}},terminal)]),set())
        self.assertEqual(superseded_status_reads([(capture,receipt),(final,terminal),
            ({**capture,'captureRef':'other'},{'executionId':'other','status':'IN_PROGRESS'})]),set())
        missing={**capture,'request':{'question':'净利润'}}
        self.assertEqual(superseded_status_reads([(missing,receipt),({**missing,'captureRef':'final'},terminal)]),set())

    def test_same_value_tasks_are_listed_without_duplicate_interpretations_or_cross_scope_merging(self):
        from analysis_extension.adapters.budget_query import present_value_groups
        capture,receipt=business_fixture()
        first=receipt['data']['resultSets'][0]
        copy=deepcopy(first)
        copy['context']['target']['taskCode']='TASK2';copy['context']['taskName']='第二任务'
        receipt['data']['resultSets'] += [deepcopy(first),copy]
        values=present_value_groups(receipt['data'])
        matched=[item for item in values if item['dimensions']==first['rows'][0]['dimensions']]
        self.assertEqual(len(matched),1)
        self.assertEqual([task['code'] for task in matched[0]['tasks']],['TASK','TASK2'])
        self.assertEqual(matched[0]['value'],first['rows'][0]['value'])

    def test_value_filter_requires_exact_population_proof_and_detects_tampered_decision(self):
        from analysis_extension.adapters.budget_query import validate
        capture, receipt = business_fixture()
        predicate = {'operator': 'GT', 'value': '-1'}
        capture['request']['intentDraft'] = {'valueFilter': predicate}
        receipt['data']['resolution']['intentDraft'] = deepcopy(capture['request']['intentDraft'])
        evaluated = [{**deepcopy(row), 'resultSetId': result['resultSetId'], 'matched': row['valueState'] == 'PRESENT'}
                     for result in receipt['data']['resultSets'] for row in result['rows']]
        for result in receipt['data']['resultSets']:
            result['rows'] = [row for row in result['rows'] if row['valueState'] == 'PRESENT']
        receipt['data']['valueFilter'] = {**predicate, 'populationCount': len(evaluated), 'matchedCount': sum(row['matched'] for row in evaluated),
                                        'populationComplete': True, 'evaluatedFacts': evaluated}
        validate(capture, receipt)
        update(capture, receipt)
        from analysis_extension.adapters.budget_batch import selectors, catalog
        selected = selectors(capture)
        self.assertTrue(all(item['rowIndicesOptional'] is False for item in selected))
        self.assertEqual(len(catalog(capture)), sum(row['matched'] for row in evaluated))
        self.assertEqual(len(catalog(capture, selected)), sum(row['matched'] for row in evaluated))
        with self.assertRaises(AnalysisError):
            catalog(capture, [{key: value for key, value in selected[0].items() if key != 'rowIndices'}])
        evaluated[0]['matched'] = False
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        evaluated[0]['matched'] = True
        evaluated[0]['value'] = '999999'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_filter_population_proof_is_not_an_analysis_input(self):
        from analysis_extension.adapters.budget_batch import selectors, tables
        from analysis_extension.adapters.budget_v2 import form_rows
        capture, receipt = business_fixture()
        proof = deepcopy(receipt['data']['stepResults'][0])
        proof.update(stepId='population-batch', evidenceRole='FILTER_POPULATION')
        receipt['data']['stepResults'].append(proof)
        request = deepcopy(receipt['data']['runtimeRequests'][0])
        request['id'] = 'population-batch'
        receipt['data']['runtimeRequests'].append(request)
        update(capture, receipt)
        self.assertNotIn('population-batch', [step for step, _, _ in tables(receipt)])
        self.assertNotIn('population-batch', [item['stepId'] for item in selectors(capture)])
        proof['formResults'] = [{'observationId': 'proof-only'}]
        self.assertNotIn('population-batch', [step for step, _ in form_rows(receipt)])

    def test_host_preserves_original_question_and_rebuilds_followup_from_business_text(self):
        from types import SimpleNamespace
        from analysis_extension.followup_scope import prepare_request
        turn = SimpleNamespace(state={'request': '那25年呢',
            'previousQueryIntent': {'metric': '净利润', 'population': '久其集团', 'scenario': '预算', 'period': '2026年'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['period']}})
        request = prepare_request(turn, {'originalToolName': 'execute_budget_query'},
                                  {'purpose': 'QUERY', 'question': '模型改写', 'contextText': '模型自行删掉的条件'})
        self.assertEqual(request['question'], '那25年呢')
        self.assertEqual(request['contextText'], '净利润 久其集团 预算')
        self.assertNotIn('targets', request)

    def test_followup_recovers_scenario_from_successful_request_when_model_intent_omits_it(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from analysis_extension.followup_scope import prepare_request
        capture = {'originalToolName': 'execute_budget_query', 'response': {}, 'request': {
            'question': '2026年久其集团净利润预算', 'intentDraft': {'sharedConditions': [], 'targets': [
                {'businessPhrases': [{'text': '净利润', 'sourceText': '净利润'}, {'text': '预算', 'sourceText': '净利润预算'}]}]}}}
        receipt = {'status': 'SUCCEEDED', 'data': {'resultSets': [{'resolvedTerms': [
            {'term': '净利润', 'dimensionCode': 'MD_BUDACCOUNT'}, {'term': '预算', 'dimensionCode': 'MD_SCENE'}]}]}}
        service = SimpleNamespace(owner='owner', store=SimpleNamespace(get_capture=Mock(return_value=capture)),
            sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(raw_receipt=Mock(return_value=receipt)))))
        turn = SimpleNamespace(service=service, state={'request': '看二级单位的，从利润表查',
            'previousQueryIntent': {'metric': '净利润', 'population': '久其集团', 'period': '2026年'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['population']},
            'inheritedScope': {'captureRefs': ['prior'], 'selections': []}})
        origin = {'originalToolName': 'execute_budget_query'}
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, origin, {'purpose': 'QUERY'})
            self.assertIn('预算', result['contextText'])
            self.assertNotIn('久其集团', result['contextText'])
            turn.state['followupContext']['changedFields'] = ['scenario', 'population']
            self.assertNotIn('预算', prepare_request(turn, origin, {'purpose': 'QUERY'})['contextText'])
            turn.state['followupContext']['changedFields'] = ['metric', 'population']
            result = prepare_request(turn, origin, {'purpose': 'QUERY'})
            self.assertIn('预算', result['contextText'])
            self.assertNotIn('净利润', result['contextText'])
            turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['population'],
                                             'changedDimensions': ['情景']}
            self.assertNotIn('预算', prepare_request(turn, origin, {'purpose': 'QUERY'})['contextText'])
            turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['population']}
            capture['request']['question'] += '，对比实际'
            capture['request']['intentDraft']['targets'].append({'businessPhrases': [
                {'text': '净利润', 'sourceText': '净利润'}, {'text': '实际', 'sourceText': '实际'}]})
            result = prepare_request(turn, origin, {'purpose': 'QUERY'})
            self.assertNotIn('预算', result['contextText'])
            self.assertNotIn('实际', result['contextText'])
            capture['request']['intentDraft']['targets'].pop()
            receipt['status'] = 'FAILED'
            self.assertNotIn('预算', prepare_request(turn, origin, {'purpose': 'QUERY'})['contextText'])
            receipt['status'] = 'SUCCEEDED'
            turn.state['followupContext'] = {'mode': 'NEW'}
            self.assertNotIn('contextText', prepare_request(turn, origin, {'purpose': 'QUERY'}))
        turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['population']}
        with patch('analysis_extension.followup_scope._same_source', return_value=False):
            self.assertNotIn('预算', prepare_request(turn, origin, {'purpose': 'QUERY'})['contextText'])

    def test_explicit_scenario_switch_uses_resolved_words_even_when_previous_runtime_returned_no_facts(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from analysis_extension.followup_scope import prepare_request
        capture = {'originalToolName': 'execute_budget_query', 'response': {}, 'request': {
            'question': '2026年示例集团净利润预算', 'intentDraft': {
                'sharedConditions': [{'text': '示例集团', 'sourceText': '示例集团'}, {'text': '预算', 'sourceText': '预算'}],
                'targets': [{'id': 'profit', 'periodText': '2026年', 'businessPhrases': [{'text': '净利润', 'sourceText': '净利润'}]}]}}}
        receipt = {'status': 'PARTIAL', 'data': {'resultSets': [], 'resolution': {'exactRecallComplete': True},
            'targets': [{'allOf': [{'term': term, 'anyOf': [{'baseDataCode': domain}]}
                        for term, domain in [('示例集团', 'ORG_DEF'), ('净利润', 'ACCOUNT_DEF'), ('预算', 'SCENE_DEF')]]}],
            'dimensionCatalog': [{'valueDomain': domain, 'dimCode': code} for domain, code in
                [('ORG_DEF', 'MD_ORG'), ('ACCOUNT_DEF', 'ACCOUNT'), ('SCENE_DEF', 'MD_SCENE')]]}}
        service = SimpleNamespace(owner='owner', store=SimpleNamespace(get_capture=Mock(return_value=capture)),
            sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(raw_receipt=Mock(return_value=receipt)))))
        turn = SimpleNamespace(service=service, state={'request': '改看实际数，其余条件不变',
            'previousQueryIntent': {'metric': '净利润预算', 'population': '示例集团', 'scenario': '预算', 'period': '2026年'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['scenario']}, 'selectedContextRefs': ['ctx'],
            'businessContexts': [{'contextRef': 'ctx', 'captureRefs': ['prior']}], 'inheritedScope': {}})
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            for status in ('PARTIAL', 'FAILED'):
                receipt['status'] = status
                result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, {'purpose': 'QUERY'})
                self.assertIn('净利润', result['contextText'])
                self.assertIn('示例集团', result['contextText'])
                self.assertNotIn('预算', result['contextText'])
                self.assertNotIn('selections', result)

    def test_historical_sentence_quotes_are_reduced_only_to_retained_confirmed_terms(self):
        from analysis_extension.followup_scope import _inherit_phrase_quotes
        quote = '请查示例企业2027年的运输收入预算'
        phrases = [{'text': word, 'sourceText': quote} for word in ('运输收入', '预算', '2027年')]
        request = {'intentDraft': {'sharedConditions': [], 'targets': [{'businessPhrases': phrases}]}}
        terms = ['运输收入', '实际数', '2026年']
        _inherit_phrase_quotes(request, [{'question': quote}], terms)
        self.assertEqual(phrases[0]['sourceText'], '运输收入')
        self.assertEqual(phrases[1]['sourceText'], quote)
        self.assertEqual(phrases[2]['sourceText'], quote)
        self.assertNotIn(quote, terms)
        self.assertNotIn('预算', terms)
        self.assertNotIn('2027年', terms)

        # A fragment of a confirmed member name, or a fabricated source sentence,
        # cannot acquire evidence merely because its characters look related.
        for word, source in [('收入', quote), ('运输收入', '此前已确认运输收入')]:
            phrase = {'text': word, 'sourceText': source}
            result = {'intentDraft': {'sharedConditions': [phrase]}}
            original = deepcopy(result)
            _inherit_phrase_quotes(result, [{'question': quote}], ['运输收入'])
            self.assertEqual(result, original)

    def test_unknown_physical_state_is_displayed_as_waiting_not_failure(self):
        from agentscope.app._service._progress_context import public_observations
        receipt = {'status': 'FAILED', 'executionState': 'UNKNOWN', 'data': {},
                   'error': {'code': 'QUERY_WAIT_TIMEOUT', 'recoveryAction': 'READ_STATUS'}}
        facts = public_observations(receipt, 'capture', 'execute_budget_query', {})['facts']
        self.assertTrue(any(fact['kind'] == 'waiting' for fact in facts))
        self.assertFalse(any(fact['kind'] == 'failure' for fact in facts))
    def test_followup_identity_ignores_labels_ids_and_condition_order(self):
        from analysis_extension.followup_scope import _business_targets_key
        capture, receipt = business_fixture()
        original = receipt['data']['targets']
        changed = deepcopy(original)[::-1]
        for target in changed:
            target['id'] += '-new'
            target['label'] = '新的表达'
            for condition in target['allOf']:
                condition['term'] = '同义说法'
        self.assertEqual(_business_targets_key(original), _business_targets_key(changed))
        changed[0]['allOf'][0]['anyOf'][0]['objectCode'] = 'different'
        self.assertNotEqual(_business_targets_key(original), _business_targets_key(changed))

    def test_server_plan_and_native_values_are_validated_without_ai_dsl(self):
        capture, receipt = business_fixture()
        self.assertEqual(len(validate(capture, receipt)), 1)
        self.assertEqual(_step_request(capture, 'batch')[0]['operation'], 'BATCH_DATA_QUERY')
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        self.assertEqual(validate_batch(capture, receipt, 'batch', table)['rowCount'], 2)
        self.assertIsNotNone(receipt_from_capture(capture))

    def test_named_form_scope_uses_canonical_names_and_native_codes(self):
        capture, receipt = business_fixture()
        capture['request']['scope']['formNames'] = ['利润表']
        validate(capture, receipt)
        receipt['data']['resultSets'][0]['context']['formName'] = '资产负债表'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_value_period_member_and_task_drift_are_rejected(self):
        for change in ('value', 'period', 'member', 'task', 'intent'):
            capture, receipt = business_fixture()
            result = receipt['data']['resultSets'][0]
            if change == 'value': result['rows'][0]['value'] = '999'
            if change == 'period': result['rows'][0]['dataPeriod'] = '2025N0001'
            if change == 'member': result['rows'][0]['dimensions']['MD_BUDACCOUNT'] = 'OTHER'
            if change == 'task': result['context']['target']['taskCode'] = 'OTHER'
            if change == 'intent': receipt['data']['targets'][0]['allOf'][0]['anyOf'][0]['objectCode'] = 'OTHER'
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                validate(capture, receipt)

    def test_preview_preserves_nulls_and_separates_display_from_execution_coverage(self):
        capture, receipt = business_fixture()
        before = deepcopy(receipt)
        view = compact_receipt(receipt, 'ref', len(encode(receipt)))
        self.assertEqual(view['viewType'], 'business_tables')
        self.assertTrue(view['complete'])
        self.assertTrue(view['allRowsIncluded'])
        self.assertEqual(view['tables'][0]['factCount'], 2)
        self.assertIn('2026年', view['markdown'])
        self.assertIn('空值', view['markdown'])
        self.assertNotIn('"rows":', view['markdown'])
        self.assertEqual(receipt, before)
        page = read_receipt(receipt, 'ref', '/data/resultSets/0/rows', limit=1)
        self.assertEqual(page['resultSetContext']['context']['target']['taskCode'], 'TASK')
        self.assertTrue(page['sourceState']['budgetQuery']['complete'])

    def test_full_form_allows_prior_columns_but_requires_a_named_form(self):
        capture, receipt = business_fixture()
        capture['request'].update(selection='FULL_FORM', targets=[])
        receipt['data'].update(selection='FULL_FORM', targets=[])
        for result in receipt['data']['resultSets']:
            result['resolvedTerms'] = []
            result['rows'][0]['dataPeriod'] = '2025N0001'
        validate(capture, receipt)
        capture['request']['scope'].pop('formCodes')
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_unresolved_groups_cannot_be_declared_complete(self):
        capture, receipt = business_fixture()
        receipt['data']['unresolvedGroups'] = [{'code': 'SOURCE_UNAVAILABLE'}]
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        receipt['data']['complete'] = False
        validate(capture, receipt)

    def test_bound_full_form_is_readable_and_enforces_verified_task_and_form(self):
        from analysis_extension.adapters.budget_v2 import catalog
        capture, receipt = business_fixture()
        capture['request'].update(selection='FULL_FORM', targets=[])
        receipt['data'].update(selection='FULL_FORM', targets=[])
        proofs = [{'binding': {'text': text, 'modelCode': model, 'executionId': 'search-' + code,
                              'candidateId': code, 'contextId': 'context', 'querySessionId': 'session'},
                   'code': code, 'name': name}
                  for model, code, text, name in [('BUDGET_TASK', 'TASK', '年度预算简称', '年度预算'),
                                                   ('BUDGET_FORM_TEMPLATE', 'LRB', '利润表简称', '利润表')]]
        capture['request']['scope'] = {'bindings': [proof['binding'] for proof in proofs],
                                       'formNames': ['利润表简称']}
        receipt['data']['resolution']['scopeBindings'] = proofs
        update(capture, receipt)
        # Catalog/AnalysisPython source loading uses the same receipt validator.
        receipt_from_capture(capture)
        self.assertTrue(catalog({**capture, 'captureRef': 'bound'}))
        for field in ('taskCode', 'formCode'):
            changed = deepcopy(receipt)
            changed['data']['resultSets'][0]['context']['target'][field] = 'OTHER'
            with self.assertRaises(AnalysisError):
                validate(capture, changed)
        for change in ('missing', 'wrong-proof', 'wrong-code'):
            changed = deepcopy(receipt)
            if change == 'missing':
                changed['data']['resolution'].pop('scopeBindings')
            elif change == 'wrong-proof':
                changed['data']['resolution']['scopeBindings'][1]['binding']['candidateId'] = 'other'
            else:
                changed['data']['resolution']['scopeBindings'][1]['code'] = 'OTHER'
            with self.assertRaises(AnalysisError):
                validate(capture, changed)

    def test_full_form_catalog_keeps_data_formulas_and_nulls_without_header_facts(self):
        from analysis_extension.adapters.budget_v2 import catalog
        capture, receipt = business_fixture()
        context = receipt['data']['resultSets'][0]['context']
        capture.update(captureRef='ref')
        capture['request'].update(selection='FULL_FORM', targets=[])
        cells = [
            {'key': '0:1', 'row': 0, 'column': 1, 'rawValueJson': '1', 'mapType': 'COL_INDEX'},
            {'key': '1:1', 'row': 1, 'column': 1, 'rawValueJson': '"标题"', 'mapType': 'DR'},
            {'key': '2:1', 'row': 2, 'column': 1, 'rawValueJson': '"0.00"', 'mapType': 'ZB', 'headers': ['金额']},
            {'key': '3:1', 'row': 3, 'column': 1, 'rawValueJson': 'null', 'mapType': 'ZB'},
            {'key': '4:1', 'row': 4, 'column': 1, 'rawValueJson': '"5"', 'formula': '=SUM(A2:A3)'},
        ]
        receipt['data'].update(selection='FULL_FORM', targets=[], resultSets=[], origins=[],
            runtimeRequests=[{'id': 'full', 'operation': 'FORM_VIEW_QUERY'}],
            stepResults=[{'stepId': 'full', 'operation': 'FORM_VIEW_QUERY', 'formResults': [{
                'runtime': {'context': context, 'observation': {'observationId': 'o'}},
                'cells': cells}]}])
        update(capture, receipt)
        facts = catalog(capture)
        self.assertEqual([fact['cellKey'] for fact in facts], ['2:1', '3:1', '4:1'])
        self.assertEqual([fact['value'] for fact in facts], ['0', None, '5'])
        self.assertEqual(facts[0]['headers'], ['金额'])
        self.assertEqual(facts[2]['formula'], '=SUM(A2:A3)')

    def test_whole_form_review_uses_lossless_dictionary_encoding_before_capacity_failure(self):
        from analysis_extension.turn_completion import query_review_payloads, _wire_review_json
        from analysis_extension.review_encoding import unpack_review_records
        from analysis_extension.review_revision import compact_query_facts
        facts = [{'factId': f'f-{index}', 'value': None if index % 7 == 0 else str(index),
                  'valueState': 'NO_VALUE' if index % 7 == 0 else 'PRESENT',
                  'headers': ['营业收入' if index % 2 else '净利润', '本年预算数'],
                  'dimensions': {'DATATIME': '2026N0001', 'MD_BUDACCOUNT': str(index % 4),
                                 'MD_ORG': 'GT00', 'MD_STAGE': 'FINAL'},
                  'contextRef': f'task-{index // 100}'} for index in range(1000)]
        evidence = {'sources': [{'facts': facts, 'entityRows': []}],
                    'entityDefinitions': {}, 'fieldDefinitions': {}, 'identityPrefixes': {}}
        envelope = {'candidateAnswer': '不同任务的利润表分别列示。'}
        raw_size = len(_wire_review_json({**envelope, 'evidence': evidence}))
        early_array_size = len(_wire_review_json({**envelope, 'evidence': compact_query_facts(evidence)}))
        self.assertLess(raw_size + 100, early_array_size)
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', raw_size + 100):
            parts = query_review_payloads(envelope, evidence)
        self.assertEqual(len(parts), 1)
        self.assertEqual(unpack_review_records(json.loads(parts[0]))['evidence'], evidence)
