"""交付投影不得丢失主体、未取值成员或实际执行依赖。"""
from copy import deepcopy
from unittest import TestCase

from analysis_extension.review_projection import project_analysis_review


class ReviewProjectionTests(TestCase):
    def fixture(self):
        return {'facts': [{'factId': 'present', 'value': '1'}, {'factId': 'missing', 'value': None}],
                'scope': {'members': ['a', 'b']}, 'completion': {'pendingTaskIds': ['pending']},
                'toolFailures': [{'code': 'SOURCE_FAILED'}],
                'documentBindings': {'bindings': [{'valueRef': 'shown'}]},
                'derived': [
                    {'derivedId': 'shown', 'outputKind': 'TABLE', 'executionStatus': 'COMPLETED', 'inputRefs': ['middle'], 'pythonRunRef': 'run'},
                    {'derivedId': 'middle', 'outputKind': 'TABLE', 'executionStatus': 'COMPLETED', 'inputRefs': ['present', 'missing']},
                    {'derivedId': 'unused', 'outputKind': 'TABLE', 'executionStatus': 'COMPLETED', 'pythonRunRef': 'unused-run'},
                    {'derivedId': 'failed', 'outputKind': 'TABLE', 'executionStatus': 'FAILED', 'diagnostics': ['缺数']},
                    {'derivedId': 'diagnostic', 'outputKind': 'DIAGNOSTIC', 'executionStatus': 'COMPLETED', 'data': {'value': None}}],
                'pythonRuns': [{'runRef': 'run', 'inputRefs': ['middle'], 'inputRecords': [{'ref': 'missing', 'value': None}]},
                               {'runRef': 'unused-run', 'inputRefs': []}]}

    def test_dependency_closure_preserves_population_null_and_diagnostics(self):
        original = self.fixture()
        before = deepcopy(original)
        result = project_analysis_review(original)
        self.assertEqual([item['derivedId'] for item in result['derived']], ['shown', 'middle', 'failed', 'diagnostic'])
        self.assertEqual(result['pythonRuns'], original['pythonRuns'][:1])
        for field in ('facts', 'scope', 'completion', 'toolFailures'):
            self.assertEqual(result[field], original[field])
        self.assertEqual(original, before)
        self.assertEqual(result['reviewProjection']['omittedExploratoryTableCount'], 1)

    def test_actual_python_inputs_and_work_items_are_roots(self):
        original = self.fixture()
        original['pythonRuns'][0]['inputRefs'].append('unused')
        projected = project_analysis_review(original)
        self.assertEqual(projected['reviewTargets'], ['shown'])
        self.assertEqual({key:value for key,value in projected.items() if key != 'reviewTargets'}, original)
        original['pythonRuns'][0]['inputRefs'].remove('unused')
        original['completion']['items'] = [{'ref': 'unused'}]
        projected = project_analysis_review(original)
        self.assertEqual(projected['reviewTargets'], ['shown', 'unused'])
        self.assertEqual({key: value for key, value in projected.items() if key != 'reviewTargets'}, original)

    def test_no_declared_delivery_references_does_not_guess(self):
        original = self.fixture()
        original.pop('documentBindings')
        self.assertEqual(project_analysis_review(original), original)

    def test_explicit_replacement_drops_unused_old_diagnostic_but_keeps_downstream_dependencies(self):
        original = self.fixture()
        original['derived'][-1].update(operator='PYTHON', pythonRunRef='old-run')
        original['pythonRuns'] += [{'runRef': 'old-run', 'status': 'COMPLETED'}]
        original['pythonRuns'][0].update(status='COMPLETED', supersedesRunRefs=['old-run'])
        projected = project_analysis_review(original)
        self.assertNotIn('diagnostic', [item['derivedId'] for item in projected['derived']])
        self.assertTrue(projected['reviewProjection']['supersededRuns'])
        original['derived'][0]['inputRefs'].append('diagnostic')
        projected = project_analysis_review(original)
        self.assertIn('diagnostic', [item['derivedId'] for item in projected['derived']])
        self.assertIn('old-run', [item['runRef'] for item in projected['pythonRuns']])
