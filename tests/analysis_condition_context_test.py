from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase

from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.tool import ToolResponse
from analysis_extension.condition_context import advance, prepare, apply_omissions, effective_request
from analysis_extension.condition_recovery import plans, run
from analysis_extension.models import AnalysisError


def request():
    return {'purpose': 'QUERY', 'question': '查询党建经费预算', 'contextText': '水泥 华东 预算',
            'intentDraft': {'sharedConditions': [{'text': '水泥', 'sourceText': '水泥'},
                                                {'text': '华东', 'sourceText': '华东'},
                                                {'text': '预算', 'sourceText': '预算'}],
                            'targets': [{'id': 'fees', 'businessPhrases': [{'text': '党建经费', 'sourceText': '党建经费'}]}]},
            'conditionContext': {'turn': 4, 'conditions': [
                {'id': 'product', 'term': '水泥', 'scopeId': '', 'dimensionCodes': ['PRODUCT'], 'confirmedTurn': 1, 'source': {}},
                {'id': 'region', 'term': '华东', 'scopeId': '', 'dimensionCodes': ['REGION'], 'confirmedTurn': 1, 'source': {}},
                {'id': 'scenario', 'term': '预算', 'scopeId': '', 'dimensionCodes': ['MD_SCENE'], 'confirmedTurn': 1, 'source': {}}],
                'omitted': []}}


