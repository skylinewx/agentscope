"""混合粒度占比、计算续建和展示故障交付的通用回归。"""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from analysis_budget_batch_test import batch_fixture, update
from analysis_extension.adapters.budget_v2 import BudgetV2Adapter
from analysis_extension.analysis_inputs import adapt_inputs
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.models import AnalysisError
from analysis_extension.operation_plan import merge_operations
from analysis_extension.service import bind_source_origins
from analysis_extension.sources import SourceRegistry
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
from analysis_extension.closeout_content import ReviewedParagraphs
import analysis_closeout_recovery_test as recovery_fixtures


class GrainComparisonTests(TestCase):
    def data(self):
        capture, selection, _, receipt = batch_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['rows'][0][:2] = [None, None]
        update(capture, receipt)
        reference, compare, _, reference_receipt = batch_fixture(aggregate=False)
        reference['captureRef'] = compare['captureRef'] = 'reference'
        reference_table = reference_receipt['data']['stepResults'][0]['batchResults'][0]
        reference_table['batchId'] = compare['batchId'] = 'reference-batch'
        reference_receipt['evidence'][0]['fact']['batchId'] = 'reference-batch'
        update(reference, reference_receipt)
        captures = {'batch-ref': capture, 'reference': reference}
        data = adapt_inputs(SourceRegistry([BudgetV2Adapter]), captures,
                            [{**selection, 'role': 'PRIMARY', 'columnNames': ['metric1']},
                             {**compare, 'role': 'REFERENCE', 'rowIndices': [0], 'columnNames': ['metric1']}],
                            {'label': '分组与总体对照', 'selectionReason': '用户要求'}, 100)
        bind_source_origins(data['facts'], captures)
        return data

    def test_null_group_share_with_different_reference_grain(self):
        data = self.data()
        self.assertEqual(data['scope']['memberDimension'], 'PER_INPUT_GRAIN')
        primary = [fact for fact in data['facts'] if fact['inputRole'] == 'PRIMARY']
        results = calculate(data['facts'], data['scope'], [
            {'id': 'total', 'operator': 'SUM'},
            {'id': 'share', 'operator': 'RATIO', 'inputRefs': [primary[0]['factId'], 'total']},
            {'id': 'rank', 'operator': 'RANK'},
        ], RuleRegistry())
        self.assertTrue(all(result['calculationValidation'] == 'PASS' for result in results), results)
        self.assertEqual(results[0]['value'], '86293800.00')
        self.assertEqual(set(results[1]['sourceFactIds']), {fact['factId'] for fact in primary})
        self.assertIsNone(primary[0]['dimensions']['MD_ORG__MD_YWBK'])
        self.assertEqual(data['scope']['sourceScopes'][0]['omittedRows'], [])
        from analysis_extension.validation import validate_source_chart
        validate_source_chart(primary, data['scope'])
        with self.assertRaises(AnalysisError):
            validate_source_chart(data['facts'], data['scope'])
        unidentified = deepcopy(primary)
        unidentified[0].pop('metricKey')
        with self.assertRaises(AnalysisError) as raised:
            validate_source_chart(unidentified, data['scope'])
        self.assertEqual(raised.exception.code, 'SOURCE_CHART_UNCONFIRMED')

    def test_grain_does_not_relax_period_metric_or_unselected_dimensions(self):
        data = self.data()
        primary = [fact for fact in data['facts'] if fact['inputRole'] == 'PRIMARY']
        for field, value, code in [('period', '2025N0001', 'COMPARABILITY_UNCONFIRMED'),
                                    ('metricKey', 'different-metric', 'METRIC_CONFLICT'),
                                    ('unit', '吨', 'UNIT_CONFLICT')]:
            facts = deepcopy(primary)
            facts[1][field] = value
            result = calculate(facts, data['scope'], [{'id': 'ratio', 'operator': 'RATIO',
                               'inputRefs': [fact['factId'] for fact in facts]}], RuleRegistry())[0]
            self.assertEqual(result['diagnostics'][0]['code'], code)
        facts = deepcopy(primary)
        facts[1]['dimensions']['MD_SCENE'] = 'ACTUAL'
        result = calculate(facts, data['scope'], [{'id': 'ratio', 'operator': 'RATIO',
                           'inputRefs': [fact['factId'] for fact in facts]}], RuleRegistry())[0]
        self.assertEqual(result['diagnostics'][0]['code'], 'METRIC_CONFLICT')


