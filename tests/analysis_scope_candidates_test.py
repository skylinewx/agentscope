from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock
from analysis_extension.models import AnalysisError
from analysis_extension.scope_candidates import candidates, bind, inherit
from analysis_extension.capture import ReceiptCapture
from analysis_extension.business_tables import model_text


class ExpandedTargetSearchTest(IsolatedAsyncioTestCase):
    async def test_searches_only_the_unlocated_quote_once_and_returns_real_choices(self):
        from analysis_extension.query_recovery import expand_targets
        query = {'contractVersion': '2.0', 'contextId': 'ctx', 'querySessionId': 'session',
                 'intentDraft': {'targets': [{'id': 'located', 'businessPhrases': [{'text': '收入'}]},
                                            {'id': 'missing', 'businessPhrases': [{'text': '保障预算'}]}]}}
        receipt = {'status': 'PARTIAL', 'data': {'unresolvedGroups': [{'code': 'TARGET_DISCOVERY_UNRESOLVED',
                   'diagnostics': {'logicalTargetId': 'missing', 'unresolvedTitleTerms': ['保障']}}]}}
        source = {'request': query, 'response': receipt, 'originalToolName': 'execute_budget_query'}
        search = {'originalToolName': 'search_entities', 'captureRef': 'cap_abc', 'request': {'entityMention': '保障'},
                  'response': {'status': 'SUCCEEDED', 'data': {'candidates': [
                      {'modelCode': 'BUDGET_FORM_TEMPLATE', 'kind': 'RESOURCE', 'key': code, 'label': code,
                       'matchedBy': ['RESOURCE_RDF_VERIFIED']} for code in ('A', 'B')]}}}
        records = {'one': source, 'two': source, 'cap_abc': search,
                   'empty': dict(source, response={'status': 'SUCCEEDED', 'data': {'resultSets': []}})}
        store = Mock()
        store.get_capture.side_effect = lambda owner, ref: records[ref]
        sources = Mock()
        sources.for_capture.return_value.raw_receipt.side_effect = lambda response: response
        turn = SimpleNamespace(service=SimpleNamespace(store=store, owner='user', sources=sources))
        runner = AsyncMock(return_value=(['cap_abc'], None))
        text = await expand_targets(turn, ['empty', 'one', 'two'], runner)
        runner.assert_awaited_once_with(turn, source, 'search_entities', {
            'contractVersion': '2.0', 'contextId': 'ctx', 'querySessionId': 'session', 'entityMention': '保障'})
        self.assertIn('scand:cap_abc:0', text)
        self.assertIn('scand:cap_abc:1', text)
        self.assertNotIn('scope', query)



