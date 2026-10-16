import json
import tempfile
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER, fixture, saved_fixture, rules
from analysis_extension.adapters.budget_v2 import adapt
from analysis_extension.engine import calculate
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.query_results import read_receipt, at_pointer, encode
from analysis_extension.service import AnalysisService
from analysis_extension.store import AnalysisStore
from analysis_extension.models import canonical
from analysis_extension.presentation import project
from analysis_extension.validation import validate_views
import analysis_visuals_test as visuals


class BoundedReadTests(TestCase):
    def test_adaptive_pages_preserve_whole_items_and_search_positions(self):
        rows = [{'name': 'match' if i % 2 else 'other', 'value': str(i), 'nested': 'x' * 5100} for i in range(27)]
        receipt = {'status': 'PARTIAL', 'resumeRef': 'source-next', 'data': {'rows': rows}}
        offset, found, pointers = 0, [], []
        while offset is not None:
            page = read_receipt(receipt, 'capture', '/data/rows', offset=offset, limit=100, search='match')
            self.assertLessEqual(len(encode(page)), 24000)
            self.assertEqual(page['sourceState']['resumeRef'], 'source-next')
            found.extend(page['items']); pointers.extend(page['itemPointers'])
            if page['nextOffset'] is not None:
                self.assertEqual(page['nextOffset'], offset + len(page['items']))
                self.assertTrue(page['pageLimited'])
            offset = page['nextOffset']
        expected = rows[1::2]
        self.assertEqual(found, expected)
        self.assertEqual([at_pointer(receipt, p) for p in pointers], expected)

    def test_single_wide_object_and_field_errors_supply_real_escaped_paths(self):
        receipt = {'data': {'wide/items': [{'label': 'item', 'detail': 'x' * 26000}]}}
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt, 'capture', '/data', limit=1)
        option = error.exception.details['readOptions'][0]
        self.assertEqual(option['pointer'], '/data/wide~1items')
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt, 'capture', option['pointer'])
        details = error.exception.details
        self.assertEqual(details['pointer'], '/data/wide~1items/0')
        self.assertEqual(details['availableFields'], ['label', 'detail'])
        for option in details['readOptions']:
            self.assertIsNotNone(at_pointer(receipt, option['pointer']))
        self.assertEqual(read_receipt(receipt, 'capture', '/data/wide~1items', fields=['label'])['items'], [{'label': 'item'}])
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt, 'capture', '/data/wide~1items/2')
        self.assertEqual(error.exception.details['parentPointer'], '/data/wide~1items')
        self.assertEqual(error.exception.details['readOptions'][0]['pointer'], '/data/wide~1items/0')

    def test_recovery_hints_are_bounded_and_only_return_real_names(self):
        wide = {('field-' + str(i) + 'x' * 500): 'large' * 6000 for i in range(30)}
        receipt = {'data': {str(i): wide for i in range(20)}}
        with self.assertRaises(AnalysisError) as error:
            read_receipt(receipt, 'capture', '/data')
        self.assertLess(len(encode(error.exception.details)), 24000)
        for option in error.exception.details['readOptions']:
            child = at_pointer(receipt, option['pointer'])
            self.assertTrue(all(key in child for key in option['availableFields']))


def subject_fixture():
    cap, selections, scope = fixture(values=('10', '20', '90'))
    receipt = json.loads(cap['response']['content'][0]['text'])
    all_rows = receipt['data']['stepResults'][0]['rows']
    receipt['data']['stepResults'].append({'stepId': 'names', 'rows': deepcopy(all_rows), 'exactness': 'EXACT'})
    receipt['data']['stepResults'][0]['rows'] = all_rows[:2]
    cap['response']['content'][0]['text'] = json.dumps(receipt)
    scope['referenceMembers'], scope['members'] = scope['members'][2:], scope['members'][:2]
    return cap, selections, scope


class ReferenceScopeTests(TestCase):
    def test_comparator_does_not_invalidate_or_enter_primary_population(self):
        cap, selections, scope = subject_fixture()
        data = adapt({'replace': cap}, selections, scope, 50000)
        self.assertTrue(data['scope']['populationComplete'])
        self.assertEqual(data['scope']['coverage'], 'COMPLETE')
        self.assertEqual(data['scope']['observedMembers'], scope['members'])
        refs = [f['factId'] for f in data['facts']]
        result = calculate(data['facts'], data['scope'], [
            {'id': 'subjects', 'operator': 'COUNT', 'wholePopulation': True},
            {'id': 'compare', 'operator': 'COUNT', 'select': {'objectId': scope['referenceMembers'][0]}},
            {'id': 'wrong', 'operator': 'COUNT', 'inputRefs': refs, 'wholePopulation': True},
            {'id': 'subset', 'operator': 'COUNT', 'inputRefs': refs[:1], 'wholePopulation': True},
        ], rules())
        self.assertEqual([r['value'] for r in result[:2]], ['2', '1'])
        for r in result[2:]:
            self.assertEqual(r['calculationValidation'], 'BLOCKED')
            self.assertEqual(r['diagnostics'][0]['code'], 'POPULATION_MISMATCH')

    def test_missing_comparator_is_not_zero_and_overlapping_roles_fail(self):
        cap, selections, scope = subject_fixture()
        data = adapt({'replace': cap}, selections[:2], scope, 50000)
        self.assertEqual(data['scope']['coverage'], 'COMPLETE')
        self.assertFalse(data['scope']['selectedDataComplete'])
        self.assertEqual(data['scope']['pendingReferenceMembers'], scope['referenceMembers'])
        self.assertEqual(data['facts'][-1]['valueState'], 'NOT_QUERIED')
        with self.assertRaises(AnalysisError):
            adapt({'replace': cap}, selections, {**scope, 'referenceMembers': scope['members'][:1]}, 50000)


class RebuildTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AnalysisStore(self.temp.name)
        self.service = AnalysisService(self.store, OWNER, AsyncMock(), rules())
        self.cap, self.selections, self.scope = saved_fixture(self.store)
        self.ops = [{'id': 'sum', 'operator': 'SUM', 'ruleRef': 'fixture-additive'}]
        self.plan = [{'taskId': 'compute', 'kind': 'COMPUTATION', 'title': '计算所选对象合计', 'refs': []}]

    async def test_draft_rebuild_inherits_pending_plan_and_population_and_can_publish(self):
        first = await self.service.build([self.cap], self.selections, self.scope, self.ops, 'first', workPlan=self.plan)
        scope = {k: v for k, v in self.scope.items() if k != 'population'}
        next_args = ([self.cap], self.selections, scope, self.ops, 'next')
        rebuilt = await self.service.build(*next_args, baseAnalysisRef=first['analysisRef'], workPlan=[])
        self.assertEqual(rebuilt['scope']['population'], self.scope['population'])
        self.assertEqual(rebuilt['completion']['pendingTaskIds'], ['compute'])
        self.assertEqual(rebuilt['baseRevision'], 0)
        self.assertEqual(rebuilt['analysisRef'].split(':')[0], first['analysisRef'].split(':')[0])
        replay = await self.service.build(*next_args, baseAnalysisRef=first['analysisRef'], workPlan=[])
        self.assertEqual(replay['analysisRef'], rebuilt['analysisRef'])
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(rebuilt['analysisRef'], [], [], 'incomplete', 0)
        self.assertEqual(error.exception.code, 'INCOMPLETE_WORK_PLAN')
        published = await self.service.publish(rebuilt['analysisRef'], [], [], 'complete', 0,
                                              workRefs=[{'taskId': 'compute', 'refs': ['derived_sum']}])
        self.assertEqual(published['summary']['completion']['state'], 'READY')
        third = await self.service.build([self.cap], self.selections, self.scope, self.ops, 'third', baseAnalysisRef=rebuilt['analysisRef'])
        self.assertEqual(third['completion']['state'], 'READY')
        self.assertEqual(third['baseRevision'], 2)
        self.assertEqual(self.store.get_analysis(OWNER, first['analysisRef'])['plan']['workPlan'], self.plan)

    async def test_scope_change_cannot_silently_drop_population_or_cross_owner(self):
        first = await self.service.build([self.cap], self.selections, self.scope, self.ops, 'first', workPlan=self.plan)
        scope = {k: v for k, v in self.scope.items() if k != 'population'}
        scope['members'] = scope['members'][:1]
        with self.assertRaises(AnalysisError) as error:
            await self.service.build([self.cap], self.selections[:1], scope, [], 'narrow', baseAnalysisRef=first['analysisRef'])
        self.assertEqual(error.exception.code, 'POPULATION_REQUIRED')
        other = AnalysisService(self.store, Owner('other', 'a', 's'), AsyncMock(), rules())
        with self.assertRaises(AnalysisError):
            await other.build([self.cap], self.selections, self.scope, [], 'foreign', baseAnalysisRef=first['analysisRef'])

    async def test_removing_needed_operation_does_not_reuse_old_computation(self):
        first = await self.service.build([self.cap], self.selections, self.scope, self.ops, 'first',
                                        workPlan=[{**self.plan[0], 'refs': ['derived_sum']}])
        with self.assertRaises(AnalysisError) as error:
            await self.service.build([self.cap], self.selections, self.scope, [], 'no-operation', baseAnalysisRef=first['analysisRef'])
        self.assertEqual(error.exception.code, 'INVALID_WORK_REF')


class FormulaLabelTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = visuals.VisualTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    async def test_diagram_details_and_waterfall_use_the_same_business_names(self):
        await self.case.draft()
        artifact = deepcopy(self.case.artifact)
        artifact['publicationState'] = 'PUBLISHED'
        for fact in artifact['facts']:
            fact['headers'].extend(['2026 年', '预算数'])
        common = {'scopeId': self.case.sid, 'valueRefs': ['derived_formula'],
                  'labels': [{'valueRef': self.case.results[0], 'label': '业务结余'}]}
        artifact['viewPlan'] = [
            {**common, 'viewId': 'flow', 'type': 'FORMULA', 'includeDetails': True},
            {**common, 'viewId': 'waterfall', 'type': 'WATERFALL'},
        ]
        before = canonical({'facts': artifact['facts'], 'values': artifact['derivedValues']})
        validate_views(artifact, artifact['viewPlan'])
        view = project(artifact, self.case.case.rules)
        relation = next(s for s in view['scenes'] if s['type'] == 'FORMULA')['relationship']
        table = next(t for t in view['tables'] if t['kind'] == 'formula')
        waterfall = next(s for s in view['scenes'] if s['type'] == 'WATERFALL')
        self.assertEqual(relation['result']['label'], '业务结余')
        self.assertEqual([n['label'] for n in relation['inputs']], ['收入', '成本'])
        self.assertIn('业务结余 = 收入 − 成本', [r['note'] for r in table['rows']])
        self.assertEqual({r['label'] for r in table['rows']}, {'业务结余', '收入', '成本'})
        self.assertTrue(any(p.get('text') == '收入' for p in waterfall['primitives']))
        self.assertTrue(any(p.get('text') == '成本' for p in waterfall['primitives']))
        for item in (relation, table, waterfall):
            self.assertNotIn('2026 年', canonical(item).decode())
            self.assertNotIn('预算数', canonical(item).decode())
        self.assertEqual(canonical({'facts': artifact['facts'], 'values': artifact['derivedValues']}), before)
