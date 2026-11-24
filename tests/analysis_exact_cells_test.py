from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch, Mock
from types import SimpleNamespace

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_extension.adapters.budget_v2 import catalog, receipt_from_capture
from analysis_extension.adapters import budget_batch, budget_exact
from analysis_extension.models import AnalysisError


def exact_fixture():
    capture, receipt = business_fixture()
    data = receipt['data']; table = data['stepResults'][0]['batchResults'][0]
    planned = data['runtimeRequests'][0]['batchQuery']
    targets, origins, values = [], [], []
    for index, template in enumerate(table['request']['metrics']):
        representative = table['metrics'][index]
        template['cellExp'] = representative['cellExp'] = 'MX_FINANCIAL_N[ZB_JE2]'
        for row_index, old in enumerate(table['data']['rows']):
            origin = deepcopy(representative)
            origin.update(targetRef=f'target-{index}-{row_index}', selectionCellKey=f'{index + 1}:{row_index + 1}',
                          runtimeCellExp='MX_FINANCIAL_N[ZB_JE2]')
            dims = {**origin['context']['effectiveDimensions'], **origin['selectedDimensions'], 'MD_ORG': old[0], 'DATATIME': '2026N0001'}
            origin['selectedDimensions'] = dims
            target = {'targetRef': origin['targetRef'], 'metric': template['name'], 'dimensions': dims}
            value = old[index + 2]
            state = 'PRESENT' if value is not None else 'NULL_VALUE'
            values.append({**target, 'modelCode': 'MX_FINANCIAL', 'measureCode': 'ZB_JE2', 'dimensionNames': {'MD_ORG': old[1]},
                           'value': value, 'valueType': 'DECIMAL_STRING', 'valueState': state, 'unit': '元'})
            origins.append(origin); targets.append(target)
            fact = data['resultSets'][index]['rows'][row_index]
            fact.update(dimensions=dims, valueType='DECIMAL_STRING')
            source = next(item for item in data['origins'] if item['originId'] == fact['originRefs'][0])
            source['targetRef'] = origin['targetRef']; source.pop('rowIndex', None)
    table['request'].pop('batchDimensions')
    planned.pop('batchDimensions')
    table['request']['cellTargets'] = planned['cellTargets'] = targets
    table['cellOrigins'] = planned['cellSelections'] = origins
    table['data'] = {'columns': [], 'rows': [], 'rowCount': 0, 'cellResults': values,
                     'cellSummary': {'requestedCount': 4, 'uniqueCellCount': 4, 'presentCount': 3,
                                     'nullValueCount': 1, 'missingCount': 0, 'sourceQueryCount': 1}}
    update(capture, receipt)
    return capture, receipt


