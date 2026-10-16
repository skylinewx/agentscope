from copy import deepcopy
from unittest import TestCase

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.completion import completion_summary, continue_plan, validate_target_continuity
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError


class TargetCompletionTests(TestCase):
    def fixture(self):
        return {'facts': [{'factId': 'first', 'valueState': 'PRESENT'}, {'factId': 'second', 'valueState': 'PRESENT'}],
                'derivedValues': [{'derivedId': 'derived_first', 'operator': 'FORMULA_CHECK', 'inputRefs': ['first'],
                                   'sourceFactIds': ['first', 'second'], 'calculationValidation': 'PASS', 'executionStatus': 'COMPLETED'}],
                'relations': [], 'plan': {'workPlan': [{'taskId': 'formulas', 'title': '目标公式', 'kind': 'COMPUTATION',
                                                       'operator': 'FORMULA_CHECK', 'targetRefs': ['first', 'second'], 'refs': ['derived_first']}]}}

    def test_an_operand_is_not_a_completed_formula_target(self):
        artifact = self.fixture()
        pending = completion_summary(artifact)
        self.assertEqual(pending['state'], 'INCOMPLETE')
        self.assertEqual(pending['items'][0]['missingTargetRefs'], ['second'])
        artifact['derivedValues'].append({**artifact['derivedValues'][0], 'derivedId': 'derived_second', 'inputRefs': ['second']})
        artifact['workRefs'] = [{'taskId': 'formulas', 'refs': ['derived_first', 'derived_second']}]
        self.assertEqual(completion_summary(artifact)['state'], 'READY')

    def test_other_operator_and_blocked_target_do_not_complete_work(self):
        for override in ({'operator': 'COUNT'}, {'calculationValidation': 'BLOCKED', 'executionStatus': 'BLOCKED'}):
            artifact = self.fixture()
            artifact['derivedValues'][0].update(override, inputRefs=['first', 'second'])
            result = completion_summary(artifact)
            self.assertEqual(result['state'], 'INCOMPLETE')
            self.assertEqual(result['items'][0]['missingTargetRefs'], ['first', 'second'])

    def test_rebuild_preserves_targets_and_rejects_narrowing(self):
        artifact = self.fixture()
        task = artifact['plan']['workPlan'][0]
        incoming = {k: v for k, v in task.items() if k not in ('operator', 'targetRefs')}
        _, tasks = continue_plan(artifact, {}, [incoming])
        self.assertEqual(tasks[0]['targetRefs'], ['first', 'second'])
        self.assertEqual(tasks[0]['operator'], 'FORMULA_CHECK')
        for targets in (['first'], [], [None], 'first'):
            with self.subTest(targets=targets), self.assertRaises(AnalysisError):
                continue_plan(artifact, {}, [{**task, 'targetRefs': targets}])

    def test_invalid_targets_are_contract_errors(self):
        for change in ({'operator': []}, {'operator': 'invented'}, {'targetRefs': []},
                       {'targetRefs': ['first', 'first']}, {'targetRefs': ['invented']}, {'kind': 'VALUES'}):
            artifact = self.fixture(); artifact['plan']['workPlan'][0].update(change)
            with self.subTest(change=change), self.assertRaises(AnalysisError): completion_summary(artifact)

    def test_fresh_observation_can_rebind_same_business_target_but_not_another_position(self):
        base = self.fixture()
        for index, fact in enumerate(base['facts']):
            fact.update(objectId='object-'+str(index), objectModelCode='member-model', bindingRef='binding',
                        cellKey='2:1', measureCode='amount', period='period', dimensions={'account':'metric'},
                        sourceOrigins=['origin'], context={'sourceId':'source','schemaHash':'schema','snapshotId':'old'})
        updated = deepcopy(base)
        updated['facts'][1].update(factId='fresh-second', value='300', context={**base['facts'][1]['context'],'snapshotId':'new'})
        updated['plan']['workPlan'][0]['targetRefs'] = ['first','fresh-second']
        _, updated['plan']['workPlan'] = continue_plan(base, {}, updated['plan']['workPlan'])
        validate_target_continuity(base, updated)
        for change in ({'objectId':'other'}, {'period':'another'}, {'measureCode':'other'}, {'bindingRef':'other'},
                       {'dimensions':{'account':'another'}}, {'objectModelCode':'another'}, {'sourceOrigins':['other']},
                       {'context':{'sourceId':'other','schemaHash':'schema'}}):
            invalid=deepcopy(updated);invalid['facts'][1].update(change)
            with self.subTest(change=change), self.assertRaises(AnalysisError):validate_target_continuity(base, invalid)


