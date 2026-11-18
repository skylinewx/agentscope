"""真实回执形状：混合粒度、参考范围、表结构及观察升级。"""
from copy import deepcopy
import json
from unittest import TestCase

from analysis_budget_batch_test import batch_fixture, update
from analysis_fixtures import fixture
from analysis_extension.analysis_inputs import adapt_inputs
from analysis_extension.adapters.budget_v2 import BudgetV2Adapter, validate_form_request
from analysis_extension.models import AnalysisError
from analysis_extension.sources import SourceRegistry


class InputRolesTests(TestCase):
    def setUp(self):
        self.registry = SourceRegistry([BudgetV2Adapter])
        self.scope = {'label': '所选分组与集团对照', 'selectionReason': '用户指定范围'}

    def test_sector_primary_with_one_org_reference_does_not_require_other_orgs(self):
        primary, selection, _, _ = batch_fixture()
        reference, compare, _, receipt = batch_fixture(aggregate=False)
        reference['captureRef'] = compare['captureRef'] = 'reference'
        receipt['data']['stepResults'][0]['batchResults'][0]['batchId'] = compare['batchId'] = 'batch-reference'
        receipt['evidence'][0]['fact']['batchId'] = 'batch-reference'
        update(reference, receipt)
        result = adapt_inputs(self.registry, {'batch-ref': primary, 'reference': reference},
                              [{**selection, 'role': 'PRIMARY'},
                               {**compare, 'role': 'REFERENCE', 'rowIndices': [0]}], self.scope, 100)
        self.assertEqual(len(result['facts']), 6)
        self.assertEqual(len(result['scope']['members']), 2)
        self.assertEqual(len(result['scope']['referenceMembers']), 1)
        self.assertTrue(result['scope']['selectedDataComplete'])
        self.assertTrue(result['scope']['populationComplete'])
        reference_audit = result['scope']['sourceScopes'][1]
        self.assertEqual(reference_audit['missingSelectedCellCount'], 0)
        self.assertEqual(reference_audit['omittedRows'][0]['rowIndex'], 1)
        self.assertEqual(result['facts'][0]['dimensionNames']['MD_ORG__MD_YWBK'], '综合')

    def test_two_years_two_sectors_have_four_composite_members(self):
        capture, selection, _, receipt = batch_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['columns'].insert(0, {'name': 'DATATIME', 'label': '时期', 'type': 'STRING'})
        table['columnRoles']['DATATIME'] = {'kind': 'DIMENSION', 'dimCode': 'DATATIME', 'valueColumn': 'DATATIME'}
        table['data']['fixedDimValMap'].pop('DATATIME')
        table['data']['fixedDimNameMap'].pop('DATATIME')
        table['data']['rows'] = [[period] + row for period in ('2025N0001', '2026N0001') for row in table['data']['rows']]
        table['data']['rowCount'] = 4
        update(capture, receipt)
        result = adapt_inputs(self.registry, {'batch-ref': capture},
                              [{**selection, 'role': 'PRIMARY', 'grain': ['DATATIME', 'MD_ORG__MD_YWBK']}], self.scope, 100)
        self.assertEqual(len(result['scope']['members']), 4)
        self.assertEqual(len(result['facts']), 8)
        self.assertTrue(result['scope']['selectedDataComplete'])

    def test_partial_primary_needs_reason_and_keeps_omitted_data(self):
        capture, selection, _, _ = batch_fixture()
        selected = {**selection, 'role': 'PRIMARY', 'rowIndices': [1]}
        with self.assertRaises(AnalysisError) as error:
            adapt_inputs(self.registry, {'batch-ref': capture}, [selected], self.scope, 100)
        self.assertEqual(error.exception.code, 'SELECTION_REASON_REQUIRED')
        selected['selectionReason'] = '用户只分析投资分组'
        result = adapt_inputs(self.registry, {'batch-ref': capture}, [selected], self.scope, 100)
        self.assertEqual(len(result['scope']['members']), 1)
        self.assertFalse(result['scope']['populationComplete'])
        self.assertEqual(result['scope']['sourceScopes'][0]['omittedRows'][0]['values']['metric1'], '85084700.00')

    def test_batch_formula_recovery_uses_actual_result_org_not_probe_org(self):
        from analysis_extension.formulas import trace_formula
        capture, selection, scope, _ = batch_fixture(aggregate=False)
        result = self.registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        for fact in result['facts']:
            trace = trace_formula(result['facts'], fact['factId'])
            self.assertEqual(trace['applicabilityStatus'], 'NO_CAPTURED_FORMULA')
            self.assertEqual(trace['recovery']['dimensions']['MD_ORG'], fact['dimensions']['MD_ORG'])
            self.assertNotEqual(trace['recovery']['dimensions']['MD_ORG'], 'seed')

    def test_structure_cells_do_not_become_numeric_facts(self):
        capture, selection, _, _ = batch_fixture()
        form_capture, selectors, _ = fixture(('10',))
        form_capture['captureRef'] = 'structure'
        selector = {**selectors[0], 'captureRef': 'structure', 'role': 'STRUCTURE'}
        result = adapt_inputs(self.registry, {'batch-ref': capture, 'structure': form_capture},
                              [{**selection, 'role': 'PRIMARY'}, selector], self.scope, 100)
        self.assertEqual(len(result['facts']), 4)
        self.assertEqual(result['structureEvidence'][0]['cells'][0]['key'], '2:1')

    def test_observation_upgrade_requires_input_vo_and_traced_old_new_ids(self):
        capture, _, _ = fixture(('10',))
        receipt = json.loads(capture['response']['content'][0]['text'])
        form = receipt['data']['stepResults'][1]['formResults'][0]
        capture['request']['plan']['steps'][1]['formQuery']['observationId'] = 'old-grid'
        form['runtime']['observation']['entryType'] = 'INPUT_VO'
        form['visitedObservationIds'] = ['old-grid', 'observation-0']
        validate_form_request(capture, receipt, 'form-0', form)
        invalid = deepcopy(form)
        invalid['runtime']['observation']['entryType'] = 'FOR_GRID'
        with self.assertRaises(AnalysisError):
            validate_form_request(capture, receipt, 'form-0', invalid)
