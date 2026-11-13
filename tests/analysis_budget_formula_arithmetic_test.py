"""Budget null/zero semantics across computation, status, datasets and both renderers."""
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from analysis_depth_test import chain
from analysis_extension.comparison_scenes import comparison_scenes
from analysis_extension.completion import computation_coverage
from analysis_extension.document_data import formula_dataset
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.formula_arithmetic import BUDGET_FORMULA
from analysis_extension.formula_check_summary import summarize
from analysis_extension.formulas import Expression, trace_formula
from analysis_extension.models import AnalysisError
from analysis_extension.native_analysis import formula_check_summary


def budget_facts(expression='B1-C1', first='10', second=None, source='10'):
    facts = chain()[:3]
    for fact, value in zip(facts, (source, first, second)):
        fact.update(formula=None, value=value, valueState='NO_VALUE' if value is None else 'PRESENT',
                    formulaArithmetic=BUDGET_FORMULA)
    facts[0]['formula'] = expression
    return facts


def computed(facts, scope='LOCAL'):
    return calculate(facts, {'scopeId': 's'}, [{'id': 'f', 'operator': 'FORMULA_CHECK',
                     'inputRefs': ['root'], 'checkScope': scope}], RuleRegistry())[0]


class BudgetFormulaArithmeticTests(TestCase):
    def test_confirmed_null_participates_in_all_supported_operations(self):
        for expression, expected in [('B1+C1', '10'), ('B1-C1', '10'), ('B1*C1', '0'),
                                     ('C1/B1', '0'), ('B1/C1', '0'), ('SUM(B1,C1)', '10'),
                                     ('B1/(C1-C1)+2', '2'), ('-C1+B1', '10')]:
            facts = budget_facts(expression, source=expected)
            original = deepcopy(facts)
            for scope in ('LOCAL', 'RECURSIVE'):
                with self.subTest(expression=expression, scope=scope):
                    value = computed(facts, scope)
                    self.assertEqual(value['value'], expected)
                    self.assertEqual(value['calculationValidation'], 'PASS')
                    self.assertEqual(value['sourceConsistency'], 'MATCH')
                    self.assertEqual(value['formulaTrace']['nullAsZeroRefs'], ['expense'])
                    self.assertTrue(computation_coverage(value, {}, 'FORMULA_CHECK')[0])
            self.assertEqual(facts, original)

    def test_zero_denominator_and_nested_division_are_budget_only(self):
        for expression in ('10/0', '0/0', '10/(2-2)', '10/(3/0)'):
            with self.subTest(expression=expression):
                events = []
                self.assertEqual(Expression(expression).evaluate(lambda _: None,
                                 arithmetic_policy=BUDGET_FORMULA, zero_divisions=events), Decimal(0))
                self.assertTrue(events)
                with self.assertRaises(AnalysisError) as caught:
                    Expression(expression).evaluate(lambda _: None)
                self.assertEqual(caught.exception.code, 'FORMULA_ZERO_DIVISOR')
        value = computed(budget_facts('B1/C1', second='0', source='0'))
        self.assertEqual(value['formulaTrace']['targetLocalCheck']['zeroDivisorCount'], 1)

    def test_strict_sources_and_generic_arithmetic_do_not_inherit_budget_semantics(self):
        facts = budget_facts()
        for fact in facts:
            fact.pop('formulaArithmetic')
        self.assertEqual(computed(facts)['calculationValidation'], 'BLOCKED')
        for expression, refs in [('v0/0', ['root']), ('v0+v1', ['root', 'expense'])]:
            value = calculate(budget_facts(), {'scopeId': 's'}, [{'id': 'generic', 'operator': 'ARITHMETIC',
                              'inputRefs': refs, 'expression': expression}], RuleRegistry())[0]
            self.assertEqual(value['calculationValidation'], 'BLOCKED')

    def test_failed_unread_conflicting_and_display_only_inputs_still_block(self):
        for change in ({'valueState': 'NOT_QUERIED'}, {'valueState': 'FAILED'},
                       {'valueState': 'CONFLICT'}, {'valueState': 'NOT_APPLICABLE'},
                       {'precision': 'display_only'}, {'unit': 'other'}, {'scale': '1000'}):
            for scope in ('LOCAL', 'RECURSIVE'):
                facts = budget_facts('B1/C1', source='0')
                facts[2].update(change)
                with self.subTest(change=change, scope=scope):
                    self.assertEqual(computed(facts, scope)['executionStatus'], 'BLOCKED')
        facts = budget_facts()
        facts.pop()
        self.assertEqual(computed(facts)['executionStatus'], 'BLOCKED')

    def test_null_input_does_not_hide_another_invalid_input(self):
        facts = budget_facts(first=None, second=None)
        facts[2]['valueState'] = 'FAILED'
        self.assertEqual(computed(facts)['executionStatus'], 'BLOCKED')

    def test_recursive_expansion_keeps_local_and_recursive_results_separate(self):
        facts = chain()
        for fact in facts:
            fact['formulaArithmetic'] = BUDGET_FORMULA
        facts[4].update(value=None, valueState='NO_VALUE')
        trace = trace_formula(facts, 'root')
        self.assertEqual(trace['targetLocalCheck']['value'], '-8')
        self.assertEqual(trace['targetLocalCheck']['status'], 'MATCH')
        self.assertEqual(trace['value'], '42')
        self.assertEqual(trace['status'], 'MISMATCH')
        self.assertEqual(trace['differenceOriginRefs'], ['subtotal'])
        self.assertEqual(trace['externalBoundaries'], ['cost'])
        self.assertEqual(trace['nullAsZeroRefs'], ['cost'])
        self.assertFalse(summarize(trace)['hasIncompleteLayers'])

    def test_null_target_preserves_source_and_exposes_comparison_basis(self):
        facts = budget_facts(first='10', second='10', source=None)
        trace = trace_formula(facts, 'root')
        self.assertEqual(trace['status'], 'MATCH')
        self.assertIsNone(trace['nodes'][0]['observed'])
        self.assertEqual(trace['nodes'][0]['effectiveSourceValue'], '0')
        self.assertEqual(trace['comparisons']['local']['leftRole'], 'source_null_as_zero')
        facts[2]['value'] = '3'
        self.assertEqual(trace_formula(facts, 'root')['targetLocalCheck']['difference'], '-7')

    def test_formula_datasets_keep_raw_null_and_zero_contribution(self):
        facts = budget_facts()
        value = computed(facts)
        node = formula_dataset(value, {f['factId']: f for f in facts}, None, None)['nodes'][0]
        self.assertIsNone(node['operands'][1]['value'])
        self.assertEqual(node['operands'][1]['valueState'], 'NO_VALUE')
        self.assertEqual(node['operands'][1]['calculationValue']['value'], '0')
        self.assertTrue(node['operands'][1]['nullAsZero'])
        step = node['waterfall']['steps'][1]
        self.assertEqual(step['value'], '0')
        self.assertEqual(step['start'], step['end'])
        self.assertEqual(step['geometry']['width'], 0)
        self.assertEqual(node['waterfall']['steps'][-1]['value'], '10')
        self.assertTrue(formula_check_summary(value['formulaTrace'])['allLocalChecksCompleted'])
        self.assertNotIn('未完成', str(summarize(value['formulaTrace'])))

    def test_all_null_additive_formula_has_zero_length_steps(self):
        facts = budget_facts(first=None, second=None, source='0')
        value = computed(facts)
        node = formula_dataset(value, {f['factId']: f for f in facts}, None, None)['nodes'][0]
        self.assertEqual(len(node['waterfall']['steps']), 3)
        self.assertTrue(all(step['geometry']['width'] == 0 for step in node['waterfall']['steps']))

    def test_division_by_zero_computes_without_fabricating_additive_waterfall(self):
        facts = budget_facts('B1/C1', source='0')
        value = computed(facts)
        node = formula_dataset(value, {f['factId']: f for f in facts}, None, None)['nodes'][0]
        self.assertEqual(node['values']['localValue']['value'], '0')
        self.assertEqual(node['zeroDivisorCount'], 1)
        self.assertNotIn('waterfall', node)

    def test_original_waterfall_renderer_uses_the_same_effective_inputs(self):
        facts = budget_facts()
        value = computed(facts)
        artifact = {'facts': facts, 'derivedValues': [value], 'technicalDetails': False,
                    'viewPlan': [{'viewId': 'v', 'type': 'WATERFALL', 'valueRefs': ['derived_f']}]}
        charts = comparison_scenes(artifact)
        self.assertEqual(len(charts), 1)
        self.assertIsNone(facts[-1]['value'])

    def test_source_mismatch_is_not_balanced_away(self):
        facts = budget_facts(source='7')
        value = computed(facts)
        self.assertEqual(value['sourceConsistency'], 'MISMATCH')
        node = formula_dataset(value, {f['factId']: f for f in facts}, None, None)['nodes'][0]
        self.assertEqual(node['values']['localDifference']['value'], '-3')
        self.assertEqual(node['waterfall']['steps'][-1]['value'], '10')