class ConditionContextTests(TestCase):
    def test_members_keep_roles_and_explicit_filters_when_changing_measures(self):
        from analysis_extension.condition_context import previous_target_scopes
        term = {'term': '旧指标', 'dimensionCode': 'ROLE_A', 'baseDataCode': 'SHARED',
                'objectCode': '01', 'label': '原表指标行'}
        def binding(role):
            return {'id': role, 'draft': {'selector': {'type': 'VALUES', 'members': ['旧指标']}},
                    'bindings': [{'dimCode': role, 'valueDomain': 'SHARED', 'condition': {'values': ['01']}}]}
        selected = {'resultSetId': 'selected', 'targetId': 'old', 'resolvedTerms': [term], 'rows': [
            {'factId': 'fact', 'conditionEvidence': [{**term, 'runtimeHeaders': ['原表指标行']}]}]}
        receipt = {'data': {'targets': [{'id': 'old', 'label': '旧指标'}], 'resultSets': [selected],
                            'conditionState': {'entries': [binding('ROLE_A'), binding('ROLE_B')]}}}
        source = {'resultSetId': 'selected', 'factIds': ['fact']}
        draft = {'question': '看这些成员的新指标', 'intentDraft': {'targets': [
            {'businessPhrases': [{'text': '新指标'}]}]}}
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A'])
        self.assertEqual(previous_target_scopes(receipt, {**source, 'factIds': ['other']}, draft), [])
        receipt['data']['scopeResolution'] = [{'status': 'RESOLVED', 'dimCode': 'ROLE_A', 'valueDomain': 'SHARED'}]
        self.assertEqual(previous_target_scopes(receipt, source, draft), [])
        receipt['data'].pop('scopeResolution')
        selected['resolvedTerms'].append({**term, 'dimensionCode': 'ROLE_B'})
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A'])
        selected['rows'][0]['conditionEvidence'].append({**term,'dimensionCode':'ROLE_B','runtimeHeaders':['原表指标行']})
        receipt['data']['targets'][0]['titleTerms'] = ['标题口径']
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A', 'ROLE_B'])
        receipt['data']['scopeResolution'] = [{'status': 'RESOLVED', 'dimCode': 'ROLE_B', 'valueDomain': 'SHARED'}]
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A'])
        mixed = binding('ROLE_A')
        mixed.update(id='both', bindings=mixed['bindings'] + binding('ROLE_B')['bindings'])
        receipt['data']['conditionState']['entries'].append(mixed)
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A'])
        # Alternate form, same metric and phrase, different dimension. This is one native history entry.
        receipt['data'].pop('scopeResolution')
        second = selected['resolvedTerms'].pop()
        receipt['data']['resultSets'].append({'resultSetId':'alternate', 'targetId':'old','rows':[], 'resolvedTerms':[second]})
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A', 'both'])
        receipt['data']['resultSets'][-1]['targetId'] = 'different'
        self.assertEqual(previous_target_scopes(receipt, source, draft), ['ROLE_A'])

    def test_members_replace_only_unused_parts_of_a_compound_metric(self):
        from analysis_extension.condition_context import previous_target_scopes
        terms = [('D_WORK','检修'),('D_COST','设备费'),('D_REGION','北区')]
        members = [dict(term=term,label=term,dimensionCode=dim,baseDataCode=dim,objectCode='01') for dim,term in terms]
        row = {'factId':'fact','dimensions':{dim:'01' for dim,_ in terms},
               'conditionEvidence':[dict(m,verifiedBy='RUNTIME_DIMENSION',runtimeHeaders=['检修','设备费']) for m in members]}
        old = {'intentDraft':{'targets':[{'id':'cost','businessPhrases':[{'text':term} for _,term in terms]}]}}
        receipt = {'data':{'targets':[{'id':'cost:1','label':'检修 设备费 北区','titleTerms':['支出口径']}],
            'resultSets':[{'resultSetId':'old','targetId':'cost:1','resolvedTerms':members,'rows':[row]}],
            'conditionState':{'entries':[{'id':dim,'draft':{'selector':{'type':'VALUES','members':[term]}},
                'bindings':[{'dimCode':dim,'valueDomain':dim,'condition':{'values':['01']}}]} for dim,term in terms]}}}
        def released(phrases):
            req = {'question':'这些站点的情况','intentDraft':{'targets':[{'id':'new','businessPhrases':[{'text':p} for p in phrases]}]}}
            return previous_target_scopes(receipt,{'resultSetId':'old','factIds':['fact']},req,old)
        self.assertEqual(released(['设备费']), ['D_WORK'])
        self.assertEqual(released(['年度产出']), ['D_WORK','D_COST'])
        self.assertEqual(released([term for _,term in terms]), [])

    def test_stage_is_protected_and_account_is_an_ordinary_business_binding(self):
        from analysis_extension.condition_context import system_scope
        req = request()
        for dimension, protected in [('MD_STAGE', True), ('MD_BUDACCOUNT', False)]:
            with self.subTest(dimension=dimension):
                self.assertEqual(protected, system_scope({'bindings': [{'dimCode': dimension}]}))
                item = req['conditionContext']['conditions'][0]
                item['dimensionCodes'] = [dimension]
                req['conditionContext']['omitted'] = [item['id']]
                if protected:
                    with self.assertRaises(AnalysisError):
                        apply_omissions(req)
                else:
                    self.assertNotIn(item, apply_omissions(req)['conditionContext']['conditions'])

    def receipt(self, turn=1):
        rows = []
        for key, dimension, domain, value, caption in [('bound-p','PRODUCT','P','01','产品甲'), ('bound-s','MD_SCENE','S','01','预算')]:
            rows.append({'id': key, 'lastConfirmedTurn': turn, 'dimensionAliases': [dimension], 'appliesToAllTargets': True,
                         'draft': {'id': key, 'dimensionHint': dimension, 'sourceText': caption, 'targetIds': [],
                                   'selector': {'type': 'VALUES', 'members': [caption]}},
                         'bindings': [{'dimCode': dimension, 'valueDomain': domain, 'condition': {'dimCode': dimension, 'values': [value]}}]})
        return {'status': 'SUCCEEDED', 'executionId': 'original', 'context': {'contextId': 'ctx'},
                'querySessionId': 'session', 'data': {'conditionState': {'turn': turn, 'conversationId': 'chat',
                     'entries': rows, 'active': ['bound-p','bound-s']}}}

    def test_history_comes_from_bound_codes_even_without_model_phrases_spans_or_fact_rows(self):
        from analysis_extension.dimension_scopes import remember, inherit
        from unittest.mock import Mock
        receipt = self.receipt()
        service = SimpleNamespace(sources=Mock(), owner=SimpleNamespace(session_id='chat'))
        service.sources.for_capture.return_value.raw_receipt.return_value = receipt
        turn = SimpleNamespace(state={'conditionTurn': 1}, service=service)
        original = {'question': '产品甲销售预算', 'intentDraft': {'targets': []}}
        capture = {'captureRef': 'cap', 'originalToolName': 'execute_budget_query', 'response': {}, 'request': original}
        remember(turn, capture)
        records = turn.state['nativeConditionHistory']['cap']
        self.assertEqual([r['bindings'][0]['condition']['values'] for r in records], [['01'], ['01']])
        self.assertEqual([r['confirmedTurn'] for r in records], [1, 1])
        turn.state.update(conditionTurn=3, inheritedDimensionScopes=records, followupContext={'mode':'NEW'})
        for question in ('查询产品甲预算', '仅查询其他指标', '用别名来查'):
            query = prepare(turn, inherit(turn, {'question': question, 'intentDraft': {}}))
            envelope = query['conditionContext']
            self.assertEqual(envelope['turn'], 3)
            self.assertEqual(envelope['conversationId'], 'chat')
            self.assertEqual([r['id'] for r in envelope['conditions']], ['bound-p', 'bound-s'])
            self.assertTrue(all(set(r) == {'source','scopeId','id'} for r in envelope['conditions']))
            self.assertTrue(all(r['source']['scopeId'] == r['id'] for r in envelope['conditions']))
        import json
        from jsonschema import validate
        from pathlib import Path
        schema = json.loads((Path(__file__).parent / 'fixtures/budget-regression/condition-context-schema.json').read_text('utf-8'))
        validate(envelope, schema)

    def test_reusing_rows_without_new_native_confirmation_cannot_refresh_age(self):
        from analysis_extension.condition_context import native_records
        from analysis_extension.conversation_context import save_context
        receipt = self.receipt()
        records = native_records(receipt, 'cap')
        state = {'turnKey': 'msg4', 'conditionTurn': 4, 'request': '产品甲别名', 'queryIntent': {'metric':'销售预算'},
                 'resolvedSubjectScope': {'captureRefs':['cap']}, 'dimensionScopeCaptures': {'cap':records},
                 'nativeConditionHistory': {'cap':records}}
        save_context(state, 'DIRECT')
        self.assertEqual([r['confirmedTurn'] for r in state['businessContexts'][0]['conditionHistory']], [1,1])

    def test_reused_facts_get_a_context_even_with_no_new_query_or_intent(self):
        from analysis_extension.condition_context import native_records, history_sources
        from analysis_extension.conversation_context import next_context_state, save_context
        records = native_records(self.receipt(3), 'source')
        state = {'turnKey': 'third-message', 'conditionTurn': 3, 'request': '再看前面的结果',
                 'resolvedSubjectScope': {'selections': [{'captureRef':'source'}]},
                 'nativeConditionHistory': {'source':records}, 'dimensionScopeCaptures': {'source':records},
                 'lastCheck': {'status':'DIRECT'}, 'previousQueryIntent': {'metric':'预算'}}
        save_context(state, 'DIRECT')
        following = next_context_state(state)
        self.assertEqual(following['previousQueryIntent'], {'metric':'预算'})
        self.assertEqual(following['conversationConditions'], records)
        earlier = native_records(self.receipt(1), 'old')
        self.assertEqual([r['confirmedTurn'] for r in history_sources(following, earlier).values()], [3,3])
        following['scopeEdits'] = [{'dimensionHint':'PRODUCT','selector':None}]
        self.assertEqual(set(history_sources(following, earlier)), {'bound-s'})

    def test_old_result_cannot_reinstate_a_cleared_condition_as_conversation_history(self):
        from analysis_extension.condition_context import native_records, source_references
        old = native_records(self.receipt(), 'old')
        state = {'conditionTurn': 3, 'conversationConditions': [old[1]]}
        _, refs = source_references(state, old)
        self.assertTrue(refs['bound-p']['sourceOnly'])
        self.assertNotIn('sourceOnly', refs['bound-s'])

    def test_followup_to_selected_facts_uses_the_applied_bindings_and_preserves_full_history(self):
        from analysis_extension.condition_context import native_records
        from analysis_extension.conversation_context import next_context_state
        from analysis_extension.dimension_scopes import inherit
        old = native_records(self.receipt(), 'old')
        narrowed = deepcopy(old[0])
        narrowed['conditionId'] = 'different-bound-role'
        narrowed['confirmedSource']['scopeId'] = narrowed['conditionId']
        state = {'turnKey':'user-2','request':'按另一张表','lastCheck':{'status':'DIRECT'},
                 'resolvedSubjectScope':{'selections':[{'captureRef':'new'}]},
                 'dimensionScopeCaptures':{'new':[narrowed]}, 'nativeConditionHistory':{'new':[old[0],narrowed]}}
        following = next_context_state(state)
        self.assertEqual(following['inheritedDimensionScopes'], [narrowed])
        self.assertEqual(len(following['conversationConditions']), 2)
        following['followupContext']={'mode':'NEW'}
        draft=inherit(SimpleNamespace(state=following), {'intentDraft':{}})['intentDraft']
        self.assertEqual({row['id'] for row in draft['dimensionScopes']}, {old[0]['conditionId'],narrowed['conditionId']})

    def test_local_omission_has_separate_active_and_historical_native_bindings(self):
        from analysis_extension.dimension_scopes import remember
        from unittest.mock import Mock
        receipt = self.receipt()
        receipt['data']['conditionState']['active'] = ['bound-s']
        service = SimpleNamespace(sources=Mock())
        service.sources.for_capture.return_value.raw_receipt.return_value = receipt
        turn = SimpleNamespace(state={}, service=service)
        remember(turn, {'originalToolName':'execute_budget_query','captureRef':'cap','request':{},'response':{}})
        self.assertEqual([r['conditionId'] for r in turn.state['dimensionScopeCaptures']['cap']], ['bound-s'])
        self.assertEqual([r['conditionId'] for r in turn.state['nativeConditionHistory']['cap']], ['bound-p', 'bound-s'])

    def test_native_requested_year_is_inherited_even_if_model_omitted_structured_period(self):
        from unittest.mock import Mock
        from analysis_extension.dimension_scopes import remember
        r = self.receipt()
        period = r['data']['conditionState']['entries'][0]
        period.update(scopeField='period', systemRequest={'scope':{'periodCode':'2026N0001'}, 'periodTexts':['26年']})
        service=SimpleNamespace(sources=Mock())
        service.sources.for_capture.return_value.raw_receipt.return_value=r
        turn=SimpleNamespace(state={},service=service)
        remember(turn,{'originalToolName':'execute_budget_query','captureRef':'src','request':{},'response':{}})
        self.assertEqual(turn.state['systemRequestCaptures']['src'], period['systemRequest'])

    def test_relaxed_review_keeps_system_baseline_and_model_edits_without_business_history(self):
        from analysis_extension.followup_scope import review_context
        from analysis_extension.review_request import make_review_request
        from analysis_extension.turn_completion import semantic_review_key
        organization = {'draft': {'dimensionHint': '组织机构',
                                 'selector': {'type': 'HIERARCHY', 'rootText': '集团', 'level': 2}},
                        'bindings': [{'dimCode': 'MD_ORG'}]}
        product = {'draft': {'dimensionHint': '产品', 'selector': {'type': 'VALUES', 'members': ['水泥']}},
                   'bindings': [{'dimCode': 'CUSTOM_PRODUCT'}]}
        state = {'inheritedDimensionScopes': [organization, product],
                 'scopeEdits': [{'dimensionHint': '组织机构', 'selector': None},
                                {'dimensionHint': '产品', 'selector': None}],
                 'conditionEffectiveScopes': {'cap': [{'formName': '社保表'}]}, 'queryCaptureRefs': ['cap']}
        original = deepcopy(state)
        context = review_context(SimpleNamespace(state=state))
        self.assertEqual(context['inheritedSystemConditions'], [organization['draft']])
        self.assertEqual(context['scopeEdits'], [state['scopeEdits'][0]])
        self.assertEqual(context['effectiveScopes'], [{'formName': '社保表'}])
        material = {'userRequest': '查询社保预算', 'followupScope': context, 'facts': []}
        for query in (False, True):
            markdown = make_review_request(material, query=query).markdown
            self.assertIn('本轮开始时已确认的系统维度范围', markdown)
            self.assertIn('第2层', markdown)
            self.assertIn('模型声明的范围调整', markdown)
            self.assertNotIn('水泥', markdown)
        changed = deepcopy(material)
        changed['followupScope']['inheritedSystemConditions'][0]['selector']['level'] = 3
        self.assertNotEqual(semantic_review_key(material, 'review'), semantic_review_key(changed, 'review'))
        self.assertEqual(state, original)

    def test_older_group_anchor_does_not_hide_the_last_delivered_member_scope_from_model_or_reviewer(self):
        from analysis_extension.condition_context import review_system_scope
        from analysis_extension.review_request import make_review_request
        from analysis_extension.task_projection import render
        def record(selector):
            return {'draft': {'dimensionHint': '组织机构', 'selector': selector}, 'bindings': [{'dimCode': 'MD_ORG'}]}
        parent = record({'type': 'VALUES', 'members': ['示例集团']})
        children = record({'type': 'HIERARCHY', 'rootText': '示例集团', 'level': 2})
        state = {'activeBusinessContextRef': 'latest', 'selectedContextRefs': ['earlier'],
                 'inheritedDimensionScopes': [parent], 'businessContexts': [
                     {'contextRef': 'latest', 'request': '列出二级单位', 'dimensionScopes': [children]}]}
        scope = review_system_scope(state)
        self.assertEqual(scope['latestSourceScope']['conditions'], [children['draft']])
        material = {'userRequest': '查示例集团的新指标', 'followupScope': scope}
        for markdown in [render(material), *(make_review_request(material, query=q).markdown for q in (True, False))]:
            self.assertIn('列出二级单位', markdown)
        self.assertIn('第2层', make_review_request(material, query=True).markdown)
        self.assertNotIn('latestSourceScope', review_system_scope({**state, 'selectedContextRefs': ['latest']}))
        self.assertNotIn('latestSourceScope', review_system_scope({**state, 'inheritedDimensionScopes': [children]}))

    def test_active_native_organization_is_provided_even_after_clearing_the_model_declaration(self):
        from unittest.mock import Mock
        proof = {'scopeId':'org', 'contextId':'c', 'querySessionId':'s', 'executionId':'exec'}
        record = {'conditionId':'org','confirmedSource':proof,'appliesToAllTargets':True,
                  'draft':{'dimensionHint':'组织机构','selector':{'type':'HIERARCHY','level':2,'rootText':'示例集团'}},
                  'bindings':[{'dimCode':'MD_ORG'}]}
        other = {**record,'conditionId':'other','bindings':[{'dimCode':'MD_PRODUCT'}]}
        scoped = {**record,'conditionId':'comparison','appliesToAllTargets':False}
        anchor = {'contextRef':'last','dimensionScopes':[record,other,scoped]}
        turn = SimpleNamespace(state={'conditionTurn':3,'businessContexts':[anchor],'activeBusinessContextRef':'last',
                               'selectedContextRefs':['earlier'],'inheritedDimensionScopes':[]},
                               service=SimpleNamespace(owner=Mock(session_id='chat')))
        envelope = prepare(turn, {'question':'再看收入','intentDraft':{}})['conditionContext']
        self.assertEqual(envelope['organizationDefaults'], ['org'])
        self.assertEqual(envelope['conditions'], [{'id':'org','source':proof}])
        self.assertNotIn('organizationDefaults', prepare(turn, {'basis':{'kind':'MEMBERS'},'intentDraft':{}})['conditionContext'])

    def test_native_organization_correction_is_bounded_to_the_current_question_and_frozen_proofs(self):
        proof = {'scopeId':'org','executionId':'verified'}
        prior = {'id':'saved','dimensionHint':'组织机构','selector':{'type':'HIERARCHY','level':2},
                 'sourceText':'二级单位','confirmedSource':proof}
        req = {'purpose':'QUERY','question':'查示例集团的收入','intentDraft':{'dimensionScopes':[prior]},
               'conditionContext':{'turn':2,'conditions':[{'id':'org','scopeId':'saved','source':proof}],
                                   'organizationDefaults':['org'],'omitted':[]}}
        current = deepcopy(req)
        named = {'id':'saved','dimensionHint':'MD_ORG','selector':{'type':'VALUES','members':['示例集团']},
                 'sourceText':'示例集团','targetIds':[]}
        current['intentDraft']['dimensionScopes'] = [named]
        current['conditionContext']['conditions'][0].pop('scopeId')
        self.assertEqual(effective_request(req, {'effectiveRequest':current}), current)
        named['selector']['members'] = ['另一集团']
        with self.assertRaises(AnalysisError):
            effective_request(req, {'effectiveRequest':current})
        named['selector']['members'] = ['示例集团']
        current['intentDraft']['valueFilter'] = {'gt':500}
        with self.assertRaises(AnalysisError):
            effective_request(req, {'effectiveRequest':current})

    def test_comparison_cannot_replace_a_target_owned_organization_or_adopt_a_peer(self):
        question = '对比甲公司2025年收入和乙公司2026年收入'
        org = {'id': 'org', 'dimensionHint': 'MD_ORG', 'sourceText': '甲公司', 'targetIds': ['current'],
               'selector': {'type': 'VALUES', 'members': ['甲公司']}}
        req = {'purpose': 'QUERY', 'question': question,
               'intentDraft': {'targets': [{'id': 'current'}], 'dimensionScopes': [org]},
               'conditionContext': {'turn': 2, 'conditions': [], 'omitted': [], 'comparisonBatch': {
                   'targetIds': ['current'], 'peers': [{'targetId': 'other', 'terms': ['乙公司'], 'periodText': '2026年'}]}}}
        self.assertEqual(effective_request(req, {'effectiveRequest': deepcopy(req)}), req)
        actual = deepcopy(req)
        actual_org = actual['intentDraft']['dimensionScopes'][0]
        actual_org.update(targetIds=[], sourceText=question)
        actual_org['selector']['members'] = ['甲公司', '乙公司']
        with self.assertRaises(AnalysisError) as error:
            effective_request(req, {'effectiveRequest': actual})
        self.assertEqual(error.exception.code, 'EFFECTIVE_SCOPE_MISMATCH')

        req['intentDraft']['dimensionScopes'] = []
        with self.assertRaises(AnalysisError) as error:
            effective_request(req, {'effectiveRequest': actual})
        self.assertEqual(error.exception.code, 'EFFECTIVE_SCOPE_MISMATCH')
        actual_org['selector']['members'] = ['甲公司']
        self.assertEqual(effective_request(req, {'effectiveRequest': actual}), actual)

    def test_legacy_receipts_preserve_scopes_without_inventing_confirmation_turns(self):
        from analysis_extension.dimension_scopes import remember
        from unittest.mock import Mock
        receipt = self.receipt()
        receipt['data'] = {'dimensionCatalog': [{'dimCode': 'PRODUCT', 'valueDomain': 'P', 'label': '产品'}],
                           'resultSets': [{'resolvedTerms': [{'term': '水泥', 'dimensionCode': 'PRODUCT', 'baseDataCode': 'P'}]}]}
        service = SimpleNamespace(sources=Mock())
        service.sources.for_capture.return_value.raw_receipt.return_value = receipt
        turn = SimpleNamespace(state={}, service=service)
        remember(turn, {'originalToolName':'execute_budget_query','captureRef':'cap','response':{},
                'request':{'question':'查水泥', 'intentDraft':{'sharedConditions':[{'text':'水泥','sourceText':'水泥'}]}}})
        record, = turn.state['dimensionScopeCaptures']['cap']
        self.assertEqual(record['confirmedSource']['sharedTerm'], '水泥')
        self.assertNotIn('confirmedTurn', record)

    def test_age_changes_only_for_new_user_message_and_replay_keeps_number(self):
        first = advance(None, 'message1')
        self.assertEqual(first['conditionTurn'], 1)
        self.assertEqual(advance(first, 'message1'), first)
        second = advance(first, 'message2')
        self.assertEqual(second['conditionTurn'], 2)
        self.assertEqual(advance(second, 'message1')['conditionTurn'], 1)
        self.assertEqual(advance(second, 'message3')['conditionTurn'], 3)

    def test_all_equal_age_combinations_keep_system_conditions_and_current_target(self):
        original = request()
        variants = list(plans(original, {'tier': 1, 'dropAll': [], 'chooseFrom': ['product', 'region'], 'chooseCount': 1}))
        self.assertEqual([v['conditionContext']['omitted'] for v in variants], [['product'], ['region']])
        for variant in variants:
            effective = apply_omissions(variant)
            self.assertIn({'text': '预算', 'sourceText': '预算'}, effective['intentDraft']['sharedConditions'])
            self.assertEqual(effective['intentDraft']['targets'], original['intentDraft']['targets'])
        self.assertEqual(original, request())

    def test_effective_conditions_never_restore_omitted_original(self):
        from analysis_extension.query_lineage import original_request
        original = request()
        original['conditionContext']['omitted'] = ['product']
        actual = apply_omissions(original)
        receipt = {'data': {'effectiveRequest': actual}}
        restored = original_request({'request': original}, receipt)
        self.assertNotIn('水泥', restored['contextText'])
        self.assertEqual(restored['intentDraft']['sharedConditions'], actual['intentDraft']['sharedConditions'])
        self.assertIn('水泥', original['contextText'])

    def test_omission_keeps_a_current_phrase_with_the_same_caption(self):
        original = request()
        original['question'] = '比较水泥销售额'
        target = {'text': '水泥', 'sourceText': '水泥销售额'}
        original['intentDraft']['targets'][0]['businessPhrases'] = [target]
        original['conditionContext']['omitted'] = ['product']
        actual = apply_omissions(original)
        self.assertEqual(actual['intentDraft']['targets'][0]['businessPhrases'], [target])

    def test_in_service_selection_is_verified_against_the_frozen_request(self):
        original = request()
        branch = deepcopy(original)
        branch['conditionContext']['omitted'] = ['product']
        actual = apply_omissions(branch)
        receipt = {'appliedHistoricalConditions': ['product'], 'effectiveRequest': actual}
        self.assertEqual(effective_request(original, receipt), actual)
        for wrong in (['unknown'], ['scenario'], ['region'], ['product', 'product']):
            with self.assertRaises(AnalysisError):
                effective_request(original, {**receipt, 'appliedHistoricalConditions': wrong})
        self.assertEqual(original, request())

    def test_omission_preserves_other_complete_evidence_containing_same_word(self):
        original = request()
        original['contextText'] += ' 白水泥'
        original['intentDraft']['targets'][0]['businessPhrases'].append({'text': '白水泥', 'sourceText': '白水泥'})
        original['conditionContext']['omitted'] = ['product']
        self.assertEqual(apply_omissions(original)['contextText'], '华东 预算 白水泥')

    def test_unauthorized_changes_and_missing_effective_proof_are_rejected(self):
        original = request()
        original['conditionContext']['omitted'] = ['product']
        with self.assertRaisesRegex(AnalysisError, '缺少实际执行条件'):
            effective_request(original, {})
        actual = apply_omissions(original)
        actual['intentDraft']['sharedConditions'] = []
        with self.assertRaisesRegex(AnalysisError, '不一致'):
            effective_request(original, {'effectiveRequest': actual})
        for condition in ['scenario', 'unknown']:
            changed = request()
            changed['conditionContext']['omitted'] = [condition]
            with self.assertRaises(AnalysisError):
                apply_omissions(changed)

    def test_empty_shared_list_preserves_history_until_explicit_clear(self):
        from analysis_extension.query_lineage import restore_shared_conditions
        current = {'question': '继续查询', 'contextText': '预算', 'intentDraft': {'sharedConditions': []}}
        prior = {'intentDraft': {'sharedConditions': [{'text': '预算', 'sourceText': '预算'}]}}
        restore_shared_conditions(current, [prior])
        self.assertEqual(current['intentDraft']['sharedConditions'], prior['intentDraft']['sharedConditions'])
        current['intentDraft']['sharedConditions'] = []
        restore_shared_conditions(current, [prior], {'预算'})
        self.assertEqual(current['intentDraft']['sharedConditions'], [])

    def test_new_topic_keeps_condition_history_without_adopting_previous_facts(self):
        from analysis_extension.dimension_scopes import inherit
        proof = {'executionId': 'x', 'contextId': 'c', 'querySessionId': 's', 'scopeId': 'old'}
        def record(code, label):
            return {'draft': {'id': code, 'dimensionHint': label, 'sourceText': label,
                             'selector': {'type': 'VALUES', 'members': [label]}},
                    'bindings': [{'dimCode': code}], 'confirmedSource': proof, 'appliesToAllTargets': True}
        turn = SimpleNamespace(state={'followupContext': {'mode': 'NEW'},
            'inheritedDimensionScopes': [record('MD_CURRENCY', '人民币'), record('PRODUCT', '水泥')]})
        actual = inherit(turn, {'question': '查询党建经费', 'intentDraft': {}})
        self.assertEqual([s['dimensionHint'] for s in actual['intentDraft']['dimensionScopes']], ['人民币', '水泥'])

    def test_local_omission_does_not_delete_history_or_rewrite_effective_facts(self):
        from analysis_extension.dimension_scopes import condition_history
        def record(code, name, age):
            return {'draft': {'dimensionHint': name, 'selector': {'type': 'VALUES', 'members': [name]}},
                    'bindings': [{'dimCode': code, 'valueDomain': code}], 'confirmedTurn': age}
        water, party = record('PRODUCT', '水泥', 1), record('EXPENSE', '党建经费', 2)
        state = {'conditionTurn': 2, 'inheritedDimensionScopes': [water]}
        actual = [party]
        history = condition_history(state, actual)
        self.assertEqual(history, [water, party])
        self.assertEqual(actual, [party])
        state.update(conditionTurn=3, inheritedDimensionScopes=history)
        newer = record('PRODUCT', '熟料', 3)
        self.assertEqual(condition_history(state, [newer]), [party, newer])
        state['followupContext'] = {'changedDimensions': ['PRODUCT']}
        self.assertEqual(condition_history(state, []), [party])

    def test_changed_metric_releases_its_metadata_dimension_but_keeps_other_history(self):
        from analysis_extension.dimension_scopes import inherit, condition_history, changed_dimensions
        def record(code, label, member):
            return {'draft': {'id': code, 'dimensionHint': label, 'sourceText': member,
                             'selector': {'type': 'VALUES', 'members': [member]}},
                    'bindings': [{'dimCode': code, 'valueDomain': code}], 'appliesToAllTargets': True}
        account = record('CUSTOM_ACCOUNT', '预算科目', '净利润')
        product = record('CUSTOM_PRODUCT', '产品', '水泥')
        state = {'previousQueryIntent': {'metric': '净利润'}, 'inheritedDimensionScopes': [account, product],
                 'followupContext': {'mode': 'CONTINUE', 'changedFields': ['metric']}}
        self.assertIn('custom_account', changed_dimensions(state))
        actual = inherit(SimpleNamespace(state=state), {'question': '查询财务费用', 'intentDraft': {}})
        self.assertEqual([s['dimensionHint'] for s in actual['intentDraft']['dimensionScopes']], ['产品'])
        self.assertEqual(condition_history(state, []), [product])

    def test_new_topic_applies_explicit_system_dimension_and_period_changes(self):
        from analysis_extension.scope_changes import intent_context, reconcile_selectors, reconcile_period, retained_selector_terms
        from analysis_extension.dimension_scopes import inherit
        old = {'draft': {'id': 'version', 'dimensionHint': '管理版本', 'sourceText': '上报版',
                        'selector': {'type': 'VALUES', 'members': ['上报版']}},
               'bindings': [{'dimCode': 'MD_MGRVER'}], 'appliesToAllTargets': True,
               'confirmedSource': {'executionId': 'x', 'contextId': 'c', 'querySessionId': 's', 'scopeId': 'version'}}
        state = {'request': '查询2027年调整版预算', 'previousQueryIntent': {'period': '2026年', 'scenario': '实际'},
                 'followupContext': {'mode': 'NEW'}, 'inheritedDimensionScopes': [old]}
        context = intent_context(state, state['followupContext'], {'scenario': '预算'})
        self.assertEqual(context['changedFields'], ['scenario'])
        state['followupContext'] = context
        request = {'question': state['request'], 'intentDraft': {
            'targets': [{'id': 't', 'periodText': '2027年'}],
            'dimensionScopes': [{'id': 'new-version', 'dimensionHint': '管理版本', 'sourceText': '调整版',
                                 'selector': {'type': 'VALUES', 'members': ['调整版']}}]}}
        turn = SimpleNamespace(state=state)
        reconcile_selectors(turn, request)
        reconcile_period(state, request)
        self.assertEqual(set(state['followupContext']['changedFields']), {'scenario', 'period'})
        self.assertIn('管理版本', state['followupContext']['changedDimensions'])
        actual = inherit(turn, request)
        self.assertEqual(len(actual['intentDraft']['dimensionScopes']), 1)
        self.assertEqual(actual['intentDraft']['dimensionScopes'][0]['selector']['members'], ['调整版'])
        request['intentDraft']['dimensionScopes'][0]['selector']['members'] = ['上报版']
        self.assertEqual(retained_selector_terms(state, request), ['上报版'])


