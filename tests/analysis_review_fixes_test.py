"""End-to-end regressions for source identities and rule-aware projections."""
from copy import deepcopy
import json

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
import analysis_source_boundary_test as boundary
from analysis_visuals_test import visual_capture
from analysis_extension.adapters import default_sources
from analysis_extension.engine import RuleRegistry
from analysis_extension.models import AnalysisError
from analysis_extension.presentation import project, page
from analysis_extension.tables import measure_key
from analysis_extension.engine import calculate
from analysis_extension.findings import signature


class ReviewFixTests(PresentationCase):
    budget_capture = boundary.SourceBoundaryTests.budget_capture

    def save(self, capture, selections, scope):
        ref = self.store.capture(OWNER, capture)
        for item in selections:
            item['captureRef'] = ref
        if scope.get('population'):
            scope['population']['captureRef'] = ref
        return ref, selections, scope

    async def test_comparability_rule_survives_rank_render_and_filtered_sum(self):
        capture, selections, scope = fixture(('100', '120', '-5'))
        receipt = json.loads(capture['response']['content'][0]['text'])
        for i, step in enumerate(receipt['data']['stepResults'][1:]):
            period = '2025N0001' if i == 0 else '2026N0001'
            form = step['formResults'][0]
            form['runtime']['context']['period'] = period
            form['runtime']['context']['effectiveDimensions']['DATATIME'] = period
            form['page']['cells'][0]['dimensions']['DATATIME'] = period
            capture['request']['plan']['steps'][i + 1]['formQuery']['periodCode'] = period
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        scope['expectedDimensions'] = {}
        ref, selections, scope = self.save(capture, selections, scope)
        registry = RuleRegistry({**self.rules.rules, 'period-policy': {
            'version': '1', 'source': 'Synthetic explicitly comparable periods', 'operators': ['RANK', 'SUM'],
            'comparability': {'periods': ['2025N0001', '2026N0001'], 'varyingDimensions': ['DATATIME']}}})
        self.service.rules = self.delivery.rules = registry
        built = await self.service.build([ref], selections, scope, [
            {'id': 'rank', 'operator': 'RANK', 'comparabilityRef': 'period-policy'},
            {'id': 'sum', 'operator': 'SUM', 'ruleRef': 'fixture-additive', 'comparabilityRef': 'period-policy'}], 'rule-build')
        self.assertTrue(all(v['calculationValidation'] == 'PASS' for v in built['derivedValues']))
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        views = [{'viewId': 'details', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
                  'valueRefs': [f['factId'] for f in artifact['facts']]},
                 {'viewId': 'rank-chart', 'scopeId': built['scope']['scopeId'], 'type': 'BAR_RANK', 'valueRefs': ['derived_rank']},
                 {'viewId': 'sum-metric', 'scopeId': built['scope']['scopeId'], 'type': 'METRIC', 'valueRefs': ['derived_sum']}]
        await self.service.publish(built['analysisRef'], [], views, 'rule-publish', 0)
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        full = project(artifact, registry)
        filtered = project(artifact, registry, {'topN': 2})
        self.assertEqual([s['viewId'] for s in full['scenes']], ['rank-chart'])
        self.assertIsNone(full['chartUnavailable'])
        self.assertEqual(full['metrics'][0]['display'], '215')
        self.assertEqual(filtered['metrics'][0]['display'], '220')
        self.assertEqual(filtered['viewDerivedValues'][0]['validation'], 'PASS')
        self.delivery.rules = RuleRegistry()  # Published projections use saved rules.
        frozen = self.delivery.project_view(built['analysisRef'], {'topN': 2})
        self.assertEqual(frozen['metrics'][0]['display'], '220')
        self.assertEqual([s['viewId'] for s in frozen['scenes']], ['rank-chart'])
        exported = await self.service.render_report(built['analysisRef'], 'rule-report')
        self.assertIn('215', self.delivery.report(built['analysisRef'], exported['exportRef'])['html'])

    async def test_reopened_budget_context_keeps_snapshot_comparability_and_evidence_identity(self):
        first, a, scope = self.budget_capture('first', ('100',))
        second, b, _ = self.budget_capture('second', ('120',))
        built = await self.service.build([first, second], a+b, scope, [], 'reopened')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(artifact['context']['kind'], 'SINGLE_SNAPSHOT')
        self.assertEqual(artifact['comparability'], 'CONFIRMED')
        self.assertEqual(len({f['sourceContextId'] for f in artifact['facts']}), 2)
        self.assertEqual(len({f['memberRef'] for f in artifact['facts']}), 1)
        self.assertEqual({t['contextId'] for t in built['verificationTasks']}, {'context-first', 'context-second'})
        self.assertEqual({t['querySessionId'] for t in built['verificationTasks']}, {'first', 'second'})
        self.assertEqual(built['receiptVerification'], 'UNVERIFIED')

    async def test_same_snapshot_does_not_allow_transplanting_resume_identity(self):
        capture, selections, scope = fixture(('100',))
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt.update(status='PARTIAL', resumeRef=receipt['executionId'])
        receipt['completeness']['enumerationExhausted'] = False
        page = receipt['data']['stepResults'][1]['formResults'][0]['page']
        page.update(totalRows=6, endRowExclusive=3, observationExhausted=False, nextCursor='opaque-next')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        first, selections, scope = self.save(capture, selections, scope)
        scope.pop('population')
        for field in ('contextId', 'querySessionId'):
            resumed = deepcopy(capture)
            continued = deepcopy(receipt)
            resumed['request'] = {'contractVersion': '2.0', 'contextId': receipt['context']['contextId'],
                                  'querySessionId': receipt['querySessionId'], 'resumeRef': receipt['executionId']}
            resumed['request'][field] = 'reopened-' + field
            if field == 'contextId':
                continued['context'][field] = resumed['request'][field]
                continued['data']['stepResults'][1]['formResults'][0]['runtime']['context']['baseContext'] = deepcopy(continued['context'])
            else:
                continued[field] = resumed['request'][field]
            continued.update(executionId='execution-B', continuationOf=receipt['executionId'], status='SUCCEEDED', resumeRef=None)
            continued['completeness']['enumerationExhausted'] = True
            continued['data']['stepResults'][1]['formResults'][0]['page'].update(startRow=3, endRowExclusive=6,
                                                                                observationExhausted=True, nextCursor=None, cells=[])
            resumed['response']['content'][0]['text'] = json.dumps(continued)
            last = self.store.capture(OWNER, resumed)
            with self.subTest(field=field):
                with self.assertRaises(AnalysisError) as failure:
                    await self.service.build([first, last], selections, scope, [], 'wrong-resume-' + field)
                self.assertEqual(failure.exception.code, 'RESUME_UNPROVEN')

    async def test_real_data_boundary_changes_still_require_composite_and_comparison_rule(self):
        first, a, scope = self.budget_capture('first')
        for field, replacement in [('asOf', '2026-02-01T00:00:00Z'), ('snapshotId', 'snapshot-B'), ('schemaHash', 'schema-B')]:
            with self.subTest(field=field):
                capture = self.store.get_capture(OWNER, first)
                receipt = json.loads(capture['response']['content'][0]['text'])
                receipt['context'][field] = replacement
                receipt['context']['contextId'] = capture['request']['contextId'] = 'context-' + field
                receipt['querySessionId'] = capture['request']['querySessionId'] = 'query-' + field
                for step in receipt['data']['stepResults'][1:]:
                    step['formResults'][0]['runtime']['context']['baseContext'] = deepcopy(receipt['context'])
                capture['response']['content'][0]['text'] = json.dumps(receipt)
                second = self.store.capture(OWNER, capture)
                b = [{**s, 'captureRef': second} for s in a]
                with self.assertRaises(AnalysisError) as failure:
                    await self.service.build([first, second], a+b, scope, [], 'blocked-' + field)
                self.assertEqual(failure.exception.code, 'CONTEXT_CONFLICT')
                built = await self.service.build([first, second], a+b, {**scope, 'contextMode': 'COMPOSITE'},
                    [{'id': 'difference', 'operator': 'DIFFERENCE'}], 'composite-' + field)
                self.assertEqual(built['derivedValues'][0]['diagnostics'][0]['code'], 'COMPARABILITY_UNCONFIRMED')

    def colliding_sources(self):
        refs, selections = [], []
        adapter = default_sources().create('json-records', boundary.OPTIONS)
        for origin, name, value in [('a', 'A company', '10'), ('b', 'B company', '-20')]:
            capture = {'resourceId': origin, 'serverName': origin, 'connectionFingerprint': origin,
                       'originalToolName': 'list_sales', 'toolCallId': origin, 'request': {},
                       'captureState': 'success', 'sourceAdapter': adapter.snapshot(),
                       'response': {'state': 'success', 'content': [{'type': 'text', 'text': json.dumps({
                           'records': [{'id': '1', 'name': name, 'amount': value, 'unit': 'yuan'}], 'complete': True})}]}}
            ref = self.store.capture(OWNER, capture)
            refs.append(ref)
            selections.append({'captureRef': ref, 'stepId': 'rows', 'cellKeys': ['/records/0/amount']})
        scope = {'label': 'Two companies', 'members': ['1'], 'memberDimension': 'MD_ORG',
                 'selectionReason': 'Both sources', 'contextMode': 'COMPOSITE',
                 'population': {'captureRef': refs[0], 'stepId': 'rows', 'memberPointer': '/id'}}
        return refs, selections, scope

    async def test_colliding_source_ids_are_distinct_in_table_count_filter_and_pagination(self):
        refs, selections, scope = self.colliding_sources()
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build(refs, selections, scope, [], 'ambiguous')
        self.assertEqual(failure.exception.code, 'AMBIGUOUS_MEMBER')
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG', detail='full')
        scope['members'] = [f['memberRef'] for f in inventory['items']]
        built = await self.service.build(refs, selections, scope, [{'id': 'count', 'operator': 'COUNT'}], 'qualified')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual({f['sourceObjectId'] for f in artifact['facts']}, {'1'})
        self.assertEqual(len({f['objectId'] for f in artifact['facts']}), 2)
        self.assertEqual(built['derivedValues'][0]['counts']['observed'], 2)
        self.assertNotEqual(signature(artifact['facts'][0]), signature(artifact['facts'][1]))
        self.assertEqual(built['scope']['coverage'], 'UNKNOWN')  # One source cannot prove both populations.
        views = [{'viewId': 'details', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
                  'valueRefs': [f['factId'] for f in artifact['facts']]}]
        await self.service.publish(built['analysisRef'], [], views, 'publish', 0)
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        document = project(artifact, self.rules)
        self.assertEqual(document['objectCount'], 2)
        self.assertEqual(len(document['tables'][0]['rows']), 2)
        self.assertEqual({r['label'] for r in document['tables'][0]['rows']}, {'A company', 'B company'})
        first_page, second_page = page(document, 0, 1), page(document, 1, 1)
        self.assertEqual(len(first_page['rows']), 1)
        self.assertTrue(set(first_page['memberKeys']).isdisjoint(second_page['memberKeys']))
        positive = next(f for f in artifact['facts'] if f['value'] == '10')
        filtered = project(artifact, self.rules, {'filter': 'positive', 'columnKey': measure_key(positive, artifact['scope'])})
        self.assertEqual(filtered['objectCount'], 1)
        self.assertEqual([r['exact'] for r in filtered['rows']], ['10'])
        exported = await self.service.render_report(built['analysisRef'], 'report')
        html = self.delivery.report(built['analysisRef'], exported['exportRef'])['html']
        self.assertIn('A company', html)
        self.assertIn('B company', html)

    async def test_same_raw_id_can_be_subject_and_reference_on_different_sources(self):
        refs, selections, scope = self.colliding_sources()
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG', detail='full')
        by_capture = {f['captureRef']: f['memberRef'] for f in inventory['items']}
        scope.update(members=[by_capture[refs[0]]], referenceMembers=[by_capture[refs[1]]])
        built = await self.service.build(refs, selections, scope, [{'id': 'count', 'operator': 'COUNT'}], 'subject-reference')
        self.assertEqual(built['scope']['coverage'], 'COMPLETE')
        self.assertEqual(built['derivedValues'][0]['counts']['observed'], 1)
        self.assertEqual(built['scope']['observedReferenceMembers'], [by_capture[refs[1]]])

    async def test_budget_sources_also_namespace_reused_member_ids(self):
        first, a, scope = self.budget_capture('first')
        capture = self.store.get_capture(OWNER, first)
        capture['resourceId'] = 'another-budget-resource'
        second = self.store.capture(OWNER, capture)
        b = [{**s, 'captureRef': second} for s in a]
        scope['contextMode'] = 'COMPOSITE'
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG', detail='full')
        scope['members'] = [f['memberRef'] for f in inventory['items']]
        built = await self.service.build([first, second], a+b, scope, [{'id': 'count', 'operator': 'COUNT'}], 'two-budget-sources')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(artifact['context']['kind'], 'COMPOSITE')
        self.assertEqual(len({f['objectId'] for f in artifact['facts']}), 2)
        self.assertEqual({f['sourceObjectId'] for f in artifact['facts']}, {'object-0'})
        self.assertEqual(built['derivedValues'][0]['counts']['observed'], 2)

    async def test_scoped_exclusions_cannot_hide_selected_or_foreign_members(self):
        refs, selections, scope = self.colliding_sources()
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG', detail='full')
        by_capture = {f['captureRef']: f['memberRef'] for f in inventory['items']}
        scope['members'] = list(by_capture.values())
        for excluded in [{'objectId': '1', 'captureRef': refs[0], 'reason': 'selected'},
                         {'objectId': by_capture[refs[1]], 'captureRef': refs[0], 'reason': 'wrong source'},
                         {'objectId': '2', 'captureRef': 'unknown', 'reason': 'unknown source'}]:
            with self.subTest(excluded=excluded):
                with self.assertRaises(AnalysisError) as failure:
                    await self.service.build(refs, selections, {**scope, 'excludedMembers': [excluded]}, [], 'exclude-' + excluded['reason'])
                self.assertEqual(failure.exception.code, 'INVALID_EXCLUSION')
        built = await self.service.build(refs, selections,
            {**scope, 'excludedMembers': [{'objectId': '2', 'captureRef': refs[0], 'reason': 'outside requested scope'}]}, [], 'valid-exclusion')
        self.assertEqual(built['scope']['excludedMembers'][0]['sourceObjectId'], '2')
        self.assertNotIn(built['scope']['excludedMembers'][0]['objectId'], scope['members'])

    async def test_scoped_missing_values_remain_separate_and_partial(self):
        first, a, scope = self.budget_capture('first', ('10', '20'))
        capture = self.store.get_capture(OWNER, first)
        capture['resourceId'] = 'another-budget-resource'
        second = self.store.capture(OWNER, capture)
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG', detail='full')
        scope.update(contextMode='COMPOSITE', members=[f['memberRef'] for f in inventory['items']])
        built = await self.service.build([first, second], [a[0], {**a[0], 'captureRef': second}], scope,
                                         [{'id': 'count', 'operator': 'COUNT'}], 'partial-scoped')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        missing = [f for f in artifact['facts'] if f['valueState'] == 'NOT_QUERIED']
        self.assertEqual(built['scope']['coverage'], 'PARTIAL')
        self.assertEqual(len(missing), 2)
        self.assertEqual(len({f['objectId'] for f in missing}), 2)
        self.assertEqual({f['sourceObjectId'] for f in missing}, {'object-1'})
        self.assertTrue(all(f['value'] is None for f in missing))

    async def test_scoped_relationships_keep_original_references_and_source_isolation(self):
        first, a, scope = visual_capture(self.store)
        capture = self.store.get_capture(OWNER, first)
        capture['resourceId'] = 'another-budget-resource'
        second = self.store.capture(OWNER, capture)
        b = [{**s, 'captureRef': second} for s in a]
        members, cursor = set(), 0
        while cursor is not None:
            inventory = await self.service.inspect('inventory', memberDimension=scope['memberDimension'], detail='full', offset=cursor)
            members.update(f['memberRef'] for f in inventory['items'] if f['captureRef'] in (first, second))
            cursor = inventory['nextOffset']
        scope.update(contextMode='COMPOSITE', members=sorted(members))
        discovered = await self.service.inspect('relations')
        built = await self.service.build([first, second], a+b, scope, [], 'scoped-relations')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual({r['relationRef'] for r in artifact['relations']}, {r['relationRef'] for r in discovered['items']})
        relation = next(r for r in artifact['relations'] if r['captureRef'] == first)
        facts = [f for f in artifact['facts'] if first in f['captureRefs'] and f['cellKey'] == '2:1']
        self.assertEqual([f['objectId'] for f in facts], [relation['parentId']] + relation['childIds'])
        operation = {'id': 'total', 'operator': 'RECONCILE', 'aggregation': 'SUM',
                     'relationRef': relation['relationRef'], 'inputRefs': [f['factId'] for f in facts]}
        value = calculate(artifact['facts'], artifact['scope'], [operation], self.rules, artifact['relations'])[0]
        self.assertEqual(value['calculationValidation'], 'PASS')
        self.assertEqual(value['value'], '-20')
        foreign = next(f for f in artifact['facts'] if second in f['captureRefs']
                       and f['cellKey'] == '2:1' and f['sourceObjectId'] == facts[-1]['sourceObjectId'])
        operation['inputRefs'][-1] = foreign['factId']
        blocked = calculate(artifact['facts'], artifact['scope'], [operation], self.rules, artifact['relations'])[0]
        self.assertEqual(blocked['calculationValidation'], 'BLOCKED')