class ScopedRecoveryTests(TestCase):
    def test_current_org_correction_keeps_only_the_executed_branch_and_its_filter(self):
        from analysis_extension.scope_candidates import _correct_recovery_organization
        previous = {'id':'units','dimensionHint':'MD_ORG','selector':{'type':'HIERARCHY','level':2},'confirmedSource':{'scopeId':'old'}}
        changed = {'id':'new','dimensionHint':'MD_ORG','selector':{'type':'VALUES','members':['示例集团']}}
        branch = {'question':'查询一下集团的预算','intentDraft':{'dimensionScopes':[previous], 'valueFilter':{'op':'LT'}},
                  'groupBy':[{'scopeId':'units'}]}
        proposed = {'intentDraft':{'dimensionScopes':[changed,{'id':'oldProduct','dimensionHint':'产品','selector':{'type':'VALUES','members':['水泥']}}]}}
        _correct_recovery_organization(branch, proposed)
        self.assertEqual(branch['intentDraft']['dimensionScopes'], [{'id':'units','dimensionHint':'MD_ORG',
            'sourceText':branch['question'],'selector':{'type':'VALUES','members':['集团']}}])
        self.assertEqual(branch['intentDraft']['valueFilter'],{'op':'LT'})
        self.assertEqual(branch['groupBy'],[{'scopeId':'units'}])
        self.assertEqual(changed['selector']['members'],['示例集团'])
    def test_explicitly_withdrawn_unverified_role_does_not_override_the_selected_form(self):
        from analysis_extension.scope_candidates import _withdraw_unverified_role
        member = {'dimensionHint': '费用类别', 'id': 'candidate', 'selector': {'type': 'VALUES', 'members': ['保障']}}
        scopes = [dict(member, targetIds=['b']),
                  dict(member, id='verified', targetIds=['b']),
                  dict(member, id='history', targetIds=['b'], confirmedSource={'scopeId': 'old'}),
                  dict(member, id='both'),
                  dict(member, id='compound', targetIds=['b'], selector={'type':'VALUES','members':['保障','其他']}),
                  dict(member, id='org', targetIds=['b'], dimensionHint='组织机构')]
        request = {'question':'查一下保障预算', 'intentDraft':{'targets':[{'id':'a'},{'id':'b'}], 'dimensionScopes':scopes}}
        choices = [{'targetId':'b','text':'保障'}]
        proof = {'scopeResolution':[{'id':'verified','status':'RESOLVED'}]}
        edits = {'scopeEdits':[{'dimensionHint':'费用类别','selector':None}, {'dimensionHint':'组织机构','selector':None}]}
        for state, question in [({}, request['question']), (edits, '按费用类别查询保障预算')]:
            item = deepcopy(request)
            item['question'] = question
            _withdraw_unverified_role(state, item, choices, proof)
            self.assertEqual(item['intentDraft']['dimensionScopes'], scopes)
        _withdraw_unverified_role(edits, request, choices, proof)
        self.assertEqual(request['intentDraft']['dimensionScopes'], scopes[1:])

    def test_shared_and_inherited_search_terms_keep_target_ownership(self):
        from analysis_extension.query_recovery import target_recovery
        req = {'intentDraft': {'sharedConditions': [{'text':'社保'}], 'targets': [
            {'id':'a', 'businessPhrases':[{'text':'管理费'}]},
            {'id':'b', 'inheritFrom':['a:管理费'], 'businessPhrases':[]}]}}
        failure = {'status':'PARTIAL', 'data':{'unresolvedGroups':[{'code':'TARGET_DISCOVERY_UNRESOLVED',
                   'diagnostics':{'logicalTargetId':'b', 'unresolvedTitleTerms':['社保','管理费']}}]}}
        self.assertEqual(target_recovery(failure, req)['targets'], [
            {'logicalTargetId':'b','targetIndex':1,'searchTerms':['社保','管理费']}])
        failure['data']['unresolvedGroups'][0]['diagnostics']['searchTerms'] = ['社保']
        self.assertEqual(target_recovery(failure, req)['sourceTerms'], ['社保'])
        self.assertIsNone(target_recovery({'status':'SUCCEEDED','data':{'resultSets':[]}}, req))

    def test_scoped_candidate_reuses_effective_draft_and_refuses_cross_target_or_turn(self):
        records = {}
        store = Mock()
        store.get_capture.side_effect = lambda owner, ref: records[ref]
        sources = Mock()
        sources.for_capture.return_value.raw_receipt.side_effect = lambda r: r
        turn = SimpleNamespace(state={'request':'比较两个指标', 'conditionTurn':2},
                               service=SimpleNamespace(owner='owner',store=store,sources=sources))
        source = {'serverName':'ontology','resourceId':'budget','connectionFingerprint':'one'}
        draft = {'targets':[{'id':'a','businessPhrases':[{'text':'销售费'}]}, {'id':'b','inheritFrom':['a:销售费']}],
                 'sharedConditions':[{'text':'社保'}], 'valueFilter':{'operator':'GT','value':'0','targetId':'a'}}
        original = {'question':turn.state['request'],'contextId':'ctx','querySessionId':'s','scope':{'periodCode':'2026N0001'},
                    'intentDraft':draft,'conditionContext':{'turn':2}}
        failed = {'status':'PARTIAL','executionId':'failed','context':{'contextId':'ctx'},'querySessionId':'s',
                  'data':{'unresolvedGroups':[{'code':'TARGET_DISCOVERY_UNRESOLVED',
                       'diagnostics':{'logicalTargetId':'b','searchTerms':['社保']}}]}}
        records['cap_aaa'] = dict(source,originalToolName='execute_budget_query',request=original,response=failed)
        search = {'status':'SUCCEEDED','executionId':'search','context':{'contextId':'ctx'},'querySessionId':'s',
                  'data':{'candidates':[{'modelCode':'BUDGET_FORM_TEMPLATE','kind':'RESOURCE','candidateId':'chosen','key':'S','label':'保障明细',
                                        'matchedBy':['RESOURCE_RDF_VERIFIED']}]}}
        records['cap_bbb'] = dict(source,originalToolName='search_entities',captureRef='cap_bbb',request={'entityMention':'社保'},response=search)
        choice = {'text':'社保','candidateRef':'scand:cap_bbb:0:cap_aaa:1'}
        request = {'scope':{'bindings':[choice]},'intentDraft':{'targets':[{'id':'b'}]}}
        bound = bind(turn,source,request)
        self.assertEqual(bound['intentDraft'],draft)
        self.assertEqual(bound['scope']['periodCode'],'2026N0001')
        self.assertEqual(bound['scope']['bindings'][0]['targetId'],'b')
        def with_terms(word):
            return {**request, 'intentDraft': {'targets': [{'id': 'b', 'businessPhrases': [{'text': word}]}]}}
        for word in ('销售费', '社保'):
            self.assertEqual(bind(turn, source, with_terms(word))['intentDraft'], draft)
        predicate = {'operator':'GT','value':'200','targetId':'a'}
        amended = {**request, 'intentDraft': {'valueFilter': predicate,
                   'targets': [{'id':'b','periodText':'2027年'}], 'sharedConditions':[{'text':'两个指标'}]},
                   'scope': {**request['scope'], 'periodCode':'2027N0001'}}
        amended_result = bind(turn, source, amended)
        self.assertEqual(amended_result['intentDraft']['valueFilter'], predicate)
        self.assertEqual(amended_result['scope']['periodCode'], '2027N0001')
        self.assertEqual(amended_result['intentDraft']['targets'][1]['periodText'], '2027年')
        self.assertEqual([p['text'] for p in amended_result['intentDraft']['sharedConditions']], ['社保','两个指标'])
        self.assertEqual(bind(turn, source, request)['intentDraft'], draft)
        with self.assertRaises(AnalysisError) as unknown:
            bind(turn, source, {**request, 'intentDraft': {'valueFilter': {**predicate, 'targetId':'unrelated'}}})
        self.assertEqual(unknown.exception.code, 'INVALID_VALUE_FILTER')
        scoped = {'id':'new','dimensionHint':'类型','selector':{'type':'VALUES','members':['两个指标']}}
        changed_scope = bind(turn, source, {**request,'intentDraft':{'dimensionScopes':[scoped]}})
        self.assertEqual(changed_scope['intentDraft']['dimensionScopes'][0]['sourceText'], turn.state['request'])
        self.assertNotIn('sourceText',scoped)
        paraphrase = bind(turn, source, {**request,'intentDraft':{'sharedConditions':[{'text':'成本预测'}]}})
        self.assertEqual(paraphrase['intentDraft']['sharedConditions'][-1]['sourceText'],turn.state['request'])
        from analysis_extension.scope_candidates import _apply_recovery_conditions
        relaxed = deepcopy(bound)
        relaxed['intentDraft']['sharedConditions'] = []
        with self.assertRaises(AnalysisError) as obsolete:
            _apply_recovery_conditions(relaxed, {'intentDraft':{'sharedConditions':[{'text':'社保'}]}}, {'a':'a','b':'b'}, original)
        self.assertEqual(obsolete.exception.code,'RECOVERY_CONDITION_UNCONFIRMED')
        amended_filter = {'operator':'OR','children':[{'operator':'GT','value':'200','targetId':'a'},
                                                     {'operator':'LT','value':'-1','targetId':'b'}]}
        self.assertEqual(bind(turn, source, {**request,'intentDraft':{'valueFilter':amended_filter}})['intentDraft']['valueFilter'], amended_filter)
        grouped = bind(turn, source, {**request,'groupBy':['区域'],'intentDraft':{'sharedConditions':[{'text':'社保','alternatives':[]}]}})
        self.assertEqual(grouped['groupBy'],['区域'])
        self.assertEqual(grouped['intentDraft']['sharedConditions'][0]['alternatives'],[])
        self.assertNotIn('alternatives',draft['sharedConditions'][0])
        with self.assertRaises(AnalysisError) as ambiguous:
            bind(turn, source, {**request,'intentDraft':{'targets':[{'id':'b','periodText':'2027年'}]}})
        self.assertEqual(ambiguous.exception.code,'RECOVERY_PERIOD_AMBIGUOUS')
        corrected = with_terms('另一指标')
        with self.assertRaises(AnalysisError) as changed:
            bind(turn, source, corrected)
        self.assertEqual(changed.exception.code, 'RECOVERY_TARGET_CHANGED')
        self.assertEqual(changed.exception.details['proposedPhrases'], ['另一指标'])
        corrected.pop('scope')
        self.assertEqual(bind(turn, source, corrected)['intentDraft']['targets'][0]['businessPhrases'], [{'text': '另一指标'}])
        choice['candidateRef'] = 'scand:cap_bbb:0:cap_aaa:0'
        with self.assertRaises(AnalysisError): bind(turn,source,request)
        choice['candidateRef'] = 'scand:cap_bbb:0:cap_aaa:1'
        turn.state['conditionTurn'] = 3
        with self.assertRaises(AnalysisError): bind(turn,source,request)


