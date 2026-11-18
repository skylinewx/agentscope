"""Data-first planning: execution-based corrections never erase business obligations."""
from copy import deepcopy
from unittest import TestCase
from uuid import uuid4

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.completion import apply_publication_completion, completion_summary, continue_plan
from analysis_extension.models import AnalysisError
from analysis_extension.plan_revisions import revision_basis
from analysis_extension.protocol import validate_artifact


class PlanRevisionTests(PresentationCase):
    async def prepare(self, *, rule=None, dependent=False):
        key = uuid4().hex
        capture, selections, scope = saved_fixture(self.store, ('-3', '-2', '10'))
        initial = await self.service.build([capture], selections, scope, [], 'inputs-' + key)
        refs = [f['factId'] for f in self.store.get_analysis(OWNER, initial['analysisRef'])['facts'][:2]]
        operations = [{'id': 'loss', 'operator': 'SUM', 'inputRefs': refs, **({'ruleRef': rule} if rule else {})},
                      {'id': 'stats', 'operator': 'DESCRIBE', 'inputRefs': refs}]
        work = [{'taskId': 'loss', 'title': '所选对象金额统计', 'kind': 'COMPUTATION', 'operator': 'SUM', 'refs': ['loss']}]
        if dependent:
            operations.append({'id': 'share', 'operator': 'ARITHMETIC', 'inputRefs': ['loss'], 'expression': 'v0/10'})
            work.append({'taskId': 'share', 'title': '比较比例', 'kind': 'COMPUTATION', 'operator': 'ARITHMETIC', 'refs': ['share']})
        built = await self.service.build(baseAnalysisRef=initial['analysisRef'], operations=operations, workPlan=work, requestKey='wrong-method-' + key)
        return built, self.store.get_analysis(OWNER, built['analysisRef']), refs

    def correction(self, **kwargs):
        return {'taskId': 'loss', 'title': '不能替换原业务标题', 'kind': 'COMPUTATION', 'operator': 'DESCRIBE',
                'refs': [{'valueRef': 'derived_stats', 'field': 'sum'}],
                'revisionReason': '来源行描述统计误选了要求业务规则的 SUM，改用同一批原值的描述统计。', **kwargs}

    async def test_build_replaces_failed_method_and_preserves_frozen_original(self):
        built, before, refs = await self.prepare()
        output = await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[],
                                          workPlan=[self.correction()], requestKey='corrected')
        current = self.store.get_analysis(OWNER, output['analysisRef'])
        self.assertEqual(output['completion']['state'], 'READY')
        self.assertEqual(current['plan']['workPlan'][0]['title'], '所选对象金额统计')
        self.assertEqual(set(current['plan']['workPlan'][0]['targetRefs']), set(refs))
        self.assertEqual(current['methodRevisions'][0]['failureCodes'], ['RULE_REQUIRED'])
        self.assertNotIn('derived_loss', {v['derivedId'] for v in current['derivedValues']})
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef']), before)
        validate_artifact(current)
        next_version = await self.service.build(baseAnalysisRef=output['analysisRef'], operations=[], requestKey='continue')
        self.assertEqual(next_version['completion']['state'], 'READY')
        self.assertEqual(self.store.get_analysis(OWNER, next_version['analysisRef'])['methodRevisions'], current['methodRevisions'])

    async def test_unexecuted_method_can_be_corrected_with_exact_target_coverage(self):
        capture, selections, scope = saved_fixture(self.store, ('-3', '-2', '10'))
        initial = await self.service.build([capture], selections, scope, [], 'unexecuted-inputs')
        refs = [f['factId'] for f in self.store.get_analysis(OWNER, initial['analysisRef'])['facts'][:2]]
        pending = await self.service.build(baseAnalysisRef=initial['analysisRef'], operations=[],
            workPlan=[{'taskId':'loss','title':'所选对象金额统计','kind':'COMPUTATION',
                       'operator':'SUM','refs':[], 'targetRefs':refs}], requestKey='unexecuted-plan')
        result = await self.service.build(baseAnalysisRef=pending['analysisRef'],
            operations=[{'id':'stats','operator':'DESCRIBE','inputRefs':refs}],
            workPlan=[self.correction()], requestKey='unexecuted-correction')
        self.assertEqual(result['completion']['state'], 'READY')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(artifact['methodRevisions'][0]['failureCodes'], [])
        self.assertEqual(set(artifact['methodRevisions'][0]['targetRefs']), set(refs))

    async def test_publication_rebinding_is_atomic_and_reopens_without_old_failure(self):
        _, artifact, refs = await self.prepare()
        update = {key: value for key, value in self.correction().items() if key in ('taskId', 'refs', 'operator', 'revisionReason')}
        output = apply_publication_completion(artifact, {'workRefs': [update]})
        self.assertEqual(output['state'], 'READY')
        self.assertNotIn('workRefs', artifact)
        self.assertEqual(artifact['plan']['workPlan'][0]['operator'], 'DESCRIBE')
        self.assertEqual(set(artifact['plan']['workPlan'][0]['targetRefs']), set(refs))
        self.assertEqual(artifact['calculationValidation'], 'PASS')
        validate_artifact(artifact)
        apply_publication_completion(artifact, {'workRefs': [update]})
        self.assertEqual(len(artifact['methodRevisions']), 1)
        self.assertEqual(completion_summary(artifact)['state'], 'READY')

    async def test_preview_confirm_and_later_presentation_revision_share_correction(self):
        built, artifact, _ = await self.prepare()
        update = {key: value for key, value in self.correction().items() if key in ('taskId', 'refs', 'operator', 'revisionReason')}
        preview = await self.service.publish(built['analysisRef'], [], [{
            'viewId': 'values', 'type': 'TABLE', 'scopeId': artifact['scope']['scopeId'],
            'valueRefs': [f['factId'] for f in artifact['facts']], 'showUnit': True}],
            'corrected-preview', workRefs=[update], previewOnly=True)
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])
        current = self.store.get_analysis(OWNER, published['analysisRef'])
        self.assertEqual(completion_summary(current)['state'], 'READY')
        self.assertEqual(len(current['methodRevisions']), 1)
        revised = await self.service.build(baseAnalysisRef=current['analysisRef'], operations=[], requestKey='after-publish')
        self.assertEqual(revised['completion']['state'], 'READY')

    async def test_wrong_subset_or_reference_population_cannot_complete_correction(self):
        built, before, refs = await self.prepare()
        all_refs = [f['factId'] for f in before['facts']]
        for index, inputs in enumerate((refs[:1], all_refs)):
            with self.subTest(inputs=inputs), self.assertRaises(AnalysisError) as error:
                await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[
                    {'id': 'other', 'operator': 'DESCRIBE', 'inputRefs': inputs}],
                    workPlan=[self.correction(refs=[{'valueRef': 'other', 'field': 'sum'}])], requestKey='wrong-' + str(index))
            self.assertEqual(error.exception.code, 'INVALID_WORK_PLAN')
        original = deepcopy(before)
        with self.assertRaises(AnalysisError):
            apply_publication_completion(before, {'workRefs': [{'taskId': 'loss', 'operator': 'DESCRIBE',
                'refs': [{'valueRef': 'derived_stats', 'field': 'minimum'}], 'revisionReason': '改成最小值'}]})
        self.assertEqual(before, original)

    async def test_store_rederives_revision_and_rejects_forged_facts_and_audit(self):
        built, artifact, _ = await self.prepare()
        update = {key: value for key, value in self.correction().items() if key in ('taskId', 'refs', 'operator', 'revisionReason')}
        request = {'workRefs': [update]}
        proposed = deepcopy(artifact)
        apply_publication_completion(proposed, request)
        changed = deepcopy(proposed)
        changed['facts'][0]['value'] = '999'
        forged = deepcopy(proposed)
        forged['methodRevisions'][0]['reason'] = '伪造记录'
        for index, value in enumerate((changed, forged)):
            with self.subTest(index=index), self.assertRaises(AnalysisError) as error:
                self.store.publish(OWNER, built['analysisRef'], value, request, 'tampered-' + str(index), artifact['baseRevision'])
            self.assertEqual(error.exception.code, 'IMMUTABLE_FACTS')
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef']), artifact)

    async def test_reason_is_not_execution_evidence_and_valid_business_rules_remain(self):
        for rule in ('fixture-additive', 'nonexistent-explicit-rule'):
            with self.subTest(rule=rule):
                built, artifact, _ = await self.prepare(rule=rule)
                with self.assertRaises(AnalysisError):
                    continue_plan(artifact, artifact['scope'], [self.correction()])
        _, artifact, _ = await self.prepare()
        artifact['plan']['workPlan'][0]['refs'] = []
        with self.assertRaises(AnalysisError):
            continue_plan(artifact, artifact['scope'], [self.correction()])

    async def test_failed_replacement_cannot_be_committed(self):
        built, _, refs = await self.prepare()
        with self.assertRaises(AnalysisError):
            await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[
                {'id': 'bad', 'operator': 'ARITHMETIC', 'inputRefs': refs, 'expression': 'unknown'}],
                workPlan=[self.correction(operator='ARITHMETIC', refs=['bad'])], requestKey='still-invalid')

    async def test_downstream_failure_stays_until_it_is_rebound_and_recomputed(self):
        built, _, _ = await self.prepare(dependent=True)
        first = await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[],
                                         workPlan=[self.correction()], requestKey='one-correction')
        self.assertEqual(first['completion']['pendingTaskIds'], ['share'])
        self.assertTrue(any(v['derivedId'] == 'derived_loss' for v in self.store.get_analysis(OWNER, first['analysisRef'])['derivedValues']))
        fixed = await self.service.build(baseAnalysisRef=first['analysisRef'], operations=[
            {'id': 'share', 'operator': 'ARITHMETIC', 'inputRefs': [{'valueRef': 'derived_stats', 'field': 'sum'}], 'expression': 'v0/10'}], requestKey='downstream')
        self.assertEqual(fixed['completion']['state'], 'READY')
        self.assertNotIn('derived_loss', {v['derivedId'] for v in self.store.get_analysis(OWNER, fixed['analysisRef'])['derivedValues']})

    async def test_python_correction_uses_executed_outputs_with_and_without_native_prerequisite(self):
        built, _, refs = await self.prepare()
        for index, operations in enumerate(([], [{'id': 'count', 'operator': 'COUNT', 'inputRefs': refs}])):
            output = await self.service.python(analysisRef=built['analysisRef'], inputRefs=refs, operations=operations,
                code='result=[{"name":"comparison","label":"所选金额统计","value":sum(D(i["value"]) for i in inputs),"unit":"元","inputRefs":[i["ref"] for i in inputs]}]',
                purpose='特殊比较的精确处理', requestKey='python-correction-' + str(index),
                workPlan=[self.correction(operator='PYTHON', refs=['comparison'])])
            self.assertEqual(output['completion']['state'], 'READY')
            current = self.store.get_analysis(OWNER, output['analysisRef'])
            self.assertEqual(current['methodRevisions'][0]['operator'], 'PYTHON')
            self.assertEqual(current['calculationValidation'], 'PASS')

    async def test_formula_reconcile_conversion_and_formula_goal_never_downgrade(self):
        _, artifact, refs = await self.prepare()
        for operator in ('FORMULA_CHECK', 'RECONCILE', 'CONVERT'):
            task = {**artifact['plan']['workPlan'][0], 'operator': operator}
            with self.subTest(operator=operator), self.assertRaises(AnalysisError):
                revision_basis(artifact, task, self.correction())
        task = {**artifact['plan']['workPlan'][0], 'requiredEvidence': 'FORMULAS', 'targetRefs': refs}
        with self.assertRaises(AnalysisError):
            revision_basis(artifact, task, self.correction())


