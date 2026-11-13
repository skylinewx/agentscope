"""Business and review views explain counts without changing the native facts."""
from copy import deepcopy
from unittest import TestCase

from analysis_business_tables_test import fact, receipt
from analysis_extension.aggregation_explanation import annotate, explain
from analysis_extension.business_tables import business_view, model_text
from analysis_presentation_fixtures import PresentationCase


class AggregationExplanationTests(TestCase):
    def counts(self):
        return {'rangeSummary': {'targetCombinationCount': 51, 'alignedRowCount': 42,
                                 'matchedTargetCount': 42, 'filteredOutRowCount': 0},
                'aggregationSummary': {'leafTargetCombinationCount': 42, 'detailRowCount': 42,
                                       'groupCountBeforeHaving': 5, 'filteredOutGroupCount': 0},
                'rowCount': 5}

    def test_business_markdown_distinguishes_leaf_members_from_values_and_groups(self):
        source = receipt([fact('x', {'板块': ''}, '289929166.64')], stepResults=[{
            'stepId': 'batch', 'batchResults': [{'batchId': 'batch-A', 'data': self.counts()}]}])
        before = deepcopy(source)
        text = model_text(business_view(source, 'cap-A'))
        for expected in ('聚合口径', '原始范围目标组合 51', '有效末级参与目标组合 42', '实际返回 5 组',
                         '上级不重复参与', '不等于有值数', '不继承父级属性', '保留空属性组', '不含已去重的上级行'):
            self.assertIn(expected, text)
        self.assertNotIn('42 个有值', text)
        self.assertEqual(source, before)
        self.assertIn('289929166.64', text)

    def test_filter_stage_counts_are_separate_and_nulls_are_preserved(self):
        counts = self.counts()
        counts['rangeSummary']['filteredOutRowCount'] = 3
        counts['aggregationSummary']['filteredOutGroupCount'] = 1
        counts['rowCount'] = 4
        text = explain(counts)
        self.assertIn('明细条件筛除行 3', text)
        self.assertLess(text.index('明细条件筛除行 3'), text.index('汇总前明细行'))
        self.assertIn('分组条件筛除组数 1', text)
        self.assertIn('实际返回 4 组', text)
        source = receipt([fact('x', {'板块': ''}, None, valueState='NULL')], stepResults=[{
            'stepId': 'batch', 'batchResults': [{'batchId': 'batch-A', 'data': counts}]}])
        self.assertIn('空值', business_view(source, 'cap-A')['markdown'])

    def test_nonaggregation_and_missing_counts_do_not_invent_deduplication_numbers(self):
        self.assertEqual(explain({'rangeSummary': {'targetCombinationCount': 51}}), '')
        note = explain({'aggregationSummary': {'groupCountBeforeHaving': 0}, 'rowCount': 0})
        self.assertIn('实际返回 0 组', note)
        self.assertNotIn('有效末级参与目标组合 0', note)

    def test_scopes_keep_exact_raw_counts_and_reference_identity(self):
        source = {**self.counts(), 'captureRef': 'cap-A', 'batchId': 'batch-A', 'stepId': 'batch',
                  'selectedRowCount': 3, 'columnSelection': ['metric1']}
        before = deepcopy(source)
        result, = annotate([source])
        self.assertEqual(result['aggregationExplanation'], explain(source))
        for key, value in before.items():
            self.assertEqual(result[key], value)
        self.assertEqual(source, before)

    def test_multidimensional_counts_never_become_organization_counts(self):
        note = explain(self.counts())
        self.assertNotIn('51 个组织', note)
        self.assertNotIn('42 家', note)

    def test_multiple_batches_are_explained_individually(self):
        smaller = self.counts()
        smaller['rangeSummary']['targetCombinationCount'] = 11
        smaller['aggregationSummary']['leafTargetCombinationCount'] = 11
        source = receipt([fact('x', {'板块': '甲'}, '100')], stepResults=[{
            'stepId': 'batch', 'batchResults': [{'batchId': 'one', 'data': self.counts()},
                {'batchId': 'two', 'data': smaller}]}])
        text = business_view(source, 'cap-A')['markdown']
        self.assertIn('（one）', text)
        self.assertIn('（two）', text)
        self.assertIn('原始范围目标组合 11', text)
        self.assertIn('原始范围目标组合 51', text)


class AggregationExplanationIntegrationTests(PresentationCase):
    async def test_native_capture_build_inspect_and_review_share_the_same_explanation(self):
        import json
        from analysis_budget_batch_test import batch_fixture, update
        from analysis_fixtures import OWNER
        from analysis_extension.turn_completion import TurnCompletion, review_payload

        capture, selection, scope, receipt_data = batch_fixture()
        data = receipt_data['data']['stepResults'][0]['batchResults'][0]['data']
        data['rangeSummary']['targetCombinationCount'] = 8
        update(capture, receipt_data)
        reference = self.store.capture(OWNER, capture)
        selection['captureRef'] = reference
        expected = explain(data)
        for typed in (False, True):
            with self.subTest(typed=typed):
                selector = {**selection, **({'role': 'PRIMARY'} if typed else {})}
                built = await self.service.build([reference], [selector], scope, [], 'scope-' + str(typed))
                artifact = self.store.get_analysis(OWNER, built['analysisRef'])
                inspected = await self.service.inspect(section='summary', analysisRef=built['analysisRef'])
                turn = TurnCompletion(self.service, {'request': '按板块看预算', 'mode': 'ANALYSIS', 'goal': '按板块看预算'})
                reviewed = json.loads(review_payload(turn, artifact, ''))
                for sources in (artifact['scope']['sourceScopes'], built['scope']['sourceRangeSummary'],
                                inspected['scope']['sourceScopes'], reviewed['sourceScopes']):
                    self.assertTrue(sources)
                    for source in sources:
                        self.assertEqual(source['aggregationExplanation'], expected)
                        self.assertEqual(source['rangeSummary'], data['rangeSummary'])
                        self.assertEqual(source['aggregationSummary'], data['aggregationSummary'])
                # Legacy artifacts can be explained from the frozen counts too;
                # the persisted evidence and amounts remain unchanged.
                legacy = deepcopy(artifact)
                for source in legacy['scope']['sourceScopes']:
                    source.pop('aggregationExplanation')
                rebuilt = self.service._built(legacy)
                rereviewed = json.loads(review_payload(turn, legacy, ''))
                self.assertEqual(rebuilt['scope']['sourceRangeSummary'][0]['aggregationExplanation'], expected)
                self.assertEqual(rereviewed['sourceScopes'][0]['aggregationExplanation'], expected)
                self.assertEqual(legacy['facts'], artifact['facts'])
                self.assertNotIn('aggregationExplanation', legacy['scope']['sourceScopes'][0])
