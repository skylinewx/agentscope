"""最终答复范围不能被后发探查覆盖；主体选择复用既有核对模型调用。"""
from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

from analysis_followup_scope_test import FollowupScopeTest
from analysis_extension.followup_scope import remember_subject, validate_primary
from analysis_extension.models import AnalysisError
from analysis_extension.query_subject import candidates, freeze_subject
from analysis_extension.turn_completion import TurnCompletion, query_evidence, review_query_answer


class QuerySubjectTests(FollowupScopeTest):
    def make_later_exploration(self):
        capture, receipt = deepcopy(self.capture), deepcopy(self.receipt)
        condition = [{'dimCode': 'MD_ORG', 'hierarchy': {'rootCode': 'GT00', 'level': 3}}]
        capture['request']['plan']['steps'][1]['batchQuery']['batchDimensions'] = condition
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['request']['batchDimensions'] = condition
        table['data'].update(rows=[['descendant', '探查下级', '500', '1']], rowCount=1)
        ref = self.save_capture(capture, receipt)
        self.previous['queryCaptureRefs'].append(ref)
        return TurnCompletion(self.service, self.previous), ref

    async def test_review_selects_answered_table_not_latest_exploration(self):
        turn, later = self.make_later_exploration()
        available = candidates(turn)
        self.assertEqual(len(available), 2)
        answer_source = next(candidate for candidate in available if candidate['captureRef'] == self.ref)
        verdict = {'issues': [], 'subjectSelections': [
            {'candidateRef': answer_source['candidateRef'], 'rowIndices': None}]}
        model = AsyncMock(return_value=verdict)
        with patch('analysis_extension.turn_completion._model_review', new=model):
            evidence = query_evidence(turn)
            await review_query_answer(None, turn, evidence, '列出已查询的11个对象')
            # 模拟恢复时状态不含选择结果，持久化核对缓存必须同时恢复主体。
            turn.state.pop('queryAnswerSubject')
            await review_query_answer(None, turn, evidence, '列出已查询的11个对象')
        model.assert_awaited_once()
        remember_subject(turn, {'status': 'DIRECT'})
        selected = turn.state['resolvedSubjectScope']['selections']
        self.assertEqual([item['captureRef'] for item in selected], [self.ref])
        self.turn.state['inheritedScope'] = deepcopy(turn.state['resolvedSubjectScope'])
        validate_primary(self.turn, {'facts': self.facts(self.ref)})
        with self.assertRaises(AnalysisError):
            validate_primary(self.turn, {'facts': self.facts(later)})

    def test_final_subset_is_retained_not_expanded_back_to_table(self):
        turn, _ = self.make_later_exploration()
        available = candidates(turn)
        freeze_subject(turn, available, [{'candidateRef': available[0]['candidateRef'], 'rowIndices': [9, 10]}])
        remember_subject(turn, {'status': 'DIRECT'})
        selected = turn.state['resolvedSubjectScope']['selections'][0]
        self.assertEqual(selected['rowIndices'], [9, 10])
        self.assertEqual(selected['captureRef'], self.ref)

    def test_missing_or_invalid_selection_never_defaults_to_last_table(self):
        turn, _ = self.make_later_exploration()
        available = candidates(turn)
        for choices in ([], [{'candidateRef': 'invented', 'rowIndices': None}],
                        [{'candidateRef': available[0]['candidateRef'], 'rowIndices': [True]}],
                        [{'candidateRef': available[0]['candidateRef'], 'rowIndices': [999]}]):
            with self.subTest(choices=choices):
                freeze_subject(turn, available, choices)
                remember_subject(turn, {'status': 'DIRECT'})
                self.assertEqual(turn.state['resolvedSubjectScope']['selections'], [])
                self.assertEqual(turn.state['resolvedSubjectScope']['basis'], 'UNRESOLVED_QUERY_SUBJECT')

    def test_failed_exploration_receipt_is_not_a_subject(self):
        turn = TurnCompletion(self.service, self.previous)
        turn.state['queryCaptureRefs'].append('missing-capture')
        self.assertEqual([item['captureRef'] for item in candidates(turn)], [self.ref])
