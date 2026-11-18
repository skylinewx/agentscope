"""Actual usage, host deadlines and schema failures are distinct observations."""
import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agentscope.model import ChatUsage
from copy import deepcopy
from analysis_extension.models import AnalysisError
from analysis_extension.review_control import call_review_model, progress, reserve_review
from analysis_extension.review_output import ReviewOutputUnavailable
from analysis_extension.turn_completion import _model_review, review_answer, REVIEW_PROMPT
from analysis_presentation_fixtures import PresentationCase
import analysis_closeout_recovery_test as recovery_fixtures


class ReviewTelemetryTests(PresentationCase):
    turn = recovery_fixtures.CloseoutRecoveryTests.turn
    observe = recovery_fixtures.CloseoutRecoveryTests.observe

    def model(self, content, usage=None):
        return SimpleNamespace(model=SimpleNamespace(generate_structured_output=AsyncMock(
            return_value=SimpleNamespace(content=content, usage=usage))),
            state=SimpleNamespace(context=[], reply_id='reply'))

    async def test_records_actual_usage_without_guessing_network_or_first_response(self):
        turn = self.turn()
        verdict = {'issues': [], 'coverage': [{'requirement': '本阶段核对', 'status': 'COVERED', 'location': '材料', 'evidenceRefs': []}]}
        agent = self.model(verdict, ChatUsage(123, 9, .01, cache_input_tokens=100,
                                                    metadata={'private': 'do not expose'}))
        result = await call_review_model(turn, lambda: _model_review(agent, REVIEW_PROMPT, '{}'),
            phase='DOCUMENT', input_chars=2, round_started=time.perf_counter())
        self.assertEqual(result, verdict)
        call, = progress(turn)['reviewModelCalls']
        self.assertEqual(call['tokenUsage']['inputTokens'], 123)
        self.assertEqual(call['tokenUsage']['outputTokens'], 9)
        self.assertEqual(call['tokenUsage']['cachedInputTokens'], 100)
        self.assertNotIn('private', str(call))
        self.assertIsNone(call['firstResponseMs'])
        self.assertEqual(call['networkTiming'], 'UNKNOWN')

    async def test_review_output_exhaustion_keeps_report_and_releases_the_business_round(self):
        turn = self.turn()
        agent = self.model({'wrong': 'schema'}, ChatUsage(23, 4, .01))
        artifact = await recovery_fixtures.CloseoutRecoveryTests.published(self, turn)
        with self.assertRaises(ReviewOutputUnavailable):
            await review_answer(agent, turn, artifact, '')
        saved = progress(turn)
        self.assertEqual([c['retryReason'] for c in saved['reviewModelCalls']], [None, 'REVIEW_OUTPUT', 'REVIEW_OUTPUT'])
        self.assertTrue(all(c['failureKind'] == 'MODEL_OUTPUT_SCHEMA' for c in saved['reviewModelCalls']))
        self.assertEqual(saved['review']['attempts'], 0)
        self.assertEqual(saved['reviewTotals']['chargedAttempts'], 0)
        self.assertGreater(saved['review']['elapsedMs'], 0)
        self.assertEqual(saved['closeoutReason'], 'REVIEW_UNAVAILABLE')
        self.assertEqual(agent.model.generate_structured_output.await_count, 3)

    async def test_fifth_round_repairs_only_evidence_refs_without_changing_business_conclusions(self):
        turn = self.turn()
        for _ in range(5):
            reserve_review(turn)
        invalid = {'issues': [], 'coverage': [
            {'requirement': '核对金额', 'status': 'COVERED', 'location': '原文金额', 'evidenceRefs': ['表1']},
            {'requirement': '核对范围', 'status': 'SOURCE_LIMITED', 'location': '范围说明', 'evidenceRefs': ['表1第1行', '查询范围与选择条件']}]}
        patch_refs = {'repairs': [{'index': 1, 'sources': {}}, {'index': 0, 'sources': {'表1': [1]}}]}
        agent = self.model(None)
        agent.model.generate_structured_output.side_effect = [
            SimpleNamespace(content=data, usage=None) for data in (invalid, patch_refs)]
        payload = {'facts': [{'factId': 'source_fact', 'recordTable': '预算明细', 'value': '8', 'unit': '元'}]}
        result = await call_review_model(turn, lambda: _model_review(agent, REVIEW_PROMPT, payload),
            phase='DOCUMENT', input_chars=200, round_started=time.perf_counter(), material_key='frozen')
        expected = deepcopy(invalid)
        expected['coverage'][0]['evidenceRefs'] = ['source_fact']
        expected['coverage'][1]['evidenceRefs'] = ['source_fact']
        self.assertEqual(result, expected)
        item = agent.model.generate_structured_output.call_args.kwargs['structured_model']['properties']['repairs']['items']
        self.assertEqual(item['properties']['sources']['properties']['表1']['items'], {'type': 'integer', 'minimum': 1, 'maximum': 1})
        self.assertEqual(progress(turn)['review']['attempts'], 5)
        self.assertEqual(progress(turn)['reviewOutputRepairs'], {'frozen': 1})
        with self.assertRaisesRegex(AnalysisError, '限额'):
            reserve_review(turn)

    async def test_host_deadline_is_not_reported_as_network_timeout(self):
        turn = self.turn()
        self.store.limits = replace(self.store.limits, review_budget_seconds=1)
        async def slow():
            await asyncio.sleep(2)
        with self.assertRaises(AnalysisError):
            await call_review_model(turn, slow, phase='DOCUMENT', input_chars=5,
                                    round_started=time.perf_counter())
        call, = progress(turn)['reviewModelCalls']
        self.assertEqual(call['failureKind'], 'HOST_WAIT_TIMEOUT')
        self.assertIsNone(call['tokenUsage'])
        self.assertFalse(call['retry'])
