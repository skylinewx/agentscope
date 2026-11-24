from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from analysis_business_tables_test import fact, receipt
from analysis_extension.business_tables import build_tables
from analysis_extension.direct_query_delivery import _column_comparisons
from analysis_extension.member_filters import select_facts
from analysis_extension.models import AnalysisError
from analysis_extension.objectives import merge, pending_members, resolve_dependencies
from analysis_extension.publication import repair_request
from analysis_extension.work_bindings import bind_native, python_bindings
from analysis_fixtures import saved_fixture
from analysis_presentation_fixtures import PresentationCase


class CapabilityTests(TestCase):
    def test_adopted_members_and_fresh_query_are_distinct(self):
        turn = SimpleNamespace(state={'turnKey': 'turn'}, service=Mock())
        turn.state['objectives'] = merge(turn.state, [
            {'question': '筛选', 'capability': 'VALUES', 'sourceRef': 'selected'},
            {'question': '构成', 'capability': 'DATA_COMPOSITION', 'membersFromObjective': 1},
            {'question': '对照', 'capability': 'VALUES', 'role': 'REFERENCE', 'sourceRef': 'all'}])
        turn.service.store.get_source_view.side_effect = lambda _, ref: {'sourceRef': ref, 'rowCount': 2}
        resolve_dependencies(turn)
        self.assertEqual(turn.state['objectives'][1]['resolvedMemberSource'], 'selected')
        self.assertEqual(turn.state['objectives'][2]['sourceRef'], 'all')
        self.assertFalse(pending_members(turn.state))
        turn.state['historyReferences'] = [{'use': 'REQUERY', 'role': 'PRIMARY', 'sourceRef': 'selected'}]
        resolve_dependencies(turn)
        self.assertEqual(pending_members(turn.state)[0]['question'], '构成')

    def test_dimension_and_member_are_resolved_as_a_pair(self):
        rows = [{'dimensions': {'A': '01', 'B': '02'}, 'dimensionNames': {'A': '甲', 'B': '甲'}},
                {'dimensions': {'A': '02', 'B': '01'}, 'dimensionNames': {'A': '乙', 'B': '乙'}}]
        scopes = [{'dimensionLabels': {'A': '申请部门', 'B': '承担部门'}}]
        self.assertEqual(select_facts(rows, {'申请部门': '甲', '承担部门': '02'}, scopes), rows[:1])
        self.assertFalse(select_facts(rows, {'申请部门': '01', '承担部门': '01'}, scopes))
        with self.assertRaisesRegex(AnalysisError, '找不到'):
            select_facts(rows, {'A': '不存在'}, scopes)
        with self.assertRaisesRegex(AnalysisError, '未唯一'):
            select_facts(rows, {'部门': '01'}, [{'dimensionLabels': {'A': '部门', 'B': '部门'}}])

    def test_slice_extrema_compare_same_metric_across_organizations(self):
        facts = [fact(str(i), {'MD_ORG': org, 'account': label}, value,
                      dimensionNames={'MD_ORG': org, 'account': label})
                 for i, (org, label, value) in enumerate([
                     ('甲', '工资', '11'), ('乙', '工资', '12'), ('甲', '保险', '8'), ('乙', '保险', '2')])]
        t = build_tables(receipt(facts))[0]
        notes = _column_comparisons(t, t.rows, t.columns)
        self.assertTrue(any('保险' in note and '最低 乙 2' in note and '最高 甲 8' in note for note in notes))

    def test_local_binding_does_not_assign_one_output_to_two_tasks(self):
        work = [{'kind': 'COMPUTATION', 'operator': 'SUM', 'taskId': t, 'refs': []} for t in ('a', 'b')]
        op = {'id': 'added', 'operator': 'SUM'}
        result = {'derivedId': 'derived_added', 'sourceFactIds': ['f']}
        bind_native(work, [op], [result])
        self.assertFalse(any(t['refs'] for t in work))
        bind_native(work, [{**op, 'taskId': 'b'}], [result])
        self.assertEqual([t['refs'] for t in work], [[], ['derived_added']])
        plans = [{**t, 'operator': 'PYTHON', 'refs': []} for t in work]
        self.assertEqual(python_bindings(plans, ['final']), plans)
        self.assertEqual(python_bindings(plans[:1], ['final'])[0]['refs'], ['final'])

    def test_new_binding_and_text_edit_validate_atomically(self):
        saved = {'analysisRef': 'base', 'document': {'markdown': '已有结论', 'bindings': []}}
        patch_text = {'op': 'replaceText', 'path': '/document/markdown',
                      'value': {'oldText': '已有结论', 'newText': '已有结论 {{delta}}'}}
        binding = {'op': 'add', 'path': '/document/bindings/-', 'value': {'key': 'delta', 'valueRef': 'derived_delta'}}
        self.assertEqual(repair_request(saved, 'base', [patch_text, binding]),
                         repair_request(saved, 'base', [binding, patch_text]))
        with self.assertRaises(AnalysisError):
            repair_request(saved, 'base', [patch_text])
        self.assertEqual(saved['document']['bindings'], [])


