"""多轮业务上下文：交付/待续隔离、历史定位、Python 结果子集和公式目标。"""
from copy import deepcopy
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock

from agentscope.message import UserMsg, TextBlock
from analysis_extension.conversation_context import (save_context, next_context_state, context_directory,
                                                    select_context, result_subject, evidence_turn)
from analysis_extension.completion import formula_target_refs, continue_plan
from analysis_extension.models import AnalysisError
from analysis_extension.followup_scope import remember_subject
from analysis_extension.turn_completion import current_turn, STATE_KEY


class ConversationContextTest(unittest.TestCase):
    def setUp(self):
        self.first = {'turnKey': 't1', 'request': '查询第一组', 'goal': '第一组金额', 'mode': 'DIRECT',
                      'queryIntent': {'task': '任务甲', 'period': '2026', 'population': '第一组'},
                      'resolvedSubjectScope': {'scopeRef': 's1', 'selections': [{'captureRef': 'c1'}]},
                      'queryCaptureRefs': ['c1'], 'lastCheck': {'status': 'DIRECT'}}
        save_context(self.first, 'DIRECT')

    def test_failed_new_intent_does_not_overwrite_committed_scope(self):
        failed = {**next_context_state(self.first), 'turnKey': 't2', 'request': '改到任务乙的2025年',
                  'mode': 'DIRECT', 'queryIntent': {'task': '任务乙', 'period': '2025'},
                  'followupContext': {'mode': 'CONTINUE', 'changedFields': ['task', 'period']},
                  'lastCheck': {'status': 'INCOMPLETE'}}
        state = next_context_state(failed)
        self.assertEqual(state['previousQueryIntent'], self.first['queryIntent'])
        self.assertEqual(state['inheritedScope']['scopeRef'], 's1')
        pending = state['businessContexts'][-1]
        self.assertEqual(pending['kind'], 'PENDING')
        state['request'] = '继续刚才没完成的查询'
        select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE', 'contextRefs': [pending['contextRef']],
                                                       'userQuote': state['request']})
        self.assertEqual(state['previousQueryIntent']['task'], '任务乙')
        self.assertEqual(state['basePendingChanges'], ['task', 'period'])

    def test_history_selection_uses_matching_intent_and_scope(self):
        second = {**next_context_state(self.first), 'turnKey': 't2', 'request': '另一组',
                  'queryIntent': {'task': '任务乙'}, 'resolvedSubjectScope': {'scopeRef': 's2'},
                  'lastCheck': {'status': 'DIRECT'}}
        save_context(second, 'DIRECT')
        state = next_context_state(second)
        state['request'] = '回到第一组'
        select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE',
                       'contextRefs': [self.first['activeBusinessContextRef']], 'userQuote': '第一组'})
        self.assertEqual(state['previousQueryIntent'], self.first['queryIntent'])
        self.assertEqual(state['inheritedScope']['scopeRef'], 's1')

    def test_unknown_context_or_false_quote_cannot_mutate_state(self):
        state = next_context_state(self.first)
        state['request'] = '回到第一组'
        before = deepcopy(state)
        with self.assertRaises(AnalysisError):
            select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE', 'contextRefs': ['foreign'], 'userQuote': '第一组'})
        self.assertEqual(state, before)
        other = {**self.first, 'turnKey': 'other', 'queryIntent': {'task': '乙'}}
        save_context(other, 'DIRECT')
        state['businessContexts'].append(other['businessContexts'][-1])
        before = deepcopy(state)
        with self.assertRaises(AnalysisError):
            select_context(SimpleNamespace(state=state), {'mode': 'CONTINUE', 'contextRefs': [other['activeBusinessContextRef']], 'userQuote': '用户没说过'})
        self.assertEqual(state, before)

    def test_synthesis_keeps_only_shared_intent_and_no_implicit_union(self):
        state = next_context_state(self.first)
        other = {**self.first, 'turnKey': 't2', 'queryIntent': {'task': '任务乙', 'period': '2026'},
                 'resolvedSubjectScope': {'scopeRef': 's2'}}
        save_context(other, 'DIRECT')
        state['businessContexts'].append(other['businessContexts'][-1])
        state['request'] = '综合这两次查询'
        select_context(SimpleNamespace(state=state), {'mode': 'SYNTHESIZE', 'contextRefs': [
            self.first['activeBusinessContextRef'], other['activeBusinessContextRef']], 'userQuote': '综合这两次查询'})
        self.assertEqual(state['previousQueryIntent'], {'period': '2026'})
        self.assertEqual(state['inheritedScope'], {})

    def test_directory_paginates_without_attaching_source_tables(self):
        state = deepcopy(self.first)
        for number in range(10):
            state.update(turnKey='t' + str(number + 2), request='材料' + str(number))
            save_context(state, 'DIRECT')
        page = context_directory(state, limit=2)
        self.assertEqual(page['total'], 11)
        self.assertEqual(page['nextOffset'], 2)
        self.assertNotIn('subjectScope', page['contexts'][0])
        self.assertEqual(context_directory(state, search='材料0')['total'], 1)

    def test_history_only_answer_keeps_source_bounds_and_actual_delivery_preview(self):
        state = {**next_context_state(self.first), 'turnKey': 'top-three', 'mode': 'DIRECT',
                 'request': '回到第一组，只要前三名', 'queryIntent': {'population': '第一组前三名'},
                 'deliveredAnswer': '前三名的实际交付内容', 'followupContext': {'mode': 'CONTINUE'}}
        remember_subject(SimpleNamespace(state=state, service=SimpleNamespace()), {'status': 'DIRECT'})
        snapshot = state['businessContexts'][-1]
        self.assertEqual(snapshot['captureRefs'], ['c1'])
        self.assertEqual(snapshot['subjectScope']['basis'], 'INHERITED_SOURCE')
        self.assertEqual(snapshot['answerPreview'], '前三名的实际交付内容')
        self.assertEqual(snapshot['intent']['population'], '第一组前三名')
        self.assertNotIn('answerPreview', context_directory(state)['contexts'][0])

    def test_context_index_survives_new_turn_and_json_storage(self):
        state = SimpleNamespace(middle_context={STATE_KEY: json.loads(json.dumps(self.first))}, context=[])
        user = UserMsg(name='user', content=[TextBlock(text='继续分析')])
        current = current_turn(state, user)
        self.assertEqual(current['previousQueryIntent'], self.first['queryIntent'])
        self.assertEqual(len(current['businessContexts']), 1)
        self.assertIs(current, current_turn(state, []))

    def test_recovered_report_retains_composition_sources_across_direct_followup(self):
        state = {**next_context_state(self.first), 'turnKey': 'recovery', 'request': '继续未完成的分析',
                 'queryIntent': self.first['queryIntent'], 'followupContext': {'mode': 'CONTINUE'}}
        service = SimpleNamespace(store=Mock(), owner='owner')
        service.store.get_analysis.return_value = {'plan': {}, 'facts': [], 'captureRefs': ['c1', 'grid-details']}
        remember_subject(SimpleNamespace(state=state, service=service), {'status': 'READY', 'analysisRef': 'report:2'})
        saved = state['businessContexts'][-1]
        self.assertEqual(set(saved['captureRefs']), {'c1', 'grid-details'})
        self.assertEqual(saved['analysisRef'], 'report:2')
        state['lastCheck'] = {'status': 'READY'}
        followup = {**next_context_state(state), 'turnKey': 'count', 'request': '其中有几家',
                    'queryIntent': self.first['queryIntent'], 'followupContext': {'mode': 'CONTINUE'}}
        remember_subject(SimpleNamespace(state=followup, service=service), {'status': 'DIRECT'})
        self.assertEqual(set(followup['businessContexts'][-1]['captureRefs']), {'c1', 'grid-details'})
        self.assertEqual(followup['businessContexts'][-1]['analysisRef'], 'report:2')

    def test_history_review_restores_report_sources_without_loading_other_topics(self):
        state = next_context_state(self.first)
        state['businessContexts'][0]['analysisRef'] = 'report:2'
        service = SimpleNamespace(store=Mock(), owner='owner')
        service.store.get_analysis.return_value = {'captureRefs': ['c1', 'grid-details']}
        original = deepcopy(state)
        evidence = evidence_turn(SimpleNamespace(state=state, service=service))
        self.assertEqual(evidence.state['queryCaptureRefs'], ['c1', 'grid-details'])
        self.assertEqual(state, original)
        service.store.get_analysis.assert_called_once_with('owner', 'report:2')
        state.update(turnKey='followup', request='其中有几家', queryIntent=self.first['queryIntent'])
        remember_subject(SimpleNamespace(state=state, service=service), {'status': 'DIRECT'})
        self.assertEqual(set(state['businessContexts'][-1]['captureRefs']), {'c1', 'grid-details'})
        self.assertEqual(state['businessContexts'][-1]['analysisRef'], 'report:2')

    def test_new_topic_clears_selected_context_without_erasing_history(self):
        state = next_context_state(self.first)
        state['request'] = '查询另一种产品'
        select_context(SimpleNamespace(state=state), {'mode': 'NEW', 'userQuote': '另一种产品'})
        self.assertEqual(state['selectedContextRefs'], [])
        self.assertEqual(state['inheritedScope'], {})
        self.assertEqual(state['previousQueryIntent'], {})
        self.assertEqual(len(state['businessContexts']), 1)

    def test_new_topic_clears_legacy_context_without_directory(self):
        state = {'request': '换成另外一个任务', 'previousQueryIntent': {'task': '旧任务'},
                 'inheritedScope': {'scopeRef': 'old'}}
        select_context(SimpleNamespace(state=state), {'mode': 'NEW', 'userQuote': '另外一个任务'})
        self.assertEqual(state['previousQueryIntent'], {})
        self.assertEqual(state['inheritedScope'], {})

    def test_revision_cannot_drop_explicit_formula_obligation(self):
        task = {'taskId': 'composition', 'title': '必要构成', 'kind': 'COMPUTATION',
                'operator': 'PYTHON', 'requiredEvidence': 'FORMULAS', 'targetRefs': ['target'], 'refs': []}
        base = {'plan': {'workPlan': [task]}}
        with self.assertRaises(AnalysisError):
            continue_plan(base, {}, [{**task, 'requiredEvidence': 'VALUES'}])
        _, work = continue_plan(base, {}, [{key: value for key, value in task.items() if key != 'requiredEvidence'}])
        self.assertEqual(work[0]['requiredEvidence'], 'FORMULAS')

    def test_python_result_subset_uses_exact_fact_references(self):
        facts = [{'factId': 'f' + str(index), 'dimensions': {'MD_ORG': 'org' + str(index)},
                  'captureRefs': ['capture'], 'stepId': 'step', 'batchId': 'batch',
                  'rowIndex': index, 'columnName': 'amount'} for index in range(5)]
        output = {'derivedId': 'losses', 'label': '筛选结果', 'outputKind': 'TABLE',
                  'columns': [{'name': 'source', 'type': 'STRING'}], 'rows': [['f3'], ['f4']], 'rowCount': 2}
        run = {'sourceAnalysisRef': 'analysis', 'inputRecords': [{'ref': fact['factId']} for fact in facts], 'outputs': [output]}
        service = SimpleNamespace(owner=None, store=Mock())
        service.store.get_python_run.return_value = run
        service.store.get_analysis.return_value = {'facts': facts, 'captureRefs': ['capture']}
        turn = SimpleNamespace(service=service, state={'queryResultRefs': [{'pythonRunRef': 'run', 'outputRefs': ['losses']}]})
        scope = result_subject(turn)
        self.assertEqual(scope['selections'][0]['rowIndices'], [3, 4])
        # 没有明确身份时保留产物引用，不能用金额相等或自造列别名扩大回原始全集。
        output['rows'], output['columns'] = [['-10']], [{'name': 'amount', 'type': 'DECIMAL_STRING'}]
        scope = result_subject(turn)
        self.assertEqual(scope['selections'], [])
        self.assertEqual(scope['basis'], 'SELECTED_RESULTS')

    def test_python_formula_targets_do_not_include_overview(self):
        artifact = {'plan': {'workPlan': [
            {'taskId': 'overview', 'kind': 'VALUES', 'refs': ['a', 'b', 'c', 'd']},
            {'taskId': 'composition', 'kind': 'COMPUTATION', 'operator': 'PYTHON',
             'requiredEvidence': 'FORMULAS', 'targetRefs': ['c', 'd'], 'refs': ['calc']} ]},
            'derivedValues': [{'derivedId': 'calc', 'operator': 'PYTHON', 'inputRefs': ['a', 'b', 'c', 'd']}]}
        self.assertEqual(formula_target_refs(artifact), {'c', 'd'})
        artifact['plan']['workPlan'].pop()
        self.assertEqual(formula_target_refs(artifact), set())


if __name__ == '__main__':
    unittest.main()
