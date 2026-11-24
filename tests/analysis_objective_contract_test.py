from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase

import analysis_fixtures
from jsonschema import Draft202012Validator
from analysis_extension import form_history, objectives
from analysis_extension.condition_context import advance
from analysis_extension.models import AnalysisError
from analysis_extension.query_contract import model_schema
from analysis_extension.review_coverage import validate_coverage
from analysis_extension.tools import AnalysisTask


class ObjectiveContractTests(TestCase):
    def test_values_only_analysis_gets_a_choice_after_selecting_the_subject(self):
        from analysis_extension.task_projection import render
        values = {'objectiveRef': 'one', 'role': 'PRIMARY', 'capability': 'VALUES'}
        packet = {'mode': 'ANALYSIS', 'delivery': 'DOCUMENT', 'objectives': [values]}
        text = render(packet)
        self.assertIn('为主体追加 DATA_COMPOSITION', text)
        self.assertIn('只做口径比较', text)
        self.assertNotIn('为主体追加', render({**packet, 'mode': 'DIRECT'}))
        packet['objectives'].append({**values, 'objectiveRef': 'two', 'capability': 'DATA_COMPOSITION'})
        packet['preparedFormulaInputs'] = {'expected': 2, 'ready': 2}
        self.assertNotIn('为主体追加', render(packet))
        self.assertIn('默认展开', render(packet))

    def test_complete_empty_delivery_remembers_both_scenarios_but_not_an_unpublished_probe(self):
        from unittest.mock import patch
        from analysis_extension.query_empty_result import empty_query_answer
        from analysis_extension.query_subject import remembered_subject
        def source(ref):
            return {'captureRef': ref, 'source': {
                'businessTableView': {'allFactsIncluded': True, 'markdown': '表样甲，金额为空'},
                'businessQuery': {'purpose': 'QUERY', 'complete': True, 'failureSummary': {'outcome': 'NO_DATA'}}}}
        answer = empty_query_answer({'sources': [source('delivered')]})
        turn = SimpleNamespace(state={'hostCloseout': answer})
        candidates = [{'captureRef': 'delivered', 'batchId': scenario, 'stepId': scenario, 'candidateRef': scenario}
                      for scenario in ('S1', 'S2')]
        candidates.append({'captureRef': 'probe', 'batchId': 'other', 'candidateRef': 'unused'})
        with patch('analysis_extension.query_subject.candidates', return_value=candidates):
            scope = remembered_subject(turn)
            self.assertEqual([x['batchId'] for x in scope['selections']], ['S1', 'S2'])
            turn.state.clear()
            self.assertEqual(remembered_subject(turn)['selections'], [])

    def test_explicit_sources_identify_all_conflicts_and_only_committed_objective_refs(self):
        from analysis_extension.task_projection import render

        existing = {'objectiveRef': 'obj-old', 'question': '预算的构成', 'capability': 'DATA_COMPOSITION',
                    'role': 'PRIMARY', 'sourceRef': 'src-old'}
        proposed = {'objectiveRef': 'obj-new', 'question': '比较基准', 'capability': 'VALUES',
                    'role': 'REFERENCE', 'sourceRef': 'src-new'}
        selected = {'role': 'PRIMARY', 'sourceRef': 'src-new', 'rowCount': 2, 'label': '本轮筛选结果'}
        state = {'objectives': [existing, proposed], 'adoptedInputs': [selected]}
        with self.assertRaises(AnalysisError) as failure:
            objectives.adopt_bound_sources(SimpleNamespace(state=state), explicit=True, committed_objectives=[existing])
        self.assertEqual(failure.exception.code, 'OBJECTIVE_SOURCE_MISMATCH')
        self.assertEqual(failure.exception.details, {
            'conflicts': [existing, {k: v for k, v in proposed.items() if k != 'objectiveRef'}],
            'subjectSources': [selected],
        })
        existing['sourceRef'] = 'src-new'
        state['adoptedInputs'].append({**selected, 'role': 'REFERENCE'})
        objectives.adopt_bound_sources(SimpleNamespace(state=state), explicit=True, committed_objectives=[existing])
        markdown = render({'objectives': state['objectives']})
        self.assertIn('已绑定来源', markdown)
        self.assertIn('预算的构成 | src-new |', markdown)
        self.assertIn('比较基准 | src-new |', markdown)
        self.assertNotIn('已绑定来源', render({'objectives': [{**existing, 'sourceRef': None}]}))

    def test_pending_relation_can_be_corrected_only_with_exact_objective_formula_roots(self):
        from analysis_extension.completion import formula_correction, continue_plan
        goal = {'objectiveRef': 'one', 'role': 'PRIMARY', 'capability': 'DATA_COMPOSITION', 'formulaRefs': ['c1', 'c2']}
        old = {'taskId': 'work', 'title': '数据构成', 'kind': 'RELATION', 'refs': []}
        result = {'objectiveCoverage': [goal], 'facts': [{'factId': k} for k in ('f1','f2','other')],
                  'plan': {'workPlan': [old]}, 'derivedValues': [
                      {'derivedId': 'c'+n, 'operator':'FORMULA_CHECK', 'formulaTrace':{'targetFactRef':'f'+n}} for n in ('1','2')]}
        change = {**old, 'kind':'COMPUTATION', 'operator':'FORMULA_CHECK', 'targetRefs':['f2','f1'], 'refs':['c1','c2']}
        _, plan = continue_plan(result, {}, [change])
        self.assertEqual(plan[0]['objectiveRef'], 'one')
        for invalid in ({'refs':['c1']}, {'targetRefs':['f1','other']}, {'objectiveRef':'other'}, {'operator':'DESCRIBE'}):
            with self.subTest(invalid=invalid), self.assertRaises(AnalysisError):
                continue_plan(result, {}, [{**change, **invalid}])
        self.assertIsNone(formula_correction(result, {**old,'refs':['real_relationship']}, change))
        self.assertIsNone(formula_correction(result, old, {'operator':'FORMULA_CHECK'}))
        goal['missingTargets'] = 1
        self.assertIsNone(formula_correction(result, old, change))

    def test_source_values_in_selected_group_exclude_formula_leaves_and_reference_rows(self):
        from analysis_extension.completion import automatic_value_refs
        a = {'plan':{'workPlan':[{'taskId':'v','kind':'VALUES','refs':[]}]}, 'scope':{'selectedDataComplete':True,
             'coverage':'UNKNOWN','inputGroups':[{'role':'PRIMARY','complete':True,'factRefs':['a','b']},
                                                  {'role':'REFERENCE','complete':True,'factRefs':['c']}]},
             'facts':[{'factId':k,'metricKey':'profit'} for k in ('a','b','c')] + [{'factId':'expense','metricKey':'cost'}]}
        self.assertEqual(automatic_value_refs(a), {'v':['a','b']})
        a['scope']['inputGroups'][0]['complete'] = False
        self.assertEqual(automatic_value_refs(a), {})

    def test_two_results_for_one_observation_do_not_cover_another_subject(self):
        state={'objectives':[{'objectiveRef':'o','capability':'DATA_COMPOSITION','role':'PRIMARY'}],
               'budgetFormulaTargets':[{'key':k,'objectiveRefs':['o']} for k in ('a','b')],
               'budgetFormulaPreparation':[{'targetKey':k,'objectiveRefs':['o'],'subject':{'ORG':k},
                   'selection':{'captureRef':'c','stepId':'s','observationId':k}} for k in ('a','b')]}
        art={'plan':{'workPlan':[{'taskId':'work','kind':'COMPUTATION','operator':'FORMULA_CHECK','targetRefs':['fa','fb']}]},
             'facts':[{'factId':k,'captureRefs':['c'],'stepId':'s','observationId':'a','dimensions':{'ORG':'a'}} for k in ('fa','fb')],
             'derivedValues':[{'derivedId':k,'operator':'FORMULA_CHECK','formulaTrace':{'targetFactRef':k,
                                'nodes':[{'expression':'A+B'}]}} for k in ('fa','fb')]}
        self.assertEqual(objectives.coverage(state,art)[0]['missingTargets'], 1)
        art['facts'][1].update(observationId='b',dimensions={'ORG':'b'})
        self.assertNotIn('missingTargets',objectives.coverage(state,art)[0])
    def test_declaration_and_execution_have_independent_views(self):
        state = {'turnKey': 't', 'request': '看一下这个结果'}
        tool = SimpleNamespace(service=SimpleNamespace(turn=SimpleNamespace(state=state)))
        first = AnalysisTask.model_input_schema.fget(tool)
        check = Draft202012Validator(first)
        check.validate({'action': 'declare', 'mode': 'DIRECT'})
        check.validate({'action': 'declare', 'mode': 'ANALYSIS', 'objectives': [{'capability': 'DATA_COMPOSITION'}]})
        self.assertNotIn('operations', first['properties'])
        self.assertNotIn('materials', first['properties'])
        self.assertFalse(check.is_valid({'action': 'prepare_inputs', 'analysisRef': 'a'}))
        state['mode'] = 'ANALYSIS'
        execution = Draft202012Validator(AnalysisTask.model_input_schema.fget(tool))
        execution.validate({'action': 'prepare_inputs', 'analysisRef': 'a'})
        self.assertFalse(execution.is_valid({'action': 'prepare_inputs', 'mode': 'DIRECT'}))
        self.assertFalse(execution.is_valid({'action': 'prepare_inputs', 'operations': 'broken'}))
        self.assertNotIn('operations', first['properties'])

    def test_independent_objectives_select_and_refine_a_single_business_object(self):
        state = {'turnKey': 'one'}
        state['objectives'] = objectives.merge(state, [
            {'question': '主体净利润', 'capability': 'DATA_COMPOSITION'},
            {'question': '总体分布', 'capability': 'VALUES', 'role': 'REFERENCE'}])
        subject, reference = state['objectives']
        with self.assertRaises(AnalysisError):
            objectives.query_objective(state, {})
        self.assertEqual(objectives.query_objective(state, {'objectiveRef': reference['objectiveRef']}), reference)
        new = objectives.merge(state, [{'objectiveRef': subject['objectiveRef'], 'capability': 'DATA_COMPOSITION',
                                         'sourceRef': 'source', 'factRefs': ['fact']}])
        self.assertEqual(new[1], reference)
        self.assertEqual(new[0]['factRefs'], ['fact'])
        self.assertEqual(objectives.for_selection({'objectives': new}, {'sourceRef': 'another'}), [])

    def test_reviewer_cannot_cover_subject_with_other_groups_formula_or_ranking(self):
        material = {'objectiveCoverage': [{'number': 1, 'capability': 'DATA_COMPOSITION', 'formulaRefs': ['f1']}],
                    'derived': [{'derivedId': i} for i in ('f1', 'f2', 'rank')]}
        for wrong in ('f2', 'rank'):
            with self.assertRaises(AnalysisError):
                validate_coverage({'coverage': [{'objective': 1, 'status': 'COVERED', 'evidenceRefs': [wrong]}]}, material)
        validate_coverage({'coverage': [{'objective': 1, 'status': 'COVERED', 'evidenceRefs': ['f1']}]}, material)
        validate_coverage({'coverage': [{'objective': 1, 'status': 'MISSING', 'evidenceRefs': []}],
                           'issues': [{'severity': 'BLOCKING'}]}, material)

    def test_binding_existing_goal_and_two_sources_with_the_same_business_question(self):
        state = {'turnKey': 'two'}
        goal = {'capability': 'DATA_COMPOSITION', 'question': '数值构成'}
        state['objectives'] = objectives.merge(state, [goal])
        first = state['objectives'][0]['objectiveRef']
        bound = [{**goal, 'sourceRef': s} for s in ('a', 'b')]
        state['objectives'] = objectives.merge(state, bound)
        self.assertEqual([i['sourceRef'] for i in state['objectives']], ['a', 'b'])
        self.assertEqual(state['objectives'][0]['objectiveRef'], first)
        self.assertEqual(objectives.merge(state, bound), state['objectives'])
        with self.assertRaises(AnalysisError):
            objectives.query_objective(state, {})
        self.assertEqual(objectives.query_objective(state, {'basis': {'sourceRef': 'b'}})['sourceRef'], 'b')

    def test_reviewer_receives_markdown_and_exact_object_number_and_formula_result(self):
        from analysis_extension.review_business_markdown import render_business_markdown
        material = {'userRequest': '分析预算构成', 'objectiveCoverage': [
            {'number': 1, 'question': '甲|乙', 'role': 'PRIMARY', 'capability': 'DATA_COMPOSITION', 'formulaRefs': ['d']}],
            'derived': [{'derivedId': 'd', 'operator': 'FORMULA_CHECK', 'label': '净利润'}]}
        wire = {}
        page = render_business_markdown(material, {}, wire_references=wire)
        self.assertIn(r'| 1 | 甲\|乙 | 主体 | 数据构成 | 结果1 |', page)
        self.assertEqual(wire['结果1'], 'd')

    def test_only_the_selected_real_forms_renew_including_successful_empty(self):
        origin = {'resourceId': 'budget', 'serverName': 's', 'connectionFingerprint': 'x'}
        sets = [{'resultSetId': key, 'context': {'target': {'formBindingKey': key}},
                 'rows': rows} for key, rows in [('a', [{'valueState': 'NULL'}]), ('b', []), ('c', [{'valueState': 'ERROR'}])]]
        receipt = {'executionId': 'exec', 'querySessionId': 'q', 'context': {'contextId': 'c', 'sourceId': 's'},
                   'status': 'SUCCEEDED', 'data': {'complete': True, 'resultSets': sets}}
        capture = {**origin, 'originalToolName': 'execute_budget_query'}
        service = SimpleNamespace(store=SimpleNamespace(get_capture=lambda *_: capture), owner=None,
                                  sources=SimpleNamespace(for_capture=lambda _: SimpleNamespace(receipt=lambda _: receipt)))
        state = {**advance({}, 'one'), 'resolvedSubjectScope': {'selections': [
            {'captureRef': 'cap', 'resultSetId': i} for i in ('a', 'b', 'c')]}}
        turn = SimpleNamespace(state=state, service=service)
        form_history.remember(turn)
        self.assertEqual({i['source']['resultSetId'] for i in form_history.preferences(state, origin)}, {'a', 'b'})
        self.assertTrue(all(set(i) == {'lastUsedTurn', 'source'} for i in form_history.preferences(state, origin)))
        state.update(priorFormHistory=deepcopy(state['formHistory']), **advance(state, 'two'))
        state['resolvedSubjectScope']['selections'] = [{'captureRef': 'cap', 'resultSetId': 'a'}]
        form_history.remember(turn)
        ages = lambda: {i['binding']: state['conditionTurn']-i['lastUsedTurn'] for i in state['formHistory'].values()}
        self.assertEqual(ages(), {'a': 0, 'b': 1})
        state['resolvedSubjectScope']['selections'] = [{'captureRef': 'cap', 'resultSetId': 'b'}]
        form_history.remember(turn)
        self.assertEqual(ages(), {'a': 1, 'b': 0})
        self.assertEqual(form_history.preferences(state, {**origin, 'resourceId': 'another'}), [])
        service.store.get_capture = lambda owner, ref: {**capture, 'turnKey': ref}
        prior = form_history.restore_prior(service, {'conditionTurns': {'u1': 7, 'u2': 8}}, [
            {'kind': 'DELIVERED', 'turnKey': 'u2', 'subjectScope': {'selections': [{'captureRef': 'u2', 'resultSetId': 'b'},
                {'captureRef': 'u1', 'resultSetId': 'a'}]}},
            {'kind': 'DELIVERED', 'turnKey': 'u1', 'subjectScope': {'selections': [{'captureRef': 'u1', 'resultSetId': 'a'}]}},
            {'kind': 'CANDIDATE', 'turnKey': 'u2', 'subjectScope': {'selections': [{'captureRef': 'cap', 'resultSetId': 'a'}]}}])
        self.assertEqual({item['binding']: item['lastUsedTurn'] for item in prior.values()}, {'a': 7, 'b': 8})

    def test_condition_only_receipt_is_not_a_second_value_query(self):
        from analysis_extension.adapters.budget_query import validate
        conditions = {'turn': 2, 'conversationId': 'talk', 'selected': ['a'],
                      'conditions': [{'id': 'a'}], 'omitted': []}
        req = {'question': '分析上轮结果', 'purpose': 'QUERY', 'conditionContext': conditions}
        data = {'resultType': 'BUDGET_QUERY_RESULT', 'purpose': 'QUERY', 'complete': True,
                'question': req['question'], 'effectiveRequest': req, 'summary': {'kind': 'CONDITION_BINDINGS'},
                'conditionState': {'turn': 2, 'conversationId': 'talk', 'active': ['a']},
                **{k: [] for k in ('targets','resultSets','runtimeRequests','unresolvedGroups','stepResults','origins')}}
        self.assertEqual(validate({'request': req}, {'data': data}), [])
        with self.assertRaises(AnalysisError):
            validate({'request': req}, {'data': {**data, 'resultSets': [{'value': 99}]}})
        with self.assertRaises(AnalysisError):
            validate({'request': req}, {'data': {**data, 'conditionState': {'conversationId': 'other', 'active': ['a'], 'turn': 2}}})

    def test_explicit_member_range_on_reused_data_limits_only_matching_formula_rows(self):
        from analysis_extension.budget_preparation import _scoped_formula_rows
        dims = [{'dimCode': 'MD_TEST', 'label': '责任主体', 'names': ['单位']}]
        rows = [{'dimensions': {'MD_TEST': k}, 'dimensionNames': {'MD_TEST': label}}
                for k, label in [('01', '甲'), ('02', '乙'), ('03', '丙')]]
        edit = [{'dimensionHint': '单位', 'selector': {'type': 'VALUES', 'members': ['01', '丙']}}]
        self.assertEqual(_scoped_formula_rows(rows, edit, dims), [rows[0], rows[2]])
        self.assertEqual(_scoped_formula_rows(rows, [], dims), rows)
        self.assertEqual(_scoped_formula_rows([], edit, dims), [])
        with self.assertRaises(AnalysisError):
            _scoped_formula_rows([*rows, {'dimensions': {'MD_TEST': '04'}, 'dimensionNames': {'MD_TEST': '丙'}}], edit, dims)

    def test_table_output_repair_exposes_missing_kinds_and_sources_together(self):
        from analysis_extension.python_products import products
        with self.assertRaises(AnalysisError) as problem:
            products([{'name': 'a', 'label': '本期', 'rows': [], 'columns': []},
                      {'name': 'b', 'label': '上期', 'inputRefs': ['f'], 'columns': [], 'rows': []}], {}, [], 's', 'r')
        self.assertEqual(problem.exception.details['tableOutputsMissingKind'], [0, 1])
        self.assertEqual(problem.exception.details['outputsMissingInputRefs'], [0])
