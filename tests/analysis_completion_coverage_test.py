import tempfile
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER, saved_fixture, rules
from analysis_extension.store import AnalysisStore
from analysis_extension.service import AnalysisService
from analysis_extension.models import AnalysisError
from analysis_extension.completion import completion_summary
from analysis_extension.coverage import represented


class CoverageTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AnalysisStore(self.temp.name)
        self.service = AnalysisService(self.store, OWNER, AsyncMock(), rules())
        self.cap, self.selections, self.scope = saved_fixture(self.store)
        base = await self.service.build([self.cap], self.selections, self.scope, [], 'first')
        self.facts = self.store.get_analysis(OWNER, base['analysisRef'])['facts']
        self.refs = [f['factId'] for f in self.facts]
        self.scope['reportTopics'] = [{'title': '各成员', 'question': '分别多少', 'factRefs': self.refs}]

    async def build(self, ops, key='topic', work=None):
        return await self.service.build([self.cap], self.selections, self.scope, ops, key, workPlan=work)

    async def test_order_alone_and_aggregate_do_not_answer_member_topic(self):
        built = await self.build([{'id': 'order', 'operator': 'SORT', 'inputRefs': self.refs},
                                  {'id': 'sum', 'operator': 'SUM', 'inputRefs': self.refs, 'ruleRef': 'fixture-additive'}])
        sid = built['scope']['scopeId']
        for name, views in [
            ('order', [{'viewId': 'o', 'scopeId': sid, 'type': 'ORDER', 'valueRefs': ['derived_order']}]),
            ('sum', [{'viewId': 'm', 'scopeId': sid, 'type': 'METRIC', 'valueRefs': ['derived_sum']}]),
            ('sum-table', [{'viewId': 't', 'scopeId': sid, 'type': 'TABLE', 'valueRefs': ['derived_sum']}]),
        ]:
            with self.subTest(name=name), self.assertRaises(AnalysisError) as error:
                await self.service.publish(built['analysisRef'], [], views, name, built['baseRevision'])
            self.assertEqual(error.exception.code, 'REPORT_TOPIC_UNREPRESENTED')
            self.assertEqual(set(error.exception.details['missingFactRefs']), set(self.refs))
        result = await self.service.publish(built['analysisRef'], [], [
            {'viewId': 'details', 'scopeId': sid, 'type': 'TABLE', 'valueRefs': self.refs, 'orderRef': 'derived_order', 'collapsed': True}
        ], 'complete', built['baseRevision'])
        self.assertTrue(result['summary']['represented']['topics'][0]['represented'])
        self.assertEqual(len(result['summary']['represented']['objectIds']), 3)

    async def test_chart_cutoff_is_not_complete_without_available_details(self):
        cap, selections, scope = saved_fixture(self.store, values=tuple(str(i) for i in range(14)))
        base = await self.service.build([cap], selections, scope, [], 'wide-base')
        artifact = self.store.get_analysis(OWNER, base['analysisRef'])
        refs = [f['factId'] for f in artifact['facts']]
        scope['reportTopics'] = [{'title': '所有成员', 'question': '所有值', 'factRefs': refs}]
        built = await self.service.build([cap], selections, scope, [], 'wide')
        view = {'viewId': 'bars', 'scopeId': built['scope']['scopeId'], 'type': 'BAR_VALUES', 'valueRefs': refs}
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(built['analysisRef'], [], [view], 'wide-bad', built['baseRevision'])
        self.assertEqual(len(error.exception.details['missingFactRefs']), 2)
        result = await self.service.publish(built['analysisRef'], [], [view, {**view, 'viewId': 'details', 'type': 'TABLE', 'collapsed': True}], 'wide-ok', built['baseRevision'])
        self.assertTrue(result['summary']['represented']['topics'][0]['represented'])

    async def test_work_plan_is_bound_to_real_results_and_idempotency(self):
        work = [{'taskId': 'values', 'title': '取值', 'kind': 'VALUES', 'refs': self.refs},
                {'taskId': 'relation', 'title': '查看直属下级', 'kind': 'RELATION', 'refs': []}]
        built = await self.build([], work=work)
        self.assertEqual(built['completion']['state'], 'INCOMPLETE')
        self.assertEqual(built['completion']['pendingTaskIds'], ['relation'])
        with self.assertRaises(AnalysisError):
            await self.build([], key='invalid', work=[{**work[0], 'refs': ['invented']}])
        with self.assertRaises(AnalysisError):
            await self.build([], work=[])

    async def test_unused_group_and_empty_table_captions_do_not_count_as_display(self):
        built = await self.build([{'id':'order','operator':'SORT','inputRefs':self.refs}])
        parts = [{'valueRef': ref} for ref in self.refs]
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(built['analysisRef'], [], [{
                'viewId':'empty-table', 'scopeId':built['scope']['scopeId'], 'type':'TABLE',
                'valueRefs':['derived_order'], 'descriptionParts':parts}], 'empty-caption', built['baseRevision'],
                presentation={'groups':[{'groupId':'unused','title':'未显示的组','descriptionParts':parts}]})
        self.assertEqual(error.exception.code, 'REPORT_TOPIC_UNREPRESENTED')


class CompletionTests(TestCase):
    def test_completed_mismatch_is_not_source_consistency_and_no_children_is_not_zero(self):
        artifact = {'facts': [], 'derivedValues': [{'derivedId': 'derived_a', 'executionStatus': 'COMPLETED',
            'calculationValidation': 'PASS', 'sourceConsistency': 'MISMATCH'}],
            'relations': [{'relationRef': 'r', 'complete': True, 'childIds': []}],
            'plan': {'workPlan': [
                {'taskId': 'c', 'title': '核对', 'kind': 'COMPUTATION', 'refs': ['derived_a']},
                {'taskId': 'r', 'title': '下级检查', 'kind': 'RELATION', 'refs': ['r']}]}}
        result = completion_summary(artifact)
        self.assertEqual(result['state'], 'READY')
        self.assertEqual(result['items'][0]['items'][0]['sourceConsistency'], 'MISMATCH')
        self.assertEqual(result['items'][1]['items'][0]['childCount'], 0)
        self.assertNotIn('childSum', result['items'][1]['items'][0])

    def test_selected_formula_nodes_do_not_cover_other_nodes(self):
        refs = ['f1', 'f2', 'f3', 'f4']
        artifact = {'facts': [{'factId': r, 'objectId': 'o', 'cellKey': r} for r in refs],
            'derivedValues': [{'derivedId': 'd', 'formulaTrace': {'nodes': [
                {'factRef': 'f1', 'expression': 'A1', 'dependencies': [{'factRef': 'f2'}]},
                {'factRef': 'f2', 'expression': 'A2', 'dependencies': [{'factRef': 'f3'}]},
                {'factRef': 'f3', 'expression': None, 'dependencies': []}]}}],
            'scope': {}, 'claims': [], 'viewPlan': [
                {'viewId': 'v', 'type': 'FORMULA', 'valueRefs': ['d'], 'nodeRefs': ['f1']}]}
        actual = represented(artifact)
        self.assertEqual(actual['factRefs'], ['f1', 'f2'])
