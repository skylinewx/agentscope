"""批量分析、整表依赖与来源身份的组合回归。"""
from copy import deepcopy
import json
from unittest import TestCase

from analysis_fixtures import fixture
from analysis_extension.adapters.budget_v2 import BudgetV2Adapter
from analysis_extension.analysis_inputs import adapt_inputs
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.formulas import trace_formula
from analysis_extension.identity import same_source_position
from analysis_extension.sources import SourceRegistry
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
from analysis_extension.models import AnalysisError


class SourceIdentityTests(TestCase):
    def form_data(self):
        capture, selectors, _ = fixture(('10', '20'))
        capture['captureRef'] = 'capture'
        receipt = json.loads(capture['response']['content'][0]['text'])
        for index, step in enumerate(receipt['data']['stepResults'][1:]):
            form = step['formResults'][0]
            form['runtime']['observation']['entryType'] = 'INPUT_VO'
            cell = form['page']['cells'][0]
            cell['formula'] = 'A3'
            operand = deepcopy(cell)
            operand.update(key='3:1', row=3, formula=None)
            operand['dimensions']['ACCOUNT'] = 'income'
            form['page']['cells'].append(operand)
            form['page'].update(endRowExclusive=4, totalRows=4)
            receipt['evidence'][index]['fact'] = deepcopy(form['runtime'])
            capture['request']['plan']['steps'][index + 1]['formQuery']['targetDimensions'] = {'ACCOUNT': 'profit'}
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        selectors = [{**selector, 'captureRef': 'capture', 'cellKeys': ['2:1', '3:1'],
                      'role': 'PRIMARY', 'grain': ['MD_ORG', 'ACCOUNT']} for selector in selectors]
        return adapt_inputs(SourceRegistry([BudgetV2Adapter]), {'capture': capture}, selectors,
                            {'label': '构成', 'selectionReason': '分析原表公式'}, 100)

    def test_page_dependencies_cross_accounts_without_crossing_observations(self):
        data = self.form_data()
        self.assertEqual(len(data['facts']), 4)
        targets = [fact for fact in data['facts'] if fact['formula']]
        for target in targets:
            trace = trace_formula(data['facts'], target['factId'])
            self.assertEqual(trace['status'], 'MATCH')
            self.assertEqual(trace['value'], target['value'])
            self.assertEqual(trace['pendingDependencies'], [])
        incomplete = [fact for fact in data['facts'] if not
                      (fact['observationId'] == targets[0]['observationId'] and fact['cellAddress'] == 'A3')]
        self.assertEqual(trace_formula(incomplete, targets[0]['factId'])['status'], 'BLOCKED')

    def test_formula_evidence_matches_full_source_position_not_analysis_grain(self):
        form = next(fact for fact in self.form_data()['facts'] if fact['formula'])
        batch = deepcopy(form)
        batch.update(factId='batch-value', objectId='different-analysis-group', grain=['DATATIME', 'MD_ORG'],
                     sourceMeasureCode=form['measureCode'], measureCode='metric1', formula=None, formulaStatus='NOT_CAPTURED')
        self.assertTrue(same_source_position(batch, form))
        for dimension in ('DATATIME', 'MD_ORG', 'ACCOUNT', 'SCENE', 'MD_CURRENCY'):
            wrong = deepcopy(batch)
            wrong['dimensions'][dimension] = 'different'
            self.assertFalse(same_source_position(wrong, form), dimension)
        for field in ('bindingRef', 'sourceContextId', 'sourceMeasureCode'):
            wrong = {**batch, field: 'different'}
            self.assertFalse(same_source_position(wrong, form), field)

    def test_real_null_dependency_stays_unavailable(self):
        data = self.form_data()
        target = next(fact for fact in data['facts'] if fact['formula'])
        operand = next(fact for fact in data['facts'] if fact['observationId'] == target['observationId'] and not fact['formula'])
        operand.update(value=None, valueState='NO_VALUE')
        trace = trace_formula(data['facts'], target['factId'])
        self.assertEqual(trace['status'], 'BLOCKED')
        self.assertEqual(trace['pendingDependencies'], [])
        self.assertTrue(any(d['code'] == 'FORMULA_SOURCE_VALUE_MISSING' for n in trace['nodes'] for d in n['diagnostics']))
        self.assertEqual(trace['executionStatus'], 'COMPLETED')
        self.assertEqual(trace['completionReason'], 'SOURCE_VALUE_MISSING')
        self.assertEqual(trace['sourceConsistency'], 'NOT_CHECKED')
        self.assertEqual(trace['recovery']['nextAction'], 'EXPLAIN_SOURCE_NULL')

    def test_dependent_calculations_accept_plan_names_and_save_actual_refs(self):
        facts = [{'factId': 'f1', 'objectId': 'one', 'value': '3', 'valueState': 'PRESENT'},
                 {'factId': 'f2', 'objectId': 'two', 'value': '7', 'valueState': 'PRESENT'}]
        operations = [{'id': 'total', 'operator': 'ARITHMETIC', 'expression': 'v0+v1', 'inputRefs': ['f1', 'f2']},
                      {'id': 'share', 'operator': 'ARITHMETIC', 'expression': 'v0/v1*100', 'inputRefs': ['f1', 'total']}]
        result = calculate(facts, {'scopeId': 'scope', 'members': ['one', 'two']}, operations, RuleRegistry())
        self.assertEqual(result[-1]['value'], '30')
        self.assertEqual(result[-1]['inputRefs'], ['f1', 'derived_total'])

    def test_source_sum_is_limited_to_disjoint_groups_of_one_native_batch(self):
        from analysis_budget_batch_test import batch_fixture
        capture, selection, scope, _ = batch_fixture()
        selection['columnNames'] = ['metric1']
        data = BudgetV2Adapter().adapt({'batch-ref': capture}, [selection], scope, 100)
        operation = [{'id': 'sum', 'operator': 'SUM'}]
        result = calculate(data['facts'], data['scope'], operation, RuleRegistry())[0]
        self.assertEqual(result['value'], '86293800.00', result)
        self.assertTrue(result['ruleSource'].startswith('native_aggregation:'))
        # 来源指标可加不等于任意派生结果可加；只能对原生聚合值自动取规则。
        dependent = calculate(data['facts'], data['scope'], operation +
                              [{'id': 'sumDerived', 'operator': 'SUM', 'inputRefs': ['sum']}], RuleRegistry())[-1]
        self.assertEqual(dependent['calculationValidation'], 'BLOCKED')
        self.assertEqual(dependent['diagnostics'][0]['code'], 'RULE_REQUIRED')
        for field, value in (('calcType', 'AVG'), ('batchId', 'different'), ('period', '2025N0001')):
            changed = deepcopy(data['facts'])
            changed[0][field] = value
            blocked = calculate(changed, data['scope'], operation, RuleRegistry())[0]
            self.assertEqual(blocked['calculationValidation'], 'BLOCKED', field)
        duplicated = data['facts'] + [deepcopy(data['facts'][0])]
        duplicated[-1]['factId'] = 'another-observation'
        self.assertEqual(calculate(duplicated, data['scope'], operation, RuleRegistry())[0]['calculationValidation'], 'BLOCKED')


