"""有限补做、并发审查复用、事实摘要和局部公式的组合回归。"""
import asyncio
from types import SimpleNamespace
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import AsyncMock, patch

import analysis_closeout_recovery_test as recovery_fixtures
import analysis_flow_regression_test as flow_fixtures
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.middleware import AnalysisMiddleware
from analysis_extension.models import AnalysisError
from analysis_extension.python_products import products
from analysis_extension.review_control import progress, reserve_review, record_repair, closeout_reason
from analysis_extension.turn_completion import TurnCompletion, review_answer
from analysis_extension.validation import render_parts


class CloseoutControlTests(PresentationCase):
    turn = recovery_fixtures.CloseoutRecoveryTests.turn
    observe = recovery_fixtures.CloseoutRecoveryTests.observe
    published = recovery_fixtures.CloseoutRecoveryTests.published

    async def test_concurrent_identical_reviews_share_one_model_call(self):
        turn = self.turn()
        artifact = await self.published(turn)
        restored = TurnCompletion(self.service, deepcopy(turn.state))
        async def review(*args):
            await asyncio.sleep(.05)
            return {'issues': []}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(side_effect=review)) as model:
            results = await asyncio.gather(review_answer(None, turn, artifact, ''),
                                           review_answer(None, restored, artifact, ''))
        self.assertEqual(results, [{'issues': []}, {'issues': []}])
        self.assertEqual(model.await_count, 1)
        self.assertEqual(progress(turn)['review']['attempts'], 1)

    async def test_review_limit_survives_material_changes_and_turn_reload(self):
        turn = self.turn()
        artifact = await self.published(turn)
        self.store.limits = replace(self.store.limits, max_review_attempts=2)
        bad = {'issues': [{'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH', 'message': '金额与原值不一致'}]}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value=bad)) as model:
            await review_answer(None, turn, artifact, '')
            changed = deepcopy(artifact)
            changed['freeDocument']['html'] += '<p>修改了表述。</p>'
            await review_answer(None, turn, changed, '')
            resumed = TurnCompletion(self.service, json.loads(json.dumps(turn.state)))
            changed['freeDocument']['html'] += '<p>再次修改。</p>'
            with self.assertRaises(AnalysisError) as raised:
                await review_answer(None, resumed, changed, '')
            self.assertEqual(raised.exception.code, 'ANALYSIS_RETRY_LIMIT')
        self.assertEqual(model.await_count, 2)

    async def test_repeated_unavailability_closes_without_a_false_pass(self):
        turn = self.turn()
        artifact = await self.published(turn)
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(side_effect=TimeoutError())):
            for attempt in range(2):
                with self.assertRaises(TimeoutError):
                    await review_answer(None, turn, artifact, '')
        self.assertEqual(closeout_reason(turn), 'REVIEW_UNAVAILABLE')
        self.assertEqual(progress(turn)['review']['unavailable'], 2)
        self.assertFalse(any(not verdict['issues'] for verdict in turn.state['documentReviews'].values()))

    def test_repairs_are_deduplicated_and_do_not_count_successful_calls(self):
        turn = self.turn()
        self.store.limits = replace(self.store.limits, max_analysis_repairs=2)
        record_repair(turn, 'first', 'INVALID_SELECTOR')
        record_repair(turn, 'first', 'INVALID_SELECTOR')
        self.assertIsNone(closeout_reason(turn))
        restored = TurnCompletion(self.service, deepcopy(turn.state))
        record_repair(restored, 'second', 'INVALID_WORK_REF')
        self.assertEqual(closeout_reason(restored), 'REPAIR_LIMIT')
        self.assertEqual(len(progress(restored)['repairCalls']), 2)

    async def test_binding_failure_does_not_exhaust_content_review_repairs(self):
        from agentscope.message import TextBlock, ToolCallBlock
        from agentscope.tool import ToolResponse
        from agentscope.message import ToolResultState
        from analysis_turn_completion_test import TurnCompletionTests
        turn = self.turn()
        artifact = await TurnCompletionTests.generic_result(self, turn, published=False)
        failure = ToolResponse(state=ToolResultState.ERROR, content=[TextBlock(text=json.dumps({
            'error': {'code': 'UNSUPPORTED_CLAIM', 'message': '计算不能绑定集合',
                      'details': {'path': '/document/datasets/0'}}}))])
        turn.observe(ToolCallBlock(id='binding', name='AnalysisPublish', input='{}'), failure)
        for index in range(2):
            response = ToolResponse(content=[TextBlock(text=json.dumps({
                'analysisRef': artifact['analysisRef'], 'previewOnly': True,
                'nextAction': 'REPAIR_DOCUMENT',
                'diagnostics': {'semanticReview': {'status': 'REVISE'}}}))])
            turn.observe(ToolCallBlock(id=f'review-{index}', name='AnalysisPublish', input='{}'), response)
        self.assertEqual(progress(turn)['repairCounts'], {'parameter': 1, 'semantic': 2})
        self.assertIsNone(closeout_reason(turn))
        record_repair(turn, 'formal-review', 'DOCUMENT_SEMANTIC_ERRORS')
        self.assertEqual(closeout_reason(turn), 'REPAIR_LIMIT')

    async def test_failed_preview_checks_do_not_count_cached_or_unrun_content_review(self):
        from agentscope.message import TextBlock, ToolCallBlock
        from agentscope.tool import ToolResponse
        from analysis_turn_completion_test import TurnCompletionTests
        turn = self.turn()
        artifact = await TurnCompletionTests.generic_result(self, turn, published=False)
        for index, review_status in enumerate(('NOT_REVIEWED', 'REVISE')):
            response = ToolResponse(content=[TextBlock(text=json.dumps({
                'analysisRef': artifact['analysisRef'], 'previewOnly': True,
                'nextAction': 'REPAIR_DOCUMENT',
                'diagnostics': {
                    'publicationCheck': {'code': 'DOCUMENT_ORDER_NOT_CHECKED'},
                    'semanticReview': {'status': review_status},
                }}))])
            turn.observe(ToolCallBlock(id=f'page-{index}', name='AnalysisPublish', input='{}'), response)
        self.assertEqual(progress(turn)['repairCounts'], {'parameter': 2})
        self.assertIsNone(closeout_reason(turn))

    async def test_host_closeout_keeps_published_result_and_does_not_review_again(self):
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        turn = self.turn()
        artifact = await self.published(turn)
        original = turn.state['delivery']['markdown']
        self.service.preview_reviewer = AsyncMock(side_effect=AssertionError('不得重复审查'))
        answer = await middleware._closeout('REVIEW_LIMIT')
        self.assertIn(original, answer)
        self.assertEqual(turn.state['analysisRef'], artifact['analysisRef'])
        self.assertEqual(turn.state['hostCloseout']['status'], 'READY')
        self.assertEqual(await middleware._closeout('REVIEW_LIMIT'), answer)

    async def test_host_closeout_interprets_available_facts_without_restarting_review(self):
        from analysis_turn_completion_test import TurnCompletionTests
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        turn = self.turn()
        artifact = await TurnCompletionTests.generic_result(self, turn, published=False)
        self.service.preview_reviewer = AsyncMock(side_effect=AssertionError('事实摘要不要求模型重新审查'))
        progress(turn, lambda state: state.update(lastReviewBlocked=True))
        from analysis_model_closeout_test import summary_response
        agent = SimpleNamespace(model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response)))
        answer = await middleware._closeout('REVIEW_UNAVAILABLE', agent)
        self.assertEqual(turn.state['hostCloseout']['status'], 'PARTIAL', answer)
        self.assertEqual(turn.state['hostCloseout']['semanticReview'], 'NOT_REVIEWED')
        self.assertEqual(turn.state['hostCloseout']['deliveryStatus'], 'MODEL_SUMMARY')
        self.assertEqual(self.store.get_analysis(OWNER, turn.state['analysisRef'])['facts'], artifact['facts'])
        agent.model.generate_structured_output.assert_awaited_once()

    async def test_real_agent_delivers_interpreted_summary_without_restarting_business_loop(self):
        from agentscope.agent import Agent, InjectionConfig
        from agentscope.event import ReplyEndEvent
        from agentscope.message import Msg, UserMsg
        from agentscope.permission import PermissionMode
        from agentscope.state import AgentState
        from agentscope.tool import Toolkit
        from agentscope.types import ReplyFinishedReason
        from analysis_turn_completion_test import TurnCompletionTests
        from utils import MockModel
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        original_reasoning = middleware.on_reasoning
        async def finish_saved_work(agent, input_kwargs, next_handler):
            turn = self.service.turn
            turn.task(mode='ANALYSIS', goal='展示给定读数')
            await TurnCompletionTests.generic_result(self, turn, published=False)
            progress(turn, lambda saved: saved.update(closeoutReason='REVIEW_LIMIT'))
            async for event in original_reasoning(agent, input_kwargs, next_handler):
                yield event
        middleware.on_reasoning = finish_saved_work
        model = MockModel(context_size=1000000)
        model.set_responses([AssertionError('不应重启业务推理循环')])
        from analysis_model_closeout_test import summary_response
        model.generate_structured_output = AsyncMock(side_effect=summary_response)
        state = AgentState()
        state.permission_context.mode = PermissionMode.BYPASS
        agent = Agent(name='closeout-test', model=model, system_prompt='test', state=state,
                      middlewares=[middleware], toolkit=Toolkit(tools=await middleware.list_tools()),
                      injection_config=InjectionConfig(inject_runtime_state=False))
        events = [event async for event in agent.reply_stream(UserMsg('user', '分析给定读数'), yield_final_msg=True)]
        end = next(event for event in events if isinstance(event, ReplyEndEvent))
        self.assertEqual(end.finished_reason, ReplyFinishedReason.COMPLETED)
        self.assertEqual(end.metadata['analysisCompletion']['status'], 'PARTIAL')
        self.assertEqual(end.metadata['analysisCompletion']['semanticReview'], 'NOT_REVIEWED')
        self.assertEqual(model.cnt, 0)
        saved = [event for event in events if isinstance(event, Msg)][-1]
        answer = ''.join(block.text for block in saved.content if block.type == 'text')
        model.generate_structured_output.assert_awaited_once()
        self.assertIn('当前已取得的金额', answer)
        self.assertEqual(saved.metadata['presentation']['answer_block_ids'],
                         [block.id for block in saved.content if block.type == 'text'])

    def test_single_batch_metric_is_inferred_but_multiple_metrics_stay_explicit(self):
        from analysis_extension.adapters.budget_batch import selection_scope
        table = {'columnRoles': {'amount': {'kind': 'METRIC'}, 'org': {'kind': 'DIMENSION'}},
                 'data': {'rowCount': 3}}
        self.assertEqual(selection_scope({}, table), ({'amount'}, None))
        table['columnRoles']['cost'] = {'kind': 'METRIC'}
        with self.assertRaises(AnalysisError) as error:
            selection_scope({}, table)
        self.assertEqual(error.exception.details['availableMetricColumns'], ['amount', 'cost'])

    async def test_preview_capacity_stops_immediately_and_repeated_failed_preview_is_bounded(self):
        from agentscope.message import TextBlock, ToolCallBlock
        from agentscope.tool import ToolResponse
        from analysis_turn_completion_test import TurnCompletionTests
        turn = self.turn()
        artifact = await TurnCompletionTests.generic_result(self, turn, published=False)
        ref = artifact['analysisRef']
        self.service.preview_reviewer = AsyncMock(side_effect=AnalysisError('REVIEW_CAPACITY', '材料超限'))
        refs = [fact['factId'] for fact in artifact['facts']]
        preview = await self.service.publish(ref, [], [
            {'viewId': 'values', 'scopeId': artifact['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': refs}
        ], 'capacity',  previewOnly=True, workRefs=[{'taskId': 'values', 'refs': refs}])
        self.assertEqual(preview['nextAction'], 'HOST_CLOSEOUT')
        self.assertEqual(closeout_reason(turn), 'REVIEW_CAPACITY')
        self.service.preview_reviewer.assert_awaited_once()
        # 换一个用户问题，连续返回成功状态但要求修订的预览也受补做上限约束。
        turn.state['turnKey'] += '-revisions'
        self.store.limits = replace(self.store.limits, max_review_attempts=2)
        for index in range(2):
            response = ToolResponse(content=[TextBlock(text=json.dumps({
                'analysisRef': ref, 'previewOnly': True, 'nextAction': 'REPAIR_DOCUMENT',
                'diagnostics': {'semanticReview': {'status': 'REVISE'}}}))])
            turn.observe(ToolCallBlock(id=f'revise-{index}', name='AnalysisPublish', input='{}'), response)
        self.assertEqual(closeout_reason(turn), 'REPAIR_LIMIT')

    def test_local_formula_check_can_complete_with_deeper_missing_values(self):
        data = flow_fixtures.SourceIdentityTests().form_data()
        root = next(fact for fact in data['facts'] if fact.get('formula'))
        child = next(fact for fact in data['facts'] if fact['observationId'] == root['observationId'] and fact['cellAddress'] == 'A3')
        child['formula'] = 'A4'
        values = calculate(data['facts'], data['scope'], [
            {'id': 'local', 'operator': 'FORMULA_CHECK', 'inputRefs': [root['factId']], 'checkScope': 'LOCAL'},
            {'id': 'recursive', 'operator': 'FORMULA_CHECK', 'inputRefs': [root['factId']], 'checkScope': 'RECURSIVE'},
        ], RuleRegistry())
        self.assertEqual(values[0]['calculationValidation'], 'PASS')
        self.assertEqual(values[0]['value'], root['value'])
        self.assertEqual(values[1]['calculationValidation'], 'BLOCKED')
        known = {value['derivedId']: value for value in values}
        rendered, refs = render_parts(known, [{'valueRef': 'derived_recursive', 'field': 'localValue'}])
        self.assertIn(str(root['value']), str((rendered, refs)))
        with self.assertRaises(AnalysisError):
            render_parts(known, [{'valueRef': 'derived_recursive', 'field': 'value'}])

    def test_explicit_python_output_precision_does_not_reduce_intermediate_precision(self):
        from decimal import Decimal, localcontext
        with localcontext() as context:
            context.prec = 128
            raw = str(Decimal('1') / Decimal('3000'))
        output = {'kind': 'TABLE', 'name': 'ratio', 'label': '占比', 'inputRefs': ['f'],
                  'columns': [{'name': 'ratio', 'label': '占比', 'type': 'DECIMAL_STRING', 'decimalPlaces': 8}],
                  'rows': [[raw]]}
        values = products([output], {'f': {'value': '1'}}, ['f'], 'scope', 'run')
        self.assertEqual(values[0]['rows'], [['0.00033333']])
        self.assertEqual(output['rows'], [[raw]])
        no_precision = deepcopy(output)
        no_precision['columns'][0].pop('decimalPlaces')
        with self.assertRaises(AnalysisError) as raised:
            products([no_precision], {'f': {'value': '1'}}, ['f'], 'scope', 'run')
        self.assertFalse(raised.exception.details['requiresSourceQuery'])
