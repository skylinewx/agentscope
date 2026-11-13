"""Committed source evidence across grouping follow-ups, failures and explicit changes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from analysis_extension.conversation_context import save_context, next_context_state, select_context
from analysis_extension.followup_scope import prepare_request
from analysis_extension.language_evidence import bind
from analysis_extension.models import AnalysisError
from analysis_extension.query_attempts import prepare, logical_request


class SourceEvidenceTest(TestCase):
    def fixture(self):
        quote = '从利润表查询久其集团二级单位的26年净利润预算，并按从大到小排'
        scope = {'id': 'org', 'dimensionHint': '组织机构', 'sourceText': quote,
                 'selector': {'type': 'HIERARCHY', 'rootText': '久其集团', 'level': 2}}
        record = {'draft': scope, 'appliesToAllTargets': True, 'captureRef': 'previous',
                  'confirmedSource': {'executionId': 'execution', 'scopeId': 'org', 'contextId': 'original-context', 'querySessionId': 'original-session'}}
        capture = {'originalToolName': 'execute_budget_query', 'response': {}, 'request': {
            'question': quote, 'scope': {'formNames': ['利润表'], 'periodCode': '2026N0001'},
            'intentDraft': {'dimensionScopes': [scope], 'sharedConditions': [], 'targets': [
                {'id': 't', 'periodText': '26年', 'businessPhrases': [
                    {'text': '净利润', 'sourceText': '净利润'}, {'text': '预算', 'sourceText': '预算'}]}]}}}
        receipt = {'status': 'SUCCEEDED', 'data': {'resultSets': [{'resolvedTerms': [
            {'term': '净利润', 'dimensionCode': 'MD_BUDACCOUNT'}, {'term': '预算', 'dimensionCode': 'MD_SCENE'}]}]}}
        service = SimpleNamespace(owner='owner', store=SimpleNamespace(get_capture=Mock(return_value=capture)),
            sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(raw_receipt=Mock(return_value=receipt)))))
        state = {'turnKey': 'group', 'request': '按板块口径分析一下二级单位的数据',
                 'followupContext': {'mode': 'CONTINUE', 'changedFields': ['grouping']},
                 'inheritedScope': {'captureRefs': ['previous'], 'selections': []},
                 'inheritedDimensionScopes': [record],
                 'previousQueryIntent': {'metric': '净利润', 'scenario': '预算', 'period': '26年', 'population': '久其集团二级单位'}}
        request = {'purpose': 'QUERY', 'groupBy': ['板块'], 'intentDraft': {'sharedConditions': [],
                   'targets': [{'id': 't', 'periodText': '26年', 'businessPhrases': [{'text': '净利润'}, {'text': '预算'}]}]}}
        return SimpleNamespace(state=state, service=service), capture, request

    def prepare(self, turn, capture, request):
        with patch('analysis_extension.followup_scope._same_source', return_value=True), patch(
                'analysis_extension.followup_scope._inherited_tables', return_value=[(None, capture, None, None)]):
            return prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)

    def test_grouping_inherits_form_and_scope_without_repeating_historical_sentence(self):
        turn, capture, request = self.fixture()
        result = self.prepare(turn, capture, request)
        inherited = result['intentDraft']['dimensionScopes'][0]
        self.assertEqual(inherited['sourceText'], capture['request']['question'])
        self.assertEqual(inherited['confirmedSource'], {'executionId': 'execution', 'scopeId': 'org',
                                                      'contextId': 'original-context', 'querySessionId': 'original-session'})
        self.assertEqual(result['scope']['formNames'], ['利润表'])
        for word in ['利润表', '久其集团', '净利润', '预算', '26年']:
            self.assertIn(word, result['contextText'])
        self.assertNotIn(capture['request']['question'], result['contextText'])
        self.assertEqual(result['question'], turn.state['request'])
        self.assertTrue(any(r['source'] == 'CONFIRMED_EXECUTION' for rows in turn.state['businessEvidence'].values() for r in rows))

    def test_member_reuse_releases_scope_bound_shared_names_including_after_facts(self):
        for dimension, root in [('MD_ORG', '久其集团'), ('MD_PROJECT', '示例项目'), ('CUSTOM_DIM', '示例分类')]:
            for archived in (False, True):
                with self.subTest(dimension=dimension, archived=archived):
                    turn, capture, _ = self.fixture()
                    turn.state['request'] = '按分类分析这些数据'
                    turn.state['previousQueryIntent'].pop('population', None)
                    capture['response'] = {'fixture': 'native-receipt'}
                    prior = capture['request']
                    prior['question'] = root + '26年净利润预算'
                    prior['intentDraft']['sharedConditions'] = [{'text': root, 'sourceText': root}]
                    prior['intentDraft']['dimensionScopes'][0]['selector']['rootText'] = root
                    if dimension == 'CUSTOM_DIM':
                        prior['intentDraft']['dimensionScopes'][0]['selector'] = {
                            'type': 'FILTER', 'where': {'fieldHint': '分类', 'op': 'EQ', 'value': root}}
                    receipt = turn.service.sources.for_capture.return_value.raw_receipt.return_value
                    receipt['data']['resultSets'][0]['targetId'] = 't'
                    receipt['data']['scopeResolution'] = [{'id': 'org', 'status': 'RESOLVED', 'dimCode': dimension}]
                    if archived:
                        receipt['data']['sourceBasis'] = {'operation': 'IMMUTABLE_FACT_VIEW', 'originalRequest': deepcopy(prior)}
                        capture['request'] = {'question': '筛选原值', 'basis': {'kind': 'FACTS'},
                                              'intentDraft': {'targets': [], 'sharedConditions': []}}
                    result = self.prepare(turn, capture, {'purpose': 'QUERY', 'groupBy': ['分类'],
                        'basis': {'kind': 'MEMBERS', 'memberDimensions': [dimension]}})
                    self.assertNotIn(root, result.get('contextText', ''))
                    self.assertEqual(result['intentDraft']['sharedConditions'], [])
                    self.assertEqual([p['text'] for p in result['intentDraft']['targets'][0]['businessPhrases']], ['净利润', '预算'])
                    self.assertFalse(result['intentDraft'].get('dimensionScopes'))

    def test_expanding_to_all_releases_old_root_instead_of_forcing_hierarchy(self):
        turn, capture, _ = self.fixture()
        turn.state['request'] = '查询所有单位的净利润预算'
        turn.state['inheritedDimensionScopes'] = []  # A selected archive need not have a session scope index.
        capture['response'] = {'fixture': 'receipt'}
        capture['request']['intentDraft']['sharedConditions'] = [{'text': '久其集团', 'sourceText': '久其集团'}]
        receipt = turn.service.sources.for_capture.return_value.raw_receipt.return_value
        receipt['data']['resultSets'][0]['targetId'] = 't'
        receipt['data']['scopeResolution'] = [{'id': 'org', 'status': 'RESOLVED', 'dimCode': 'MD_ORG'}]
        result = self.prepare(turn, capture, {'purpose': 'QUERY', 'intentDraft': {
            'dimensionScopes': [{'id': 'all', 'dimensionHint': '组织机构', 'selector': {'type': 'ALL'}}]}})
        self.assertNotIn('久其集团', result.get('contextText', ''))
        self.assertEqual(result['intentDraft']['sharedConditions'], [])
        self.assertEqual(result['intentDraft']['dimensionScopes'][0]['selector'], {'type': 'ALL'})

    def test_history_quote_cannot_restore_replaced_period_metric_or_form(self):
        turn, capture, request = self.fixture()
        turn.state['request'] = '改看二级单位25年营业收入预算'
        turn.state['followupContext']['changedFields'] = ['period', 'metric']
        request['intentDraft']['targets'][0].update(periodText='25年', businessPhrases=[{'text': '营业收入'}, {'text': '预算'}])
        result = self.prepare(turn, capture, request)
        for old in ['26年', '净利润', '利润表']:
            self.assertNotIn(old, result.get('contextText', ''))
        self.assertNotIn('formNames', result.get('scope', {}))
        self.assertNotIn('periodCode', result.get('scope', {}))
        self.assertIn('久其集团', result['contextText'])
        self.assertEqual(result['intentDraft']['dimensionScopes'][0]['sourceText'], capture['request']['question'])

    def test_native_scope_proof_retains_explicit_root_without_session_scope_index(self):
        turn, capture, _ = self.fixture()
        turn.state['inheritedDimensionScopes'] = []
        turn.state['previousQueryIntent'].pop('population')
        capture['response'] = {'fixture': 'receipt'}
        receipt = turn.service.sources.for_capture.return_value.raw_receipt.return_value
        receipt['data']['resultSets'][0]['targetId'] = 't'
        receipt['data']['scopeResolution'] = [{'id': 'org', 'status': 'RESOLVED', 'dimCode': 'MD_ORG'}]
        request = {'purpose': 'QUERY', 'intentDraft': {'dimensionScopes': [
            {'id': 'continued', 'dimensionHint': '组织机构',
             'selector': {'type': 'HIERARCHY', 'rootText': '久其集团', 'level': 2}}]}}
        result = self.prepare(turn, capture, request)
        self.assertIn('久其集团', result['contextText'])
        request['intentDraft']['dimensionScopes'][0]['selector']['rootText'] = '无关集团'
        with self.assertRaises(AnalysisError) as invalid:
            self.prepare(turn, capture, request)
        self.assertEqual(invalid.exception.code, 'QUERY_DRAFT_EVIDENCE_INVALID')

    def test_explicit_scope_codes_keep_confirmed_evidence_without_inventing_labels(self):
        turn, capture, request = self.fixture()
        capture['request']['scope'] = {'formCodes': ['LRB'], 'taskNames': ['年度二上']}
        capture['request']['question'] += '，年度二上'
        result = self.prepare(turn, capture, request)
        self.assertEqual(result['scope']['formCodes'], ['LRB'])
        self.assertIn('LRB', result['contextText'])
        self.assertIn('年度二上', result['contextText'])

    def test_inherited_reference_and_quote_cannot_be_fabricated_by_model(self):
        turn, _, _ = self.fixture()
        record = turn.state['inheritedDimensionScopes'][0]
        inherited = {**deepcopy(record['draft']), 'confirmedSource': record['confirmedSource']}
        for field, value in [('sourceText', '自行改写的原话'), ('confirmedSource', {'executionId': 'foreign', 'scopeId': 'org'})]:
            item = deepcopy(inherited)
            item[field] = value
            with self.assertRaises(AnalysisError):
                bind(turn, {'question': turn.state['request'], 'intentDraft': {'dimensionScopes': [item]}})

    def test_pending_context_preserves_confirmed_scope_and_recovers_legacy_snapshot(self):
        turn, _, _ = self.fixture()
        record = turn.state['inheritedDimensionScopes'][0]
        base = {'contextRef': 'base', 'kind': 'DELIVERED', 'status': 'DIRECT', 'intent': turn.state['previousQueryIntent'],
                'dimensionScopes': [record], 'subjectScope': turn.state['inheritedScope'], 'captureRefs': ['previous']}
        turn.state.update(mode='ANALYSIS', queryIntent={**base['intent'], 'grouping': '板块'},
                          businessContexts=[base], selectedContextRefs=['base'], activeBusinessContextRef='base')
        save_context(turn.state, 'INCOMPLETE')
        pending = turn.state['businessContexts'][-1]
        self.assertEqual(pending['dimensionScopes'], [record])
        self.assertEqual(pending['kind'], 'PENDING')
        for legacy in [False, True]:
            state = next_context_state(turn.state)
            chosen = next(item for item in state['businessContexts'] if item['contextRef'] == pending['contextRef'])
            if legacy:
                chosen['dimensionScopes'] = []
            state['request'] = '继续按板块口径分析二级单位'
            select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE', 'contextRefs': [pending['contextRef']], 'userQuote': state['request']})
            self.assertEqual(state['inheritedDimensionScopes'], [record])

    def test_changed_dimension_is_not_resurrected_after_pending_task_resume(self):
        turn, _, _ = self.fixture()
        record = turn.state['inheritedDimensionScopes'][0]
        base = {'contextRef': 'base', 'kind': 'DELIVERED', 'status': 'DIRECT', 'intent': turn.state['previousQueryIntent'],
                'dimensionScopes': [record], 'subjectScope': turn.state['inheritedScope']}
        turn.state.update(mode='ANALYSIS', queryIntent={'population': '其他集团'}, businessContexts=[base],
                          selectedContextRefs=['base'], activeBusinessContextRef='base')
        turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedDimensions': ['MD_ORG']}
        save_context(turn.state, 'INCOMPLETE')
        pending = turn.state['businessContexts'][-1]
        self.assertEqual(pending['dimensionScopes'], [])
        state = next_context_state(turn.state)
        state['request'] = '继续刚才的查询'
        select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE', 'contextRefs': [pending['contextRef']], 'userQuote': state['request']})
        self.assertEqual(state['inheritedDimensionScopes'], [])
        self.assertEqual(state['basePendingDimensions'], ['MD_ORG'])

    def test_identical_nonretryable_failure_stops_but_unknown_execution_keeps_identity(self):
        owner = SimpleNamespace(key='owner')
        origin = {'originalToolName': 'execute_budget_query'}
        request = {'requestId': 'model', 'question': '分析二级单位', 'intentDraft': {}}
        key = logical_request(owner, 'turn', origin, request)
        progress = {'queryAttempts': {key: {'attempt': 0, 'status': 'FAILED', 'retryAllowed': False,
                    'executionId': 'failed', 'errorCode': 'QUERY_HIERARCHY_REQUIRED', 'recoveryAction': 'CORRECT_REQUEST'}}}
        store = SimpleNamespace(turn_progress=lambda owner, turn, action: action(progress), limits=SimpleNamespace(max_query_retries=2))
        with self.assertRaises(AnalysisError) as raised:
            prepare(store, owner, 'turn', origin, request)
        self.assertEqual(raised.exception.code, 'QUERY_EQUIVALENT_FAILURE')
        progress['queryAttempts'][key].update(status='IN_PROGRESS', recoveryAction='READ_STATUS')
        first, _ = prepare(store, owner, 'turn', origin, request)
        second, _ = prepare(store, owner, 'turn', origin, request)
        self.assertEqual(first['requestId'], second['requestId'])