class ScopeCandidateTest(TestCase):
    def test_binding_preserves_proven_year_wording_but_accepts_a_changed_year_or_period(self):
        from analysis_extension.scope_candidates import _apply_recovery_periods
        annual = {'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'YEAR', 'year': 2026}}}
        for supplied in ({}, annual):
            targets = {'x': {'periodText': '26年'}}
            scope = deepcopy(annual)
            _apply_recovery_periods(targets, {'x': 'x'}, scope, supplied, [{'id': 'x', 'periodText': '2026年'}])
            self.assertEqual((targets['x']['periodText'], scope), ('26年', annual))
        for caption in ('2027年', '2026年3季度累计', '2026年1月'):
            targets = {'x': {'periodText': '26年'}}
            scope = deepcopy(annual)
            _apply_recovery_periods(targets, {'x': 'x'}, scope, {}, [{'id': 'x', 'periodText': caption}])
            self.assertEqual((targets['x']['periodText'], scope), (caption, {}))

    def setUp(self):
        self.origin = dict(resourceId='budget', serverName='ontology', connectionFingerprint='connection')
        self.capture = dict(self.origin, originalToolName='search_entities', captureRef='cap_abc123', request={'entityMention': '年度预算二上'})
        self.receipt = {'status': 'SUCCEEDED', 'executionId': 'execution', 'context': {'contextId': 'context'}, 'querySessionId': 'session',
                        'data': {'candidates': [dict(candidateId='first', modelCode='BUDGET_TASK', kind='RESOURCE', key='A', label='年度预算二上其他版', matchedBy=['RESOURCE_RDF_VERIFIED']),
                                                dict(candidateId='second', modelCode='BUDGET_TASK', kind='RESOURCE', key='B', label='年度预算二上终版', matchedBy=['RESOURCE_RDF_VERIFIED'])]}}
        self.adapter = Mock()
        self.adapter.raw_receipt.return_value = self.receipt
        self.capture['response'] = self.receipt
        self.service = SimpleNamespace(store=Mock(), sources=Mock(), owner='owner')
        self.service.store.get_capture.return_value = self.capture
        self.service.sources.for_capture.return_value = self.adapter
        self.turn = SimpleNamespace(service=self.service)

    def test_selected_reference_is_resolved_by_owner_and_never_defaults_to_first(self):
        options = candidates(self.receipt, self.capture)
        request = {'scope': {'bindings': [{'text': '年度预算二上', 'candidateRef': options[1]['candidateRef']}]}}
        original = deepcopy(request)
        result = bind(self.turn, self.origin, request)
        self.assertEqual(request, original)
        self.assertEqual(result['scope']['bindings'][0], {'text': '年度预算二上', 'modelCode': 'BUDGET_TASK',
            'executionId': 'execution', 'candidateId': 'second', 'contextId': 'context', 'querySessionId': 'session'})
        self.service.store.get_capture.assert_called_once_with('owner', 'cap_abc123')
        with self.assertRaises(AnalysisError):
            bind(self.turn, {**self.origin, 'connectionFingerprint': 'other'}, request)
        self.receipt['data']['candidates'][1]['matchedBy'] = []
        with self.assertRaises(AnalysisError):
            bind(self.turn, self.origin, request)

    def test_candidate_view_and_binding_error_show_the_same_captured_search_word(self):
        from analysis_extension.business_tables import model_text
        options = candidates(self.receipt, self.capture)
        view = model_text({'viewType': 'scope_candidates', 'scopeCandidates': options, 'note': ''})
        self.assertIn('检索原词 "年度预算二上"', view)
        with self.assertRaises(AnalysisError) as mismatch:
            bind(self.turn, self.origin, {'scope': {'bindings': [
                {'text': options[1]['name'], 'candidateRef': options[1]['candidateRef']}]}})
        self.assertEqual(mismatch.exception.code, 'INVALID_SCOPE_CANDIDATE')
        self.assertEqual(mismatch.exception.details['expectedText'], self.capture['request']['entityMention'])

    def test_binding_and_original_quote_inherit_and_clear_by_business_role(self):
        prior = {'bindings': [dict(text='任务原话', modelCode='BUDGET_TASK', candidateId='a'),
                              dict(text='表原话', modelCode='BUDGET_FORM_TEMPLATE', candidateId='b')]}
        inherited = {}
        inherit(inherited, prior, set())
        self.assertEqual(inherited.get('bindings'), prior['bindings'])
        changed = {}
        inherit(changed, prior, {'task'})
        self.assertEqual(changed.get('bindings'), [prior['bindings'][1]])
        changed = {}
        inherit(changed, prior, {'form'})
        self.assertEqual(changed['bindings'], [prior['bindings'][0]])
        target_bound = {**prior['bindings'][1], 'targetId': 'old-target'}
        changed = {}
        inherit(changed, {'bindings': [prior['bindings'][0], target_bound]}, {'metric'})
        self.assertEqual(changed['bindings'], [prior['bindings'][0]])

    def test_small_search_also_exposes_refs_as_markdown(self):
        capture = ReceiptCapture.__new__(ReceiptCapture)
        capture.sources = self.service.sources
        capture.store, capture.owner = self.service.store, self.service.owner
        from unittest.mock import patch
        with patch('analysis_extension.analysis_materials.response_materials', return_value=None):
            view = capture._material_projection(self.adapter, self.receipt, self.capture, 100)
        rendered = model_text(view)
        self.assertIn('| 任务 | 年度预算二上终版 | B | scand:cap_abc123:1 |', rendered)
        self.assertIn('不能默认第一条', rendered)
        self.adapter.compact.assert_not_called()

    def test_followup_inherits_native_proof_and_original_text_before_evidence_validation(self):
        from unittest.mock import patch
        from analysis_extension.followup_scope import _inherit_business_request
        from analysis_extension.language_evidence import bind as bind_evidence
        proof = {'text': '年度预算二上', 'modelCode': 'BUDGET_TASK', 'candidateId': 'second',
                 'executionId': 'search', 'contextId': 'old-context', 'querySessionId': 'old-session'}
        prior = {'question': '查一下年度预算二上中26年水泥的销售预算',
                 'scope': {'bindings': [proof], 'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'YEAR', 'year': 2026}}}}
        capture = dict(self.origin, originalToolName='execute_budget_query', request=prior)
        turn = SimpleNamespace(state={'request': '沿用刚才口径，第三季度金额是多少',
                                      'followupContext': {'mode': 'CONTINUE'}, 'queryIntent': {}, 'previousQueryIntent': {}}, service=self.service)
        def confirmed(turn, origin, changes, released, source_texts, confirmed_requests, proposed_scopes=(), retained_scenarios=None):
            confirmed_requests.append(prior)
            source_texts.append(prior['question'])
            return []
        request = {'purpose': 'QUERY', 'scope': {'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'QUARTER', 'year': 2026, 'period': 3}}}}
        turn.state['historyReferences'] = [{'use': 'REQUERY', 'mention': '沿用刚才口径', 'contextRef': 'original'}]
        with patch('analysis_extension.followup_scope._changes', return_value={'period'}), \
             patch('analysis_extension.followup_scope._selectors', return_value=[]), \
             patch('analysis_extension.followup_scope._confirmed_business_terms', side_effect=confirmed), \
             patch('analysis_extension.followup_scope._inherited_tables', return_value=[(None, capture, None, None)]):
            prepared = _inherit_business_request(turn, self.origin, request)
        bind_evidence(turn, prepared)
        self.assertEqual(prepared['scope']['bindings'], [proof])
        self.assertEqual(prepared['scope']['periodRequest'], request['scope']['periodRequest'])
        self.assertIn('年度预算二上', prepared['contextText'])
        self.assertNotIn('年度预算二上终版', prepared['contextText'])
