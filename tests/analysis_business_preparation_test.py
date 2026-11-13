from analysis_fact_view_fixtures import fact_view
from analysis_extension.source_views import register, register_selections, select, install
from copy import deepcopy
from unittest.mock import AsyncMock

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
from analysis_extension.budget_preparation import batch_targets, formula_subjects
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion


class BusinessPreparationTests(PresentationCase):
    async def test_expired_context_reopens_only_same_source_and_freezes_selected_fact_proof(self):
        capture, receipt = self.source()
        targets = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        opened = deepcopy(capture)
        opened.pop('captureRef', None)
        opened['originalToolName'] = 'get_context'
        update(opened, {'contractVersion': '2.0', 'status': 'SUCCEEDED', 'action': 'open',
            'session': {'querySessionId': 'reopened-session', 'context': {**receipt['context'], 'contextId': 'reopened-context'}}})
        opened_ref = self.store.capture(OWNER, opened)
        runner = self.service.budget_preparation.runner = AsyncMock(side_effect=[
            ([], {'code': 'CONTEXT_EXPIRED', 'recoveryAction': 'RESTART_CONTEXT'}), ([opened_ref], None), ([], None)])
        await self.service.budget_preparation._fetch(self.turn, targets)
        calls = runner.await_args_list
        self.assertEqual([call.args[2] for call in calls], ['execute_budget_query', 'get_context', 'execute_budget_query'])
        first, retry = calls[0].args[3], calls[2].args[3]
        self.assertEqual(first['basis'], retry['basis'])
        self.assertEqual(retry['contextId'], 'reopened-context')
        self.assertEqual(retry['basis']['contextId'], receipt['context']['contextId'])
        self.assertIsNone(self.service.budget_preparation._match(self.store.get_capture(OWNER, opened_ref), targets[0]))

    async def test_unknown_execution_is_not_reopened_or_replayed_as_new_query(self):
        self.source()
        targets = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        runner = self.service.budget_preparation.runner = AsyncMock(return_value=([], {
            'code': 'QUERY_WAIT_TIMEOUT', 'recoveryAction': 'READ_STATUS'}))
        await self.service.budget_preparation._fetch(self.turn, targets)
        runner.assert_awaited_once()

    def exact_source(self, reverse=False, same_null_coordinates=False):
        from analysis_exact_cells_test import exact_fixture
        raw, receipt = exact_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        if same_null_coordinates:
            account = table['cellOrigins'][0]['selectedDimensions']['MD_BUDACCOUNT']
            for target in (receipt['data']['targets'][1], receipt['data']['resolution']['interpretations'][1]):
                target['allOf'][0]['anyOf'][0]['objectCode'] = account
            receipt['data']['resultSets'][1]['resolvedTerms'][0]['objectCode'] = account
            table['metrics'][1]['selectedDimensions']['MD_BUDACCOUNT'] = account
            receipt['data']['runtimeRequests'][0]['batchQuery']['metrics'][1]['selection']['targetDimensions']['MD_BUDACCOUNT'] = account
            for index in (2, 3):
                for dimensions in (table['data']['cellResults'][index]['dimensions'],
                                   table['cellOrigins'][index]['selectedDimensions'], table['request']['cellTargets'][index]['dimensions'],
                                   receipt['data']['resultSets'][1]['rows'][index - 2]['dimensions']):
                    dimensions['MD_BUDACCOUNT'] = account
            coordinates = table['data']['cellResults'][0]['dimensions']
            for index in (0, 2):
                table['data']['cellResults'][index].update(dimensions=coordinates, value=None, valueState='NULL_VALUE')
                table['cellOrigins'][index]['selectedDimensions'] = coordinates
                table['request']['cellTargets'][index]['dimensions'] = coordinates
                receipt['data']['resultSets'][index // 2]['rows'][0].update(dimensions=coordinates, value=None, valueState='NULL')
            table['data']['cellSummary'].update(presentCount=1, nullValueCount=3)
        # A representative metric may describe a different cell in the same batch.
        for origin in table['cellOrigins']:
            origin['cellDimensions'] = {key: value for key, value in origin['selectedDimensions'].items()
                                        if key not in origin['context']['effectiveDimensions'] or key == 'MD_ORG'}
        receipt['data']['runtimeRequests'][0]['batchQuery']['cellSelections'] = table['cellOrigins']
        if reverse:
            table['data']['cellResults'].reverse()
        update(raw, receipt)
        ref = self.store.capture(OWNER, raw)
        selection = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        self.turn = TurnCompletion(self.service, {'turnKey': 'exact-selected', 'request': '分析这项预算的构成',
            'mode': 'ANALYSIS', 'requiredEvidence': 'FORMULAS', 'failures': {}, 'buildRefs': [],
            'followupContext': {'mode': 'CONTINUE'}, 'inheritedScope': {'selections': [selection]}})
        self.service.turn = self.turn
        return self.store.get_capture(OWNER, ref), receipt, selection

    async def test_exact_query_prepares_selected_fact_by_target_ref_after_reordered_results(self):
        capture, _, selection = self.exact_source(reverse=True)
        target, = batch_targets(self.service, capture, [selection])
        self.assertEqual(target['sourceSelection'], {'executionId': 'execution-A', 'resultSetId': 'r0', 'factIds': ['f0-1']})
        self.assertEqual(target['subject']['MD_ORG'], '05')
        self.assertEqual(target['query']['targetDimensions']['MD_ORG'], '05')
        self.assertEqual(target['query']['targetDimensions']['MD_BUDACCOUNT'], '0205')
        # The selected cell is in the grid; do not change the opening organization.
        self.assertIn('observationId', target['query'])
        runner = self.service.budget_preparation.runner = AsyncMock(return_value=([], None))
        await self.service.budget_preparation._fetch(self.turn, [target])
        self.assertEqual(runner.call_args.args[-1]['basis']['source'], target['sourceSelection'])
        self.assertEqual(runner.call_args.args[-1]['purpose'], 'ANALYZE')
        self.assertNotIn('scope', runner.call_args.args[-1])

    async def test_exact_formula_subjects_use_cell_origin_and_not_representative_or_other_metric(self):
        self.exact_source(reverse=True)
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(target['sourceSelection']['factIds'], ['f0-0'])
        self.assertEqual(target['query']['targetDimensions']['MD_ORG'], '07')
        self.assertIn('observationId', target['query'])
        self.turn.state['inheritedScope']['selections'][0]['columnNames'] = ['metric2']
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(target['sourceSelection']['factIds'], ['f1-0'])

    async def test_exact_sparse_structure_sample_only_selects_its_metric_rows(self):
        capture, _, selection = self.exact_source()
        targets = batch_targets(self.service, capture)
        self.assertEqual([target['sourceSelection']['factIds'] for target in targets], [['f0-0'], ['f1-0']])
        self.assertEqual(batch_targets(self.service, capture, [{**selection, 'rowIndices': [2]}]), [])

    async def test_identical_sparse_null_rows_still_reference_the_selected_metric(self):
        self.exact_source(same_null_coordinates=True)
        self.turn.state['inheritedScope']['selections'][0]['columnNames'] = ['metric2']
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(target['sourceSelection'], {'executionId': 'execution-A', 'resultSetId': 'r1', 'factIds': ['f1-0']})

    def source(self, values=('-20.00', '0')):
        raw, receipt = business_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        aggregation = {'groupBy': [{'dimCode': 'MD_ORG'}], 'measures': [{'metric': 'metric1'}, {'metric': 'metric2'}]}
        table['request']['aggregation'] = deepcopy(aggregation)
        receipt['data']['runtimeRequests'][0]['batchQuery']['aggregation'] = deepcopy(aggregation)
        table['data']['aggregationSummary'] = {'detailRowCount': 2, 'groupCountBeforeHaving': 2, 'filteredOutGroupCount': 0}
        for index, value in enumerate(values):
            table['data']['rows'][index][2] = value
            receipt['data']['resultSets'][0]['rows'][index].update(value=value, valueState='PRESENT' if value is not None else 'NULL')
        update(raw, receipt)
        ref = self.store.capture(OWNER, raw)
        selection = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        self.turn = TurnCompletion(self.service, {'turnKey': 'selected', 'request': '分析亏损企业的亏损原因',
            'mode': 'ANALYSIS', 'requiredEvidence': 'FORMULAS', 'failures': {}, 'buildRefs': [],
            'followupContext': {'mode': 'CONTINUE', 'changedFields': ['population', 'analysisFocus']},
            'inheritedScope': {'selections': [selection]}})
        self.service.turn = self.turn
        return self.store.get_capture(OWNER, ref), receipt

    async def test_numeric_filter_restores_selected_fact_and_fetches_no_other_org(self):
        self.source()
        source = register(self.turn, self.turn.state['inheritedScope']['selections'][0])
        filtered, _ = fact_view(self.turn, source['sourceRef'], {'operator': 'LT', 'value': '0'})
        install(self.turn, select(self.turn, [{'sourceRef': filtered['sourceRef']}]))
        self.assertEqual(filtered['selection']['rowIndices'], [0])
        targets = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(targets[0]['outer']['MD_ORG'], '07')
        self.assertEqual(formula_subjects(self.turn, ['f0-0'])[0]['subject'], targets[0]['subject'])
        runner = self.service.budget_preparation.runner = AsyncMock(return_value=([], None))
        await self.service.budget_preparation._fetch(self.turn, targets)
        request = runner.call_args.args[-1]
        self.assertEqual(request['basis']['source']['factIds'], ['f0-0'])
        self.assertEqual(request['basis']['kind'], 'FACTS')
        self.assertEqual(request['purpose'], 'ANALYZE')
        self.assertNotIn('intentDraft', request)
        self.assertNotIn('scope', request)

    async def test_filter_does_not_treat_null_as_zero_and_keeps_exact_decimal(self):
        for values, predicate, count in [(('0', None), {'operator': 'EQ', 'value': '0'}, 1),
                (('0', None), {'operator': 'LT', 'value': '0'}, 0),
                (('-0.00000000000000000001', '0'), {'operator': 'LT', 'value': '0'}, 1)]:
            self.source(values)
            source = register(self.turn, self.turn.state['inheritedScope']['selections'][0])
            view, _ = fact_view(self.turn, source['sourceRef'], predicate)
            self.assertEqual(view['rowCount'], count)

    async def test_structure_sample_uses_selected_row_reference_without_rerunning_population(self):
        capture, _ = self.source()
        selection = {**self.turn.state['inheritedScope']['selections'][0], 'rowIndices': [1]}
        target, = batch_targets(self.service, capture, [selection])
        self.assertEqual(target['subject'], {'MD_ORG': '05'})
        self.assertEqual(target['sourceSelection']['factIds'], ['f0-1'])
        runner = self.service.budget_preparation.runner = AsyncMock(return_value=([], None))
        await self.service.budget_preparation._fetch(self.turn, [target])
        self.assertEqual(runner.call_args.args[-1]['basis']['source'], target['sourceSelection'])
        self.assertNotIn('intentDraft', runner.call_args.args[-1])

    async def test_multiple_candidates_require_explicit_selection_even_if_only_one_has_negative_values(self):
        self.source()
        first = deepcopy(self.turn.state['inheritedScope']['selections'][0])
        choices = register_selections(self.turn, [first, {**first, 'columnNames': ['metric2']}])
        self.assertEqual(len(choices), 2)
        self.assertNotIn('adoptedInputs', self.turn.state)
        install(self.turn, select(self.turn, [{'sourceRef': choices[0]['sourceRef']}]))
        targets = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(targets[0]['sourceSelection']['resultSetId'], 'r0')

    async def test_ambiguous_metrics_or_changed_period_do_not_restore_a_target(self):
        self.source()
        self.turn.state['inheritedScope']['selections'][0]['columnNames'] = ['metric1', 'metric2']
        with self.assertRaises(AnalysisError):
            register(self.turn, self.turn.state['inheritedScope']['selections'][0])
        self.turn.state['inheritedScope']['selections'][0]['columnNames'] = ['metric1']
        self.turn.state['followupContext']['changedFields'].append('period')
        with self.assertRaises(AnalysisError):
            formula_subjects(self.turn, [{'MD_ORG': '07'}])

    async def test_result_reference_bypasses_natural_language_root_inheritance(self):
        from analysis_extension.followup_scope import prepare_request
        capture, _ = self.source()
        source = register(self.turn, {**self.turn.state['inheritedScope']['selections'][0], 'rowIndices': [0]})
        self.turn.state['previousQueryIntent'] = {'population': '集团', 'metric': '净利润', 'period': '2026年'}
        request = prepare_request(self.turn, capture, {'purpose': 'ANALYZE',
            'basis': {'kind': 'FACTS', 'sourceRef': source['sourceRef']}, 'question': '改写'})
        self.assertEqual(request['question'], self.turn.state['request'])
        self.assertEqual(request['basis']['source']['factIds'], ['f0-0'])
        self.assertNotIn('contextText', request)
        self.assertNotIn('scope', request)
        with self.assertRaises(AnalysisError):
            prepare_request(self.turn, capture, {'basis': {'kind': 'FACTS', 'sourceRef': 'invented'}})
