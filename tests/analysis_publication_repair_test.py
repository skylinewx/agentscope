from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
from analysis_presentation_fixtures import PresentationCase
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.presentation import project, page
from analysis_extension.completion import completion_summary
from analysis_extension.publication import repair_request


class PublicationRepairTests(PresentationCase):
    async def build(self, formula=False, work=None, count=3):
        cap, selections, scope = mixed_fixture(self.store) if formula else saved_fixture(self.store, tuple(str(i) for i in range(count)))
        ops = [{'id': 'check', 'operator': 'FORMULA_CHECK', 'select': {'cellKey': '2:1', 'objectId': 'object-0'}}] if formula else [
            {'id': 'sort', 'operator': 'SORT', 'direction': 'desc'}]
        result = await self.service.build([cap], selections, scope, ops, 'build', workPlan=work)
        self.ref, self.sid = result['analysisRef'], result['scope']['scopeId']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.refs = [f['factId'] for f in self.artifact['facts']]

    def view(self, kind, refs, **extra):
        return {'viewId': kind, 'scopeId': self.sid, 'type': kind, 'valueRefs': refs, **extra}

    async def test_errors_point_to_multiple_items_and_repair_preserves_all_other_content(self):
        await self.build(formula=True)
        views = [self.view('TABLE', self.refs, includeDetails=True),
                 self.view('FORMULA', ['derived_check'], nodeRefs=['derived_check'], title='计算关系')]
        claims = [{'claimId': 'c', 'scopeId': self.sid, 'kind': 'computed', 'parts': [{'text': '**结果** '}, {'valueRef': self.refs[0]}]}]
        presentation = {'labels': [{'valueRef': self.refs[0], 'role': 'measure', 'label': '结果'}]}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, claims, views, 'bad', 0, presentation=presentation)
        details = caught.exception.details
        kind_error = next(i for i in details['issues'] if i['path']=='/claims/0/kind')
        self.assertEqual(kind_error['suggestedKind'], 'fact')
        self.assertEqual(kind_error['valueRefs'], [self.refs[0]])
        self.assertEqual({i['path'] for i in details['issues']}, {'/claims/0/kind', '/viewPlan/0/includeDetails', '/viewPlan/1/nodeRefs'})
        allowed = next(i for i in details['issues'] if i['path'].endswith('nodeRefs'))['allowedNodeRefs']
        self.assertTrue(allowed)
        self.assertTrue(all(ref.startswith('fact_') for ref in allowed))
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')
        edits = [{'op': 'replace', 'path': '/claims/0/kind', 'value': 'fact'},
                 {'op': 'remove', 'path': '/viewPlan/0/includeDetails'},
                 {'op': 'remove', 'path': '/viewPlan/1/nodeRefs'}]
        result = await self.service.publish(self.ref, requestKey='fixed', expectedBaseRevision=0, repairRef=details['repairRef'], repairs=edits)
        saved = self.store.get_analysis(OWNER, self.ref)
        self.assertEqual(saved['presentation'], presentation)
        self.assertEqual(saved['claims'][0]['parts'], claims[0]['parts'])
        self.assertEqual([v['type'] for v in saved['viewPlan']], ['TABLE', 'FORMULA'])
        self.assertEqual(saved['facts'], self.artifact['facts'])
        self.assertEqual(saved['derivedValues'], self.artifact['derivedValues'])
        self.assertTrue(diagrams(self.delivery.view(result['delivery']['path'])))
        retry = await self.service.publish(self.ref, requestKey='fixed', expectedBaseRevision=0, repairRef=details['repairRef'], repairs=edits)
        self.assertEqual(result['analysisRef'], retry['analysisRef'])
        with self.assertRaises(AnalysisError):
            self.store.get_publication_proposal(Owner('other', OWNER.agent_id, OWNER.session_id), details['repairRef'])

    async def test_failed_repairs_are_atomic_and_cannot_change_facts_or_identity(self):
        await self.build()
        request = {'analysisRef': self.ref, 'claims': [], 'viewPlan': [self.view('TABLE', self.refs)], 'presentation': {'labels': []}}
        before = deepcopy(request)
        for path in ('/facts/0/value', '/analysisRef', '/viewPlan/-1/title', '/viewPlan/99/title'):
            with self.subTest(path=path), self.assertRaises(AnalysisError):
                repair_request(request, self.ref, [{'op': 'replace', 'path': '/viewPlan/0/type', 'value': 'METRIC'}, {'op': 'replace', 'path': path, 'value': 'bad'}])
            self.assertEqual(request, before)
        with self.assertRaises(AnalysisError): repair_request(request, 'another-analysis', [])

    async def test_shared_alias_expands_only_to_matching_source_measure_and_preserves_source_labels(self):
        await self.build()
        labels = {'labels': [{'valueRef': self.refs[0], 'role': 'measure', 'label': '业务简称'}]}
        result = await self.service.publish(self.ref, [], [self.view('TABLE', self.refs)], 'publish', 0, presentation=labels)
        table = self.delivery.view(result['delivery']['path'])['tables'][0]
        self.assertEqual(table['columns'][0]['label'], '业务简称')
        self.assertEqual(len(table['rows']), 3)
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['facts'], self.artifact['facts'])

    async def test_explicit_conflicting_aliases_have_paths_and_are_not_silently_replaced(self):
        await self.build()
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, [], [self.view('TABLE', self.refs)], 'bad', 0,
                presentation={'labels': [{'valueRef': self.refs[0], 'role': 'measure', 'label': '甲'},
                                         {'valueRef': self.refs[1], 'role': 'measure', 'label': '乙'}]})
        self.assertEqual(caught.exception.code, 'AMBIGUOUS_TABLE_LABEL')
        self.assertEqual(caught.exception.details['path'], '/presentation/labels/1/label')
        self.assertEqual(caught.exception.details['conflictingPath'], '/presentation/labels/0/label')
        self.assertTrue(caught.exception.details['repairRef'])

    async def test_each_table_retains_all_rows_and_group_membership_across_raw_pages(self):
        await self.build(count=17)
        plans = [self.view('TABLE', self.refs[:11], viewId='one', groupId='g1', pageSize=3),
                 self.view('TABLE', self.refs[11:], viewId='two', groupId='g2', pageSize=4)]
        result = await self.service.publish(self.ref, [], plans, 'publish', 0,
            presentation={'groups': [{'groupId': 'g1', 'title': '主题一'}, {'groupId': 'g2', 'title': '主题二'}]})
        document = project(self.store.get_analysis(OWNER, self.ref), self.rules)
        for offset in (0, 12):
            snapshot = page(document, offset, 12)
            self.assertEqual([len(t['rows']) for t in snapshot['tables']], [11, 6])
            self.assertEqual([s['blocks'][0]['tableIndexes'] for s in snapshot['sections']], [[0], [1]])
        export = self.delivery.render_report(self.ref, 'report')
        html = self.delivery.report(self.ref, export['exportRef'])['html']
        self.assertLess(html.index('主题一'), html.index('主题二'))
        self.assertEqual(html.count('id="detail-title-'), 2)
        self.assertIn('查看其余 7 项（共 11 项）' if plans[0]['pageSize'] == 4 else '查看其余 8 项（共 11 项）', html)

    async def test_work_refs_close_real_tasks_without_rebuilding_and_mismatch_is_not_pending(self):
        await self.build(work=[{'taskId': 'sort', 'title': '排序', 'kind': 'COMPUTATION', 'refs': []}])
        views = [self.view('TABLE', self.refs, orderRef='derived_sort')]
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, [], views, 'pending', 0)
        self.assertEqual(caught.exception.code, 'INCOMPLETE_WORK_PLAN')
        result = await self.service.publish(self.ref, requestKey='done', expectedBaseRevision=0,
            repairRef=caught.exception.details['repairRef'], repairs=[{'op':'replace','path':'/workRefs','value':[{'taskId':'sort','refs':['derived_sort']}]}])
        self.assertEqual(result['summary']['completion']['state'], 'READY')
        saved = self.store.get_analysis(OWNER, self.ref)
        self.assertEqual(saved['plan'], self.artifact['plan'])
        self.assertEqual(saved['derivedValues'], self.artifact['derivedValues'])

    async def test_partial_result_stays_incomplete_and_discloses_the_declared_gap(self):
        await self.build(work=[{'taskId': 'relation', 'title': '直属关系', 'kind': 'RELATION', 'refs': []}])
        result = await self.service.publish(self.ref, [], [self.view('TABLE', self.refs)], 'partial', 0, partialReason='本次先交付已取得数值，直属范围仍需核对。')
        self.assertEqual(result['summary']['completion']['state'], 'INCOMPLETE')
        self.assertIn('直属范围仍需核对', self.delivery.view(result['delivery']['path'])['claimsScopeLabel'])
        export = self.delivery.render_report(self.ref, 'report')
        self.assertIn('直属范围仍需核对', self.delivery.report(self.ref, export['exportRef'])['html'])