class ConditionRecoveryTests(IsolatedAsyncioTestCase):
    async def exercise(self, results, *, initial_code='DIMENSION_SCOPE_UNRESOLVED'):
        records, requests = {}, []
        descriptor = {'tier': 1, 'dropAll': [], 'chooseFrom': ['product', 'region'], 'chooseCount': 1}
        initial = request()
        adapter = SimpleNamespace(raw_receipt=lambda response: response)
        def event(req, receipt):
            ref = str(len(records))
            records[ref] = {'captureRef': ref, 'request': deepcopy(req), 'response': receipt,
                            'originalToolName': 'execute_budget_query'}
            return ToolResponse(id=ref, state=ToolResultState.SUCCESS, content=[], metadata={'analysisCaptureRef': ref})
        first = event(initial, {'status': 'FAILED', 'error': {'code': initial_code},
                                'data': {'conditionRecovery': descriptor}})
        async def pages(agent, kwargs, handler, *, frozen_request=None):
            if frozen_request is None:
                yield first
            else:
                requests.append(deepcopy(frozen_request))
                yield event(frozen_request, results[len(requests)-1])
        capture = SimpleNamespace(_run_pages=pages, owner='owner',
            store=SimpleNamespace(get_capture=lambda owner, ref: records[ref]),
            sources=SimpleNamespace(for_capture=lambda record: adapter))
        # Combining presentation is independent from scheduling and covered by the capture suites.
        from unittest.mock import patch
        with patch('analysis_extension.query_continuation.combined_response', side_effect=lambda *a, **k: a[2][-1]):
            output = [event async for event in run(capture, None,
                {'tool_call': ToolCallBlock(id='call', name='query', input='{}')}, None)]
        return requests, output

    async def test_first_success_does_not_skip_same_age_peer_and_empty_is_success(self):
        next_tier = {'tier': 2, 'dropAll': [], 'chooseFrom': ['product', 'region'], 'chooseCount': 2}
        requests, output = await self.exercise([
            {'status': 'SUCCEEDED', 'data': {'resultSets': [{'rows': []}]}},
            {'status': 'FAILED', 'error': {'code': 'DIMENSION_SCOPE_UNRESOLVED'}, 'data': {'conditionRecovery': next_tier}}])
        self.assertEqual([r['conditionContext']['omitted'] for r in requests], [['product'], ['region']])
        self.assertTrue(output[0].metadata['conditionExecution']['frontierComplete'])

    async def test_dependency_failure_never_triggers_further_relaxation(self):
        requests, _ = await self.exercise([
            {'status': 'FAILED', 'error': {'code': 'DEPENDENCY_TIMEOUT'}, 'data': {}},
            {'status': 'FAILED', 'error': {'code': 'BUDGET_TARGET_UNRESOLVED'}, 'data': {}}])
        self.assertEqual(len(requests), 2)

    async def test_target_miss_returns_to_candidate_search_before_relaxing_history(self):
        requests, output = await self.exercise([], initial_code='BUDGET_TARGET_UNRESOLVED')
        self.assertEqual(requests, [])
        self.assertNotIn('conditionExecution', output[0].metadata)

    async def test_unlocated_peer_cannot_be_hidden_by_another_scope_recovery_plan(self):
        next_tier = {'tier': 2, 'dropAll': [], 'chooseFrom': ['product', 'region'], 'chooseCount': 2}
        requests, _ = await self.exercise([
            {'status': 'FAILED', 'error': {'code': 'BUDGET_TARGET_UNRESOLVED'}, 'data': {}},
            {'status': 'FAILED', 'error': {'code': 'DIMENSION_SCOPE_UNRESOLVED'}, 'data': {'conditionRecovery': next_tier}}])
        self.assertEqual(len(requests), 2)

    async def test_all_scope_blocked_branches_advance_once_to_next_tier(self):
        failure = {'status': 'FAILED', 'error': {'code': 'DIMENSION_SCOPE_UNRESOLVED'}, 'data': {'conditionRecovery':
            {'tier': 2, 'dropAll': [], 'chooseFrom': ['product', 'region'], 'chooseCount': 2}}}
        requests, _ = await self.exercise([failure, failure, {'status': 'SUCCEEDED', 'data': {}}])
        self.assertEqual([r['conditionContext']['omitted'] for r in requests], [['product'], ['region'], ['product', 'region']])