class PublicationFlowTests(PresentationCase):
    async def test_formula_inspection_reuses_captured_but_unselected_dependencies(self):
        capture, selectors, scope = fixture(('10',))
        receipt = json.loads(capture['response']['content'][0]['text'])
        form = receipt['data']['stepResults'][1]['formResults'][0]
        target = form['page']['cells'][0]
        target['formula'] = 'A3+A4'
        operand = deepcopy(target)
        operand.update(key='3:1', row=3, formula=None)
        form['page']['cells'].append(operand)
        form['page'].update(endRowExclusive=4, totalRows=4)
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        capture_ref = self.store.capture(OWNER, capture)
        selectors[0]['captureRef'] = capture_ref
        scope['population']['captureRef'] = capture_ref
        built = await self.service.build([capture_ref], selectors, scope, [], 'dependencies')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        result = await self.service.inspect(section='formulas', analysisRef=built['analysisRef'],
                                            factRef=artifact['facts'][0]['factId'])
        dependencies = {item['address']: item for item in result['pendingDependencies']['items']}
        self.assertEqual(dependencies['A3']['captureState'], 'CAPTURED_NOT_SELECTED')
        self.assertFalse(dependencies['A3']['requiresSourceQuery'])
        self.assertEqual(dependencies['A3']['selections'][0]['cellKeys'], ['3:1'])
        self.assertEqual(dependencies['A4']['captureState'], 'NOT_CAPTURED_OR_UNMAPPED')

    async def request(self):
        from analysis_free_document_test import FreeDocumentTests
        request = await FreeDocumentTests.request(self)
        request['renderCheckUnavailableReason'] = '离线契约测试不启动浏览器，另有真实浏览器回归。'
        return request

    async def test_published_text_edit_forks_presentation_without_rebuilding_data(self):
        request = await self.request()
        preview = await self.service.publish(**request, previewOnly=True)
        first = await self.service.publish(**preview['confirmPublish'])
        original = self.store.get_analysis(OWNER, first['analysisRef'])
        request['document']['html'] = request['document']['html'].replace('<h1>比较</h1>', '<h1>预算比较</h1>')
        revised = await self.service.publish(**{**request, 'requestKey': 'edit-published'}, previewOnly=True)
        self.assertNotEqual(revised['analysisRef'], first['analysisRef'])
        draft = self.store.get_analysis(OWNER, revised['analysisRef'])
        for field in ('facts', 'derivedValues', 'inputDigest', 'verificationRecords'):
            self.assertEqual(draft[field], original[field])
        self.assertNotIn('expectedBaseRevision', revised['confirmPublish'])
        published = await self.service.publish(**revised['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])
        self.assertEqual(self.store.get_analysis(OWNER, first['analysisRef']), original)
        self.assertEqual(await self.service.publish(**revised['confirmPublish']), published)

    async def test_frozen_draft_can_be_refreshed_from_workspace_file_without_resending_bindings(self):
        request = await self.request()
        self.delivery.root.mkdir(parents=True)
        path = self.delivery.root / 'report.html'
        path.write_text(request['document'].pop('html'), encoding='utf-8')
        request['document']['path'] = 'report.html'
        preview = await self.service.publish(**request, previewOnly=True)
        path.write_text(path.read_text('utf-8').replace('<h1>比较</h1>', '<h1>修订说明</h1>'), 'utf-8')
        revised = await self.service.publish(**{**preview['confirmPublish'], 'requestKey': 'reload-file',
            'repairs': [{'op': 'replace', 'path': '/document/path', 'value': 'report.html'}]}, previewOnly=True)
        saved = self.store.get_publication_proposal(OWNER, revised['repairRef'])
        self.assertIn('修订说明', saved['document']['html'])
        self.assertEqual(saved['document']['bindings'], request['document']['bindings'])

    async def test_content_review_follows_valid_render_before_publication(self):
        import asyncio
        from types import SimpleNamespace
        from analysis_document_checks_test import measurements
        request = await self.request()
        rendering = asyncio.Event()
        async def review(artifact):
            self.assertTrue(rendering.is_set(), '页面测量应先于内容审查完成')
            return {'issues': [{'code': 'UNSUPPORTED_CAUSE', 'message': '缺少原因证据。'}]}
        async def render(document):
            samples = [measurements(width=width) for width in (420, 900)]
            for sample in samples:
                sample['orders'] = []
            rendering.set()
            return samples
        self.service.preview_reviewer = review
        self.service.render_checker = SimpleNamespace(check=render)
        preview = await asyncio.wait_for(self.service.publish(**request, previewOnly=True), 2)
        self.assertEqual(preview['nextAction'], 'REPAIR_DOCUMENT')
        self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'REVISE')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_SEMANTIC_ERRORS')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')

    async def test_review_provider_failure_preserves_saved_preview_without_publishing(self):
        from unittest.mock import AsyncMock
        request = await self.request()
        self.service.preview_reviewer = AsyncMock(side_effect=RuntimeError('fixture provider unavailable'))
        with self.assertLogs('analysis_extension.service', level='ERROR'):
            preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['nextAction'], 'RETRY_SAVED_PREVIEW')
        self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'UNAVAILABLE')
        self.assertTrue(self.store.get_publication_proposal(OWNER, preview['repairRef'])['document']['html'])
        with self.assertLogs('analysis_extension.service', level='ERROR'), self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'REVIEW_UNAVAILABLE')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')

    async def test_removing_numeric_markers_does_not_clear_failed_coverage(self):
        from analysis_document_checks_test import measurements
        from analysis_extension.document_checks import evaluate
        from analysis_extension.free_document import compile_document
        request = await self.request()
        artifact = self.store.get_analysis(OWNER, self.ref)
        compiled = compile_document(artifact, request['document'])
        bad = measurements()
        bad['orders'] = []
        bad['numericChecks']['linearBars'].update(declared=2, measured=2, invalid=2)
        self.store.save_document_check(OWNER, self.ref, evaluate(compiled, bad))
        request['document']['html'] = request['document']['html'].replace('<h1>', '<h1 class="edited">')
        preview = await self.service.publish(**request, previewOnly=True)
        current = compile_document(artifact, request['document'])
        for width in (420, 900):
            empty = measurements(width=width)
            empty['orders'] = []
            self.store.save_document_check(OWNER, self.ref, evaluate(current, empty))
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_CHECK_COVERAGE_LOST')

    async def test_responsive_layout_can_show_fewer_bars_in_one_viewport(self):
        from analysis_document_checks_test import measurements
        from analysis_extension.document_checks import evaluate, require_publication_checks
        from analysis_extension.free_document import compile_document
        request = await self.request()
        document = compile_document(self.store.get_analysis(OWNER, self.ref), request['document'])
        records = []
        for width, count in ((420, 1), (900, 2)):
            sample = measurements(width=width)
            sample['orders'] = []
            sample['numericChecks']['linearBars'].update(declared=count, measured=count, invalid=0,
                                                        scaleGroups=1, checkedGroups=1)
            records.append(evaluate(document, sample))
        # 窄宽度的响应式简化不等于删除检查；完整图形在另一个实际宽度已测量。
        require_publication_checks(document, records, requirements={'linearBars': 2})

    async def test_document_review_is_reused_and_total_timings_keep_all_attempts(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from analysis_extension.turn_completion import review_answer, compact_analysis_evidence, review_payload
        request = await self.request()
        artifact = self.store.get_analysis(OWNER, self.ref)
        from analysis_extension.free_document import compile_document
        artifact['freeDocument'] = compile_document(artifact, request['document'])
        turn = SimpleNamespace(service=self.service, state={'request': '比较预算', 'goal': '预算比较', 'mode': 'ANALYSIS',
                                     'delivery': {'markdown': '[结果](saved-report)'}})
        full = json.loads(review_payload(turn, artifact, ''))
        compact = compact_analysis_evidence(full)
        for old, fact in zip(full['facts'], compact['facts']):
            restored = {**compact.get('commonFactContext', {}), **compact['factContexts'][fact['factContextRef']], **fact}
            restored.pop('factContextRef')
            for field, common in compact['commonFactDimensions'].items():
                if field in restored:
                    restored[field] = {**common, **restored[field]}
            self.assertEqual(restored, old)
        with patch('analysis_extension.turn_completion._model_review', new=AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            await review_answer(None, turn, artifact, '')
            await review_answer(None, turn, artifact, '[结果](saved-report)')
            self.assertEqual(model.await_count, 1)
            changed = deepcopy(artifact)
            changed['freeDocument']['html'] += '<p>新增没有证据的解释</p>'
            await review_answer(None, turn, changed, '')
            self.assertEqual(model.await_count, 2)
        self.assertEqual(turn.state['analysisReviewTotals']['attempts'], 3)
        self.assertEqual(turn.state['analysisReviewTotals']['reusedPartCount'], 1)
