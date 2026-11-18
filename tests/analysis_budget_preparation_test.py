"""预算分析的宿主准备入口，按来源绑定、目标、权限和用途核对。"""
from copy import deepcopy
import json
from unittest.mock import AsyncMock

from analysis_fixtures import OWNER
from analysis_budget_batch_test import batch_fixture, update
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.budget_preparation import batch_targets, boundary
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion


class BudgetPreparationTests(PresentationCase):
    def setUp(self):
        super().setUp()
        self.turn = TurnCompletion(self.service, {'turnKey': 'prepared', 'mode': 'ANALYSIS', 'requiredEvidence': 'VALUES',
                                                   'request': '按实际分类汇总', 'failures': {}, 'buildRefs': []})
        self.service.turn = self.turn

    def source(self, aggregate=True):
        raw, selection, scope, receipt = batch_fixture(aggregate)
        ref = self.store.capture(OWNER, raw)
        selection = {**selection, 'captureRef': ref, 'role': 'PRIMARY'}
        return self.store.get_capture(OWNER, ref), selection, scope, receipt

    def register_runner(self, original, receipt, missing=(), amounts=None):
        self.turn.state['budgetFormCapabilities'] = {boundary(original, receipt): 'issued-capability'}
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        async def execute(turn, capture, tool, arguments):
            self.assertEqual(tool, 'execute_semantic_plan')
            reply = deepcopy(receipt)
            reply['data'] = {'stepResults': []}
            for step in arguments['plan']['steps'][1:]:
                query = step['formQuery']
                self.assertEqual(step['capabilityId'], 'issued-capability')
                if step['id'] in missing:
                    continue
                metric = next(item for item in table['metrics'] if all(
                    query['targetDimensions'].get(key) == value for key, value in item['selectedDimensions'].items() if key != 'DATATIME'))
                context = deepcopy(metric['context'])
                context['effectiveDimensions'].update(query.get('dimensions', {}))
                if 'periodCode' in query:
                    context['period'] = context['effectiveDimensions']['DATATIME'] = query['periodCode']
                cell_dimensions = dict(metric['selectedDimensions'])
                if metric.get('periodBinding') == 'EXECUTION':
                    cell_dimensions['DATATIME'] = context['period']
                elif metric.get('periodBinding') == 'CELL':
                    cell_dimensions['DATATIME'] = query['targetDimensions']['DATATIME']
                expression = metric['cellExp'].replace('-0N', 'DATATIME="' + context['period'] + '"')
                if metric.get('periodBinding') == 'CELL':
                    expression = expression.replace('DATATIME="' + metric['selectedDimensions']['DATATIME'] + '"',
                                                    'DATATIME="' + cell_dimensions['DATATIME'] + '"')
                obs = 'input-' + step['id']
                cell = {'key': '1:1', 'row': 1, 'column': 1, 'modelCode': query['modelCode'], 'measureCode': metric['measureCode'],
                        'expression': expression,
                        'dimensions': cell_dimensions, 'headers': metric['headers'],
                        'rawValueJson': (amounts or {}).get(context['effectiveDimensions']['MD_ORG'], '99999'),
                        'formulaStructure': {'address': 'A1', 'status': 'SOURCE_VALUE', 'references': []}}
                form = {'status': 'VALUE_FOUND', 'runtime': {'context': context, 'observation': {'observationId': obs, 'entryType': 'INPUT_VO'}},
                        'visitedObservationIds': [query.get('observationId', obs), obs], 'cells': [cell],
                        'formulaGraph': {'targetKeys': ['1:1'], 'cellKeys': ['1:1'], 'complete': True, 'issues': []},
                        'preparation': {'targetCount': 1, 'targetKeysComplete': True, 'targetKeys': ['1:1'], 'varyingDimensions': []}}
                reply['data']['stepResults'].append({'stepId': step['id'], 'operation': 'FORM_VIEW_QUERY', 'formResults': [form]})
            raw = {**deepcopy(capture), 'request': arguments}
            update(raw, reply)
            return [self.store.capture(OWNER, raw)], None
        self.service.budget_preparation.runner = AsyncMock(side_effect=execute)
        return self.service.budget_preparation.runner

    async def test_two_accounts_prepared_once_and_neither_sample_amount_is_a_fact(self):
        capture, selection, scope, receipt = self.source()
        runner = self.register_runner(capture, receipt)
        ready = await self.service.budget_preparation.after_query(self.turn, capture)
        self.assertEqual((ready['expected'], ready['ready']), (2, 2))
        result = await self.service.build([capture['captureRef']], [selection], scope, [], 'two-accounts')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(len(artifact['structureEvidence']), 2)
        self.assertEqual(len(artifact['facts']), 4)
        self.assertNotIn('99999', [fact['value'] for fact in artifact['facts']])
        self.assertEqual(artifact['derivedValues'], [])
        self.assertEqual(runner.await_count, 1)

    async def test_missing_structure_keeps_group_values_and_explicit_incomplete_state(self):
        capture, selection, scope, receipt = self.source()
        runner = self.register_runner(capture, receipt, missing=['form2'])
        await self.service.budget_preparation.after_query(self.turn, capture)
        built = await self.service.build([capture['captureRef']], [selection], scope, [], 'missing-sample')
        self.assertEqual(built['factCount'], 4)
        self.assertEqual((built['inputPreparation']['expected'], built['inputPreparation']['ready']), (2, 1))
        self.assertEqual(built['completion']['state'], 'INCOMPLETE')
        self.assertEqual(runner.await_count, 1)

    async def test_ordinary_query_does_not_start_structure_preparation(self):
        capture, _, _, receipt = self.source()
        runner = self.register_runner(capture, receipt)
        self.turn.state['mode'] = 'DIRECT'
        self.assertIsNone(await self.service.budget_preparation.after_query(self.turn, capture))
        runner.assert_not_awaited()

    async def test_subjects_are_scoped_by_selected_rows_not_sample_org(self):
        from analysis_extension.budget_preparation import formula_subjects
        capture, selection, scope, _ = self.source(False)
        self.turn.state.update(followupContext={'mode': 'CONTINUE'},
                               inheritedScope={'selections': [{**selection, 'columnNames': ['metric1'], 'rowIndices': [0]}]})
        target = formula_subjects(self.turn, [{'MD_ORG': '07'}])[0]
        self.assertEqual(target['query']['dimensions'], {'MD_ORG': '07'})
        self.assertEqual(target['outer']['MD_STAGE'], 'FINAL')
        self.assertNotIn('observationId', target['query'])
        with self.assertRaises(AnalysisError):
            formula_subjects(self.turn, [{'MD_ORG': '05'}])
        self.assertNotEqual(*[item['key'] for item in batch_targets(self.service, capture)])

    def row_source(self, dimension, anchor, values, binding=None):
        raw, selection, _, receipt = batch_fixture(False)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        for metric, request_metric in zip(table['metrics'], table['request']['metrics']):
            metric['context']['effectiveDimensions'][dimension] = anchor
            if binding:
                metric['periodBinding'] = binding
                metric['selectedDimensions'][dimension] = anchor
                if binding == 'CELL':
                    metric['cellExp'] = metric['cellExp'].replace('-0N', 'DATATIME="' + anchor + '"')
                    request_metric['cellExp'] = metric['cellExp']
        table['columnRoles'][dimension] = {'kind': 'DIMENSION', 'dimCode': dimension, 'valueColumn': dimension}
        table['data']['columns'].append({'name': dimension, 'label': dimension, 'type': 'STRING'})
        for row, value in zip(table['data']['rows'], values):
            row[0] = '07'
            row.append(value)
        update(raw, receipt)
        ref = self.store.capture(OWNER, raw)
        self.turn.state.update(followupContext={'mode': 'CONTINUE'}, inheritedScope={'selections': [
            {**selection, 'captureRef': ref, 'columnNames': ['metric1'], 'grain': ['MD_ORG'], 'rowIndices': [1]}]})
        return self.store.get_capture(OWNER, ref), receipt

    async def test_unique_partial_subject_keeps_the_selected_project(self):
        from analysis_extension.budget_preparation import formula_subjects
        self.row_source('MD_PROJECT', 'P1', ['P1', 'P2'])
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(target['query']['dimensions'], {'MD_ORG': '07', 'MD_PROJECT': 'P2'})
        self.assertEqual(target['subject'], {'MD_ORG': '07', 'MD_PROJECT': 'P2'})
        self.assertEqual(target['grain'], ['MD_ORG'])

    async def test_period_selected_from_row_drives_both_request_and_observation_match(self):
        from analysis_extension.budget_preparation import formula_subjects, form_selector
        capture, receipt = self.row_source('DATATIME', '2026N0001', ['2026N0001', '2025N0001'], 'EXECUTION')
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.assertEqual(target['query']['periodCode'], '2025N0001')
        self.assertEqual(target['query']['targetDimensions']['DATATIME'], '2025N0001')
        self.assertNotIn('DATATIME', target['query']['dimensions'])
        runner = self.register_runner(capture, receipt)
        records = await self.service.budget_preparation.ensure(self.turn, [target], 'PRIMARY')
        selection = records[0]['selection']
        self.assertEqual(runner.await_count, 1)
        form = self.store.get_capture(OWNER, selection['captureRef'])
        from analysis_extension.adapters.budget_v2 import receipt_from_capture
        wrong = receipt_from_capture(form)
        form['request']['plan']['steps'][1]['formQuery']['periodCode'] = '2026N0001'
        other = wrong['data']['stepResults'][0]['formResults'][0]['runtime']['context']
        other['period'] = other['effectiveDimensions']['DATATIME'] = '2026N0001'
        update(form, wrong)
        self.assertIsNone(form_selector(form, wrong, target))

    async def test_fixed_cell_period_keeps_outer_version_and_unproven_offset_stops(self):
        from analysis_extension.budget_preparation import metric_target
        capture, receipt = self.row_source('DATATIME', '2026N0001', ['2026N0001', '2025N0001'], 'CELL')
        metric = receipt['data']['stepResults'][0]['batchResults'][0]['metrics'][0]
        target = metric_target(capture, receipt, metric, {'DATATIME': '2025N0001'})
        self.assertEqual(target['outer']['DATATIME'], '2026N0001')
        self.assertEqual(target['query']['targetDimensions']['DATATIME'], '2025N0001')
        self.assertIn('observationId', target['query'])
        for binding in ('OFFSET', None):
            with self.subTest(binding=binding), self.assertRaises(AnalysisError):
                metric_target(capture, receipt, {**metric, 'periodBinding': binding}, {'DATATIME': '2025N0001'})

    async def test_inferred_input_uses_inner_data_period_and_keeps_batch_grain(self):
        from analysis_extension.budget_preparation import formula_subjects
        from analysis_extension.prepared_inputs import infer_selections
        capture, receipt = self.row_source('DATATIME', '2026N0001', ['2026N0001', '2025N0001'], 'CELL')
        self.turn.state.update(requiredEvidence='FORMULAS')
        target, = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.register_runner(capture, receipt)
        form_refs, _ = await self.service.budget_preparation._fetch(self.turn, [target])
        captures = {ref: self.store.get_capture(OWNER, ref) for ref in [capture['captureRef'], *form_refs]}
        scope = {'memberDimension': 'MD_ORG'}
        selectors = infer_selections(self.service, captures, scope)
        self.assertEqual(selectors[1]['grain'], ['MD_ORG'])
        self.turn.state['inheritedScope']['selections'][0]['rowIndices'] = [0]
        with self.assertRaises(AnalysisError) as unselected:
            infer_selections(self.service, captures, scope)
        self.assertEqual(unselected.exception.code, 'PREPARATION_NEEDS_SELECTION')

    async def test_later_observation_supersedes_failed_subject_preparation(self):
        from analysis_extension.budget_preparation import formula_subjects
        capture, selection, scope, receipt = self.source(False)
        self.turn.state.update(requiredEvidence='FORMULAS', followupContext={'mode': 'CONTINUE'},
                               inheritedScope={'selections': [{**selection, 'columnNames': ['metric1']}]})
        targets = formula_subjects(self.turn, [{'MD_ORG': '07'}])
        self.turn.state['budgetFormulaTargets'] = targets
        runner = self.register_runner(capture, receipt)
        self.service.budget_preparation.runner = AsyncMock(return_value=([], {'code': 'SOURCE_UNAVAILABLE'}))
        failed = await self.service.budget_preparation.ensure(self.turn, targets, 'PRIMARY')
        self.assertEqual(failed[0]['error']['code'], 'SOURCE_UNAVAILABLE')
        self.service.budget_preparation.runner = runner
        refs, _ = await self.service.budget_preparation._fetch(self.turn, targets)
        inputs, selections, prep = await self.service.budget_preparation.for_build(
            [capture['captureRef'], *refs], [{**selection, 'columnNames': ['metric1']}], scope)
        self.assertEqual(prep, {'expected': 1, 'ready': 1, 'issues': []})
        self.assertEqual(len(selections), 2)
        self.assertEqual(runner.await_count, 1)

    async def test_explicit_and_automatic_subject_roots_merge_by_actual_observation(self):
        from analysis_extension.budget_preparation import formula_subjects
        capture, batch, scope, receipt = self.source(False)
        batch['columnNames'] = ['metric1']
        self.turn.state.update(requiredEvidence='FORMULAS', followupContext={'mode': 'CONTINUE'},
                               inheritedScope={'selections': [batch]})
        subjects = [{'MD_ORG': '07'}, {'MD_ORG': '05'}]
        targets = formula_subjects(self.turn, subjects)
        self.turn.state['budgetFormulaTargets'] = targets
        runner = self.register_runner(capture, receipt, amounts={'07': '85084700.00', '05': '1209100.00'})
        records = await self.service.budget_preparation.ensure(self.turn, targets, 'PRIMARY')
        reference = records[0]['selection']['captureRef']
        self.assertEqual(reference, records[1]['selection']['captureRef'])
        # 重现真实报文：省略唯一观察、重复写主体条件，并把固定科目写入了 grain。
        first = {'captureRef': reference, 'stepId': 'form1', 'preparedTargets': True,
                 'role': 'PRIMARY', 'targetDimensions': subjects[0], 'grain': ['MD_ORG', 'MD_BUDACCOUNT'],
                 'selectionReason': '用户指定对象'}
        second = {'captureRef': reference, 'stepId': 'form2', 'cellKeys': ['1:1'], 'role': 'PRIMARY'}
        refs = [capture['captureRef'], reference]
        inputs = [batch, first, {**first, 'observationId': 'input-form1'}, second]
        _, merged, summary = await self.service.budget_preparation.for_build(refs, inputs, scope)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[1]['grain'], ['MD_ORG'])
        self.assertEqual(merged[1]['selectionReason'], first['selectionReason'])
        self.assertEqual([row['observationId'] for row in merged[1:]], ['input-form1', 'input-form2'])
        self.assertEqual(summary, {'expected': 2, 'ready': 2, 'issues': []})
        built = await self.service.build(refs, inputs, scope, [], 'equivalent-selector')
        self.assertEqual(built['factCount'], 4)
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        batch_ids = [fact['factId'] for fact in artifact['facts'] if fact.get('batchId')]
        root_ids = {fact['factId'] for fact in artifact['facts'] if fact.get('cellKey')}
        work = [{'taskId': 'formula', 'kind': 'COMPUTATION', 'operator': 'FORMULA_CHECK', 'title': '主体公式构成',
                 'requiredEvidence': 'FORMULAS', 'targetRefs': batch_ids, 'refs': []}]
        request = dict(captureRefs=refs, selections=inputs, scope=scope, operations=[], requestKey='reuse-root-work', workPlan=work)
        result = await self.service.build(**request)
        stored = self.store.get_analysis(OWNER, result['analysisRef'])['plan']['workPlan'][0]
        self.assertEqual(set(stored['targetRefs']), root_ids)
        self.assertEqual(len(stored['refs']), 2)
        self.assertEqual(work[0]['targetRefs'], batch_ids)
        self.assertEqual(work[0]['refs'], [])
        self.assertEqual((await self.service.build(**request))['analysisRef'], result['analysisRef'])
        from analysis_extension.budget_preparation import formula_fact_links
        mismatched = [{**fact, 'value': None} if fact.get('cellKey') else fact for fact in artifact['facts']]
        self.assertFalse(formula_fact_links(self.turn, mismatched, root_ids))
        from analysis_extension.prepared_inputs import bind_formula_work
        unverified = deepcopy(work)
        bind_formula_work(unverified, root_ids, artifact['derivedValues'], {})
        self.assertEqual(unverified, work)
        duplicate_check = {'derivedId': 'check1', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}
        ambiguous_work = [{**work[0], 'targetRefs': ['root']}]
        bind_formula_work(ambiguous_work, {'root'}, [duplicate_check, {**duplicate_check, 'derivedId': 'check2'}], {})
        self.assertEqual(ambiguous_work[0]['refs'], [])
        self.assertEqual(runner.await_count, 1)

    async def test_distinct_roots_and_explicit_reference_are_not_merged(self):
        from analysis_extension.budget_preparation import _merge_prepared
        from analysis_extension.adapters.budget_v2 import receipt_from_capture
        capture, _, _, receipt = self.source()
        self.register_runner(capture, receipt)
        await self.service.budget_preparation.after_query(self.turn, capture)
        selected = self.turn.state['budgetStructureInputs'][capture['captureRef']][0]['selection']
        source = self.store.get_capture(OWNER, selected['captureRef'])
        reply = receipt_from_capture(source)
        form = reply['data']['stepResults'][0]['formResults'][0]
        other = {**form['cells'][0], 'key': '2:1', 'row': 2}
        form['cells'].append(other)
        form['preparation'].update(targetCount=2, targetKeys=['1:1', '2:1'])
        update(source, reply)
        selectors = {key: selected[key] for key in ('captureRef', 'stepId', 'observationId')}
        addition = {**selectors, 'cellKeys': ['1:1'], 'includeFormulaDependencies': True, 'role': 'PRIMARY'}
        separate = {**selectors, 'cellKeys': ['2:1'], 'role': 'REFERENCE'}
        self.assertEqual(_merge_prepared([separate], addition, source), [separate, addition])
        # 即使样本的另一个格被误写成金额用途，也只能作为结构材料。
        sample = {**source, 'toolCallId': 'host-read-structure'}
        result = _merge_prepared([separate], {**addition, 'role': 'STRUCTURE'}, sample)
        self.assertEqual([item['role'] for item in result], ['STRUCTURE', 'STRUCTURE'])
        self.assertEqual(result[0]['cellKeys'], ['2:1'])
        with self.assertRaises(AnalysisError) as conflict:
            _merge_prepared([{**addition, 'role': 'REFERENCE'}], addition, source)
        self.assertEqual(conflict.exception.code, 'INPUT_ROLE_CONFLICT')
        wrong = {**selectors, 'preparedTargets': True, 'targetDimensions': {'MD_ORG': 'other'}}
        with self.assertRaises(AnalysisError) as missing:
            _merge_prepared([wrong], addition, source)
        self.assertEqual(missing.exception.code, 'PREPARED_TARGET_NOT_FOUND')
        # 同一步有两个观察时，省略观察的显式格不能靠取首项来去重。
        ambiguous = deepcopy(form)
        ambiguous['runtime']['observation']['observationId'] = 'another-observation'
        reply['data']['stepResults'][0]['formResults'].append(ambiguous)
        update(source, reply)
        unspecified = {key: value for key, value in separate.items() if key != 'observationId'}
        with self.assertRaises(AnalysisError) as error:
            _merge_prepared([unspecified], addition, source)
        self.assertEqual(error.exception.code, 'AMBIGUOUS_FORM')
