"""回归真实慢路径：编辑误失效、后台等待、重复交付与复合计算误拦。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_review_balance_test import AnalysisReviewBalanceTests as ReviewFixtures
from analysis_turn_completion_test import TurnCompletionTests as TurnFixtures, call
from analysis_extension.completion import completion_summary
from analysis_extension.middleware import AnalysisMiddleware
from analysis_extension.review_control import progress, sync_review_metrics
from analysis_extension.turn_completion import review_answer, STATE_KEY
from agentscope.agent import Agent, ReActConfig
from agentscope.event import ReplyEndEvent
from agentscope.message import Msg
from agentscope.permission import PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from utils import MockModel


class ReviewPerformanceTests(PresentationCase):
    request = ReviewFixtures.request
    turn = ReviewFixtures.turn
    artifact = ReviewFixtures.artifact

    async def test_repaired_preview_confirmation_reuses_document_after_background_restore(self):
        artifact, turn = await self.artifact(), self.turn()
        stale = deepcopy(turn.state)
        verdicts = [{'issues': [{'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH',
                                'message': '结论与计算矛盾'}]}, {'issues': []}]
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(side_effect=verdicts)) as model:
            await review_answer(None, turn, artifact, '')
            artifact['freeDocument']['html'] += '<p>已根据实际计算更正结论。</p>'
            await review_answer(None, turn, artifact, '')
            resumed = self.turn(state=stale)
            result = await review_answer(None, resumed, artifact, '')
            self.assertEqual(result, {'issues': []})
            self.assertEqual(model.await_count, 2)
            self.assertEqual(resumed.state['analysisReviewAttempts'][-1]['cacheScope'], 'DOCUMENT')
            self.assertEqual(resumed.state['analysisReviewTotals']['chargedAttempts'], 2)

    async def test_style_whitespace_and_edit_failure_reuse_business_review(self):
        artifact, turn = await self.artifact(), self.turn()
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            changed = deepcopy(artifact)
            changed['freeDocument']['html'] = changed['freeDocument']['html'].replace(
                '</head>', '<style>p { color: red; margin: 10px; }</style></head>').replace('</p>', '  \n</p>')
            turn.state['failures'] = {'edit': {'tool': 'Edit', 'state': 'error', 'error': {'code': 'MULTIPLE_MATCHES'}}}
            await review_answer(None, turn, changed, '')
            self.assertEqual(model.await_count, 1)
            self.assertIn('edit', turn.state['failures'], '过程错误仍保留在日志状态中')

    async def test_values_units_scope_bindings_text_and_source_failure_invalidate_review(self):
        artifact = await self.artifact()
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, self.turn(), artifact, '')
            for kind in ('value', 'unit', 'scope', 'binding', 'dataset', 'text', 'source_failure'):
                changed, turn = deepcopy(artifact), self.turn()
                turn.state['turnKey'] = 'changed-' + kind
                if kind in ('value', 'unit'):
                    changed['facts'][0][kind] = '99999' if kind == 'value' else '万元'
                elif kind == 'scope':
                    turn.state['queryIntent'] = {'population': 'another population'}
                elif kind == 'binding':
                    changed['freeDocument']['bindings'][0]['valueRef'] = 'another-real-value'
                elif kind == 'dataset':
                    changed['freeDocument']['datasetBindings'] = [{'key': 'other', 'valueRef': 'derived_other'}]
                elif kind == 'text':
                    changed['freeDocument']['html'] += '<p>增加新的业务判断。</p>'
                else:
                    turn.state['failures'] = {'source': {'tool': 'budget_query', 'state': 'error'}}
                await review_answer(None, turn, changed, '')
            self.assertEqual(model.await_count, 8)

    async def test_repaired_order_marker_keeps_review_without_hiding_business_failure(self):
        artifact, turn = await self.artifact(), self.turn()
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            changed = deepcopy(artifact)
            changed['freeDocument']['html'] = changed['freeDocument']['html'].replace(
                '<body>', '<body data-analysis-set="units">')
            turn.state['failures'] = {'publish': {'tool': 'AnalysisPublish', 'state': 'error',
                                                'error': {'code': 'DOCUMENT_ORDER_NOT_CHECKED'}}}
            await review_answer(None, turn, changed, '')
            self.assertEqual(model.await_count, 1)
            self.assertIn('publish', turn.state['failures'])
            turn.state['failures']['publish']['error']['code'] = 'DOCUMENT_SEMANTIC_ERRORS'
            await review_answer(None, turn, changed, '')
            self.assertEqual(model.await_count, 2)

    async def test_style_preview_still_checks_rendering_and_binding(self):
        request, turn = await self.request(), self.turn()
        self.service.preview_reviewer = lambda artifact: review_answer(None, turn, artifact, '')
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model, \
                patch.object(self.service, '_automatic_preview_checks', wraps=self.service._automatic_preview_checks) as checks:
            await self.service.publish(**request, previewOnly=True)
            request['requestKey'] += '-style'
            request['document']['html'] = request['document']['html'].replace('</head>', '<style>p{color:blue}</style></head>')
            await self.service.publish(**request, previewOnly=True)
            self.assertEqual(model.await_count, 1)
            self.assertEqual(checks.call_count, 2)

    async def test_background_review_metrics_survive_stale_session_state(self):
        artifact, turn = await self.artifact(), self.turn()
        stale = deepcopy(turn.state)
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            resumed = self.turn(state=stale)
            await review_answer(None, resumed, artifact, '')
            totals = resumed.state['analysisReviewTotals']
            self.assertEqual(totals['attempts'], 2)
            self.assertEqual(totals['chargedAttempts'], 1)
            self.assertEqual(totals['reusedPartCount'], 1)
            self.assertEqual(model.await_count, 1)
        another = self.turn(state=deepcopy(stale))
        sync_review_metrics(another)
        self.assertEqual(another.state['analysisReviewTotals'], totals)
        attempts = another.state['analysisReviewAttempts']
        self.assertEqual(len({item['reviewId'] for item in attempts}), 2)
        self.assertAlmostEqual(totals['chargedElapsedMs'], progress(another)['review']['elapsedMs'])

    async def test_new_final_claim_is_reviewed_but_registered_entry_is_reused(self):
        artifact, turn = await self.artifact(), self.turn()
        turn.state['delivery'] = {'registered': True, 'markdown': '[查看分析](./approved)'}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            await review_answer(None, turn, artifact, turn.state['delivery']['markdown'])
            self.assertEqual(model.await_count, 1)
            await review_answer(None, turn, artifact, '全部单位都已盈利。\n\n' + turn.state['delivery']['markdown'])
            self.assertEqual(model.await_count, 2)


class CalculationChainTests(TestCase):
    def test_mixed_summary_accepts_completed_counts_without_requiring_each_to_be_arithmetic(self):
        artifact = self.fixture()
        task = artifact['plan']['workPlan'][0]
        task.update(operator='ARITHMETIC', refs=['count_loss', 'count_all', 'share'])
        summary = completion_summary(artifact)
        self.assertEqual(summary['state'], 'READY')
        self.assertTrue(summary['items'][0]['items'][0]['supportingComputation'])
        task['refs'] = ['count_loss', 'count_all']
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE', '计数不能代替未执行的算术')

    def test_supporting_results_do_not_supply_missing_operator_coverage(self):
        artifact = self.fixture()
        task = artifact['plan']['workPlan'][0]
        task.update(operator='ARITHMETIC', refs=['count_all', 'share'])
        artifact['derivedValues'][-1]['inputRefs'] = ['count_loss']
        summary = completion_summary(artifact)
        self.assertEqual(summary['state'], 'INCOMPLETE')
        self.assertEqual(summary['items'][0]['missingTargetRefs'], ['b', 'c'])
        task.update(operator='FORMULA_CHECK')
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')

    def test_failed_supporting_dependency_still_blocks_mixed_summary(self):
        artifact = self.fixture()
        task = artifact['plan']['workPlan'][0]
        task.update(operator='ARITHMETIC', refs=['count_all', 'share'])
        artifact['derivedValues'][1]['calculationValidation'] = 'FAIL'
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')

    def fixture(self):
        def derived(ref, operator, inputs):
            return {'derivedId': ref, 'operator': operator, 'inputRefs': inputs,
                    'calculationValidation': 'PASS', 'executionStatus': 'COMPLETED'}
        return {'facts': [{'factId': ref} for ref in ('a', 'b', 'c')], 'relations': [],
                'derivedValues': [derived('count_loss', 'COUNT', ['a']),
                                  derived('count_all', 'COUNT', ['a', 'b', 'c']),
                                  derived('share', 'ARITHMETIC', ['count_loss', 'count_all'])],
                'plan': {'workPlan': [{'taskId': 'count_share', 'title': '计算数量占比', 'kind': 'COMPUTATION',
                                      'operator': 'COUNT', 'refs': ['share'], 'targetRefs': ['a', 'b', 'c']}]}}

    def test_ratio_of_real_counts_completes_count_work(self):
        result = completion_summary(self.fixture())
        self.assertEqual(result['state'], 'READY')
        self.assertEqual(result['items'][0]['items'][0]['operatorMatchRefs'], ['count_all', 'count_loss'])

    def test_unrelated_count_does_not_cover_other_operands(self):
        artifact = self.fixture()
        artifact['derivedValues'][-1]['inputRefs'] = ['count_loss', 'b', 'c']
        result = completion_summary(artifact)
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertEqual(result['items'][0]['missingTargetRefs'], ['b', 'c'])

    def test_missing_blocked_and_cyclic_dependencies_remain_blocked(self):
        for kind in ('missing', 'blocked', 'cycle', 'trace_only'):
            artifact = self.fixture()
            if kind == 'missing':
                artifact['derivedValues'][1]['inputRefs'] = ['not_loaded']
            elif kind == 'blocked':
                artifact['derivedValues'][1]['calculationValidation'] = 'BLOCKED'
            elif kind == 'cycle':
                artifact['derivedValues'][1]['inputRefs'] = ['share']
            else:
                artifact['derivedValues'][-1].update(inputRefs=['a', 'b', 'c'], sourceFactIds=['count_all'])
            with self.subTest(kind=kind):
                self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')

    def test_formula_checks_still_require_each_target(self):
        artifact = self.fixture()
        artifact['derivedValues'][0]['operator'] = 'FORMULA_CHECK'
        artifact['plan']['workPlan'][0].update(operator='FORMULA_CHECK', targetRefs=['a'])
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')
        artifact['plan']['workPlan'][0]['refs'] = ['count_loss']
        self.assertEqual(completion_summary(artifact)['state'], 'READY')


class PublicationLoopTests(PresentationCase):
    turn = TurnFixtures.turn
    observe = TurnFixtures.observe
    generic_result = TurnFixtures.generic_result

    async def test_approved_delivery_requires_current_intent_and_real_publication(self):
        turn = self.turn()
        self.service.turn = turn
        self.service.preview_reviewer = lambda artifact: review_answer(None, turn, artifact, '')
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})):
            await self.generic_result(turn)
        self.assertEqual(middleware._approved_delivery(), turn.state['delivery']['markdown'])
        turn.state['queryIntent'] = {'population': 'new scope'}
        self.assertIsNone(middleware._approved_delivery())
        turn.state.pop('queryIntent')
        turn.state['delivery']['registered'] = False
        self.assertIsNone(middleware._approved_delivery())

    async def test_pending_independent_work_is_not_suspended(self):
        turn = self.turn()
        self.service.turn = turn
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        state = AgentState()
        state.middle_context['background_tools_v1'] = {'p': {'tool': 'AnalysisPublish'}, 'q': {'tool': 'query'}}
        turn.state['publicationCalls'] = {'p': 'preview'}
        progress(turn, lambda saved: saved.update(publicationWaits={'preview': {'workReady': True}}))
        agent = SimpleNamespace(state=state)
        self.assertFalse(middleware._waiting_for_publication(agent))
        state.middle_context['background_tools_v1'].pop('q')
        self.assertTrue(middleware._waiting_for_publication(agent))
        progress(turn, lambda saved: saved['publicationWaits']['preview'].update(workReady=False))
        self.assertFalse(middleware._waiting_for_publication(agent))

    async def test_background_preview_resumes_and_delivers_without_model_polling_or_rewriting(self):
        from agentscope.app._manager import BackgroundTaskManager
        from agentscope.app.message_bus import MessageBus, MessageBusKeys
        from agentscope.app.middleware import InboxMiddleware, ToolOffloadMiddleware

        turn = self.turn()
        self.service.turn = turn
        artifact = await self.generic_result(turn, published=False)
        refs = [fact['factId'] for fact in artifact['facts']]
        request = {'analysisRef': artifact['analysisRef'], 'requestKey': 'slow-preview', 'previewOnly': True,
                   'document': {'title': '原模板', 'html': '<html><head></head><body>' +
                       ''.join('<p>{{v' + str(i) + '}}</p>' for i in range(len(refs))) + '</body></html>',
                       'bindings': [{'key': 'v' + str(i), 'valueRef': ref} for i, ref in enumerate(refs)]},
                   'workRefs': [{'taskId': 'values', 'refs': refs}],
                   'renderCheckUnavailableReason': '离线流程测试未运行浏览器测量。'}
        gate, done = asyncio.Event(), asyncio.Event()
        notices = []
        state = AgentState()
        state.middle_context[STATE_KEY] = turn.state
        state.permission_context.mode = PermissionMode.BYPASS
        bus = MagicMock(spec=MessageBus)
        bus.registry_get = AsyncMock(return_value=None)
        async def push(key, payload, **kwargs):
            if key == MessageBusKeys.inbox(state.session_id):
                notices.append(('notice', payload))
                done.set()
        async def drain(key, **kwargs):
            result = list(notices)
            notices.clear()
            return result
        bus.queue_push = AsyncMock(side_effect=push)
        bus.queue_drain = AsyncMock(side_effect=drain)
        manager = BackgroundTaskManager(message_bus=bus)
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        model = MockModel(context_size=1000000)
        model.set_responses([call('AnalysisPublish', **request)])
        def agent():
            return Agent(name='publication-test', model=model, state=state, system_prompt='test',
                         toolkit=toolkit, react_config=ReActConfig(max_iters=10),
                         middlewares=[InboxMiddleware(bus), ToolOffloadMiddleware(bg_manager=manager,
                             message_bus=bus, user_id=OWNER.user_id, agent_id=OWNER.agent_id, timeout_secs=0.01), middleware])
        toolkit = Toolkit(tools=await middleware.list_tools())
        async def review(*args):
            await gate.wait()
            return {'issues': []}
        with patch('analysis_extension.turn_completion._model_review', side_effect=review) as reviewer:
            first = [event async for event in agent().reply_stream(None, yield_final_msg=True)]
            ending = next(event for event in first if isinstance(event, ReplyEndEvent))
            self.assertEqual(ending.metadata['analysisCompletion']['status'], 'WAITING')
            self.assertEqual(middleware.service.turn.state['reasoningPerformance']['calls'], 1)
            self.assertFalse(any(event.get_text_content() for event in first if isinstance(event, Msg)))
            saved_state = state.model_dump(mode='json')
            gate.set()
            await asyncio.wait_for(done.wait(), 10)
            proof = progress(middleware.service.turn)['lastPublicationPreview']
            model.set_responses([call('AnalysisPublish', analysisRef=artifact['analysisRef'], repairRef=proof['previewRef'], repairs=[], requestKey='confirm-' + proof['previewRef'])])
            state = AgentState.model_validate(saved_state)
            middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize,
                                            rules=self.service.rules, delivery=self.service.delivery)
            toolkit = Toolkit(tools=await middleware.list_tools())
            # 确认发布很快，但机器负载也可能使其转后台；此轮同步执行以隔离审查等待场景。
            resumed = Agent(name='publication-test', model=model, state=state, system_prompt='test', toolkit=toolkit,
                            react_config=ReActConfig(max_iters=10), middlewares=[InboxMiddleware(bus), middleware])
            second = [event async for event in resumed.reply_stream(None, yield_final_msg=True)]
            final = next(event for event in second if isinstance(event, ReplyEndEvent))
            self.assertEqual(final.metadata['analysisCompletion']['status'], 'READY')
            self.assertEqual(final.metadata['analysisPerformance']['reasoning']['calls'], 2)
            self.assertEqual(final.metadata['analysisPerformance']['reviewTotals']['chargedAttempts'], 1)
            self.assertEqual(reviewer.await_count, 1)
            delivered = [event.get_text_content() for event in second if isinstance(event, Msg)]
            self.assertEqual(delivered, [middleware.service.turn.state['delivery']['markdown']])
