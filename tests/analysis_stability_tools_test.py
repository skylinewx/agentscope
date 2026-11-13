from copy import deepcopy
import json
from unittest import TestCase

import analysis_business_preparation_test as preparation_fixtures
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.budget_preparation import formula_subjects
from analysis_extension.completion import completion_summary
from analysis_extension.composition_materials import composition_materials
from analysis_extension.models import AnalysisError


class StableInspectionTests(PresentationCase):
    async def formula(self):
        reference, selectors, scope = mixed_fixture(self.store, 1)
        built = await self.service.build([reference], selectors, scope,
            [{'id': 'f', 'operator': 'FORMULA_CHECK', 'select': {'cellKey': '2:1', 'objectId': 'object-0'}}], 'f')
        return self.store.get_analysis(OWNER, built['analysisRef'])

    async def test_formula_product_and_formula_reference_reuse_same_frozen_trace(self):
        artifact = await self.formula()
        value = next(v for v in artifact['derivedValues'] if v['operator'] == 'FORMULA_CHECK')
        for section in ('products', 'formulas'):
            result = await self.service.inspect(section=section, analysisRef=artifact['analysisRef'],
                                                derivedId=value['derivedId'], fields=['nodes'], limit=1)
            self.assertEqual(result['targetFactRef'], value['formulaTrace']['targetFactRef'])
            self.assertEqual(result['nodes']['items'], value['formulaTrace']['nodes'][:1])
            if result.get('nextRead'):
                following = await self.service.inspect(**result['nextRead'])
                self.assertEqual(following['nodes']['items'], value['formulaTrace']['nodes'][1:2])
        self.assertEqual(self.store.get_analysis(OWNER, artifact['analysisRef']), artifact)

    async def test_unknown_formula_ref_and_conflicting_target_still_fail(self):
        artifact = await self.formula()
        for arguments in ({'derivedId': 'foreign'}, {'derivedId': 'derived_f', 'factRef': 'other'}):
            with self.assertRaises(AnalysisError):
                await self.service.inspect(section='products', analysisRef=artifact['analysisRef'], **arguments)

    async def test_derived_read_is_scoped_to_requested_reference(self):
        artifact = await self.formula()
        result = await self.service.inspect(section='derived', analysisRef=artifact['analysisRef'], derivedId='derived_f')
        self.assertEqual([v['derivedId'] for v in result['items']], ['derived_f'])

    async def test_batch_formula_alias_preserves_pagination_and_does_not_query(self):
        artifact = await self.formula()
        result = await self.service.inspect(analysisRef=artifact['analysisRef'], allPages=True,
            requests=[{'section': 'products', 'derivedId': 'derived_f', 'fields': ['nodes'], 'limit': 1}])
        self.assertTrue(result['results'][0]['readComplete'])

    async def test_subject_fact_and_member_references_keep_selected_batch_row(self):
        capture, _ = preparation_fixtures.BusinessPreparationTests.source(self)
        catalog = self.service.sources.catalog(capture, 'MD_ORG')
        row = next(item for item in catalog if item.get('columnName') == 'metric1' and item.get('rowIndex') == 0)
        expected = formula_subjects(self.turn, [{'MD_ORG': row['dimensions']['MD_ORG']}])
        for ref in (row['factId'], row['memberRef']):
            actual = formula_subjects(self.turn, [ref])
            self.assertEqual(actual[0]['sourceSelection'], expected[0]['sourceSelection'])
            self.assertEqual(actual[0]['subject'], expected[0]['subject'])
        self.turn.state['inheritedScope']['selections'][0]['rowIndices'] = [1]
        with self.assertRaises(AnalysisError):
            formula_subjects(self.turn, [row['factId']])

    async def test_exact_cell_reference_does_not_choose_other_metric(self):
        capture, _, _ = preparation_fixtures.BusinessPreparationTests.exact_source(self)
        catalog = self.service.sources.catalog(capture, 'MD_ORG')
        row = next(item for item in catalog if item.get('columnName') == 'metric1' and item.get('rowIndex') == 0)
        actual = formula_subjects(self.turn, [row['factId']])
        self.assertEqual(actual[0]['sourceSelection']['factIds'], ['f0-0'])
        self.turn.state['inheritedScope']['selections'][0]['columnNames'] = ['metric2']
        with self.assertRaises(AnalysisError):
            formula_subjects(self.turn, [row['factId']])


