from copy import deepcopy
from unittest import TestCase

from analysis_fixtures import OWNER
from analysis_extension.completion import apply_publication_completion, completion_summary
from analysis_extension.models import AnalysisError


def audit_artifact():
    return {'facts': [], 'relations': [], 'derivedValues': [{
        'derivedId': 'check', 'operator': 'FORMULA_CHECK', 'inputRefs': [],
        'executionStatus': 'COMPLETED', 'calculationValidation': 'PASS', 'sourceConsistency': 'MISMATCH',
        'formulaTrace': {'nodes': [{'factRef': 'profit', 'observed': '-8300',
            'localCheck': {'status': 'MISMATCH', 'value': '1314680.43', 'difference': '-1322980.43'}}]}}],
        'completionContext': {'userRequest': '分析一下亏损企业', 'investigations': []},
        'plan': {'workPlan': [{'taskId': 'audit', 'kind': 'COMPUTATION', 'title': '构成核对',
                              'refs': ['check'], 'operator': 'FORMULA_CHECK'}]}}


def issue(kind, **basis):
    return {'requirement': '解释来源与复算差额', 'reason': '尚未查明差额的具体业务原因',
            'basis': {'kind': kind, **basis}}


class CompletionBasisTests(TestCase):
    def test_completed_discrepancy_is_disclosed_audit_not_partial(self):
        artifact = audit_artifact()
        # A root can match its direct source while a deeper layer differs.
        artifact['derivedValues'][0]['sourceConsistency'] = 'MATCH'
        artifact['derivedValues'][0]['formulaTrace']['nodes'].append(
            {'factRef': 'root', 'localCheck': {'status': 'MATCH', 'difference': '0'}})
        original = deepcopy(artifact['derivedValues'])
        result = apply_publication_completion(artifact, {'unresolvedItems': [issue('AUDIT_RESULT', resultRefs=['check'])]})
        self.assertEqual(result['state'], 'READY')
        self.assertEqual(result['unresolvedItems'], [])
        self.assertTrue(result['auditResults'][0]['executionCompleted'])
        self.assertEqual(result['auditResults'][0]['sourceConsistency'], 'MATCH')
        self.assertEqual(result['auditResults'][0]['mismatchedFactRefs'], ['profit'])
        self.assertEqual(artifact['derivedValues'], original)
        self.assertNotIn('partialReason', artifact)

    def test_mismatch_does_not_hide_a_blocked_layer_or_other_required_input(self):
        artifact = audit_artifact()
        artifact['derivedValues'][0]['formulaTrace']['nodes'].append(
            {'factRef': 'missing', 'localCheck': {'status': 'BLOCKED'}})
        result = apply_publication_completion(artifact, {'unresolvedItems': [issue('AUDIT_RESULT', resultRefs=['check'])]})
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertFalse(result['auditResults'][0]['executionCompleted'])
        artifact = audit_artifact()
        artifact['inputPreparation'] = {'ready': 1, 'expected': 2, 'issues': [{'error': 'source failed'}]}
        result = apply_publication_completion(artifact, {'unresolvedItems': [issue('AUDIT_RESULT', resultRefs=['check'])]})
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertEqual(result['unresolvedItems'][0]['source'], 'HOST_INPUT_PREPARATION')

    def test_explicit_scoped_investigation_stays_pending_independently_of_audit(self):
        artifact = audit_artifact()
        requirement = {'requirementRef': 'requirement_a', 'requirement': '查明甲公司的差额调整依据',
                       'userQuote': '查明甲公司的差额调整依据', 'scope': '仅甲公司利润总额'}
        artifact['completionContext']['investigations'] = [requirement]
        result = apply_publication_completion(artifact, {'unresolvedItems': [
            issue('AUDIT_RESULT', resultRefs=['check']), issue('INVESTIGATION', requirementRef='requirement_a')]})
        self.assertEqual(result['declaredWorkState'], 'READY')
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertEqual(len(result['unresolvedItems']), 1)
        self.assertEqual(result['unresolvedItems'][0]['requirement'], requirement['requirement'])
        self.assertEqual(result['investigations'][0]['scope'], '仅甲公司利润总额')

    def test_unrequested_investigation_and_fabricated_audit_ref_are_rejected(self):
        for item, code in [(issue('INVESTIGATION', requirementRef='invented'), 'UNCONFIRMED_INVESTIGATION'),
                           (issue('AUDIT_RESULT', resultRefs=['invented']), 'INVALID_COMPLETION_ISSUES')]:
            with self.subTest(code=code), self.assertRaises(AnalysisError) as caught:
                apply_publication_completion(audit_artifact(), {'unresolvedItems': [item]})
            self.assertEqual(caught.exception.code, code)

    def test_unplanned_user_requirement_remains_possible_but_model_plan_is_not_evidence(self):
        artifact = audit_artifact()
        artifact['completionContext']['userRequest'] = '分析亏损企业并按板块汇总'
        result = apply_publication_completion(artifact, {'unresolvedItems': [
            {'requirement': '按板块汇总', 'reason': '必要的板块查询失败',
             'basis': {'kind': 'USER_REQUIREMENT', 'userQuote': '按板块汇总'}}]})
        self.assertEqual(result['state'], 'INCOMPLETE')
        with self.assertRaises(AnalysisError) as caught:
            apply_publication_completion(artifact, {'unresolvedItems': [issue('USER_REQUIREMENT', userQuote='查清全部调整原因')]})
        self.assertEqual(caught.exception.code, 'UNCONFIRMED_COMPLETION_REQUIREMENT')

    def test_ready_work_cannot_be_redeclared_pending_and_pending_work_cannot_be_dropped(self):
        artifact = audit_artifact()
        with self.assertRaises(AnalysisError) as caught:
            apply_publication_completion(artifact, {'unresolvedItems': [issue('WORK_ITEM', taskId='audit')]})
        self.assertEqual(caught.exception.code, 'COMPLETION_ISSUE_RESOLVED')
        artifact['plan']['workPlan'].append({'taskId': 'missing', 'kind': 'VALUES', 'title': '缺少的查询', 'refs': []})
        result = apply_publication_completion(artifact, {'unresolvedItems': []})
        self.assertEqual(result['state'], 'INCOMPLETE')
        self.assertIn('missing', result['pendingTaskIds'])

    def test_reading_historical_partial_does_not_rewrite_status_or_hash_input(self):
        artifact = audit_artifact()
        artifact.pop('completionContext')
        artifact['unresolvedItems'] = [{'requirement': '旧版记录的缺口', 'reason': '旧版原因'}]
        before = deepcopy(artifact)
        self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE')
        self.assertEqual(artifact, before)
        with self.assertRaises(AnalysisError):
            apply_publication_completion(artifact, {'unresolvedItems': before['unresolvedItems']})

    def test_new_writes_require_basis_and_host_delivery_basis_is_not_model_controlled(self):
        for item in ({'requirement': '额外调查', 'reason': '模型想继续调查'}, issue('HOST_DELIVERY')):
            with self.subTest(item=item), self.assertRaises(AnalysisError):
                apply_publication_completion(audit_artifact(), {'unresolvedItems': [item]})
        artifact = audit_artifact()
        artifact['hostSummary'] = {'content': 'SOURCE_FACTS_ONLY'}
        result = apply_publication_completion(artifact, {'unresolvedItems': [issue('HOST_DELIVERY')]})
        self.assertEqual(result['state'], 'INCOMPLETE')

    def test_null_source_completed_arithmetic_does_not_become_missing_execution(self):
        artifact = audit_artifact()
        value = artifact['derivedValues'][0]
        value.update(sourceConsistency='NOT_CHECKED', calculationValidation='BLOCKED',
                     completionReason='SOURCE_VALUE_MISSING')
        value.pop('formulaTrace')
        result = apply_publication_completion(artifact, {'unresolvedItems': [issue('AUDIT_RESULT', resultRefs=['check'])]})
        self.assertEqual(result['state'], 'READY')

    def test_preview_repair_repeated_projection_clears_stale_partial_label(self):
        artifact = audit_artifact()
        artifact['partialReason'] = '旧的差额调查说明'
        request = {'unresolvedItems': [issue('AUDIT_RESULT', resultRefs=['check'])]}
        first = apply_publication_completion(artifact, request)
        self.assertEqual(first, apply_publication_completion(artifact, request))
        self.assertEqual(request['unresolvedItems'][0]['basis']['resultRefs'], ['check'])
        self.assertNotIn('partialReason', artifact)

    def test_new_revision_revalidates_carried_issues_even_when_request_omits_them(self):
        artifact = audit_artifact()
        artifact['unresolvedItems'] = [issue('AUDIT_RESULT', resultRefs=['check'])]
        self.assertEqual(apply_publication_completion(artifact, {})['state'], 'READY')
        self.assertEqual(artifact['unresolvedItems'], [])
        for saved in (issue('INVESTIGATION', requirementRef='previous-turn-only'),
                      {'requirement': '旧版缺口', 'reason': '旧版无依据声明'}):
            artifact = audit_artifact()
            artifact['unresolvedItems'] = [saved]
            before = deepcopy(artifact)
            with self.assertRaises(AnalysisError):
                apply_publication_completion(artifact, {})
            self.assertEqual(artifact, before)
