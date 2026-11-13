import unittest
from copy import deepcopy
from analysis_extension.task_projection import render


class TaskProjectionTests(unittest.TestCase):
    def test_context_candidates_keep_selection_fields_without_repeated_answer_bodies(self):
        value = {'mode': 'DIRECT', 'availableContexts': {'total': 2, 'nextOffset': 2,
            'contexts': [{'contextRef': 'ctx_' + str(i), 'request': 'query' + str(i),
                'intent': {'metric': 'metric' + str(i)}, 'status': 'COMPLETED',
                'kind': 'QUERY', 'answerPreview': 'long repeated answer'} for i in range(2)]}}
        original = deepcopy(value)
        result = render(value)
        for text in ('ctx_0', 'ctx_1', 'query0', 'query1', 'metric0', 'metric1', 'nextOffset'):
            self.assertIn(text, result)
        self.assertNotIn('long repeated answer', result)
        self.assertEqual(value, original)

    def test_ready_formula_and_input_groups_are_visible_without_historical_receipt_noise(self):
        value = {'mode': 'ANALYSIS', 'goal': '分别分析', 'requiredEvidence': 'FORMULAS',
            'intent': {'period': '2026年'}, 'intentDecisions': {'grouping': {'operation': 'CLEAR', 'value': None}},
            'subjectSources': [{'sourceRef': 'src_actual', 'inputRef': 'input_primary', 'role': 'PRIMARY',
                                'rowCount': 2, 'contexts': [{'baseContext': {'token': 'internal-only'}}]}],
            'followupScope': {'context': {'mode': 'CONTINUE', 'contextRefs': ['ctx_actual'], 'userQuote': '用户原话'}},
            'dimensionScopes': [{'sourceText': '历史原话'}],
            'preparedFormulaInputs': {'expected': 1, 'targets': [{'subject': {'entity': 'actual'},
                'inputRef': 'input_primary', 'captureRef': 'cap_formula'}], 'groups': [{'inputRef': 'input_primary', 'ready': 1}]}}
        original = deepcopy(value)
        result = render(value)
        self.assertIn('预期 1 个目标，就绪 1 个', result)
        self.assertIn('FORMULA_CHECK', result)
        for reference in ('src_actual', 'input_primary', 'cap_formula', 'ctx_actual'):
            self.assertIn(reference, result)
        self.assertNotIn('baseContext', result)
        self.assertNotIn('sourceText', result)
        self.assertEqual(value, original)

    def test_partial_formula_keeps_failed_target_and_exact_diagnostic(self):
        result = render({'mode': 'ANALYSIS', 'goal': '分析两个对象', 'preparedFormulaInputs': {
            'expected': 2, 'targets': [{'subject': 'first', 'captureRef': 'cap_ready'},
                {'subject': 'second', 'error': {'code': 'SOURCE_VALUE_CHANGED', 'message': '来源值发生变化'}}],
            'next': '仅补失败目标'}})
        self.assertIn('预期 2 个目标，就绪 1 个', result)
        self.assertIn('SOURCE_VALUE_CHANGED', result)
        self.assertIn('second', result)
        self.assertIn('仅补失败目标', result)
        self.assertNotIn('公式目标及依赖已取得', result)