class OperationContinuationTests(TestCase):
    def base(self):
        return {'plan': {'operations': [
            {'id': 'total', 'operator': 'SUM'},
            {'id': 'oldShare', 'operator': 'RATIO', 'inputRefs': ['fact_a', 'total']},
            {'id': 'retainedShare', 'operator': 'RATIO', 'inputRefs': ['fact_b', 'total']} ]},
            'derivedValues': [
                {'derivedId': 'derived_total', 'calculationValidation': 'PASS', 'inputRefs': ['fact_a', 'fact_b']},
                {'derivedId': 'derived_oldShare', 'calculationValidation': 'BLOCKED'},
                {'derivedId': 'derived_retainedShare', 'calculationValidation': 'BLOCKED'}]}

    def test_replacement_discards_orphan_failure_but_keeps_required_dependencies(self):
        operations = merge_operations(self.base(), [
            {'id': 'total', 'operator': 'SUM', 'inputRefs': ['fact_a', 'fact_b']},
            {'id': 'newShare', 'operator': 'RATIO', 'inputRefs': ['fact_a', 'total']}],
            [{'kind': 'COMPUTATION', 'refs': ['derived_newShare', 'derived_retainedShare']}])
        names = [operation['id'] for operation in operations]
        self.assertNotIn('oldShare', names)
        self.assertLess(names.index('total'), names.index('retainedShare'))
        self.assertLess(names.index('total'), names.index('newShare'))

    def test_referenced_failure_is_not_discarded_and_old_selector_is_frozen(self):
        operations = merge_operations(self.base(), [], [{'kind': 'COMPUTATION', 'refs': ['derived_oldShare']}])
        self.assertEqual([operation['id'] for operation in operations], ['total', 'oldShare'])
        self.assertEqual(operations[0]['inputRefs'], ['fact_a', 'fact_b'])

    def test_cycle_returns_actionable_error_without_source_query(self):
        with self.assertRaises(AnalysisError) as raised:
            merge_operations(self.base(), [{'id': 'total', 'operator': 'SUM', 'inputRefs': ['oldShare']}], [])
        self.assertEqual(raised.exception.code, 'CYCLIC_OPERATIONS')
        self.assertFalse(raised.exception.details['requiresSourceQuery'])

    def test_duplicate_new_operations_are_rejected_instead_of_overwritten(self):
        with self.assertRaises(AnalysisError) as raised:
            merge_operations(self.base(), [{'id': 'new', 'operator': 'COUNT'},
                                           {'id': 'new', 'operator': 'COUNT'}], [])
        self.assertEqual(raised.exception.code, 'INVALID_OPERATION_ID')


class ReviewedParagraphTests(TestCase):
    def test_preserves_qualifiers_and_skips_graphs_scripts_tables_and_hidden_text(self):
        parser = ReviewedParagraphs()
        parser.feed('<html><head><title>非正文</title></head><body><h2>构成说明</h2>'
                    '<p>仅核对<strong>本层</strong>，不代表全链闭合。<br>来源值保持不变。</p>'
                    '<svg><text>错误图形</text></svg><script>伪造说明</script>'
                    '<table><tr><td><p>表内读数</p></td></tr></table>'
                    '<div hidden><p>隐藏说明</p></div></body></html>')
        self.assertEqual(parser.paragraphs, ['构成说明', '仅核对本层，不代表全链闭合。\n来源值保持不变。'])