class DeliveryContractTests(PresentationCase):
    async def setup_values(self, operator='SORT', work=None):
        cap, selections, scope = saved_fixture(self.store, ('-8300', '-1033.14', '20'), unit='')
        built = await self.service.build([cap], selections, scope, [{'id': 'order', 'operator': operator}], 'b', workPlan=work)
        self.built = built
        self.artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.refs = [f['factId'] for f in self.artifact['facts']]
        return built

    def doc(self, body='{{value}}'):
        return {'title': '自由页', 'html': '<html><head></head><body><p>' + body + '</p></body></html>',
                'bindings': [{'key': 'value', 'valueRef': self.refs[0]}]}

    async def test_rank_failure_offers_explicit_source_sort_without_changing_calculation(self):
        await self.setup_values('RANK')
        value = self.artifact['derivedValues'][0]
        self.assertEqual(value['calculationValidation'], 'BLOCKED')
        recovery = value['diagnostics'][0]['details']
        self.assertEqual(recovery['sourceSortOperation'], {'id': 'order', 'operator': 'SORT'})
        self.assertEqual(self.artifact['plan']['operations'][0]['operator'], 'RANK')

    async def test_wrong_preview_base_has_executable_recovery_with_all_content_preserved(self):
        await self.setup_values()
        doc = self.doc('原有简称 {{value}}')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.built['analysisRef'], expectedBaseRevision=self.artifact['revision'],
                                       requestKey='wrong', document=doc, previewOnly=True)
        error = caught.exception
        self.assertEqual(error.code, 'PREVIEW_SCOPE')
        self.assertEqual(error.details['nextAction'], 'RETRY_PREVIEW')
        corrected = await self.service.publish(**error.details['retryPreview'])
        self.assertTrue(corrected['previewOnly'])
        request = self.store.get_publication_proposal(OWNER, corrected['repairRef'])
        self.assertEqual(request['document'], doc)
        self.assertEqual(request['expectedBaseRevision'], self.built['baseRevision'])
        self.assertEqual(self.store.get_analysis(OWNER, self.built['analysisRef']), self.artifact)

    async def test_declared_sort_cannot_be_replaced_with_manually_arranged_scalar_bindings(self):
        await self.setup_values(work=[{'taskId': 'order', 'title': '原值排序', 'kind': 'COMPUTATION', 'refs': ['derived_order']}])
        with self.assertRaises(AnalysisError) as caught: compile_document(self.artifact, self.doc())
        self.assertEqual(caught.exception.code, 'ORDER_DATASET_REQUIRED')
        doc = self.doc(); doc['datasets'] = [{'key': 'ordered', 'valueRef': 'derived_order'}]
        result = compile_document(self.artifact, doc)
        self.assertEqual([r['value'] for r in result['datasets']['ordered']['rows']], ['20', '-1033.14', '-8300'])

    async def test_partial_free_document_discloses_reason_without_forcing_markup(self):
        await self.setup_values(work=[{'taskId': 'unfinished', 'title': '尚缺的关系', 'kind': 'RELATION', 'refs': []}])
        args = {'analysisRef': self.built['analysisRef'], 'expectedBaseRevision': 0, 'previewOnly': True,
                'partialReason': '来源暂不可用 <补查>', 'requestKey': 'partial'}
        with self.assertRaises(AnalysisError) as caught: await self.service.publish(**args, document=self.doc())
        self.assertEqual(caught.exception.code, 'PARTIAL_RESULT_UNDISCLOSED')
        doc = self.doc('{{value}} <em>{{analysisPartialReason}}</em>')
        preview = await self.service.publish(**args, document=doc)
        result = await self.service.preview_content(preview['repairRef'])
        html = result['document']['freeDocument']['html']
        self.assertIn('<em>来源暂不可用 &lt;补查&gt;</em>', html)
        self.assertEqual(result['summary']['completion']['state'], 'INCOMPLETE')
        self.assertNotIn('未注明', html)
        from analysis_document_checks_test import measurements
        compiled = result['document']['freeDocument']
        for width in (420,900):
            sample = measurements(width=width); sample['orders'] = []
            await self.service.record_preview_check(preview['repairRef'], compiled['contentDigest'], compiled['frameKey'], sample)
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(published['summary']['completion']['state'], 'INCOMPLETE')
        self.assertIn('来源暂不可用', self.delivery.view(published['delivery']['path'])['freeDocument']['html'])

    async def test_inventory_field_error_points_to_removal_without_new_queries(self):
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='inventory', fields=['value'])
        self.assertEqual(caught.exception.details['nextAction'], 'REMOVE_FIELDS')
