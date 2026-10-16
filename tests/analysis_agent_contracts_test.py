"""Failure recovery, partial delivery and declared visual-check coverage."""
from copy import deepcopy
import json
from unittest import TestCase
from jsonschema import Draft202012Validator

from analysis_document_checks_test import measurements
from analysis_fixtures import OWNER, fixture, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters.budget_v2 import receipt_from_capture
from analysis_extension.completion import completion_summary
from analysis_extension.document_checks import evaluate, require_publication_checks
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisInspect


class CaptureDiagnosticsTests(TestCase):
    def failed_capture(self, capture_state='error'):
        capture, _, _ = fixture()
        capture.update(captureRef='cap-failed', captureState=capture_state)
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt.update(status='FAILED', error={'code': 'FORM_SOURCE_UNAVAILABLE', 'category': 'DEPENDENCY',
                                               'retryable': True, 'recoveryAction': 'RETRY'})
        receipt['data']['stepResults'] = [
            {'stepId': 'lookup', 'rows': [{'key': 'child'}]},
            {'stepId': 'read-child', 'formResults': [{'status': 'SOURCE_UNAVAILABLE', 'disposition': 'FAILED', 'message': '来源读取失败'}]}]
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        return capture

    def test_failed_input_identifies_capture_and_step_without_losing_source_status(self):
        for capture_state, code in [('error', 'CAPTURE_INCOMPLETE'), ('success', 'EXECUTION_INCOMPLETE')]:
            capture = self.failed_capture(capture_state)
            before = deepcopy(capture)
            with self.assertRaises(AnalysisError) as caught:
                receipt_from_capture(capture)
            self.assertEqual(caught.exception.code, code)
            details = caught.exception.details
            self.assertEqual(details['captureRef'], 'cap-failed')
            self.assertEqual(details['failedSteps'][0]['stepId'], 'read-child')
            self.assertEqual(details['sourceError']['code'], 'FORM_SOURCE_UNAVAILABLE')
            Draft202012Validator(AnalysisInspect.input_schema).validate(details['nextRead'])
            self.assertEqual(capture, before)

    def test_missing_receipt_keeps_capture_error_and_actionable_reference(self):
        capture = self.failed_capture()
        capture['response']['content'] = []
        with self.assertRaises(AnalysisError) as caught:
            receipt_from_capture(capture)
        self.assertEqual(caught.exception.code, 'CAPTURE_INCOMPLETE')
        self.assertEqual(caught.exception.details['nextRead']['captureRef'], 'cap-failed')
        self.assertNotIn('sourceError', caught.exception.details)

    def test_malformed_failure_diagnostics_do_not_mask_original_capture_error(self):
        for data in [None, [], {'stepResults': [None, {'formResults': None}]}]:
            capture = self.failed_capture()
            receipt = json.loads(capture['response']['content'][0]['text'])
            receipt['data'] = data
            capture['response']['content'][0]['text'] = json.dumps(receipt)
            with self.assertRaises(AnalysisError) as caught:
                receipt_from_capture(capture)
            self.assertEqual(caught.exception.code, 'CAPTURE_INCOMPLETE')
            self.assertEqual(caught.exception.details['failedSteps'], [])

    def test_top_level_forms_are_included_and_error_messages_bounded(self):
        capture = self.failed_capture()
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['data']['formResults'] = [{'disposition': 'FAILED', 'status': 'FAILED', 'message': 'x' * 1000}]
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        with self.assertRaises(AnalysisError) as caught:
            receipt_from_capture(capture)
        details = caught.exception.details
        self.assertEqual(details['failedStepCount'], 2)
        self.assertIsNone(details['failedSteps'][1]['stepId'])
        self.assertEqual(len(details['failedSteps'][1]['message']), 500)