class SavedCalculationTests(PresentationCase):
    async def test_batched_comparison_uses_capture_table_refs_in_any_display_order(self):
        from analysis_business_references_test import ValueReferenceTest
        from analysis_markdown_delivery_test import query_turn
        from analysis_extension.direct_query_delivery import source_answer
        from analysis_extension.query_brief_calculation import compute
        from analysis_extension.query_comparison import make_plan

        turn, raw = query_turn([])
        plan = make_plan(turn, {'items': [{'terms': ['甲项']}, {'terms': ['乙项']}, {'terms': ['丙项']}]})
        keys = [item['id'] for item in plan['items']]
        data = raw['data']
        data['targets'] = [{'id': key} for key in keys]
        template = data['resultSets'][0]
        data['resultSets'] = [{**template, 'targetId': key, 'resultSetId': f's{i}', 'rows': rows}
                              for i, (key, rows) in enumerate(zip(keys, [[], [fact('b', {}, '7')], [fact('c', {}, '19')]]))]
        saved = turn.service.store.get_capture(None, 'c')
        saved['turnKey'] = 'turn'
        plan.update(groups=[[keys[2], keys[0], keys[1]]], runs=[{'items': keys, 'refs': ['c']}])
        turn.state.update(turnKey='turn', comparisonPlan=plan)
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            delivery = source_answer(turn)
        selected = {f'B{i}': table for i, table in enumerate(delivery['briefTables'], 1)}
        self.assertEqual([table[1] for table in selected.values()], ['T3', 'T2'])
        service, _ = ValueReferenceTest().service(raw)
        turn.service = service
        result = await compute(turn, {'expression': 'v0 - v1', 'inputs': [
            {'table': label, 'column': 'C1', 'operation': 'SUM'} for label in selected]}, selected)
        self.assertTrue(result.endswith('v0 - v1 = 12'), result)

    async def test_optional_brief_calculation_preserves_the_source_and_its_explanation(self):
        from analysis_extension.direct_query_delivery import _write_brief
        turn = SimpleNamespace(state={'request': '比较数量'}, service=SimpleNamespace(review_budget=SimpleNamespace(call_timeout_seconds=20)))
        data = {'material': '| 单位甲 | 12 |\n| 单位乙 | 10 |', 'rows': 2,
                'briefTables': [('cap', 'T1', '数量表', [('C1', '数量')], [('DEPT', '部门')])]}
        calculation = {'expression': 'v0', 'inputs': [{'table': 'B1', 'column': 'C1', 'operation': 'MAX'}]}
        initial = SimpleNamespace(content={'summary': '单位甲的数量更高。', 'calculation': calculation}, usage=None)
        checked = SimpleNamespace(content={'summary': '单位甲的数量最高，为12。'}, usage=None)
        for calculator, followup, expected in [(AnalysisError('EMPTY_RANGE', '此范围为空'), initial, initial),
                                                ('12', checked, checked), ('12', RuntimeError('模型不可用'), initial)]:
            model = AsyncMock(side_effect=[initial, followup])
            agent = SimpleNamespace(model=SimpleNamespace(generate_structured_output=model))
            with patch('analysis_extension.query_brief_calculation.compute', new_callable=AsyncMock) as compute, \
                    patch('analysis_extension.direct_query_delivery.record_timing'):
                if isinstance(calculator, Exception):
                    compute.side_effect = calculator
                else:
                    compute.return_value = calculator
                text, _, status = await _write_brief(turn, agent, data)
            self.assertEqual(status, 'DIRECT')
            self.assertIn(expected.content['summary'], text)
            self.assertEqual(model.await_count, 2)
            for call in model.await_args_list:
                self.assertIn(data['material'], call.args[0][1].content[0].text)
            if isinstance(calculator, AnalysisError):
                texts = [m.content[0].text for m in model.await_args_list[-1].args[0]]
                self.assertIn('原表已成功取得', texts[0])
                self.assertNotIn(str(calculator), texts[1])
            schema = model.await_args_list[0].kwargs['structured_model']
            self.assertIn('summary', schema['required'])
            selectors = schema['properties']['calculation']['properties']['inputs']['items']['properties']
            self.assertEqual(selectors['table']['enum'], ['B1'])
            self.assertEqual(list(selectors['filters']['properties']), ['DEPT'])

    async def test_brief_calculates_only_from_the_delivered_table_and_reports_ties(self):
        from analysis_business_references_test import ValueReferenceTest
        from analysis_extension.query_brief_calculation import compute
        records = [fact(str(i), {'MD_ORG': k}, v, dimensionNames={'MD_ORG': k}) for i, (k, v) in
                   enumerate([('甲', '120.00'), ('乙', '130.00'), ('丙', '120.00')])]
        service, turn = ValueReferenceTest().service(receipt(records))
        turn.service = service
        request = {'expression': 'v0', 'inputs': [{'table': 'B1', 'column': 'C1', 'operation': 'MIN'}]}
        tables = {'B1': ('cap1', 'T1', '来源表', [('C1', '金额（元）')], [('MD_ORG', '组织机构')])}
        text = await compute(turn, request, tables)
        for expected in ('120', '甲', '丙'):
            self.assertIn(expected, text)
        self.assertNotIn('乙', text)
        request['inputs'][0]['table'] = 'another'
        with self.assertRaisesRegex(AnalysisError, '不在本次查询'):
            await compute(turn, request, tables)

    async def test_successful_script_can_rebind_without_execution(self):
        capture, selectors, scope = saved_fixture(self.store, ('10', '12'))
        built = await self.service.build([capture], selectors, scope, [], 'start')
        plan = [{'kind': 'COMPUTATION', 'operator': 'PYTHON', 'taskId': 'w1', 'title': '两项合计', 'refs': ['wrong']}]
        script = 'result=[{"name":"amount","label":"合计","value":"22","unit":"元","inputRefs":[x["ref"] for x in inputs]}]'
        with self.assertRaises(AnalysisError) as failure:
            await self.service.python(inputSetRef=built['inputSetRef'], workPlan=plan, code=script,
                                      purpose='计算合计', requestKey='run')
        self.assertEqual(failure.exception.details['executionStatus'], 'COMPLETED')
        run_ref = failure.exception.details['pythonRunRef']
        corrected = deepcopy(plan)
        corrected[0]['refs'] = []
        with patch('analysis_extension.python_analysis.execute_python', side_effect=AssertionError('unexpected rerun')):
            with self.assertRaises(AnalysisError) as retried:
                await self.service.python(retryOfRunRef=run_ref, code=script, purpose='计算合计', requestKey='retry')
            self.assertEqual(retried.exception.details['recovery']['registerRunRef'], run_ref)
            result = await self.service.python(registerRunRef=run_ref, workPlan=corrected, answerOutputs=['amount'], requestKey='bind')
            repeat = await self.service.python(registerRunRef=run_ref, workPlan=corrected, answerOutputs=['amount'], requestKey='bind')
        self.assertEqual(result['analysisRef'], repeat['analysisRef'])
        self.assertEqual(result['completion']['items'][0]['state'], 'READY')
