from unittest import TestCase
from types import SimpleNamespace
from analysis_extension.evidence_coverage import summarize
from analysis_extension.formula_check_summary import local_results
from analysis_extension.business_tables import state_markdown
from analysis_extension.followup_scope import prepare_request


class EvidenceCoverageTest(TestCase):
    def test_entity_counts_are_not_fact_counts_and_formula_scope_does_not_shrink_population(self):
        facts = [{'factId': f'{org}-{metric}', 'sourceContextId': 'context', 'inputRole': 'PRIMARY',
                  'grain': ['MD_ORG'], 'dimensions': {'MD_ORG': org}, 'objectLabel': name, 'valueState': 'PRESENT'}
                 for org, name in [('A', '企业A'), ('B', '集团（差额）'), ('C', '合计')]
                 for metric in ('income', 'cost')]
        artifact = {'facts': facts, 'plan': {'workPlan': [{'requiredEvidence': 'FORMULAS', 'targetRefs': ['A-income']}]}}
        result = summarize(artifact)
        self.assertEqual(result['groups'][0]['entityCount'], 3)
        self.assertEqual(result['selectedFactCount'], 6)
        self.assertEqual(result['formulaTargetCount'], 1)
        self.assertEqual(len(artifact['facts']), 6)

    def test_formula_mismatch_message_does_not_invent_missing_business_items_or_causes(self):
        trace = {'nodes': [{'factRef': 'total-profit', 'observed': '-8300.00', 'localCheck': {
            'status': 'MISMATCH', 'value': '1314680.43', 'difference': '-1322980.43'}}]}
        row, = local_results(trace)
        self.assertEqual(row['statement'], '来源值 -8300.00，本层复算值 1314680.43，差额 -1322980.43；两者不一致。')
        self.assertNotIn('缺少', row['statement'])

    def test_native_filter_counts_use_source_semantics_and_values_analysis_remains_query(self):
        receipt = {'status': 'SUCCEEDED', 'data': {'complete': True, 'targets': [{'id': 'a', 'label': '应收票据'}, {'id': 'b', 'label': '短期借款'}],
            'valueFilter': {'basis': 'BUDGET_NATIVE_RESULT_WHERE', 'condition': {'operator': 'AND', 'children': [
                {'targetId': 'a', 'operator': 'GT', 'value': '1000'}, {'targetId': 'b', 'operator': 'GT', 'value': '1000'}]},
                'batches': [{'rangeSummary': {'targetCombinationCount': 11, 'alignedRowCount': 11, 'matchedTargetCount': 2, 'filteredOutRowCount': 9}}]}}}
        markdown = state_markdown(receipt)
        self.assertIn('应收票据 &gt; 1000 且 短期借款 &gt; 1000', markdown)
        self.assertIn('命中目标组合：2', markdown)
        self.assertNotIn('0 项来源值', markdown)
        turn = SimpleNamespace(state={'mode': 'ANALYSIS', 'requiredEvidence': 'VALUES', 'request': '对比预算金额'})
        result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, {'purpose': 'ANALYZE'})
        self.assertEqual(result['purpose'], 'QUERY')
