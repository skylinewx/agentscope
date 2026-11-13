from analysis_presentation_fixtures import PresentationCase
from analysis_extension.review_control import record_review_issue, progress
from analysis_extension.review_revision import remember_review
from analysis_extension.turn_completion import TurnCompletion, current_turn
from agentscope.message import UserMsg
from agentscope.state import AgentState


class ReviewLifecycleTest(PresentationCase):
    def test_only_completed_review_resolves_issue_and_scope_change_supersedes(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '分析本年金额')))
        material = {'facts': [{'factId': 'f1', 'value': '-8'}], 'derived': [], 'documentText': '金额为正'}
        issue = record_review_issue(turn, {'code': 'VALUE_MISMATCH', 'severity': 'BLOCKING',
            'message': '符号错误', 'statement': '金额为正', 'evidenceRefs': ['f1']}, 'v1', material)
        remember_review(turn, material, {'issues': [issue]})
        self.assertEqual(progress(turn)['reviewIssueHistory'][issue['issueId']]['state'], 'ACTIVE')
        material['documentText'] = '金额为负'
        self.assertEqual(progress(turn)['reviewIssueHistory'][issue['issueId']]['state'], 'ACTIVE')
        remember_review(turn, material, {'issues': []})
        self.assertEqual(progress(turn)['reviewIssueHistory'][issue['issueId']]['state'], 'RESOLVED')
        issue = record_review_issue(turn, {'code': 'VALUE_MISMATCH', 'severity': 'BLOCKING',
            'message': '其他错误', 'statement': '金额为负', 'evidenceRefs': ['f1']}, 'v2', material)
        material['facts'][0]['value'] = '10'
        remember_review(turn, material, {'issues': []})
        self.assertEqual(progress(turn)['reviewIssueHistory'][issue['issueId']]['state'], 'SUPERSEDED')
