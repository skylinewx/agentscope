"""Current presentation survives preparation, retries, persistence and history selection."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from jsonschema import Draft202012Validator

from analysis_extension.conversation_context import save_context, next_context_state
from analysis_extension.followup_scope import prepare_request, query_anchors, review_context
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisTask
from analysis_extension.turn_completion import TurnCompletion
from analysis_presentation_fixtures import PresentationCase


class GroupingLifecycleTests(TestCase):
    def turn(self, previous=None, question='看下久其集团26年EVA预算'):
        state = {'turnKey': 'current', 'request': question, 'failures': {}, 'buildRefs': [],
                 'previousQueryIntent': previous if previous is not None else {'metric': '净利润', 'population': '全公司所有单位',
                     'period': '26年', 'scenario': '预算', 'grouping': '板块', 'order': '从大到小'},
                 'inheritedScope': {}}
        return TurnCompletion(SimpleNamespace(), state)

    def declare(self, turn, intent=None, **kwargs):
        request = dict(action='declare', mode='DIRECT', goal=turn.state['request'], **kwargs)
        if intent is not None:
            request['intent'] = intent
        if 'context' in request:
            request['context'] = {key: value for key, value in request['context'].items() if key in ('mode', 'contextRefs')}
        Draft202012Validator(AnalysisTask.input_schema).validate(request)
        if 'context' in request:
            request['context']['userQuote'] = turn.state['request']
        return turn.task(**request)

    def query(self, turn, **extra):
        return prepare_request(turn, {'originalToolName': 'execute_budget_query'}, {
            'purpose': 'QUERY', 'intentDraft': {'targets': [{'id': 't', 'periodText': '26年',
                'businessPhrases': [{'text': 'EVA'}]}], 'sharedConditions': [{'text': '预算'}],
                'dimensionScopes': [{'id': 'org', 'dimensionHint': '组织机构',
                    'selector': {'type': 'VALUES', 'members': ['久其集团']}}]}, **extra})

    def test_actual_eva_declaration_without_grouping_clears_old_group_and_order(self):
        turn = self.turn()
        result = self.declare(turn, {'metric': 'EVA', 'population': '久其集团', 'period': '26年',
            'scenario': '预算', 'analysisFocus': None}, context={'mode': 'CONTINUE',
            'changedFields': ['metric', 'population', 'analysisFocus'], 'userQuote': turn.state['request']})
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('order', result['intent'])
        self.assertEqual(result['intent']['period'], '26年')
        self.assertIn('本轮不分组', result['intentSummary'])
        self.assertNotIn('groupBy', self.query(turn))
        self.assertNotIn('板块', self.query(turn).get('contextText', ''))
        self.assertEqual(review_context(turn)['intentDecisions']['grouping']['operation'], 'CLEAR')
        self.assertEqual(query_anchors(turn), [])

    def test_first_declaration_without_intent_cannot_inherit_old_group_or_order(self):
        turn = self.turn()
        result = self.declare(turn)
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('order', result['intent'])
        self.assertEqual(result['intent']['period'], '26年')
        self.assertEqual(result['intent']['population'], '全公司所有单位')
        self.assertNotIn('groupBy', self.query(turn))

    def test_new_turn_omission_and_null_clear_old_presentation(self):
        for grouping in ({}, {'grouping': None}):
            with self.subTest(grouping=grouping):
                turn = self.turn()
                result = self.declare(turn, {'metric': 'EVA', **grouping})
                self.assertNotIn('grouping', result['intent'])
                self.assertNotIn('groupBy', self.query(turn))

    def test_explicit_inheritance_and_replacement_use_current_group_without_flags(self):
        for grouping, operation in (('板块', 'INHERIT'), ('地区', 'SET')):
            turn = self.turn(question='按地区或原来的板块看久其集团26年EVA预算')
            result = self.declare(turn, {'metric': 'EVA', 'grouping': grouping, 'order': '从小到大'})
            self.assertEqual(self.query(turn)['groupBy'], [grouping])
            self.assertEqual(result['intentDecisions']['grouping']['operation'], operation)
            self.assertEqual(result['intent']['order'], '从小到大')
            self.assertEqual(result['intent']['population'], '全公司所有单位')

    def test_same_turn_metadata_retries_and_json_resume_keep_clear(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'grouping': None})
        turn = TurnCompletion(turn.service, json.loads(json.dumps(turn.state)))
        for _ in range(3):
            self.assertNotIn('grouping', self.declare(turn)['intent'])
            self.assertNotIn('groupBy', self.query(turn))

    def test_same_turn_metadata_retry_keeps_explicit_grouping(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'grouping': '板块'})
        self.assertEqual(self.declare(turn)['intent']['grouping'], '板块')
        self.assertEqual(self.query(turn)['groupBy'], ['板块'])

    def test_new_topic_retry_with_added_quote_keeps_intent_and_decisions_together(self):
        turn = self.turn(previous={}, question='按地区看久其集团26年EVA预算')
        first = self.declare(turn, {'metric': 'EVA', 'grouping': '地区'})
        second = self.declare(turn, context={'mode': 'NEW', 'userQuote': turn.state['request']})
        self.assertEqual(second['intent'], first['intent'])
        self.assertEqual(second['intentDecisions'], first['intentDecisions'])
        self.assertEqual(self.query(turn)['groupBy'], ['地区'])

    def test_repeating_context_without_change_flags_does_not_undo_cancellation(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA'})
        result = self.declare(turn, context={'mode': 'CONTINUE'})
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('order', result['intent'])
        self.assertNotIn('groupBy', self.query(turn))

    def test_year_only_followup_can_explicitly_retain_grouping_and_other_scope(self):
        turn = self.turn(question='按原来的板块看25年数据')
        result = self.declare(turn, {'period': '25年', 'grouping': '板块', 'order': '从大到小'})
        self.assertEqual(result['intent']['period'], '25年')
        self.assertEqual(result['intent']['population'], '全公司所有单位')
        self.assertEqual(result['intent']['metric'], '净利润')
        self.assertEqual(result['intentDecisions']['grouping']['operation'], 'INHERIT')

    def test_new_current_intent_can_clear_group_created_earlier_in_same_turn(self):
        turn = self.turn(previous={'metric': 'EVA', 'period': '26年'})
        self.declare(turn, {'grouping': '地区'})
        self.declare(turn, {'metric': 'EVA', 'grouping': None})
        self.assertNotIn('grouping', self.declare(turn)['intent'])

    def test_null_patch_clears_group_and_order(self):
        turn = self.turn()
        self.declare(turn, {'grouping': None, 'order': None})
        self.assertNotIn('groupBy', self.query(turn))
        self.assertNotIn('order', self.declare(turn)['intent'])

    def test_new_turn_does_not_resurrect_cleared_group_from_old_context(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'population': '久其集团'})
        save_context(turn.state, 'DIRECT')
        saved = json.loads(json.dumps(turn.state))
        state = {'turnKey': 'next', 'request': '继续查看', **next_context_state(saved)}
        following = TurnCompletion(turn.service, state)
        self.assertNotIn('grouping', self.declare(following)['intent'])
        self.assertNotIn('groupBy', self.query(following))

    def test_pending_context_clear_survives_explicit_resume(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'population': '久其集团'})
        save_context(turn.state, 'INCOMPLETE')
        pending = turn.state['businessContexts'][-1]
        # No source has been queried in this fixture, so no confirmed language
        # anchors exist yet. Repeat the requested business words explicitly.
        state = {'turnKey': 'resume', 'request': '继续查询久其集团26年EVA预算', **next_context_state(turn.state)}
        following = TurnCompletion(turn.service, state)
        result = self.declare(following, context={'mode': 'CONTINUE',
            'contextRefs': [pending['contextRef']], 'userQuote': state['request']})
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('groupBy', self.query(following))

    def test_selecting_older_context_does_not_restore_its_group_to_current_ungrouped_intent(self):
        turn = self.turn()
        record = {'contextRef': 'older', 'request': '按地区分析', 'goal': '按地区分析',
                  'intent': {'metric': 'EVA', 'grouping': '地区', 'period': '26年'},
                  'status': 'DIRECT', 'kind': 'DELIVERED', 'subjectScope': {}}
        turn.state['businessContexts'] = [record]
        result = self.declare(turn, {'metric': 'EVA', 'population': '久其集团'},
            context={'mode': 'CONTINUE', 'contextRefs': ['older'], 'userQuote': turn.state['request']})
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('groupBy', self.query(turn))

    def test_new_topic_and_synthesis_do_not_restore_grouping(self):
        turn = self.turn()
        result = self.declare(turn, {'metric': 'EVA'}, context={'mode': 'NEW', 'userQuote': turn.state['request']})
        self.assertNotIn('grouping', result['intent'])
        turn.state['businessContexts'] = [{'contextRef': ref, 'intent': {'metric': 'EVA', 'grouping': '地区'},
            'subjectScope': {}, 'kind': 'DELIVERED', 'status': 'DIRECT', 'request': '旧查询', 'goal': '旧查询'}
            for ref in ('a', 'b')]
        result = self.declare(turn, {'metric': 'EVA'}, context={'mode': 'SYNTHESIZE',
            'contextRefs': ['a', 'b'], 'userQuote': turn.state['request']})
        self.assertNotIn('grouping', result['intent'])

    def test_reference_query_can_explicitly_group_without_changing_main_default(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA'})
        self.assertEqual(self.query(turn, groupBy=['地区'])['groupBy'], ['地区'])
        self.assertNotIn('grouping', turn.state['queryIntent'])
        self.assertNotIn('groupBy', self.query(turn))

    def test_invalid_declaration_cannot_clear_valid_current_group(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'grouping': '板块'})
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError):
            turn.task(mode='DIRECT', goal='invalid', intent={'grouping': ['非法形状']})
        self.assertEqual(before, turn.state)

    def test_grouped_analysis_can_explicitly_query_details_without_changing_default(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'grouping': '板块'})
        self.assertEqual(self.query(turn, groupBy=[])['groupBy'], [])
        self.assertEqual(self.query(turn)['groupBy'], ['板块'])


class CapturedGroupingLifecycleTests(PresentationCase):
    async def test_confirmed_scope_anchors_and_pending_resume_keep_the_current_group_decision(self):
        from analysis_business_query_test import business_fixture
        from analysis_fixtures import OWNER
        from analysis_budget_batch_test import update
        capture, receipt = business_fixture()
        capture['request'].update(question='2026年机构甲科目0预算按板块汇总', groupBy=['板块'],
            intentDraft={'targets': [{'id': 't', 'periodText': '2026年', 'businessPhrases': [{'text': '科目0'}]}],
                         'sharedConditions': [{'text': '预算'}]})
        receipt['data']['question'] = capture['request']['question']
        receipt['data']['resolution']['intentDraft'] = deepcopy(capture['request']['intentDraft'])
        update(capture, receipt)
        reference = self.store.capture(OWNER, capture)
        record = {'draft': {'id': 'org', 'dimensionHint': '组织机构',
                  'sourceText': capture['request']['question'], 'selector': {'type': 'VALUES', 'members': ['机构甲']}},
                  'captureRef': reference, 'appliesToAllTargets': True,
                  'confirmedSource': {'executionId': receipt['executionId'], 'scopeId': 'org'}}
        intent = {'metric': '科目0', 'population': '机构甲', 'period': '2026年', 'scenario': '预算',
                  'grouping': '板块', 'order': '从大到小'}
        scope = {'scopeRef': 'confirmed', 'captureRefs': [reference], 'selections': [
            {'captureRef': reference, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}]}
        base = {'contextRef': 'base', 'request': capture['request']['question'], 'goal': '来源查询',
                'intent': intent, 'subjectScope': scope, 'dimensionScopes': [record],
                'status': 'DIRECT', 'kind': 'DELIVERED'}
        turn = TurnCompletion(self.service, {'turnKey': 'changed', 'request': '改看明细',
            'previousQueryIntent': intent, 'inheritedScope': scope, 'inheritedDimensionScopes': [record],
            'businessContexts': [base], 'activeBusinessContextRef': 'base', 'selectedContextRefs': ['base']})
        result = turn.task(mode='DIRECT', goal='查看原范围明细', intent={'grouping': None, 'order': None})
        self.assertTrue(result['queryAnchors'])
        self.assertTrue(all('groupBy' not in anchor and 'discoverGroupingArguments' not in anchor
                            for anchor in result['queryAnchors']))
        save_context(turn.state, 'INCOMPLETE')
        pending = turn.state['businessContexts'][-1]
        self.assertEqual(pending['dimensionScopes'], [record])
        state = {'turnKey': 'resumed', 'request': '继续刚才没完成的查询',
                 **next_context_state(json.loads(json.dumps(turn.state)))}
        resumed = TurnCompletion(self.service, state)
        declared = resumed.task(mode='DIRECT', goal='继续查询', context={'mode': 'CONTINUE',
            'contextRefs': [pending['contextRef']], 'userQuote': state['request']})
        self.assertNotIn('grouping', declared['intent'])
        prepared = prepare_request(resumed, capture, {'purpose': 'QUERY', 'intentDraft': {
            'targets': [{'id': 't', 'periodText': '2026年', 'businessPhrases': [{'text': '科目0'}]}],
            'sharedConditions': [{'text': '预算'}]}})
        self.assertNotIn('groupBy', prepared)
        self.assertEqual(prepared['intentDraft']['dimensionScopes'][0]['confirmedSource'], {**record['confirmedSource'], 'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId']})
        self.assertEqual(prepared['intentDraft']['dimensionScopes'][0]['sourceText'], record['draft']['sourceText'])
        self.assertEqual(prepared['scope']['periodCode'], '2026N0001')
        self.assertEqual(prepared['scope']['taskCodes'], ['TASK'])
        self.assertNotIn('板块', prepared.get('contextText', ''))
        self.assertNotIn('grouping', review_context(resumed)['selectedContexts'][0]['intent'])