class InspectionRecoveryTests(PresentationCase):
    async def prepare(self):
        cap, selectors, scope = saved_fixture(self.store, ('12',))
        built = await self.service.build([cap], selectors, scope,
            [{'id': 'f', 'operator': 'FORMULA_CHECK'}, {'id': 's', 'operator': 'SORT'}], 'build')
        self.ref = built['analysisRef']
        self.fact = self.store.get_analysis(OWNER, self.ref)['facts'][0]['factId']

    async def test_derived_formula_reference_returns_executable_fact_call(self):
        await self.prepare()
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='formulas', analysisRef=self.ref, derivedId='derived_f')
        self.assertEqual(caught.exception.code, 'INVALID_INSPECT_ARGUMENT')
        suggestion = caught.exception.details['retryInspect']
        Draft202012Validator(AnalysisInspect.input_schema).validate(suggestion)
        self.assertEqual(suggestion['factRef'], self.fact)
        result = await self.service.inspect(**suggestion)
        self.assertEqual(result['targetFactRef'], self.fact)

    async def test_both_reference_kinds_rejected_and_explicit_fact_preserved(self):
        await self.prepare()
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='formulas', analysisRef=self.ref, factRef=self.fact, derivedId='derived_f')
        suggestion = caught.exception.details['retryInspect']
        self.assertNotIn('derivedId', suggestion)
        self.assertEqual(suggestion['factRef'], self.fact)

    async def test_missing_reference_guidance_does_not_guess_formula_target(self):
        await self.prepare()
        for section, required in [('formulas', 'factRef'), ('order', 'derivedId')]:
            with self.assertRaises(AnalysisError) as caught:
                await self.service.inspect(section=section, analysisRef=self.ref)
            details = caught.exception.details
            self.assertEqual(details['requiredField'], required)
            self.assertNotIn('retryInspect', details)
            Draft202012Validator(AnalysisInspect.input_schema).validate(details['nextRead'])
            await self.service.inspect(**details['nextRead'])

    async def test_nonformula_derived_reference_does_not_become_formula(self):
        await self.prepare()
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='formulas', analysisRef=self.ref, derivedId='derived_s')
        self.assertNotIn('retryInspect', caught.exception.details)

    async def test_partial_delivery_without_work_plan_stays_incomplete_in_all_readbacks(self):
        await self.prepare()
        reason = '一个对象的下级数值未取得，汇总核对尚未完成。'
        doc = {'title': '已取得的结果', 'html': '<html><head></head><body><p>{{v}}</p><p>{{analysisPartialReason}}</p></body></html>',
               'bindings': [{'key': 'v', 'valueRef': self.fact}]}
        preview = await self.service.publish(self.ref, requestKey='preview', expectedBaseRevision=0,
            document=doc, partialReason=reason, previewOnly=True, renderCheckUnavailableReason='本单元测试不调用浏览器')
        complete = preview['summary']['completion']
        self.assertEqual(complete['state'], 'INCOMPLETE')
        self.assertEqual(complete['declaredWorkState'], 'UNDECLARED')
        self.assertEqual(complete['partialReason'], reason)
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(published['summary']['completion'], complete)
        inspected = await self.service.inspect(section='summary', analysisRef=self.ref)
        self.assertEqual(inspected['summary']['completion'], complete)


class PartialCompletionTests(TestCase):
    def test_partial_scope_does_not_become_ready_when_declared_subset_finished(self):
        artifact = {'facts': [{'factId': 'fact', 'valueState': 'PRESENT'}], 'derivedValues': [], 'relations': [],
                    'plan': {'workPlan': [{'taskId': 'value', 'title': '已取值', 'kind': 'VALUES', 'refs': ['fact']}]},
                    'partialReason': '仍有另一项查询受阻'}
        result = completion_summary(artifact)
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertEqual(result['declaredWorkState'], 'READY')
        self.assertEqual(result['pendingTaskIds'], [])
        del artifact['partialReason']
        self.assertEqual(completion_summary(artifact)['state'], 'READY')


class NumericCoverageTests(TestCase):
    def evaluate(self, sample):
        sample['orders'] = []
        return evaluate({'contentDigest': 'frozen', 'datasets': {}}, sample)

    def test_no_marker_does_not_claim_ratio_or_count_pass(self):
        result = self.evaluate(measurements())
        self.assertEqual(result['status'], 'MEASURED')
        self.assertEqual(result['numericChecks']['counts']['status'], 'NOT_DECLARED')
        self.assertEqual(result['numericChecks']['linearBars']['status'], 'NOT_DECLARED')

    def test_hidden_and_single_bar_markers_do_not_claim_full_check(self):
        sample = measurements()
        sample['numericChecks']['counts']['declared'] = 1
        self.assertEqual(self.evaluate(sample)['numericChecks']['counts']['status'], 'NOT_MEASURED')
        bars = sample['numericChecks']['linearBars']
        bars.update(declared=1, measured=1, scaleGroups=1)
        self.assertEqual(self.evaluate(sample)['numericChecks']['linearBars']['status'], 'PARTIAL')
        bars.update(declared=2, measured=2, checkedGroups=1)
        self.assertEqual(self.evaluate(sample)['numericChecks']['linearBars']['status'], 'PASS')

    def test_ratio_failure_blocks_even_without_textual_error(self):
        sample = measurements()
        sample['numericChecks']['linearBars'].update(declared=3, measured=3, scaleGroups=1, checkedGroups=1, failedGroups=1)
        result = self.evaluate(sample)
        self.assertTrue(result['blocking'])
        with self.assertRaises(AnalysisError) as caught:
            require_publication_checks({}, [result], 'cannot inspect elsewhere')
        self.assertEqual(caught.exception.code, 'DOCUMENT_RENDER_ERRORS')

    def test_inconsistent_and_previous_measurement_contracts_rejected(self):
        mutations = [lambda s: s.update(version=2), lambda s: s.pop('numericChecks'),
                     lambda s: s['numericChecks']['counts'].update(measured=1),
                     lambda s: s['numericChecks']['linearBars'].update(declared=1, measured=1),
                     lambda s: s['numericChecks']['linearBars'].update(checkedGroups=1),
                     lambda s: s['numericChecks']['counts'].update(declared=True)]
        for mutate in mutations:
            sample = measurements(); mutate(sample)
            with self.assertRaises(AnalysisError): self.evaluate(sample)
