"""Longer analysis budgets reach execution without disabling expiry or cancellation."""
import asyncio
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import validate

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.query_waiting import wait_limit
from analysis_extension.review_budget import ReviewBudgetConfig
from analysis_extension.review_control import call_review_model, closeout_reason, record_repair
from analysis_extension.service import AnalysisService
from analysis_extension.tools import AnalysisPython


class AnalysisTimeLimitsTests(PresentationCase):
    def turn(self, key='time-budget'):
        return SimpleNamespace(service=self.service,
                               state={'turnKey': key, 'request': '分析', 'goal': '分析'})

    def test_work_after_first_repair_survives_old_deadline_and_still_expires(self):
        with patch('analysis_extension.review_control.time.time', return_value=1000):
            record_repair(self.turn(), 'first', 'INVALID_SELECTOR')
        # Recreate the turn to verify the persisted repair clock is respected.
        with patch('analysis_extension.review_control.time.time', return_value=1301):
            self.assertIsNone(closeout_reason(self.turn()))
        with patch('analysis_extension.review_control.time.time', return_value=4599):
            self.assertIsNone(closeout_reason(self.turn()))
        with patch('analysis_extension.review_control.time.time', return_value=4601):
            self.assertEqual(closeout_reason(self.turn()), 'REPAIR_TIME_LIMIT')

    def test_deployment_overrides_are_validated_and_applied_to_service(self):
        with patch.dict('os.environ', {'ANALYSIS_REVIEW_TOTAL_SECONDS': '2400',
                                      'ANALYSIS_REPAIR_TOTAL_SECONDS': '7200'}):
            service = AnalysisService(self.store, OWNER, self.service.authorize)
        self.assertEqual(service.store.limits.review_budget_seconds, 2400)
        self.assertEqual(service.store.limits.repair_budget_seconds, 7200)
        for key in ('ANALYSIS_REVIEW_TOTAL_SECONDS', 'ANALYSIS_REPAIR_TOTAL_SECONDS'):
            for value in ('0', '-1', 'not-a-number'):
                with self.subTest(key=key, value=value), patch.dict('os.environ', {key: value}):
                    with self.assertRaises(AnalysisError) as caught:
                        AnalysisService(self.store, OWNER, self.service.authorize)
                    self.assertEqual(caught.exception.code, 'REVIEW_CONFIG')

    async def test_review_continues_after_old_total_limit_and_reserves_final_call(self):
        async def completed():
            return {'issues': []}
        real_timeout = asyncio.timeout
        with patch('analysis_extension.review_control.asyncio.timeout', wraps=real_timeout) as timeout:
            result = await call_review_model(self.turn(), completed, phase='CALCULATION',
                                            input_chars=10, round_started=time.perf_counter() - 500)
        self.assertEqual(result, {'issues': []})
        self.assertEqual(timeout.call_args.args, (600,))
        with patch('analysis_extension.review_control.asyncio.timeout', wraps=real_timeout) as timeout:
            await call_review_model(self.turn('final-stage'), completed, phase='CALCULATION',
                                    input_chars=10, round_started=time.perf_counter() - 1100)
        allowance = timeout.call_args.args[0]
        self.assertGreater(allowance, 95)
        self.assertLessEqual(allowance, 100)  # 1800 total - 1100 spent - 600 final reserve.

    async def test_review_can_still_be_cancelled_without_waiting_for_long_deadline(self):
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(call_review_model(self.turn(), blocked, phase='DOCUMENT',
                                  input_chars=10, round_started=time.perf_counter()))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_time_tuning_does_not_invalidate_completed_review_identity(self):
        config = ReviewBudgetConfig()
        self.assertEqual(config.semantic_identity(), replace(config, call_timeout_seconds=1200,
                                                             final_reserve_seconds=900).semantic_identity())

    def test_query_wait_covers_execution_and_material_multiplier(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(wait_limit({}), 1890)
            self.assertEqual(wait_limit({'purpose': 'PREPARE_MATERIALS'}), 3690)
        with patch.dict('os.environ', {'BUDGET_QUERY_ACTIVE_SECONDS': '2400'}):
            self.assertEqual(wait_limit({'purpose': 'QUERY'}), 2490)
            self.assertEqual(wait_limit({'purpose': 'PREPARE_MATERIALS'}), 4890)

    async def test_python_schema_and_execution_accept_longer_budget_and_keep_upper_bound(self):
        cap, selectors, scope = saved_fixture(self.store, ('1.005',))
        built = await self.service.build([cap], selectors, scope, [], 'input')
        arguments = dict(inputSetRef=built['inputSetRef'], code='result=[{"name":"rounded","label":"舍入结果","unit":"元",'
                         '"value":D(inputs[0]["value"]).quantize(D(".01")),"inputRefs":[inputs[0]["ref"]]}]',
                         purpose='验证精确舍入', fallbackReason='测试自定义计算', requestKey='long', timeoutSeconds=120)
        validate(arguments, AnalysisPython.input_schema)
        result = await self.service.python(**arguments)
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertEqual(run['timeoutSeconds'], 120)
        self.assertEqual(result['derivedValues'][0]['value'], '1.01')
        default = await self.service.python(**{k: v for k, v in {**arguments, 'requestKey': 'default',
                                               'code': arguments['code'].replace('rounded', 'rounded_default')}.items()
                                              if k != 'timeoutSeconds'})
        self.assertEqual(self.store.get_python_run(OWNER, default['pythonRunRef'])['timeoutSeconds'], 60)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(**{**arguments, 'requestKey': 'too-long', 'timeoutSeconds': 301})
        self.assertEqual(caught.exception.code, 'PYTHON_TIMEOUT_LIMIT')
        self.assertIn('300', str(caught.exception))
