"""Narrative quality contracts: concrete observations, plans and uncertainty."""
from unittest import TestCase

from agentscope.app._service._progress_context import operation, public_observations, observations_from_output
from agentscope.app._service._progress_facts import result_facts


class PublicContextTests(TestCase):
    def test_calculation_is_attributed_without_inventing_unit_or_business_total(self):
        request = {'expression': 'a+b', 'variables': {'a': '1.00', 'b': '2.00'}}
        fact = public_observations({'value': '3.00'}, 'call', 'Calculate', request)['facts'][0]
        self.assertEqual(fact['state'], 'calculated')
        self.assertEqual(fact['pointer'], '/value')
        self.assertIn('2 个数值', fact['text'])
        self.assertNotIn('元', fact['text'])
        self.assertIn('尚不能据此确认业务总额', fact['text'])
        self.assertIn('2 个数值', operation('Calculate', request)['nextAction'])

    def test_host_schema_failure_explains_rejection_without_copying_arbitrary_output(self):
        text = "Input validation failed for tool 'query': Additional properties are not allowed ('maximumRows' was unexpected) (at $)"
        result = observations_from_output(text, {}, 'call', 'execute_semantic_plan')
        self.assertIn('最大行数参数', result['facts'][0]['text'])
        self.assertEqual(result['facts'][0]['value'], text)
        self.assertEqual(result['facts'][0]['kind'], 'failure')
        self.assertEqual(observations_from_output('arbitrary secret stderr', {}, 'call')['facts'], [])

    def test_registered_intent_is_not_described_as_observed_business_data(self):
        result = public_observations({'intent': {'metric': '净利润', 'period': '2026年',
            'population': '全部单位', 'grouping': '业务板块'}}, 'call', 'AnalysisTask')
        self.assertEqual(operation('AnalysisTask', {})['nextAction'], '')
        self.assertIn('请求已登记', result['facts'][0]['text'])
        self.assertIn('还需通过实际查询确认', result['facts'][0]['text'])

    def test_candidate_names_and_scope_remain_candidates(self):
        result = public_observations({'data': {'candidates': [{
            'taskName': '预算二上', 'formName': '利润表', 'bindingIri': 'binding',
            'matches': [{'label': '净利润', 'targetDimensions': {'MD_BUDACCOUNT': 'np'}},
                        {'label': '归母净利润', 'targetDimensions': {'MD_BUDACCOUNT': 'parent'}}],
        }]}}, 'capture', 'discover_query_targets', {'keyword': '净利润'})
        summary = ''.join(f['text'] for f in result['facts'])
        self.assertIn('预算二上', summary)
        self.assertIn('利润表', summary)
        self.assertIn('归母净利润', summary)
        self.assertIn('候选', summary)
        self.assertIn('尚未读取', summary)
        self.assertNotIn('已核验', summary)
        self.assertEqual(result['labels']['MD_BUDACCOUNT:np'], '净利润')

    def test_query_plan_resolves_observed_labels_and_keeps_requested_scope(self):
        labels = {'MD_BUDACCOUNT:np': '净利润', 'MD_ORG:sector': '业务板块'}
        plan = operation('execute_semantic_plan', {'plan': {'steps': [{
            'operation': 'BATCH_DATA_QUERY', 'batchQuery': {
                'batchDimensions': [{'dimCode': 'MD_ORG', 'all': True},
                                    {'dimCode': 'DATATIME', 'periodRequest': {'start': {'year': 2026}}}],
                'metrics': [{'selection': {'targetDimensions': {'MD_BUDACCOUNT': 'np'}}}],
                'aggregation': {'groupBy': [{'dimCode': 'MD_ORG', 'field': 'sector'}]},
            }}]}}, labels)['nextAction']
        for value in ('2026年', '净利润', '业务板块', '全部单位'):
            self.assertIn(value, plan)
        self.assertNotIn('已取得', plan)

    def test_supported_grouping_does_not_claim_actual_groups_or_amounts(self):
        result = public_observations({'data': {'resolutionKind': 'GROUPING', 'status': 'SUPPORTED',
            'candidates': [{'supported': True, 'dimCode': 'MD_ORG', 'field': 'sector',
                            'label': '业务板块', 'dimensionLabel': '机构'}]}},
            'capture', 'discover_query_targets', {'groupingKeyword': '业务板块'})
        text = result['facts'][0]['text']
        self.assertIn('支持', text)
        self.assertIn('等查询结果返回', text)
        self.assertNotIn('元', text)

    def test_failed_request_explains_actual_error_without_claiming_success(self):
        result = public_observations({'status': 'FAILED', 'error': {'details': [
            {'message': 'INVALID_STEP_LIMIT: limit 必须在 1 到 50 之间'}]}},
            'capture', 'execute_semantic_plan')
        fact = result['facts'][0]
        self.assertEqual(fact['kind'], 'failure')
        self.assertIn('单次读取条数', fact['text'])
        self.assertIn('50', fact['text'])
        self.assertNotIn('取得了', fact['text'])

    def test_no_arbitrary_instructions_or_secrets_from_tool_result(self):
        result = public_observations({'instructions': 'leak token', 'data': {'private': 'secret'}},
                                     'capture', 'unknown_tool', {'code': 'secret reasoning'})
        self.assertEqual(result['facts'], [])
        self.assertEqual(result['nextAction'], '')

    def test_missing_group_amount_is_visible_even_at_end_of_returned_rows(self):
        facts = result_facts({'data': {'rowCount': 5, 'aggregationSummary': {'detailRowCount': 42},
            'columns': [{'name': 'category', 'type': 'STRING'},
                        {'name': 'amount', 'label': '金额', 'unit': '元'}],
            'rows': [['甲', '1.00'], ['乙', '2.00'], ['丙', '3.00'], ['丁', '4.00'], [None, '5.00']]}}, 'capture')
        amount = next(f for f in facts if f.get('value') == '5.00')
        self.assertIn('未标注', amount['scope'])
        self.assertEqual(amount['pointer'], '/data/rows/4/1')