class ExactCellsTests(TestCase):
    def test_indices_for_one_metric_cannot_silently_drop_another_requested_metric(self):
        capture, _ = exact_fixture()
        selector, = budget_batch.selectors(capture)
        wrong = {**selector, 'rowIndices': [0, 1]}
        with self.assertRaises(AnalysisError) as failure:
            budget_batch.catalog(capture, [wrong])
        self.assertEqual(failure.exception.code, 'INVALID_SELECTOR')
        self.assertEqual(failure.exception.details['unmatchedColumns'],
                         {'metric2': {'availableCount': 2, 'exampleRowIndices': [2, 3]}})
        split = [{**wrong, 'columnNames': ['metric1']}, {**selector, 'columnNames': ['metric2'], 'rowIndices': [2, 3]}]
        self.assertEqual(budget_batch.catalog(capture, split), budget_batch.catalog(capture, [selector]))
        facts = budget_batch.catalog(capture, [wrong, split[1]])
        self.assertEqual(len(facts), 4)
        self.assertTrue(any(fact['value'] is None for fact in facts))

    def test_matching_row_suggestion_needs_unique_full_coordinates_in_the_same_form(self):
        _, receipt = exact_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        rows = table['data']['cellResults']
        origins = table['cellOrigins']
        for source, candidate in [(0, 3), (1, 2)]:
            rows[candidate]['dimensions'] = deepcopy(rows[source]['dimensions'])
            origins[candidate]['context'] = deepcopy(origins[source]['context'])
        options = {'metric2': [2, 3]}
        self.assertEqual(budget_exact.corresponding_rows(table, {0}, options), {'metric2': [3]})
        rows[2]['dimensions'] = deepcopy(rows[0]['dimensions'])
        origins[2]['context'] = deepcopy(origins[0]['context'])
        self.assertEqual(budget_exact.corresponding_rows(table, {0}, options), {})
        for origin in origins[2:]:
            origin['context']['target']['formBindingKey'] = 'different-table'
        self.assertEqual(budget_exact.corresponding_rows(table, {0}, options), {})

    def test_requesting_discovery_rows_from_value_batch_points_to_its_readable_values(self):
        from analysis_extension.adapters.budget_v2 import BudgetV2Adapter
        capture, receipt = exact_fixture()
        step = receipt['data']['stepResults'][0]
        self.assertEqual(BudgetV2Adapter().rows(capture, 'batch'), step['rows'])
        del step['rows']
        update(capture, receipt)
        with self.assertRaises(AnalysisError) as failure:
            BudgetV2Adapter().rows(capture, 'batch')
        self.assertEqual(failure.exception.details['nextRead'], {'section': 'materials', 'captureRef': capture['captureRef']})

    def test_batch_grain_alias_requires_one_code_in_this_tables_metadata(self):
        from analysis_extension.prepared_inputs import normalize_prepared_grains
        capture, receipt = exact_fixture()
        ref = capture['captureRef']
        item = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'grain': ['部门']}
        entries = [{'dimCode': 'MD_ORG', 'label': '部门', 'modelCode': 'MX_FINANCIAL'},
                   {'dimCode': 'MD_SCENE', 'label': '部门', 'modelCode': 'OTHER'}]
        receipt['data']['dimensionCatalog'] = entries
        update(capture, receipt)
        self.assertEqual(normalize_prepared_grains({ref: capture}, [item], {})[0]['grain'], ['MD_ORG'])
        self.assertEqual(item['grain'], ['部门'])
        entries[1]['modelCode'] = 'MX_FINANCIAL'
        update(capture, receipt)
        with self.assertRaisesRegex(AnalysisError, '多个实际维度'):
            normalize_prepared_grains({ref: capture}, [item], {})

    def test_multi_metric_selector_round_trips_and_still_rejects_a_real_filter_widening(self):
        capture, receipt = exact_fixture()
        selector, = budget_batch.selectors(capture)
        self.assertNotIn('rowIndices', selector)
        self.assertEqual(selector['columnNames'], ['metric1', 'metric2'])
        self.assertEqual(len(budget_batch.catalog(capture, [selector])), 4)
        self.assertEqual(len(budget_batch.catalog(capture, [{**selector, 'rowIndices': [0, 2]}])), 2)
        receipt['data']['resultSets'][0]['rows'].pop()
        update(capture, receipt)
        with self.assertRaisesRegex(AnalysisError, '超出'):
            budget_batch.catalog(capture, [selector])
        filtered = budget_batch.selectors(capture)
        self.assertEqual(len(budget_batch.catalog(capture, filtered)), 3)

    def test_inventory_accepts_source_measure_and_batch_selector(self):
        from analysis_extension.inventory import inventory
        capture, _ = exact_fixture()
        store = SimpleNamespace(get_capture=Mock(return_value=capture),
            limits=SimpleNamespace(max_facts=100, page_size=100, max_selections=100))
        source = inventory(store, None, 0, 100, captureRef=capture['captureRef'], measureCode='ZB_JE2')
        selector = inventory(store, None, 0, 100, captureRef=capture['captureRef'], measureCode='metric1')
        self.assertEqual(source['total'], 4)
        self.assertEqual(selector['total'], 2)
        self.assertEqual(inventory(store, None, 0, 100, captureRef=capture['captureRef'], measureCode='unknown')['total'], 0)

    def test_query_and_catalog_use_target_refs_not_empty_rows(self):
        capture, receipt = exact_fixture()
        receipt_from_capture(capture)
        facts = catalog(capture)
        self.assertEqual(len(facts), 4)
        self.assertEqual({fact['targetRef'] for fact in facts}, {'target-0-0', 'target-0-1', 'target-1-0', 'target-1-1'})
        self.assertTrue(all('/data/cellResults/' in fact['sourcePointer'] for fact in facts))
        self.assertEqual(budget_batch.selectors(capture)[0]['rowCount'], 4)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['cellResults'].reverse()
        update(capture, receipt)
        receipt_from_capture(capture)
        self.assertEqual(len(catalog(capture)), 4)

    def test_missing_duplicate_coordinate_and_value_tampering_are_rejected(self):
        for change in ('missing', 'duplicate', 'coordinate', 'value', 'origin'):
            capture, receipt = exact_fixture()
            table = receipt['data']['stepResults'][0]['batchResults'][0]; rows = table['data']['cellResults']
            if change == 'missing': rows.pop()
            if change == 'duplicate': rows[1] = deepcopy(rows[0])
            if change == 'coordinate': rows[0]['dimensions'] = {**rows[0]['dimensions'], 'MD_ORG': 'other'}
            if change == 'value': rows[0]['value'] = '99999'
            if change == 'origin': table['cellOrigins'][0]['selectionObservationId'] = 'other-observation'
            update(capture, receipt)
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                catalog(capture)

    def test_sparse_selection_does_not_invent_cross_metric_facts(self):
        capture, receipt = exact_fixture()
        selection = {'captureRef': capture['captureRef'], 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        facts = budget_batch.catalog(capture, [selection])
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(fact['queryMetricName'] == 'metric1' for fact in facts))
        from analysis_extension.batch_scope import selection_audit
        scope = {'memberDimension': 'MD_ORG', 'members': ['07', '05']}
        audit = selection_audit({capture['captureRef']: capture}, [selection], scope)
        self.assertEqual(audit[0]['missingSelectedCellCount'], 0)
        self.assertEqual(audit[0]['selectedRowCount'], 2)
        adapted = budget_batch.adapt({capture['captureRef']: capture}, [selection], scope, 100)
        self.assertEqual(len(adapted['facts']), 2)

    def test_null_missing_and_zero_are_not_conflated(self):
        capture, receipt = exact_fixture(); data = receipt['data']
        table = data['stepResults'][0]['batchResults'][0]
        table['data']['cellResults'][0]['value'] = data['resultSets'][0]['rows'][0]['value'] = '0'
        item = table['data']['cellResults'][2]; item['value'] = None; item['valueState'] = 'NO_DATA'
        fact = data['resultSets'][1]['rows'][0]; fact['value'] = None; fact['valueState'] = 'NO_DATA'
        table['data']['cellSummary'].update(presentCount=2, missingCount=1)
        update(capture, receipt)
        facts = catalog(capture)
        self.assertEqual(facts[0]['value'], '0')
        self.assertEqual(facts[2]['valueState'], 'NO_DATA')
        self.assertEqual(facts[1]['sourceValueState'], 'NULL_VALUE')

    def test_scope_filters_out_members_that_only_exist_for_an_unselected_metric(self):
        capture, receipt = exact_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['cellResults'][2]['dimensions']['MD_ORG'] = 'OTHER'
        # The native receipt, target and origin share this coordinate in the fixture.
        receipt['data']['resultSets'][1]['rows'][0]['dimensions']['MD_ORG'] = 'OTHER'
        update(capture, receipt)
        selection = {'captureRef': capture['captureRef'], 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        adapted = budget_batch.adapt({capture['captureRef']: capture}, [selection],
                                    {'memberDimension': 'MD_ORG', 'members': ['07', '05']}, 100)
        self.assertTrue(adapted['scope']['populationComplete'])
        self.assertEqual({item['objectId'] for item in adapted['facts']}, {'07', '05'})

    def test_no_data_is_terminal_but_null_zero_and_failed_reads_are_not_no_data(self):
        from analysis_extension.query_empty_result import empty_query_answer
        source = {'source': {'businessQuery': {'purpose': 'QUERY', 'complete': True,
                  'failureSummary': {'outcome': 'NO_DATA'}}}, 'facts': [{'valueState': 'NO_DATA'}]}
        evidence = {'sources': [source]}
        from analysis_empty_delivery_fixtures import attach_empty_business_view
        attach_empty_business_view(evidence)
        answer = empty_query_answer(evidence)
        self.assertEqual(answer['reason'], 'NO_DATA')
        self.assertIn('合成利润表', answer['answer'])
        self.assertIn('| 项目 | 内容 |', answer['answer'])
        self.assertIn('| 情景 | 实际数 |', answer['answer'])
        self.assertIn('| 业务表 | 指标 | 查询值 | 单位 |', answer['answer'])
        self.assertIn('| 合成利润表 | 净利润 | 无数据 | 元 |', answer['answer'])
        for state in ['PRESENT', 'NULL', 'NO_VALUE', 'ERROR']:
            source['facts'][0]['valueState'] = state
            self.assertIsNone(empty_query_answer(evidence))
        source['facts'][0]['valueState'] = 'NO_DATA'
        source['source']['businessQuery']['complete'] = False
        self.assertIsNone(empty_query_answer(evidence))


class DiscoveryEvidenceTests(TestCase):
    def test_schema_and_index_members_are_retained_as_different_evidence(self):
        from analysis_extension.adapters.budget_discovery import entity_rows
        definition = {'schemaRef': 'CLASS/MD_EVA', 'kind': 'MODEL', 'label': '经济增加值', 'modelCode': 'MD_EVA'}
        receipt = {'data': {'definitions': [{**definition, 'evidenceIds': ['schema-1']}], 'candidates': [
            {'kind': 'RESOURCE', 'resourceRef': 'MD_EVA/01', 'key': '01', 'label': '经济增加值（EVA）',
             'modelCode': 'MD_EVA', 'matchedBy': ['RESOURCE_MEMBER_INDEX_VERIFIED']},
            {'kind': 'RESOURCE', 'resourceRef': 'guess', 'matchedBy': ['FUZZY']} ]},
            'evidence': [{'evidenceId': 'schema-1', 'kind': 'SCHEMA', 'fact': definition}]}
        rows = list(entity_rows(receipt))
        self.assertEqual([row['entityKind'] for row in rows], ['MEMBER', 'SCHEMA'])
        self.assertEqual(rows[1]['schemaRef'], 'CLASS/MD_EVA')
        receipt['evidence'] = []
        self.assertEqual(len(list(entity_rows(receipt))), 1)

    def test_automatically_discovered_task_does_not_constrain_a_new_metric(self):
        from analysis_extension.followup_scope import prepare_request
        from analysis_extension.adapters import default_sources
        capture, receipt = exact_fixture()
        capture['request']['scope'].pop('taskCodes')
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        turn = SimpleNamespace(service=SimpleNamespace(sources=default_sources()), state={'request': '再查询一下EVA', 'previousQueryIntent': {
            'metric': '净利润', 'population': '久其集团', 'period': '2026年', 'scenario': '预算'},
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['metric']}})
        origin = {'originalToolName': 'execute_budget_query', 'resourceId': capture['resourceId']}
        with patch('analysis_extension.followup_scope._inherited_tables', return_value=[({}, capture, table, None)]), \
             patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, origin, {'purpose': 'QUERY', 'question': 'EVA'})
        self.assertNotIn('taskCodes', result.get('scope', {}))
        self.assertNotIn('taskNames', result.get('scope', {}))
        self.assertIn('久其集团', result['contextText']); self.assertIn('预算', result['contextText'])
