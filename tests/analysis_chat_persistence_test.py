"""Completion verdicts through real ChatService, storage and history HTTP.

Only model output and semantic-review verdicts are deterministic substitutes.
Analysis publishing, inspection, middleware, event aggregation and storage are
the production implementations. Both storage fixtures use bypass permission.
"""
import asyncio
from copy import deepcopy
from functools import partial
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import analysis_turn_completion_test as analysis_fixtures
import shared_agent_defaults_test as app_fixtures
from analysis_extension.middleware import AnalysisMiddleware
from analysis_fixtures import OWNER
from agentscope.app._service import _chat
from agentscope.message import UserMsg
from agentscope.permission import PermissionMode
from utils import MockModel


class AnalysisChatPersistenceTest(TestCase):
    def check_history(self, scenario):
        for cls in (app_fixtures.SharedDefaultsRedisTest,
                    app_fixtures.SharedDefaultsSQLTest):
            with self.subTest(storage=cls.__name__, scenario=scenario):
                app, analysis = cls(), analysis_fixtures.TurnCompletionTests()
                # This async fixture is used outside unittest's own run loop;
                # initialize its runner so registered cleanups are executed.
                analysis._setupAsyncioRunner()
                try:
                    app.setUp()
                    analysis.setUp()
                    artifact = app.portal.call(analysis.generic_result, analysis.turn())
                    ref = artifact['analysisRef']
                    sid = app.create(name='Completion history regression')
                    self.assertEqual(app.read(sid).state.permission_context.mode,
                                     PermissionMode.BYPASS)
                    middleware = AnalysisMiddleware(
                        analysis.store, OWNER, [], analysis.service.authorize,
                        delivery=analysis.delivery,
                    )
                    approved = {'status': 'READY', 'analysisRef': ref,
                                'semanticReview': 'PASS', 'issues': []}
                    rejected = {'status': 'INCOMPLETE', 'analysisRef': ref,
                                'semanticReview': 'REVISE',
                                'issues': [{'code': 'ANSWER_MISMATCH', 'message': 'Correct the draft'}]}
                    checks = ([rejected] * 4 if scenario == 'rejected' else
                              [rejected, approved] if scenario in ('repaired', 'model_error') else [approved])
                    middleware._check_end = AsyncMock(side_effect=checks)

                    async def factory(user, agent, session):
                        return [middleware]

                    service = app.app.state.chat_service
                    service._extra_agent_middlewares = factory
                    service._middlewares_take_workspace = False
                    responses = [
                        analysis_fixtures.call('AnalysisTask', action='declare',
                                               mode='ANALYSIS', goal='Explain fixture readings'),
                        analysis_fixtures.call('AnalysisInspect', analysisRef=ref, section='delivery'),
                    ]
                    if scenario == 'repaired':
                        responses += [analysis_fixtures.text('Draft'), analysis_fixtures.text('Corrected answer')]
                    elif scenario == 'rejected':
                        responses += [analysis_fixtures.text('Uncorrected draft') for _ in range(4)]
                    elif scenario == 'model_error':
                        responses += [analysis_fixtures.text('Draft'), RuntimeError('fixture model failed')]
                    elif scenario == 'interrupted':
                        responses.append(asyncio.CancelledError())
                    elif scenario == 'iteration_limit':
                        app.agent.data.chat_config.react_config.max_iters = 2
                        app.portal.call(app.storage.upsert_agent, 'owner', app.agent)
                        responses.append(analysis_fixtures.text('Iteration limit reached'))
                    else:
                        responses.append(analysis_fixtures.text('Final answer'))

                    async def model_for(viewer, config, access):
                        await access.resolve_credential(viewer, config.credential_id)
                        model = MockModel(context_size=1000000)
                        model.set_responses(responses)
                        return model

                    ends, published_events = [], []
                    original_publish = _chat.publish_session_event

                    async def publish(bus, session, event):
                        published_events.append(deepcopy(event))
                        if event.get('type') == 'REPLY_END':
                            ends.append(deepcopy(event))
                        return await original_publish(bus, session, event)

                    with patch('agentscope.app._service._chat.get_model', side_effect=model_for), \
                         patch('agentscope.app._service._chat.publish_session_event', side_effect=publish):
                        app.portal.call(partial(service.run, user_id='viewer', agent_id='shared',
                                                session_id=sid, input_msg=UserMsg('viewer', 'Explain the readings')))
                    self.assertEqual(len(ends), 1)
                    response = app.client.get(f'/sessions/{sid}/messages',
                                              params={'agent_id': 'shared'}, headers=app.headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    history = response.json()['messages'][-1]
                    expected_answer = ['Corrected answer'] if scenario == 'repaired' else ['Final answer'] if scenario == 'first_pass' else []
                    if scenario == 'rejected':
                        expected_answer = ['本次处理尚未完成。现有结果仍有未完成的计算或证据核对，暂不能作为最终结论。']
                        self.assertTrue(history['metadata']['presentation']['terminal_summary'])
                    self.assertEqual([block['text'] for block in history['content'] if block['type'] == 'text'], expected_answer)
                    self.assertEqual(''.join(event.get('delta', '') for event in published_events
                                            if event['type'] == 'TEXT_BLOCK_DELTA'), ''.join(expected_answer))
                    self.assertEqual(history['metadata'], ends[-1]['metadata'])
                    self.assertEqual(history['finished_reason'], ends[-1]['finished_reason'])
                    completion = history['metadata'].get('analysisCompletion')
                    if completion is not None:
                        self.assertRegex(completion['turnKey'], r'^[0-9a-f]{64}$')
                        completion = {key: value for key, value in completion.items() if key != 'turnKey'}
                    if scenario in ('interrupted', 'iteration_limit'):
                        self.assertEqual(completion['status'], 'INCOMPLETE')
                        self.assertEqual(completion['reason'],
                                         'interrupted' if scenario == 'interrupted' else 'ITERATION_LIMIT')
                    elif scenario == 'rejected':
                        self.assertEqual(completion, {key: rejected[key] for key in
                                                      ('status', 'analysisRef', 'semanticReview')})
                        self.assertEqual(history['error']['type'], 'completion_check')
                    elif scenario == 'model_error':
                        self.assertIsNone(completion)
                        self.assertEqual(history['finished_reason'], 'error')
                        self.assertTrue(any(block['type'] == 'hint' and
                                            'Completion check' in block.get('source', '')
                                            for block in history['content']))
                    else:
                        self.assertEqual(completion, {key: approved[key] for key in
                                                      ('status', 'analysisRef', 'semanticReview')})
                    receipts = [block for block in history['content'] if
                                block['type'] == 'tool_result' and block['name'] == 'AnalysisInspect']
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0]['state'], 'success')
                    self.assertEqual(receipts[0]['metadata']['process_summary']['kind'], 'analysis')
                finally:
                    analysis.doCleanups()
                    analysis._tearDownAsyncioRunner()
                    app.doCleanups()

    def test_first_approval_survives_history(self):
        self.check_history('first_pass')

    def test_approval_after_repair_survives_history(self):
        self.check_history('repaired')

    def test_interruption_after_delivery_survives_history(self):
        self.check_history('interrupted')

    def test_iteration_limit_after_delivery_survives_history(self):
        self.check_history('iteration_limit')

    def test_rejection_after_repair_limit_survives_history(self):
        self.check_history('rejected')

    def test_model_error_after_rejection_keeps_the_hint_without_false_approval(self):
        self.check_history('model_error')
