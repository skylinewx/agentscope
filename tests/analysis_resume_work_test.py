"""续查结构证据、混合工作项与公式摘要的通用边界。"""
from copy import deepcopy
import json
from unittest import TestCase

from analysis_fixtures import fixture
from analysis_extension.adapters.budget_v2 import resolve_form_requests
from analysis_extension.analysis_inputs import structure_evidence
from analysis_extension.completion import completion_summary
from analysis_extension.models import AnalysisError
from analysis_extension.native_analysis import formula_check_summary
from analysis_extension.result_projection import project_results


def resumed_fixture():
    original, selectors, _ = fixture(('10',))
    original['captureRef'] = 'first'
    receipt = json.loads(original['response']['content'][0]['text'])
    receipt.update(status='PARTIAL', resumeRef=receipt['executionId'])
    original['response']['content'][0]['text'] = json.dumps(receipt)
    resumed = deepcopy(original)
    resumed['captureRef'] = 'last'
    resumed['request'] = {'contextId': receipt['context']['contextId'],
                          'querySessionId': receipt['querySessionId'], 'resumeRef': receipt['executionId']}
    receipt.update(executionId='continued', continuationOf=receipt['executionId'], status='SUCCEEDED', resumeRef=None)
    resumed['response']['content'][0]['text'] = json.dumps(receipt)
    selection = {**selectors[0], 'captureRef': 'last', 'role': 'STRUCTURE'}
    return {'first': original, 'last': resumed}, selection


class ResumeStructureTests(TestCase):
    def test_structure_resume_keeps_actual_values_formulas_and_original_capture(self):
        captures, selection = resumed_fixture()
        before = deepcopy(captures)
        result = structure_evidence(captures, selection)
        self.assertEqual(json.loads(result[0]['cells'][0]['rawValueJson']), '10')
        self.assertEqual(result[0]['captureRef'], 'last')
        self.assertEqual(captures, before)
        self.assertNotIn('plan', captures['last']['request'])

    def test_missing_or_ambiguous_parent_is_not_guessed(self):
        captures, selection = resumed_fixture()
        for candidates in ({'last': captures['last']}, {**captures, 'duplicate': deepcopy(captures['first'])}):
            with self.subTest(refs=list(candidates)), self.assertRaises(AnalysisError) as error:
                structure_evidence(candidates, selection)
            self.assertEqual(error.exception.code, 'RESUME_UNPROVEN')

    def test_wrong_link_context_and_connection_are_rejected(self):
        for field in ('continuationOf', 'querySessionId', 'contextId', 'connectionFingerprint'):
            captures, selection = resumed_fixture()
            tail = captures['last']
            receipt = json.loads(tail['response']['content'][0]['text'])
            if field == 'contextId':
                receipt['context']['contextId'] = tail['request']['contextId'] = 'another-context'
            elif field == 'connectionFingerprint':
                tail[field] = 'another-connection'
            elif field == 'querySessionId':
                receipt[field] = tail['request'][field] = 'another-query'
            else:
                receipt[field] = 'wrong-link'
            tail['response']['content'][0]['text'] = json.dumps(receipt)
            with self.subTest(field=field), self.assertRaises(AnalysisError) as error:
                structure_evidence(captures, selection)
            self.assertEqual(error.exception.code, 'RESUME_UNPROVEN')

    def test_resume_cannot_substitute_plan(self):
        captures, _ = resumed_fixture()
        captures['last']['request']['plan'] = {'steps': []}
        with self.assertRaises(AnalysisError) as error:
            resolve_form_requests(captures)
        self.assertEqual(error.exception.code, 'RESUME_UNPROVEN')

    def test_multihop_resume_and_cycle(self):
        captures, selection = resumed_fixture()
        middle = captures['last']
        receipt = json.loads(middle['response']['content'][0]['text'])
        receipt.update(status='PARTIAL', resumeRef=receipt['executionId'])
        middle['response']['content'][0]['text'] = json.dumps(receipt)
        final = deepcopy(middle)
        final['captureRef'] = 'final'
        final['request']['resumeRef'] = receipt['executionId']
        receipt.update(executionId='final-execution', continuationOf='continued', status='SUCCEEDED', resumeRef=None)
        final['response']['content'][0]['text'] = json.dumps(receipt)
        captures['final'] = final
        self.assertEqual(len(structure_evidence(captures, {**selection, 'captureRef': 'final'})), 1)
        root = captures['first']
        root_receipt = json.loads(root['response']['content'][0]['text'])
        root['request']['resumeRef'] = 'continued'
        root_receipt['continuationOf'] = 'continued'
        root['response']['content'][0]['text'] = json.dumps(root_receipt)
        with self.assertRaises(AnalysisError) as error:
            resolve_form_requests(captures)
        self.assertEqual(error.exception.code, 'CYCLIC_RESUME')

    def test_unrelated_failed_source_does_not_pollute_structure(self):
        captures, selection = resumed_fixture()
        captures['unrelated'] = {'resourceId': 'another-source', 'captureState': 'FAILED'}
        self.assertEqual(len(structure_evidence(captures, selection)), 1)


