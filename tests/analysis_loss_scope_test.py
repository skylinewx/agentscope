"""Filtered receipt selectors survive projection and keep their source boundary."""
from copy import deepcopy
from decimal import Decimal

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters.budget_batch import catalog, selectors
from analysis_extension.analysis_materials import materials
from analysis_extension.models import AnalysisError
from analysis_extension.engine import calculate, RuleRegistry


class LossScopeTest(PresentationCase):
    def filtered(self, native_rows_only=False):
        raw, receipt = business_fixture()
        data = receipt['data']
        table = data['stepResults'][0]['batchResults'][0]
        if native_rows_only:
            for index, value in enumerate(('-8300.00', '-10360.23')):
                table['data']['rows'][index][2] = value
                data['resultSets'][0]['rows'][index].update(value=value, valueState='PRESENT')
        predicate = {'operator': 'LT' if native_rows_only else 'GT', 'value': '0' if native_rows_only else '-1'}
        raw['request']['intentDraft'] = {'valueFilter': predicate}
        data['resolution']['intentDraft'] = deepcopy(raw['request']['intentDraft'])
        evaluated = []
        for result in data['resultSets']:
            for row in result['rows']:
                matched = row['valueState'] == 'PRESENT' and (
                    Decimal(row['value']) < 0 if native_rows_only else Decimal(row['value']) > -1)
                evaluated.append({**deepcopy(row), 'resultSetId': result['resultSetId'], 'matched': matched})
            result['rows'] = [row for row, proof in zip(result['rows'], evaluated[-len(result['rows']):]) if proof['matched']]
        data['valueFilter'] = {**predicate, 'populationCount': len(evaluated),
                              'matchedCount': sum(row['matched'] for row in evaluated),
                              'populationComplete': True, 'evaluatedFacts': evaluated}
        update(raw, receipt)
        ref = self.store.capture(OWNER, raw)
        return self.store.get_capture(OWNER, ref)

    async def test_projected_filtered_selector_builds_exact_retained_rows(self):
        capture = self.filtered()
        projected = materials(capture, self.service.sources)
        self.assertEqual([s['rowIndices'] for s in projected['selections']], [[0], [0, 1]])
        self.assertTrue(all(s['selectionReason'] for s in projected['selections']))
        built = await self.service.build([capture['captureRef']], projected['selections'],
            {'label': '应用服务筛选结果', 'selectionReason': '复用已确认结果'}, [], 'filtered-materials')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual({(f['columnName'], f['rowIndex']) for f in artifact['facts']},
                         {('metric1', 0), ('metric2', 0), ('metric2', 1)})

    def test_native_filtered_rows_allow_omission_without_changing_values(self):
        capture = self.filtered(native_rows_only=True)
        selection, = materials(capture, self.service.sources)['selections']
        original = deepcopy(selection)
        selection.pop('rowIndices')
        values = catalog(capture, [selection])
        self.assertEqual([f['value'] for f in values], ['-8300.00', '-10360.23'])
        self.assertEqual(values, catalog(capture, [original]))

    def test_missing_or_expanded_rows_cannot_admit_unmatched_facts(self):
        capture = self.filtered()
        selection = materials(capture, self.service.sources)['selections'][0]
        for rows in (None, [0, 1], [1]):
            invalid = deepcopy(selection)
            if rows is None:
                invalid.pop('rowIndices')
            else:
                invalid['rowIndices'] = rows
            with self.subTest(rows=rows), self.assertRaises(AnalysisError) as rejected:
                catalog(capture, [invalid])
            self.assertEqual(rejected.exception.code, 'VALUE_FILTER_SCOPE_MISMATCH')

    def test_subset_empty_and_mutated_projection_preserve_boundary(self):
        capture = self.filtered(native_rows_only=True)
        selection, = materials(capture, self.service.sources)['selections']
        self.assertEqual(len(catalog(capture, [{**selection, 'rowIndices': [1]}])), 1)
        self.assertEqual(catalog(capture, [{**selection, 'rowIndices': []}]), [])
        selection['rowIndices'].append(999)
        self.assertEqual(selectors(capture)[0]['rowIndices'], [0, 1])

    def test_unfiltered_reference_still_selects_all_actual_rows(self):
        raw, _ = business_fixture()
        ref = self.store.capture(OWNER, raw)
        capture = self.store.get_capture(OWNER, ref)
        selection, = materials(capture, self.service.sources)['selections']
        self.assertNotIn('rowIndices', selection)
        self.assertEqual(len(catalog(capture, [{**selection, 'role': 'REFERENCE'}])), 4)

    async def test_reference_population_can_contain_primary_without_double_counting(self):
        from analysis_budget_batch_test import batch_fixture
        raw, selection, _, receipt = batch_fixture(aggregate=False)
        primary = self.store.capture(OWNER, raw)
        receipt['data']['stepResults'][0]['batchResults'][0]['batchId'] = 'reference-batch'
        receipt['evidence'][0]['fact']['batchId'] = 'reference-batch'
        update(raw, receipt)
        reference = self.store.capture(OWNER, raw)
        selections = [
            {**selection, 'captureRef': primary, 'columnNames': ['metric1'], 'rowIndices': [0],
             'role': 'PRIMARY', 'selectionReason': '用户指定的分析子集'},
            {**selection, 'captureRef': reference, 'batchId': 'reference-batch', 'columnNames': ['metric1'],
             'role': 'REFERENCE'},
        ]
        built = await self.service.build([primary, reference], selections,
            {'label': '子集与总体', 'selectionReason': '对照总体包含主体子集'}, [
                {'id': 'primary', 'operator': 'COUNT'},
                {'id': 'reference', 'operator': 'COUNT', 'select': {'inputRole': 'REFERENCE'}},
                {'id': 'referenceBatch', 'operator': 'COUNT', 'select': {'batchId': 'reference-batch'}},
            ], 'overlapping-population')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['scope']['members']), 1)
        self.assertEqual(len(artifact['scope']['referenceMembers']), 2)
        counts = [v['counts']['observed'] for v in artifact['derivedValues']]
        self.assertEqual(counts, [1, 2, 2])
        self.assertTrue(all(v['calculationValidation'] == 'PASS' for v in artifact['derivedValues']))
        member = artifact['scope']['members'][0]
        value, = calculate(artifact['facts'], artifact['scope'], [
            {'id': 'one', 'operator': 'COUNT', 'select': {'objectId': member}}], RuleRegistry())
        self.assertEqual(value['counts']['observed'], 1)
        reference_member = next(m for m in artifact['scope']['referenceMembers'] if m != member)
        value, = calculate(artifact['facts'], artifact['scope'], [
            {'id': 'one', 'operator': 'COUNT', 'select': {'objectId': reference_member}}], RuleRegistry())
        self.assertEqual(value['counts']['observed'], 1)

    async def test_same_fact_cannot_be_declared_twice_under_conflicting_roles(self):
        raw, _ = business_fixture()
        ref = self.store.capture(OWNER, raw)
        selection, = materials(self.store.get_capture(OWNER, ref), self.service.sources)['selections']
        with self.assertRaises(AnalysisError) as rejected:
            await self.service.build([ref], [{**selection, 'role': 'PRIMARY'}, {**selection, 'role': 'REFERENCE'}],
                {'label': '同一事实重复引用', 'selectionReason': '无独立输入'}, [], 'conflicting-roles')
        self.assertEqual(rejected.exception.code, 'INPUT_ROLE_CONFLICT')
