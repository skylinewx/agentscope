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
        patch_refs = {'repairs': {'1': [], '0': [1]}}
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
        patch_schema = agent.model.generate_structured_output.call_args.kwargs['structured_model']['properties']['repairs']
        self.assertEqual(patch_schema['properties']['0']['maxItems'], 12)
        self.assertEqual(patch_schema['properties']['1']['maxItems'], 11)
        self.assertCountEqual(patch_schema['required'], ['0', '1'])
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

    async def test_sdk_accepts_parsed_overflow_then_host_repairs_only_citations(self):
        from jsonschema import validate
        source = {'facts': [{'factId': f'fact_{i}', 'recordTable': '费用表', 'value': str(i)}
                            for i in range(1, 15)]}
        refs = [f'表1第{i}行' for i in range(1, 14)]
        verdict = {'issues': [], 'coverage': [{'requirement': '核对金额', 'status': 'COVERED',
                                               'location': '金额明细', 'evidenceRefs': refs}]}
        replies = iter([verdict, {'repairs': {'0': [2, 10]}}])
        async def sdk(messages, *, structured_model, max_tokens):
            response = next(replies)
            validate(response, structured_model)
            return SimpleNamespace(content=response, usage=None)
        agent = self.model(None)
        agent.model.generate_structured_output.side_effect = sdk
        result = await call_review_model(self.turn(), lambda: _model_review(agent, REVIEW_PROMPT, source),
            phase='DOCUMENT', input_chars=0, round_started=time.perf_counter(), material_key='overflow')
        self.assertEqual(result['coverage'][0], {**verdict['coverage'][0], 'evidenceRefs': ['fact_2', 'fact_10']})
        self.assertEqual(agent.model.generate_structured_output.await_count, 2)

    async def test_merged_limit_counts_retained_refs_across_all_source_types(self):
        from jsonschema import ValidationError, validate
        from analysis_extension.review_output import output_correction, correction_request
        from analysis_extension.review_request import make_review_request
        from analysis_extension.turn_completion import ANALYSIS_REVIEW_SCHEMA
        refs = [f'表1第{i}行' for i in range(1, 12)] + ['不存在的位置']
        row = {'requirement': '金额', 'status': 'COVERED', 'location': '明细', 'evidenceRefs': refs}
        req = make_review_request({'facts': [{'factId': f'f{i}', 'value': str(i)} for i in range(1, 15)],
                                   'candidateAnswer': '原报告正文的唯一标记'})
        token = output_correction.set({'message': '核对位置', 'indexes': [0], 'invalidPositions': {0: [11]},
                                       'verdict': {'coverage': [row], 'issues': []}})
        try:
            material, schema = correction_request(req, ANALYSIS_REVIEW_SCHEMA)
            self.assertNotIn('原报告正文的唯一标记', material)
            self.assertIn('14', material)
            with self.assertRaises(ValidationError):
                validate({'repairs': {'0': [12, 13]}}, schema)
            with self.assertRaises(ValidationError):
                validate({'repairs': {'0': [15]}}, schema)
            validate({'repairs': {'0': []}}, schema)
        finally:
            output_correction.reset(token)