class MixedCheckWorkTests(TestCase):
    def artifact(self, operator='FORMULA_CHECK'):
        def result(name, operation, inputs):
            return {'derivedId': name, 'operator': operation, 'inputRefs': inputs,
                    'executionStatus': 'COMPLETED', 'calculationValidation': 'PASS'}
        return {'facts': [{'factId': 'a'}, {'factId': 'b'}], 'relations': [],
                'derivedValues': [result('check', operator, ['a']), result('delta', 'ARITHMETIC', ['a', 'b'])],
                'plan': {'workPlan': [{'taskId': 'formula', 'title': '核对目标并解释差额', 'kind': 'COMPUTATION',
                                      'operator': operator, 'targetRefs': ['a'], 'refs': ['check', 'delta']}]}}

    def test_completed_check_can_include_supporting_calculation(self):
        for operator in ('FORMULA_CHECK', 'RECONCILE'):
            with self.subTest(operator=operator):
                summary = completion_summary(self.artifact(operator))
                self.assertEqual(summary['state'], 'READY')
                self.assertTrue(summary['items'][0]['items'][1]['supportingComputation'])

    def test_supporting_calculation_never_substitutes_target_check(self):
        artifact = self.artifact()
        artifact['plan']['workPlan'][0]['targetRefs'] = ['a', 'b']
        summary = completion_summary(artifact)
        self.assertEqual(summary['state'], 'INCOMPLETE')
        self.assertEqual(summary['items'][0]['missingTargetRefs'], ['b'])
        artifact['plan']['workPlan'][0]['refs'] = ['delta']
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')

    def test_failed_support_or_failed_check_remains_incomplete(self):
        for index in (0, 1):
            artifact = self.artifact()
            artifact['derivedValues'][index]['calculationValidation'] = 'BLOCKED'
            self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')

    def test_known_missing_source_completes_check_without_numeric_pass(self):
        artifact = self.artifact()
        check = artifact['derivedValues'][0]
        check.update(calculationValidation='BLOCKED', completionReason='SOURCE_VALUE_MISSING')
        self.assertEqual(completion_summary(artifact)['state'], 'READY')
        self.assertEqual(check['calculationValidation'], 'BLOCKED')


class FormulaProjectionTests(TestCase):
    def test_same_metric_at_different_dimension_positions_stays_distinct(self):
        from analysis_extension.native_analysis import document_inputs
        facts = [{'factId': name, 'objectLabel': 'same-metric', 'headers': ['指标'],
                  'dimensions': {'ENTITY': name, 'PERIOD': 'year'}} for name in ('a', 'b')]
        values = [{'derivedId': 'check-' + fact['factId'], 'operator': 'FORMULA_CHECK',
                   'formulaTrace': {'targetFactRef': fact['factId'], 'status': 'BLOCKED',
                                    'targetLocalCheck': {'status': 'MATCH'},
                                    'nodes': [{'factRef': fact['factId'], 'expression': 'A1',
                                               'localCheck': {'status': 'MATCH'}}]}} for fact in facts]
        directory = document_inputs({'facts': facts, 'derivedValues': values})
        self.assertEqual([item['targetContext']['dimensions']['ENTITY'] for item in directory], ['a', 'b'])

    def test_summary_separates_source_local_recursive_and_difference_direction(self):
        nodes = [{'factRef': 'a', 'expression': 'B1', 'observed': '10', 'status': 'BLOCKED',
                  'localCheck': {'status': 'MATCH', 'value': '10', 'difference': '0'}},
                 {'factRef': 'b', 'expression': 'C1+D1', 'observed': '10', 'status': 'BLOCKED',
                  'localCheck': {'status': 'MISMATCH', 'value': '12', 'difference': '-2'}}]
        summary = formula_check_summary({'nodes': nodes})
        self.assertTrue(summary['allLocalChecksCompleted'])
        self.assertFalse(summary['allLocalChecksMatch'])
        self.assertEqual(summary['exceptions'][0]['sourceValue'], '10')
        self.assertEqual(summary['exceptions'][0]['localDifference'], '-2')
        self.assertEqual(summary['exceptions'][0]['recursiveStatus'], 'BLOCKED')
        self.assertIn('来源值减本层复算值', summary['differenceMeaning'])
        value = {'derivedId': 'check', 'operator': 'FORMULA_CHECK', 'formulaTrace': {'nodes': nodes, 'targetFactRef': 'a'},
                 'executionStatus': 'COMPLETED', 'calculationValidation': 'PASS'}
        projected, _, _ = project_results([value], [value], 'analysis:1')
        node = projected[0]['formulaTrace']['nodes'][0]
        self.assertEqual(node['recursiveStatus'], 'BLOCKED')
        self.assertNotIn('status', node)
        self.assertEqual(node['localCheck']['status'], 'MATCH')
        self.assertEqual(value['formulaTrace']['nodes'][0]['status'], 'BLOCKED')

    def test_formula_summary_stays_bounded(self):
        nodes = [{'factRef': str(index), 'expression': 'A1', 'localCheck': {'status': 'BLOCKED'}} for index in range(100)]
        summary = formula_check_summary({'nodes': nodes})
        self.assertEqual(summary['exceptionCount'], 100)
        self.assertEqual(len(summary['exceptions']), 20)