class ReviewedCloseoutTests(PresentationCase):
    observe = recovery_fixtures.CloseoutRecoveryTests.observe

    async def preview(self, issues=None, broken=True, partial_reason=None):
        from analysis_closeout_recovery_test import CloseoutRecoveryTests
        from analysis_turn_completion_test import TurnCompletionTests
        from analysis_document_checks_test import measurements
        self.turn = CloseoutRecoveryTests.turn(self)
        artifact = await TurnCompletionTests.generic_result(self, self.turn, published=False)
        fact = artifact['facts'][0]
        from analysis_extension.turn_completion import review_answer
        model_review = patch('analysis_extension.turn_completion._model_review',
                             AsyncMock(return_value={'issues': [{'severity': 'BLOCKING', **issue}
                                                                for issue in issues or []]}))
        model_review.start()
        self.addCleanup(model_review.stop)
        async def review(current):
            return await review_answer(None, self.turn, current, '')
        self.service.preview_reviewer = AsyncMock(side_effect=review)
        samples = []
        for width in (420, 900):
            sample = measurements(width=width)
            sample['orders'] = []
            samples.append(sample)
        self.service.render_checker = AsyncMock()
        self.service.render_checker.check.return_value = samples
        document = {'title': '结果分析', 'html': '<html><head></head><body>'
                    '<h2>已核对的说明</h2><p>来源金额为 {{amount}}，空属性按来源保留。</p>'
                    '<svg><rect data-analysis-scale="bad"></rect></svg></body></html>',
                    'bindings': [{'key': 'amount', 'valueRef': fact['factId']}]}
        if partial_reason:
            document['html'] = document['html'].replace('<body>', '<body><p>{{analysisPartialReason}}</p>')
        work_refs = [{'taskId': 'values', 'refs': [value['factId'] for value in artifact['facts']]}]
        # 先完成真正的内容审查；随后只修改图形而产生展示错误时，保留已审查的解释。
        checked = await self.service.publish(artifact['analysisRef'], requestKey='checked-preview',
                                            document=document, previewOnly=True, workRefs=work_refs,
                                            partialReason=partial_reason)
        if not broken:
            return artifact, checked
        for sample in samples:
            sample['numericChecks']['linearBars'].update(declared=1, measured=1, invalid=1)
            sample['errors'] = ['线性条形缺少有效绑定']
        document['html'] = document['html'].replace('<svg>', '<svg class="revised-layout">')
        preview = await self.service.publish(artifact['analysisRef'], requestKey='broken-display-preview',
                                            document=document, previewOnly=True, workRefs=work_refs)
        self.assertEqual(preview['nextAction'], 'REPAIR_DOCUMENT')
        return artifact, preview

    async def test_closeout_confirms_approved_preview_without_a_new_model_call(self):
        from analysis_extension.middleware import AnalysisMiddleware
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        artifact, preview = await self.preview(broken=False)
        self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM')
        answer = await middleware._closeout('REPAIR_TIME_LIMIT')
        self.assertEqual(self.turn.state['hostCloseout']['status'], 'READY', answer)
        self.assertEqual(self.turn.state['analysisRef'], artifact['analysisRef'])
        saved = self.store.get_analysis(OWNER, artifact['analysisRef'])
        self.assertEqual(saved['publicationState'], 'PUBLISHED')
        self.assertNotIn('hostSummary', saved)
        self.assertEqual(saved['freeDocument']['contentDigest'], preview['diagnostics']['document']['contentDigest'])
        self.service.preview_reviewer.assert_awaited_once()
        self.assertEqual(await middleware._closeout('REPAIR_TIME_LIMIT'), answer)

    async def test_closeout_keeps_approved_partial_document_and_its_real_limitation(self):
        from analysis_extension.middleware import AnalysisMiddleware
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        reason = '某项来源为空，本次保留已有金额和局限。'
        artifact, _ = await self.preview(broken=False, partial_reason=reason)
        answer = await middleware._closeout('REPAIR_TIME_LIMIT')
        self.assertEqual(self.turn.state['hostCloseout']['status'], 'PARTIAL', answer)
        self.assertIn(reason, answer)
        self.assertNotIn('分析解释尚未完成', answer)
        saved = self.store.get_analysis(OWNER, artifact['analysisRef'])
        self.assertIn('freeDocument', saved)
        self.assertNotIn('hostSummary', saved)
        self.assertEqual(saved['partialReason'], reason)
        self.service.preview_reviewer.assert_awaited_once()

    async def test_closeout_cannot_confirm_a_preview_after_its_evidence_changed(self):
        from analysis_extension.middleware import AnalysisMiddleware
        from analysis_extension.review_control import progress
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        artifact, _ = await self.preview(broken=False)
        progress(self.turn, lambda state: state['lastPublicationPreview'].update(evidenceDigest='changed'))
        from types import SimpleNamespace
        from analysis_model_closeout_test import summary_response
        agent = SimpleNamespace(model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response)))
        answer = await middleware._closeout('REPAIR_TIME_LIMIT', agent)
        self.assertEqual(self.turn.state['hostCloseout']['status'], 'PARTIAL', answer)
        saved = self.store.get_analysis(OWNER, self.turn.state['analysisRef'])
        self.assertNotIn('freeDocument', saved)
        self.assertEqual(self.store.get_analysis(OWNER, artifact['analysisRef'])['publicationState'], 'DRAFT')
        self.service.preview_reviewer.assert_awaited_once()

    async def test_closeout_confirmation_cannot_replace_a_newer_published_revision(self):
        artifact, _ = await self.preview(broken=False)
        fallback = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_TIME_LIMIT')
        with self.assertRaises(AnalysisError) as raised:
            await self.service.publish_reviewed_preview(artifact['analysisRef'])
        self.assertEqual(raised.exception.code, 'VERSION_CONFLICT')
        self.assertEqual(self.store.get_analysis(OWNER, artifact['analysisRef'])['publicationState'], 'DRAFT')
        self.assertEqual(self.store.get_analysis(OWNER, fallback['analysisRef'])['publicationState'], 'PUBLISHED')
        self.service.preview_reviewer.assert_awaited_once()

    async def test_chart_failure_retains_reviewed_explanation_without_model_retry(self):
        from analysis_extension.presentation import project
        artifact, preview = await self.preview()
        result = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_LIMIT')
        saved = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(saved['hostSummary']['semanticReview'], 'PASS')
        self.assertEqual(saved['hostSummary']['content'], 'REVIEWED_ANALYSIS')
        self.assertNotIn('freeDocument', saved)
        self.assertEqual(saved['facts'], artifact['facts'])
        projected = project(saved, self.rules)
        self.assertIn('已核对的说明', projected['claims'])
        self.assertTrue(any('空属性按来源保留' in claim for claim in projected['claims']))
        self.assertIn('图表', saved['partialReason'])
        self.service.preview_reviewer.assert_awaited_once()
        self.assertTrue(result['delivery']['registered'])

    async def test_unapproved_explanation_is_never_carried_into_fact_summary(self):
        artifact, _ = await self.preview([{'code': 'ANSWER_MISMATCH', 'message': '说明不符合来源'}])
        result = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_LIMIT')
        saved = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(saved['hostSummary']['semanticReview'], 'REVISE')
        self.assertEqual(saved['hostSummary']['content'], 'SOURCE_FACTS_ONLY')
        self.assertNotIn('reviewedParagraphs', saved['hostSummary'])

    async def test_changed_evidence_cannot_reuse_a_previous_preview_pass(self):
        from analysis_extension.review_control import progress
        artifact, _ = await self.preview()
        progress(self.turn, lambda state: state['lastPublicationPreview'].update(evidenceDigest='changed'))
        result = await self.service.publish_available_facts(artifact['analysisRef'], 'REPAIR_LIMIT')
        saved = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(saved['hostSummary']['content'], 'SOURCE_FACTS_ONLY')
        self.assertNotEqual(saved['hostSummary']['semanticReview'], 'PASS')

    async def test_host_final_status_reports_display_failure_and_actual_review_pass(self):
        from analysis_extension.middleware import AnalysisMiddleware
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize, self.rules, delivery=self.delivery)
        self.service = middleware.service
        artifact, _ = await self.preview()
        answer = await middleware._closeout('REPAIR_LIMIT')
        self.assertEqual(self.turn.state['hostCloseout']['status'], 'PARTIAL', answer)
        self.assertEqual(self.turn.state['hostCloseout']['semanticReview'], 'PASS')
        self.assertIn('图表', answer)
        self.assertNotIn('分析解释尚未完成', answer)
        self.service.preview_reviewer.assert_awaited_once()

    async def test_reference_values_have_their_own_table(self):
        # 使用测试存储真实捕获入口，参考范围不进入主体事实表。
        from analysis_source_boundary_test import SourceBoundaryTests
        capture, selectors, scope = SourceBoundaryTests.json_capture(self, ('10', '20', '90'), unit='元')
        scope.pop('population', None)
        scope['referenceMembers'], scope['members'] = scope['members'][2:], scope['members'][:2]
        built = await self.service.build([capture], selectors, scope, [], 'reference-closeout')
        result = await self.service.publish_available_facts(built['analysisRef'], 'REPAIR_LIMIT')
        saved = self.store.get_analysis(OWNER, result['analysisRef'])
        tables = [view for view in saved['viewPlan'] if view['type'] == 'TABLE']
        self.assertEqual([len(view['valueRefs']) for view in tables], [2, 1])