class StableMaterialTests(TestCase):
    def artifact(self):
        return {'analysisRef': 'a:1', 'facts': [
            {'factId': 'a', 'value': '10', 'valueState': 'PRESENT', 'inputRole': 'PRIMARY', 'measureCode': 'x'},
            {'factId': 'b', 'value': '20', 'valueState': 'PRESENT', 'inputRole': 'PRIMARY', 'measureCode': 'x'},
            {'factId': 'c', 'value': '50', 'valueState': 'PRESENT', 'inputRole': 'REFERENCE', 'measureCode': 'x'}],
            'derivedValues': [], 'relations': [], 'scope': {'coverage': 'COMPLETE'},
            'plan': {'workPlan': [{'taskId': 'v', 'title': '主体原值', 'kind': 'VALUES', 'refs': []}]}}

    def test_single_value_task_binds_primary_values_only(self):
        result = completion_summary(self.artifact())
        self.assertEqual(result['state'], 'READY')
        self.assertEqual([item['ref'] for item in result['items'][0]['items']], ['a', 'b'])

    def test_incomplete_or_ambiguous_value_tasks_are_not_guessed(self):
        for mode in ('metrics', 'tasks', 'scope', 'explicit'):
            artifact = self.artifact()
            if mode == 'metrics': artifact['facts'][1]['measureCode'] = 'other'
            if mode == 'tasks': artifact['plan']['workPlan'].append({'taskId': 'w', 'title': '另一任务', 'kind': 'VALUES', 'refs': []})
            if mode == 'scope': artifact['scope']['coverage'] = 'PARTIAL'
            if mode == 'explicit': artifact['workRefs'] = [{'taskId': 'v', 'refs': []}]
            self.assertEqual(completion_summary(artifact)['state'], 'INCOMPLETE', mode)

    def test_large_materials_return_bounded_brief_with_detail_reads(self):
        artifact = self.artifact()
        artifact['facts'][0]['headers'] = ['长标题' * 10000]
        artifact['derivedValues'] = [{'derivedId': 'f', 'operator': 'FORMULA_CHECK', 'formulaTrace': {
            'targetFactRef': 'a', 'nodes': [{'factRef': 'a', 'localCheck': {'status': 'MATCH'}}]}}]
        result = composition_materials(artifact)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 16000)
        self.assertFalse(result['complete'])
        self.assertEqual(result['subjects'][0]['detailRead']['factRef'], 'a')

    def test_local_gap_keeps_available_constituents_and_other_checks(self):
        artifact = self.artifact()
        artifact['facts'][1].update(value=None, valueState='NO_VALUE')
        trace = {'targetFactRef': 'a', 'nodes': [
            {'factRef': 'a', 'expression': 'B1', 'localCheck': {'status': 'MATCH'}, 'dependencies': [{'factRef': 'b'}]},
            {'factRef': 'b', 'expression': 'C1+D1', 'localCheck': {'status': 'BLOCKED'}, 'dependencies': [{'factRef': 'c'}]},
            {'factRef': 'c', 'localCheck': {'status': 'NOT_APPLICABLE'}}]}
        artifact['derivedValues'] = [{'derivedId': 'f', 'operator': 'FORMULA_CHECK', 'formulaTrace': trace}]
        before = deepcopy(artifact)
        result = composition_materials(artifact)
        self.assertEqual(result['subjects'][0]['items'][0]['localCheck']['status'], 'MATCH')
        self.assertEqual(result['subjects'][0]['limitations'][0]['affectedFactRef'], 'b')
        self.assertEqual(result['subjects'][0]['limitations'][0]['availableDependencyRefs'], ['c'])
        self.assertEqual(artifact, before)
