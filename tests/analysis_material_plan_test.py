"""Material execution, bounded context, independent review and preservation contracts."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase

import analysis_fixtures
from analysis_extension.material_plan import normalize, needs_formulas, query_purpose
from analysis_extension.models import AnalysisError
from analysis_extension.review_budget import ReviewBudgetConfig
from analysis_extension.computation_review import computation_key, calculation_material, business_material
from analysis_extension.review_coverage import validate_coverage
from analysis_extension.model_materials import project_context
from analysis_presentation_fixtures import PresentationCase
import analysis_budget_preparation_test as preparation_fixtures


class MaterialPlanTests(TestCase):
    def test_roles_are_independent_and_formula_includes_values(self):
        plan = normalize([{'role': 'REFERENCE', 'kinds': ['VALUES']},
                          {'role': 'PRIMARY', 'kinds': ['FORMULAS']}])
        self.assertEqual(plan[0]['kinds'], ['VALUES', 'FORMULAS'])
        self.assertTrue(needs_formulas({'materials': plan}, 'PRIMARY'))
        self.assertFalse(needs_formulas({'materials': plan}, 'REFERENCE'))
        self.assertEqual(normalize(None, plan), plan)
        for bad in ([], [{'role': 'PRIMARY', 'kinds': ['UNKNOWN']}], plan + [plan[0]],
                    [{'role': 'PRIMARY', 'kinds': ['VALUES'], 'purpose': 'QUERY'}]):
            with self.assertRaises(AnalysisError): normalize(bad)

    def test_frozen_filter_precedes_formula_preparation(self):
        state = {'materials': normalize([{'role': 'PRIMARY', 'kinds': ['FORMULAS']}])}
        request = {'basis': {'kind': 'FACTS'}, 'intentDraft': {'valueFilter': {'operator': 'LT', 'value': '0'}}}
        self.assertEqual(query_purpose(state, request), 'QUERY')
        self.assertEqual(query_purpose(state, {'basis': {'kind': 'FACTS'}}), 'PREPARE_MATERIALS')
        self.assertEqual(query_purpose(state, {'materialRole': 'REFERENCE'}), 'QUERY')
        self.assertEqual(query_purpose({'mode': 'ANALYSIS'}, {}), 'QUERY')

    def test_soft_budget_is_not_a_new_capacity_gate(self):
        config = ReviewBudgetConfig(final_target_tokens=10)
        estimate = config.require({'messages': [{'content': '数据' * 1000}]}, 'FINAL_BUSINESS')
        self.assertTrue(estimate['fits'])
        self.assertTrue(estimate['exceedsOptimizationTarget'])
        self.assertFalse(estimate['optimizationTargetIsCapacityLimit'])

    def test_review_only_request_does_not_invalidate_frozen_computation(self):
        material = {'userRequest': '分析渠道', 'computationIntent': {'userRequest': '分析渠道', 'queryIntent': {'period': '2026'}},
                    'queryIntent': {'period': '2026'}, 'facts': [{'factId': 'f', 'value': '1'}],
                    'pythonRuns': [{'runRef': 'r', 'code': 'result=inputs', 'inputRecords': [{'ref': 'f', 'value': '1'}]}],
                    'derived': [{'derivedId': 'd', 'value': '1'}], 'documentText': '渠道分析'}
        key = computation_key(material, 'policy')
        changed = {**material, 'userRequest': '继续审核，不取数不计算', 'agentPlan': {'goal': '继续审核'},
                   'analysisRequirement': {'basis': 'new review request'}, 'followupScope': {'userQuote': '继续审核'}}
        self.assertEqual(computation_key(changed, 'policy'), key)
        self.assertEqual(computation_key(changed, 'policy', replace(ReviewBudgetConfig(), final_target_tokens=10)), key)
        # A new question's intent belongs to final business review; the frozen
        # computation's actual period/scope remains part of its cache identity.
        self.assertEqual(computation_key({**changed, 'queryIntent': {'period': '2027'}}, 'policy'), key)
        for field, value in [('facts', [{'factId': 'f', 'value': '2'}]),
                             ('computationIntent', {**material['computationIntent'], 'queryIntent': {'period': '2027'}}),
                             ('computationIntent', {'userRequest': '以另一组为分母'}),
                             ('pythonRuns', [{'runRef': 'r', 'code': 'result=0'}])]:
            self.assertNotEqual(computation_key({**changed, field: value}, 'policy'), key)

    def test_stage_projection_keeps_actual_inputs_and_current_business_results(self):
        source = {'userRequest': '分析', 'facts': [{'factId': 'f', 'value': None}], 'derived': [{'derivedId': 'd', 'value': '0'}],
                  'documentText': '完整报告', 'documentBindings': {'bindings': ['d']},
                  'pythonRuns': [{'runRef': 'r', 'code': 'result=inputs', 'inputRecords': [{'ref': 'f', 'value': None}],
                                  'stderr': 'unneeded runtime log' * 1000}], 'agentPlan': {'declaredGoals': ['temporary step']}}
        before = deepcopy(source)
        calculation = calculation_material(source, 'key')
        final = business_material(source, {'status': 'PASS'}, 'key')
        self.assertEqual(calculation['pythonRuns'][0]['inputRecords'], source['pythonRuns'][0]['inputRecords'])
        self.assertEqual(calculation['pythonRuns'][0]['code'], source['pythonRuns'][0]['code'])
        self.assertNotIn('stderr', calculation['pythonRuns'][0])
        self.assertNotIn('documentText', calculation)
        self.assertNotIn('pythonRuns', final)
        self.assertNotIn('agentPlan', final)
        self.assertEqual(final['documentText'], source['documentText'])
        self.assertEqual(final['facts'], source['facts'])
        self.assertEqual(source, before)

    def test_coverage_cannot_claim_missing_content_passed_or_reference_unknown_fact(self):
        item = {'requirement': '基本构成', 'status': 'MISSING', 'location': '全文', 'evidenceRefs': ['f']}
        material = {'facts': [{'factId': 'f'}]}
        with self.assertRaises(AnalysisError): validate_coverage({'coverage': [item], 'issues': []}, material)
        validate_coverage({'coverage': [item], 'issues': [{'severity': 'BLOCKING'}]}, material)
        with self.assertRaises(AnalysisError):
            validate_coverage({'coverage': [{**item, 'status': 'COVERED', 'evidenceRefs': ['invented']}], 'issues': []}, material)

    def test_short_references_restore_exact_inputs_code_missing_values_and_order(self):
        from analysis_extension.readable_material import readable_analysis_material, restore_material
        refs = ['fact_' + str(i) * 64 for i in range(1, 7)]
        facts = [{'factId': ref, 'dimensions': {'period': str(i)}, 'value': None if i == 1 else '0',
                  'sourceContextId': 'context_' + 'a' * 64} for i, ref in enumerate(refs)]
        original = {'facts': facts, 'derived': [{'derivedId': 'd', 'inputRefs': list(reversed(refs)), 'value': '0'}],
                    'pythonRuns': [{'runRef': 'r', 'inputSetRef': 'actual-frozen-input', 'code': 'result = ' + repr(refs),
                        'inputRecords': [{'ref': fact['factId'], **{k: v for k, v in fact.items() if k != 'factId'}} for fact in facts]}]}
        before = deepcopy(original)
        projected = readable_analysis_material(original)
        self.assertTrue(projected.get('referenceAliases'))
        self.assertEqual(restore_material(projected), original)
        self.assertEqual(original, before)
        self.assertEqual(restore_material(projected)['pythonRuns'][0]['code'], original['pythonRuns'][0]['code'])

    def test_history_projection_preserves_original_messages_and_current_inputs(self):
        from agentscope.message import AssistantMsg, UserMsg, ToolResultBlock, TextBlock
        captures = {f'c{i}': {'turnKey': 'old' if i < 3 else 'current', 'response': {'status': 'SUCCEEDED', 'data': {'complete': True}}}
                    for i in range(5)}
        store = SimpleNamespace(get_capture=lambda owner, ref: captures[ref])
        sources = SimpleNamespace(for_capture=lambda capture: SimpleNamespace(raw_receipt=lambda response: response))
        service = SimpleNamespace(store=store, owner='owner', sources=sources,
                                  turn=SimpleNamespace(state={'turnKey': 'current', 'queryIntent': {'scenario': '实际数'}}))
        messages = [UserMsg('user', '保留真实用户原话')]
        messages += [AssistantMsg('agent', [ToolResultBlock(id=str(i), name='query', state='success',
                    output=[TextBlock(text=str(i) * 10000)], metadata={'analysisCaptureRef': f'c{i}'})]) for i in range(5)]
        before = [message.model_dump() for message in messages]
        result, metric = project_context(service, messages, [], ReviewBudgetConfig(active_material_target_tokens=25000))
        self.assertEqual([message.model_dump() for message in messages], before)
        self.assertEqual(result[0].model_dump(), messages[0].model_dump())
        self.assertEqual(result[4].model_dump(), messages[4].model_dump())
        self.assertEqual(result[5].model_dump(), messages[5].model_dump())
        self.assertEqual(metric['compactedHistoryResults'], 3)
        self.assertLess(metric['savedEvidenceCharsAfter'], metric['savedEvidenceCharsBefore'])
        messages[1].content[0].state = 'running'
        messages[2].content[0].state = 'error'
        safe, _ = project_context(service, messages, [], ReviewBudgetConfig(active_material_target_tokens=1))
        self.assertEqual(safe[1].model_dump(), messages[1].model_dump())
        self.assertEqual(safe[2].model_dump(), messages[2].model_dump())

    def test_review_period_names_are_readable_without_rewriting_runtime_inputs(self):
        from analysis_extension.readable_material import readable_analysis_material, restore_material
        from analysis_extension.turn_completion import _wire_review_json, review_model_text
        import json
        original = {'facts': [{'factId': 'f', 'period': '2026J0002', 'dimensions': {'DATATIME': '2026J0002'}, 'value': '2'}],
                    'sourceContexts': [{'period': '2026N0001'}],
                    'pythonRuns': [{'runRef': 'r', 'inputSetRef': 'real-input', 'code': "period == '2030J0003'",
                                    'inputRecords': [{'ref': 'f', 'period': '2026J0002', 'value': '2'}]}]}
        before = deepcopy(original)
        projected = readable_analysis_material(original)
        labels = projected['materialFormat']['periods']['names']
        self.assertEqual(labels, {'2026J0002': '2026年第2季度', '2026N0001': '2026年'})
        self.assertEqual(next(iter(projected)), 'materialFormat')
        self.assertEqual(restore_material(projected), before)
        self.assertEqual(original, before)
        query_text = review_model_text(_wire_review_json({'evidence': original}))
        self.assertIn('2026年第2季度', query_text)
        self.assertIn('2026J0002', query_text)
        from analysis_extension.review_periods import period_labels
        self.assertEqual(period_labels({'period': '2026Y0002'})['names'], {'2026Y0002': '2026年2月'})
        self.assertIsNone(period_labels({'period': '2026J0005', 'code': "p='2030J0003'"}))


class AutomaticMaterialPreparationTests(PresentationCase):
    def setUp(self):
        super().setUp()
        from analysis_extension.turn_completion import TurnCompletion
        self.turn = TurnCompletion(self.service, {'turnKey': 'prepared', 'mode': 'ANALYSIS',
            'materials': normalize(None), 'request': '分析所选对象构成并对照总体', 'failures': {}, 'buildRefs': []})
        self.service.turn = self.turn
    source = preparation_fixtures.BudgetPreparationTests.source
    register_runner = preparation_fixtures.BudgetPreparationTests.register_runner

    async def test_selected_primary_prepares_all_its_targets_without_reference_expansion(self):
        from analysis_extension.source_views import register
        capture, selection, _, receipt = self.source(False)
        subject = register(self.turn, {**selection, 'columnNames': ['metric1'], 'rowIndices': [0]})
        reference = register(self.turn, {**selection, 'columnNames': ['metric1']})
        self.register_runner(capture, receipt)
        result = await self.service.task(mode='ANALYSIS', goal='分析所选对象构成并对照总体',
            materials=[{'role': 'PRIMARY', 'kinds': ['FORMULAS']}, {'role': 'REFERENCE', 'kinds': ['VALUES']}],
            subjectSources=[{'sourceRef': subject['sourceRef'], 'role': 'PRIMARY'},
                            {'sourceRef': reference['sourceRef'], 'role': 'REFERENCE'}])
        self.assertEqual(result['preparedFormulaInputs']['expected'], 1)
        self.assertTrue(result['preparedFormulaInputs']['complete'])
        self.assertEqual({target['role'] for target in self.turn.state['budgetFormulaTargets']}, {'PRIMARY'})
        from analysis_extension.task_projection import render
        self.assertIn('预期 1 个目标，就绪 1 个', render(result))


class TaskActionSchemaTests(TestCase):
    def test_prepare_fields_are_not_valid_for_blocked_declarations(self):
        from jsonschema import Draft202012Validator
        from analysis_extension.tools import AnalysisTask
        schema = Draft202012Validator(AnalysisTask.input_schema)
        self.assertTrue(list(schema.iter_errors({'action': 'blocked', 'reason': '真实失败', 'requestKey': 'not-a-call-id'})))
        self.assertFalse(list(schema.iter_errors({'action': 'declare', 'mode': 'REPORT', 'goal': '复核', 'analysisRef': 'saved-report'})))
        self.assertTrue(list(schema.iter_errors({'action': 'status', 'analysisRef': 'saved-report'})))


class ModelAdapterTelemetryTests(IsolatedAsyncioTestCase):
    async def test_query_review_keeps_readable_markdown_wire_payload(self):
        from unittest.mock import AsyncMock
        from analysis_extension.turn_completion import _model_review, QUERY_REVIEW_PROMPT, review_model_text
        import json
        from analysis_extension.review_request import make_review_request
        payload = make_review_request({'answer': '预算为1元', 'businessTableMarkdown': [
            {'captureRef': 'saved', 'markdown': '| 科目 | 金额 |\n|---|---:|\n|收入|1|'}]}, query=True)
        model = SimpleNamespace(generate_structured_output=AsyncMock(return_value=SimpleNamespace(content={'issues': []}, usage=None)))
        agent = SimpleNamespace(model=model, state=SimpleNamespace(context=[], reply_id='query'))
        self.assertEqual(await _model_review(agent, QUERY_REVIEW_PROMPT, payload), {'issues': []})
        self.assertEqual(model.generate_structured_output.call_args.args[0][1].content[0].text, payload.markdown)

    async def test_cancellation_is_an_adapter_attempt_not_a_network_diagnosis(self):
        from agentscope.model._diagnostics import observe_attempts, observed_attempt
        events = []
        async def never():
            raise asyncio.CancelledError()
        with observe_attempts(events.append):
            with self.assertRaises(asyncio.CancelledError):
                await observed_attempt(never, call_id='call', strategy='auto', attempt=1)
        self.assertEqual([e['status'] for e in events], ['STARTED', 'CANCELLED'])
        self.assertNotIn('httpStatus', events[-1])
        self.assertIn('elapsedMs', events[-1])
