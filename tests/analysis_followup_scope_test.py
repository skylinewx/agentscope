"""追问范围回归：执行遗漏补全、主体/参考隔离、明确变更及后台状态恢复。"""
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from jsonschema import Draft202012Validator
from analysis_extension.tools import AnalysisTask

import analysis_budget_batch_test as batch_fixtures
import analysis_capture_test as capture_fixtures
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters.budget_batch import catalog
from analysis_extension.capture import ReceiptCapture
from analysis_extension.followup_scope import declare_context, prepare_request, remember_subject, review_context, validate_primary
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn, STATE_KEY
from agentscope.message import ToolCallBlock, TextBlock, UserMsg
from agentscope.tool import ToolResponse


class FollowupScopeTest(PresentationCase):
    def setUp(self):
        super().setUp()
        self.capture, self.selection, _, self.receipt = batch_fixtures.batch_fixture(False)
        self.capture.update(turnKey='first', serverName='source', connectionFingerprint='connection')
        self.table = self.receipt['data']['stepResults'][0]['batchResults'][0]
        self.query = self.capture['request']['plan']['steps'][1]['batchQuery']
        condition = [{'dimCode': 'MD_ORG', 'hierarchy': {'rootCode': 'GT00', 'level': 2}}]
        self.query['batchDimensions'] = deepcopy(condition)
        self.table['request']['batchDimensions'] = deepcopy(condition)
        self.table['data']['resolvedPeriods'] = [{'dimCode': 'DATATIME', 'values': ['2026N0001']}]
        rows = [[f'positive-{index}', f'单位{index}', str(index + 1), '1'] for index in range(9)]
        rows += [['GT0009', '商旅', '-8300.00', '1'], ['GT9999', '新增', '-10360.23', '1']]
        self.table['data'].update(rows=rows, rowCount=len(rows))
        self.ref = self.save_capture(self.capture, self.receipt)
        self.selection['captureRef'] = self.ref
        self.previous = {'turnKey': 'first', 'request': '查询二级单位', 'queryCaptureRefs': [self.ref],
                         'queryIntent': {'population': '二级单位', 'period': '2026年'}, 'mode': 'DIRECT'}
        remember_subject(TurnCompletion(self.service, self.previous), {'status': 'DIRECT'})
        self.state = {'turnKey': 'next', 'request': '分析一下亏损企业', 'buildRefs': [], 'failures': {},
                      'inheritedScope': deepcopy(self.previous['resolvedSubjectScope']),
                      'previousQueryIntent': deepcopy(self.previous['queryIntent'])}
        self.turn = TurnCompletion(self.service, self.state)
        self.service.turn = self.turn
        self.turn.task(mode='ANALYSIS', goal='分析当前范围中的亏损企业')

    def save_capture(self, capture, receipt):
        batch_fixtures.update(capture, receipt)
        capture.pop('captureRef', None)
        return self.store.capture(OWNER, deepcopy(capture))

    def facts(self, ref):
        capture = self.store.get_capture(OWNER, ref)
        facts = catalog(capture)
        for fact in facts:
            fact.update(captureRefs=[ref], inputRole='PRIMARY', objectId=fact['dimensions']['MD_ORG'])
        return facts

    def test_missing_hierarchy_level_is_restored_before_execution(self):
        request = deepcopy(self.capture['request'])
        request['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy'].pop('level')
        effective = prepare_request(self.turn, self.capture, request)
        hierarchy = effective['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy']
        self.assertEqual(hierarchy, {'rootCode': 'GT00', 'level': 2})
        self.assertNotIn('level', request['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy'])
        self.assertEqual(self.turn.state['queryIntent']['population'], '二级单位')

    async def test_actual_build_retains_two_loss_units_and_exact_total(self):
        selection = {**self.selection, 'role': 'PRIMARY', 'rowIndices': [9, 10], 'columnNames': ['metric1'],
                     'selectionReason': '在上一轮二级单位结果内选择负值'}
        result = await self.service.build([self.ref], [selection], {'label': '当前范围亏损单位', 'selectionReason': '对上轮结果进一步筛选'}, [], 'two-loss')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual({fact['dimensions']['MD_ORG'] for fact in artifact['facts']}, {'GT0009', 'GT9999'})
        self.assertEqual(sum(Decimal(fact['value']) for fact in artifact['facts']), Decimal('-18660.23'))
        remember_subject(self.turn, {'status': 'READY', 'analysisRef': result['analysisRef']})
        self.assertEqual(self.turn.state['resolvedSubjectScope']['selections'][0]['rowIndices'], [9, 10])

    async def test_legacy_reference_members_do_not_become_next_subject(self):
        scope = {'label': '当前亏损单位', 'selectionReason': '其余作为参照', 'memberDimension': 'MD_ORG',
                 'members': ['GT0009', 'GT9999'], 'referenceMembers': [f'positive-{index}' for index in range(9)]}
        result = await self.service.build([self.ref], [self.selection], scope, [], 'legacy-references')
        remember_subject(self.turn, {'status': 'READY', 'analysisRef': result['analysisRef']})
        self.assertEqual(self.state['resolvedSubjectScope']['selections'][0]['rowIndices'], [9, 10])

    def test_broad_old_capture_cannot_be_reused_as_primary(self):
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        capture['request']['plan']['steps'][1]['batchQuery']['batchDimensions'] = [{'dimCode': 'MD_ORG', 'all': True}]
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['request']['batchDimensions'] = [{'dimCode': 'MD_ORG', 'all': True}]
        table['data']['rows'].append(['descendant', '下级单位', '-200', '1'])
        table['data']['rowCount'] += 1
        ref = self.save_capture(capture, receipt)
        facts = self.facts(ref)
        with self.assertRaises(AnalysisError) as caught:
            validate_primary(self.turn, {'facts': facts})
        self.assertEqual(caught.exception.code, 'FOLLOWUP_SCOPE_MISMATCH')
        for fact in facts:
            if fact['dimensions']['MD_ORG'] == 'descendant':
                fact['inputRole'] = 'REFERENCE'
        validate_primary(self.turn, {'facts': facts})

    def test_explicit_population_change_does_not_change_period(self):
        self.state['request'] = '改为全部层级的单位'
        self.turn.task(mode='ANALYSIS', goal='全层级', context={'mode': 'CONTINUE', 'changedFields': ['population'], 'userQuote': '全部层级'})
        request = deepcopy(self.capture['request'])
        query = request['plan']['steps'][1]['batchQuery']
        query['batchDimensions'] = [{'dimCode': 'MD_ORG', 'all': True}]
        query['metrics'][0]['selection'].pop('periodCode')
        effective = prepare_request(self.turn, self.capture, request)['plan']['steps'][1]['batchQuery']
        self.assertEqual(effective['batchDimensions'], [{'dimCode': 'MD_ORG', 'all': True}])
        self.assertEqual(effective['metrics'][0]['selection']['periodCode'], '2026N0001')

    def test_changing_year_retains_population(self):
        self.state['request'] = '看一下2025年的'
        self.turn.task(mode='ANALYSIS', goal='上一年', context={'mode': 'CONTINUE', 'changedFields': ['period'], 'userQuote': '2025年'})
        request = deepcopy(self.capture['request'])
        query = request['plan']['steps'][1]['batchQuery']
        query.pop('batchDimensions')
        query['metrics'][0]['selection']['periodCode'] = '2025N0001'
        effective = prepare_request(self.turn, self.capture, request)['plan']['steps'][1]['batchQuery']
        self.assertEqual(effective['batchDimensions'], self.query['batchDimensions'])
        self.assertEqual(effective['metrics'][0]['selection']['periodCode'], '2025N0001')

    def test_new_topic_and_explicit_synthesis_release_previous_scope(self):
        for mode, request_text in [('NEW', '另查全集团收入'), ('SYNTHESIZE', '把前面几次结果综合分析')]:
            with self.subTest(mode=mode):
                state = {**self.state, 'request': request_text}
                state['followupContext'] = declare_context(state, {'mode': mode, 'userQuote': request_text})
                turn = TurnCompletion(self.service, state)
                request = deepcopy(self.capture['request'])
                request['plan']['steps'][1]['batchQuery'].pop('batchDimensions')
                self.assertEqual(prepare_request(turn, self.capture, request), request)

    def test_scope_change_cannot_invent_user_quote_or_mutate_state_on_failure(self):
        before = deepcopy(self.state)
        with self.assertRaises(AnalysisError):
            self.turn.task(mode='ANALYSIS', goal='全集团', context={'mode': 'CONTINUE', 'changedFields': ['population'], 'userQuote': '所有单位'})
        self.assertEqual(self.state, before)

    def test_grouped_result_must_keep_original_population_conditions(self):
        facts = self.facts(self.ref)
        fact = deepcopy(facts[0])
        fact['dimensions'].pop('MD_ORG')
        fact['dimensions']['MD_ORG__sector'] = 'sector'
        validate_primary(self.turn, {'facts': [fact]})
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        capture['request']['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy'].pop('level')
        receipt['data']['stepResults'][0]['batchResults'][0]['request']['batchDimensions'][0]['hierarchy'].pop('level')
        ref = self.save_capture(capture, receipt)
        fact['captureRefs'] = [ref]
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': [fact]})

    def test_selected_subset_cannot_expand_back_to_whole_original_result(self):
        self.state['inheritedScope']['selections'][0]['rowIndices'] = [9, 10]
        facts = self.facts(self.ref)
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': facts})
        validate_primary(self.turn, {'facts': [fact for fact in facts if fact['rowIndex'] in (9, 10)]})

    def test_new_year_members_are_allowed_with_same_query_scope(self):
        self.state['request'] = '看2025年的'
        self.turn.task(mode='ANALYSIS', goal='上年', context={'mode': 'CONTINUE', 'changedFields': ['period'], 'userQuote': '2025年'})
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['rows'][0][0] = 'member-in-other-year'
        table['data']['fixedDimValMap']['DATATIME'] = '2025N0001'
        ref = self.save_capture(capture, receipt)
        validate_primary(self.turn, {'facts': self.facts(ref)})

    def test_other_source_is_not_rewritten_and_does_not_replace_primary(self):
        request = deepcopy(self.capture['request'])
        request['plan']['steps'][1]['batchQuery'].pop('batchDimensions')
        self.assertEqual(prepare_request(self.turn, {**self.capture, 'resourceId': 'different-source'}, request), request)
        capture = deepcopy(self.capture)
        capture['resourceId'] = 'different-source'
        ref = self.save_capture(capture, self.receipt)
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': self.facts(ref)})

    def test_failed_turn_does_not_replace_previous_subject(self):
        before = deepcopy(self.state)
        remember_subject(self.turn, {'status': 'INCOMPLETE'})
        self.assertEqual(self.state, before)

    def test_reference_scope_and_exclusive_selectors_are_not_mixed(self):
        from analysis_extension.followup_scope import _condition
        previous = {'dimCode': 'tree', 'hierarchy': {'rootCode': 'A', 'level': 2}}
        proposed = {'dimCode': 'tree', 'hierarchy': {'rootCode': 'B'}}
        self.assertEqual(_condition(previous, proposed), proposed)
        values = {'dimCode': 'tree', 'values': ['a', 'b']}
        predicate = {'dimCode': 'tree', 'where': {'field': 'category', 'op': 'EQ', 'value': 'c'}}
        self.assertEqual(_condition(values, predicate), predicate)
        self.assertEqual(_condition(previous, values), values)
        self.assertEqual(_condition(values, {'dimCode': 'tree'}), values)

    def test_new_topic_clears_old_intent_and_period_change_drops_stale_year(self):
        self.state['request'] = '另查2025年的收入'
        self.turn.task(mode='ANALYSIS', goal='收入', context={'mode': 'NEW', 'userQuote': '另查2025年的收入'}, intent={'metric': '收入'})
        self.assertEqual(self.state['queryIntent'], {'metric': '收入'})

    def test_expired_previous_receipt_is_reported_without_breaking_new_topic(self):
        self.state['inheritedScope']['selections'][0]['captureRef'] = 'cap_' + '0' * 64
        result = self.turn.task(mode='ANALYSIS', goal='继续原来的分析', context={'mode': 'CONTINUE'})
        self.assertEqual(result['followupScope']['unavailable']['code'], 'NOT_FOUND')
        self.state['request'] = '换个问题查询收入'
        result = self.turn.task(mode='ANALYSIS', goal='收入', context={'mode': 'NEW', 'userQuote': '换个问题'})
        self.assertNotIn('unavailable', result['followupScope'])
        self.assertEqual(result['followupScope']['inheritedSubject'], [])

    def test_task_change_does_not_merge_independent_organization_categories(self):
        self.state['request'] = '换到管理架构任务下的单位'
        self.turn.task(mode='ANALYSIS', goal='另一任务', context={'mode': 'CONTINUE', 'changedFields': ['task'], 'userQuote': '管理架构任务'})
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        for metric in receipt['data']['stepResults'][0]['batchResults'][0]['metrics']:
            metric['context']['target']['organizationCategory'] = 'INDEPENDENT_CATEGORY'
        ref = self.save_capture(capture, receipt)
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': self.facts(ref)})
        self.turn.task(mode='ANALYSIS', goal='管理架构单位', context={'mode': 'CONTINUE',
            'changedFields': ['task', 'population'], 'userQuote': '管理架构任务下的单位'})
        validate_primary(self.turn, {'facts': self.facts(ref)})

    def test_compound_filter_preserves_old_or_group(self):
        from analysis_extension.followup_scope import _inherit_query
        previous = {'batchDimensions': [{'dimCode': 'dimension', 'where': {'op': 'OR', 'children': [
            {'op': 'EQ', 'field': 'a', 'value': '01'}, {'op': 'EQ', 'field': 'b', 'value': '02'}]}}]}
        proposed = {'batchDimensions': [{'dimCode': 'dimension', 'where': {'op': 'EQ', 'field': 'c', 'value': '03'}}]}
        effective = _inherit_query(previous, proposed, set())
        condition = effective['batchDimensions'][0]['where']
        self.assertEqual(condition['op'], 'AND')
        self.assertEqual(condition['children'][0], previous['batchDimensions'][0]['where'])
        self.assertEqual(_inherit_query(previous, effective, set()), effective)

    def test_scope_survives_json_roundtrip_and_real_user_turn(self):
        user = UserMsg(name='user', content=[TextBlock(text='分析一下亏损企业')])
        state = SimpleNamespace(middle_context={STATE_KEY: json.loads(json.dumps(self.previous))}, context=[])
        saved = current_turn(state, user)
        self.assertEqual(saved['inheritedScope'], self.previous['resolvedSubjectScope'])
        self.assertIs(saved, current_turn(state, []))
        context = review_context(TurnCompletion(self.service, saved))
        self.assertEqual(context['inheritedSubject'][0]['selectedRowCount'], 11)
        self.assertNotIn('rows', context['inheritedSubject'][0])

    async def test_capture_sends_inherited_request_and_caches_effective_parameters(self):
        tool = capture_fixtures.CaptureTest.make_tool(self, '{}')
        origin = {'resourceId': 'registered-budget', 'serverName': 'not-a-hardcoded-prefix', 'connectionFingerprint': 'connection-v1'}
        capture = deepcopy(self.capture)
        capture.update(origin)
        ref = self.save_capture(capture, self.receipt)
        self.state['inheritedScope']['selections'][0]['captureRef'] = ref
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        middleware = ReceiptCapture(self.store, OWNER, frozenset({'registered-budget'}), self.service.authorize)
        middleware.turn_key_provider = lambda: self.state['turnKey']
        middleware.query_cache_provider = lambda: self.state.setdefault('cache', {})
        middleware.request_preparer = lambda source, request: prepare_request(self.turn, source, request)
        sent = []
        async def execute(**kwargs):
            sent.append(json.loads(kwargs['tool_call'].input))
            yield ToolResponse(content=[TextBlock(text='{"contractVersion":"2.0","status":"SUCCEEDED","data":{}}')])
        request = deepcopy(self.capture['request'])
        request.update(contractVersion='2.0', requestId='test')
        request['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy'].pop('level')
        for call_id in ('one', 'two'):
            call = ToolCallBlock(id=call_id, name=tool.name, input=json.dumps(request))
            events = [event async for event in middleware.run(agent, {'tool_call': call}, execute)]
        self.assertEqual(len(sent), 1)
        saved = self.store.get_capture(OWNER, events[0].metadata['analysisCaptureRef'])
        self.assertEqual(sent[0], saved['request'])
        self.assertEqual(json.loads(saved['submittedRequestText']), request)
        self.assertEqual(sent[0]['plan']['steps'][1]['batchQuery']['batchDimensions'][0]['hierarchy']['level'], 2)

    def test_grouping_change_is_allowed_but_does_not_expand_subject(self):
        self.state['request'] = '按自定义分类分析'
        self.state['previousQueryIntent']['grouping'] = '原来的分类'
        request = {'action': 'declare', 'mode': 'ANALYSIS', 'goal': '按用户分类分析',
                   'context': {'mode': 'CONTINUE', 'changedFields': ['grouping'], 'userQuote': self.state['request']},
                   'intent': {'grouping': '自定义分类'}}
        Draft202012Validator(AnalysisTask.input_schema).validate(request)
        result = self.turn.task(**request)
        self.assertEqual(result['intent']['grouping'], '自定义分类')
        repeated = self.turn.task(mode='ANALYSIS', goal=request['goal'])
        self.assertEqual(repeated['intent']['grouping'], '自定义分类')
        self.assertEqual(result['intent']['population'], '二级单位')
        self.assertEqual(result['intent']['period'], '2026年')
        proposed = deepcopy(self.capture['request'])
        query = proposed['plan']['steps'][1]['batchQuery']
        query.pop('batchDimensions')
        query['aggregation'] = {'groupBy': [{'dimCode': 'MD_ORG', 'field': 'CUSTOM_FIELD'}]}
        effective = prepare_request(self.turn, self.capture, proposed)
        self.assertEqual(effective['plan']['steps'][1]['batchQuery']['batchDimensions'], self.query['batchDimensions'])
        self.assertEqual(effective['plan']['steps'][1]['batchQuery']['aggregation'], query['aggregation'])
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        capture['request']['plan']['steps'][1]['batchQuery']['batchDimensions'] = [{'dimCode': 'MD_ORG', 'all': True}]
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['request']['batchDimensions'] = [{'dimCode': 'MD_ORG', 'all': True}]
        table['data']['rows'].append(['outside-scope', '外部成员', '1', '1'])
        table['data']['rowCount'] += 1
        facts = self.facts(self.save_capture(capture, receipt))
        # 改变分组不能把旧范围以外的机构变成主体。
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': facts})

    def test_cleared_grouping_and_order_intents_do_not_resurrect_previous_values(self):
        self.state['previousQueryIntent'].update(grouping='旧分类', order='旧顺序')
        self.state['request'] = '不再分组，使用另一种排序'
        result = self.turn.task(mode='ANALYSIS', goal=self.state['request'],
                               context={'mode': 'CONTINUE', 'changedFields': ['grouping', 'order'], 'userQuote': self.state['request']})
        self.assertNotIn('grouping', result['intent'])
        self.assertNotIn('order', result['intent'])
        self.assertEqual(result['intent']['population'], '二级单位')
        repeated = self.turn.task(mode='ANALYSIS', goal=self.state['request'])
        self.assertNotIn('grouping', repeated['intent'])
        self.assertNotIn('order', repeated['intent'])