class ComparisonFallbackTests(TestCase):
    def test_native_aggregation_rule_is_protected_even_before_numeric_failure(self):
        from analysis_composition_delivery_test import GrainComparisonTests
        from analysis_extension.engine import calculate, RuleRegistry, source_aggregation_rule
        for missing in (True, False):
            data = GrainComparisonTests().data()
            facts = [fact for fact in data['facts'] if fact['inputRole'] == 'PRIMARY']
            if missing:
                facts[0].update(value=None, valueState='NO_VALUE')
            else:
                facts[0]['unit'] = '另一个单位'
            self.assertIsNotNone(source_aggregation_rule(facts))
            operations = [{'id': 'sum', 'operator': 'SUM', 'inputRefs': [fact['factId'] for fact in facts]}]
            values = calculate(facts, data['scope'], operations, RuleRegistry())
            self.assertEqual(values[0]['calculationValidation'], 'BLOCKED')
            task = {'taskId': 'sum', 'title': '来源汇总', 'kind': 'COMPUTATION', 'operator': 'SUM', 'refs': ['derived_sum']}
            base = {'facts': facts, 'derivedValues': values, 'plan': {'operations': operations, 'workPlan': [task]}}
            with self.subTest(missing=missing), self.assertRaises(AnalysisError):
                revision_basis(base, task, {**task, 'operator': 'DESCRIBE', 'revisionReason': '改方法'})

    def test_overlapping_group_roles_survive_material_projection_and_markdown(self):
        from analysis_extension.fact_closeout import summarize
        from analysis_extension.model_closeout import artifact_closeout_records
        facts = [{'factId': 'shared', 'objectLabel': '单位甲', 'headers': ['指标'], 'value': '-3', 'inputRole': 'PRIMARY',
                  'inputMemberships': [{'inputRef': 'p', 'inputRole': 'PRIMARY'}, {'inputRef': 'r', 'inputRole': 'REFERENCE'}]},
                 {'factId': 'reference', 'objectLabel': '单位乙', 'headers': ['指标'], 'value': '10', 'inputRole': 'REFERENCE'}]
        projected, _ = artifact_closeout_records({'facts': facts})
        output = summarize({'facts': projected}, 'LIMIT')
        self.assertIn('| 用途 | 对象 |', output['answer'])
        self.assertIn('| 主体、对照 | 单位甲 |', output['answer'])
        self.assertIn('| 对照 | 单位乙 |', output['answer'])
        self.assertEqual(output['evidenceSummary']['displayedFactCount'], 2)
        self.assertIn('不能合并统计', output['answer'])

    def test_single_group_keeps_existing_table(self):
        from analysis_extension.fact_closeout import summarize
        output = summarize({'facts': [{'factId': 'one', 'value': '3'}]}, 'LIMIT')
        self.assertIn('| 对象 | 指标或标题 |', output['answer'])
        self.assertNotIn('| 用途 |', output['answer'])
