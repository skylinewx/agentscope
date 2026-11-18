"""Budget boundaries, full request accounting and per-task configuration isolation."""
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import analysis_fixtures
from analysis_extension.models import AnalysisError
from analysis_extension.review_budget import ReviewBudgetConfig, current_budget, use_budget, review_request


class ReviewBudgetTests(unittest.TestCase):
    def test_exact_boundary_and_one_character_over(self):
        request = review_request('系统', '数据', {'type': 'object'})
        base = ReviewBudgetConfig(output_tokens=10, safety_tokens=3)
        size = base.estimate(request)['requiredTokens']
        self.assertTrue(replace(base, context_window_tokens=size).require(request)['fits'])
        with self.assertRaises(AnalysisError) as raised:
            replace(base, context_window_tokens=size - 1).require(request)
        self.assertEqual(raised.exception.code, 'REVIEW_CAPACITY')
        self.assertFalse(raised.exception.details['requiresSourceQuery'])

    def test_coefficient_is_estimate_and_includes_prompt_and_schema(self):
        config = ReviewBudgetConfig(tokens_per_char=0.75)
        smaller = config.estimate(review_request('', '预算表', {}))
        complete = config.estimate(review_request('审核目标和约束', '预算表', {'required': ['issues']}))
        self.assertGreater(complete['inputChars'], smaller['inputChars'])
        self.assertEqual(complete['countMethod'], 'CHARACTER_COEFFICIENT_ESTIMATE')
        self.assertEqual(complete['outputReserveTokens'], config.output_tokens)

    def test_model_output_reserve_matches_effective_override(self):
        model = SimpleNamespace(parameters=SimpleNamespace(max_tokens=12345), context_size=6000)
        with patch.dict('os.environ', {}, clear=True):
            config = ReviewBudgetConfig.from_environment(model)
            self.assertEqual(config.output_tokens, 12345)
            self.assertNotEqual(config.context_window_tokens, model.context_size)
        with patch.dict('os.environ', {'ANALYSIS_REVIEW_OUTPUT_TOKENS': '2000'}, clear=True):
            self.assertEqual(ReviewBudgetConfig.from_environment(model).output_tokens, 2000)

    def test_invalid_configuration_does_not_silently_fall_back(self):
        for changed in ({'tokens_per_char': 0}, {'tokens_per_char': float('nan')},
                        {'tokens_per_char': True}, {'output_tokens': -1},
                        {'context_window_tokens': 100}, {'safety_tokens': -1}):
            with self.assertRaises(AnalysisError): ReviewBudgetConfig(**changed)

    def test_complete_cement_sized_material_fits_current_configured_profile(self):
        config = ReviewBudgetConfig()
        self.assertTrue(config.require(review_request('核对完整数据与代码', 'x' * 207737, {'type': 'object'}))['fits'])


class ReviewBudgetIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_review_profiles_do_not_share_capacity(self):
        async def check(coefficient):
            with use_budget(ReviewBudgetConfig(tokens_per_char=coefficient)):
                await asyncio.sleep(0)
                return current_budget().tokens_per_char
        self.assertEqual(await asyncio.gather(check(0.5), check(1.5)), [0.5, 1.5])
