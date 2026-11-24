import copy
import unittest

from analysis_extension.business_tables import build_tables, business_view, resolve_cell, actual_periods, model_text
from analysis_extension.models import AnalysisError


def fact(key, dimensions, value='1.20', measure='ZB_JE2', title='净利润_预算数', **extra):
    return {'factId': key, 'dimensions': dimensions, 'dimensionNames': {'MD_SCENE': {'BUDGET': '预算数', 'ACTUAL': '实际数'}.get(dimensions.get('MD_SCENE')), 'MD_BUDACCOUNT': dimensions.get('MD_BUDACCOUNT')},
            'value': value, 'valueState': 'PRESENT', 'valueType': 'DECIMAL_STRING', 'unit': '元',
            'measureCode': measure, 'title': title, **extra}


def receipt(rows, **extra):
    return {'status': 'SUCCEEDED', 'data': {'resultType': 'BUDGET_QUERY_RESULT', 'purpose': 'QUERY',
            'complete': True, 'resultSets': [{'resultSetId': 's1', 'interpretationId': 'i1',
                'context': {'taskName': '任务甲', 'formName': '利润表'}, 'rows': rows}], **extra}}


class BusinessTablesTest(unittest.TestCase):
    def test_verified_ranked_sources_have_separate_delivery_counts(self):
        from analysis_extension.review_materials import business_table_view, ranked_list_source
        result = receipt([fact('a', {'MD_ORG': '1'}), fact('b', {'MD_ORG': '2'})])
        data = result['data']
        data['resultSets'].append({'resultSetId': 's2', 'interpretationId': 'i2',
                                   'context': {'formName': '另一表样'}, 'rows': [fact('c', {'MD_ORG': '1'})]})
        data['presentation'] = {'valueOrder': 'ASC', 'scope': 'EACH_RESULT_SET', 'verified': True}
        summary = '逐来源升序的记录数：利润表 2 条、另一表样 1 条。'
        self.assertIn(summary, business_view(result, 'c')['markdown'])
        self.assertIn(summary, business_table_view(result)['markdown'])
        self.assertIn('用户指定', business_view(result, 'c')['note'])
        data['presentation']['resultSets'] = [
            {'resultSetId': 's1', 'orderedFactIds': ['a', 'b'], 'rowCount': 2},
            {'resultSetId': 's2', 'orderedFactIds': ['c'], 'rowCount': 1}]
        source = {'captureRef': 'c', 'source': {'businessTableView': business_table_view(result)}}
        self.assertIs(ranked_list_source({'sources': [source]}), source)
        self.assertIsNone(ranked_list_source({'sources': [source, source]}))
        self.assertIsNone(ranked_list_source({'sources': [source], 'referencedCalculations': ['other']}))
        data['presentation']['resultSets'][0]['orderedFactIds'].reverse()
        self.assertNotIn('completeRankedLists', business_table_view(result))
        data['presentation']['verified'] = False
        self.assertNotIn('逐来源升序', business_view(result, 'c')['markdown'])

    def test_query_completion_does_not_claim_that_an_unlocated_target_had_data(self):
        unmatched = business_view(receipt([], resultSets=[], failureSummary={'outcome': 'NO_MATCH'}), 'capture')
        self.assertTrue(unmatched['materialState']['executionComplete'])
        self.assertEqual(unmatched['materialState']['businessOutcome'], 'NO_MATCH')
        self.assertIn('未定位到', unmatched['markdown'])
        empty = business_view(receipt([], failureSummary={'outcome': 'NO_DATA'}), 'other')
        self.assertIn('没有数据', empty['markdown'])
        self.assertEqual(empty['tableCount'], 1)

    def test_business_dimension_names_come_from_the_actual_model_or_runtime(self):
        key = 'MD_BUDACCOUNT'
        sample = fact('f', {key: '01', 'MD_STAGE': 'F'}, dimensionNames={key: '成员甲', 'MD_STAGE': '编制版'},
                      cellExp='FORMMODEL_N[JE]')
        source = receipt([sample])
        def body():
            from analysis_extension.review_materials import business_table_view
            view = business_view(source, 'cap')['markdown']
            self.assertEqual(view, business_table_view(source)['markdown'])
            return view
        self.assertIn('| 维度名称待确认 | 成员甲 |', body())
        self.assertIn('| 阶段 | 编制版 |', body())
        source['data']['dimensionCatalog'] = [
            {'dimCode': key, 'label': '费用类别', 'modelCode': 'FORMMODEL'},
            {'dimCode': key, 'label': '预算科目', 'modelCode': 'ANOTHER'}]
        self.assertIn('| 费用类别 | 成员甲 |', body())
        self.assertNotIn('预算科目', body())
        source['data']['resultSets'][0]['context']['dimensions'] = [{'code': key, 'name': '本表分类'}]
        self.assertIn('| 本表分类 | 成员甲 |', body())
        from analysis_extension.review_materials import business_query_projection
        from analysis_extension.fact_closeout import summarize
        contexts, facts, _ = business_query_projection({'captureRef': 'cap'}, source, {})
        fallback = summarize({'facts': facts, 'contexts': [contexts]}, 'review')
        self.assertIn('本表分类=成员甲', fallback['answer'])

    def test_table_name_prefix_is_not_dimension_metadata_for_another_model(self):
        from analysis_extension.budget_dimensions import dimension_labels
        catalog = [{'modelCode': 'EXPENSE', 'dimCode': 'MD_BUDACCOUNT', 'label': '费用类别'}]
        for suffix in ('', '_N', '_J', '_Y'):
            self.assertEqual(dimension_labels(catalog, models=['EXPENSE' + suffix])['MD_BUDACCOUNT'], '费用类别')
        self.assertNotIn('MD_BUDACCOUNT', dimension_labels(catalog, models=['EXPENSE_PLAN']))

    def test_single_value_shares_conditions_and_preserves_both_source_cells(self):
        from analysis_extension.review_materials import business_table_view

        dimensions = {'MD_ORG': 'org', 'MD_SCENE': 'BUDGET', 'DATATIME': '2026J0002'}
        names = {'MD_ORG': '甲集团', 'MD_SCENE': '预算数', 'DATATIME': '2026年第2季度'}
        records = []
        for number, (form, dim, measure, value) in enumerate([
            ('利润表', 'account', 'ZB_JE2', '123.40'), ('经营指标表', 'indicator', 'JE', '0.00')
        ], 1):
            cell = fact(f'f{number}', {**dimensions, dim: '01'}, value, measure, title='预算数',
                        dimensionNames={**names, dim: '净利润|本年'})
            records.append({'resultSetId': f's{number}', 'targetId': 't', 'context': {
                'formName': form, 'taskName': '任务甲', 'period': '2026N0001'}, 'rows': [cell]})
        data = receipt([], resultSets=records, targets=[{'id': 't', 'label': '净利润'}],
                       dimensionCatalog=[{'dimCode': 'account', 'label': '预算科目'},
                                         {'dimCode': 'indicator', 'label': '管理指标'}])
        original = copy.deepcopy(data)
        view = business_view(data, 'cap')
        body = view['markdown']
        self.assertEqual(1, body.count('| 项目 | 内容 |'))
        for line in ['| 指标 | 净利润 |', '| 组织机构 | 甲集团 |',
                     '| 数据时期 | 2026年第2季度 |', '| 业务表 | 指标 | 本表条件 | 查询值 | 单位 |',
                     r'| 利润表 | 净利润 | 预算科目：净利润\|本年 | 123.40 | 元 |', r'| 经营指标表 | 净利润 | 管理指标：净利润\|本年 | 0.00 | 元 |']:
            self.assertIn(line, body)
        self.assertNotIn('R1', body)
        self.assertNotIn('JE', body)
        review = business_table_view(data)
        self.assertEqual(body, review['markdown'])
        for entry, (name, fact_id) in zip(view['tables'], [('利润表', 'f1'), ('经营指标表', 'f2')], strict=True):
            self.assertEqual(name, entry['sourceName'])
            column = entry['columns'][0]['id']
            self.assertEqual(fact_id, resolve_cell(data, entry['tableRef'], entry['rowRef'], column)['factId'])
            self.assertEqual([{'tableRef': entry['tableRef'], 'rowRef': 'R1', 'columnRef': column}], review['factLocations'][fact_id])
        for column in ['预算数', '预算数（元）', 'JE', 'JE（元）', 'C1']:
            self.assertEqual('f2', resolve_cell(data, 'T2', 'R1', column)['factId'])
        self.assertEqual('预算数（元）', view['tables'][1]['columns'][0]['label'])
        self.assertEqual(original, data)

    def test_single_value_states_and_scalar_results_with_no_unit(self):
        cases = [('PRESENT', '0', '0'), ('NULL', None, '空值'), ('NO_DATA', None, '无数据'),
                 ('NOT_LOADED', None, '未取得'), ('PRESENT', '编号|甲', r'编号\|甲')]
        for state, value, displayed in cases:
            with self.subTest(state=state, value=value):
                source = receipt([fact('one', {'MD_SCENE': 'BUDGET'}, value, valueState=state, unit=None,
                                       valueType='STRING' if value == '编号|甲' else 'DECIMAL_STRING')])
                body = business_view(source, 'cap')['markdown']
                self.assertIn('| 项目 | 内容 |', body)
                self.assertIn('| 业务表 | 指标 | 查询值 | 单位 |', body)
                heading = '净利润（文本）' if value == '编号|甲' else '净利润'
                self.assertIn(f'| 利润表 | {heading} | {displayed} |  |', body)
                self.assertNotIn('（元）', body)
                self.assertNotIn('| R1 |', body)

    def test_one_result_of_a_list_or_analysis_keeps_the_detail_layout(self):
        row = fact('one', {'MD_SCENE': 'BUDGET'})
        for request in [{'purpose': 'PREPARE_MATERIALS'}, {'selection': 'FULL_FORM'},
                        {'presentation': {'valueOrder': 'DESC'}}, {'grouping': [{'dimCode': 'MD_ORG'}]},
                        {'valueFilter': {'operator': 'LT', 'value': '1'}},
                        {'scopeResolution': [{'condition': {'hierarchy': {'level': 2}}}]},
                        {'scopeResolution': [{'condition': {'all': True}}]},
                        {'scopeResolution': [{'condition': {'values': ['01', '02']}}]},
                        {'targets': [{'id': 'a'}, {'id': 'b'}]}]:
            with self.subTest(request=request):
                self.assertIn('| R1 |', business_view(receipt([row], **request), 'cap')['markdown'])
        source = receipt([row, fact('two', {'MD_SCENE': 'BUDGET'})])
        first_page = business_view(source, 'cap', limit=1)
        self.assertIn('| R1 |', first_page['markdown'])
        self.assertIn('nextRead', first_page)

    def test_loading_context_and_actual_cell_conditions_stay_distinct(self):
        cell = fact('one', {'MD_SCENE': 'ACTUAL'}, '2', dataPeriod='2026J0002')
        source = receipt([cell])
        source['data']['resultSets'][0]['context'].update(
            effectiveDimensions={'DATATIME': '2026N0001', 'MD_SCENE': 'BUDGET', 'obj': '01'},
            dimensions=[{'code': 'obj', 'name': '业务对象', 'value': '01', 'valueLabel': '甲'}])
        body = business_view(source, 'cap')['markdown']
        self.assertIn('| 数据加载条件（业务对象） | 甲 |', body)
        self.assertNotIn('数据加载时期', body)
        self.assertNotIn('时期取值', body)
        self.assertIn('| 数据时期 | 2026年第2季度 |', body)
        self.assertIn('| 情景 | 实际数 |', body)
        self.assertNotIn('BUDGET', body)

    def test_point_display_uses_resolved_business_member_in_its_form_with_source_precision(self):
        from analysis_extension.single_value_presentation import single_value_markdown
        source = receipt([], targets=[{'id': 't', 'label': '预算 净利润预算'}])
        results = source['data']['resultSets']
        for i, name in enumerate(['五、净利润（净亏损以“-”号填列）', '预算完成率']):
            cell = fact(str(i), {'MD_SCENE': 'B', 'account': '001', 'DATATIME': '2026J0003'},
                        '-1234567890123456789.2300', dimensionNames={'account': name, 'MD_SCENE': '预算数'})
            result = {'resultSetId': str(i), 'targetId': 't', 'context': {'formName': '表' + str(i), 'period': '2026N0001'},
                      'resolvedTerms': [{'dimensionCode':'MD_SCENE','label':'预算数'},
                                        {'dimensionCode':'account','label':name}], 'rows':[cell]}
            if i == 0:
                results[0] = result
            else:
                results.append(result)
        before = copy.deepcopy(source)
        body = single_value_markdown(build_tables(source))
        for expected in ('| 表0 | 净利润 |', '| 表1 | 预算完成率 |', '| 数据时期 | 2026年第3季度 |',
                         '-1,234,567,890,123,456,789.2300', '| 情景 | 预算数 |'):
            self.assertIn(expected, body)
        self.assertNotIn('预算 净利润预算', body)
        self.assertEqual(source, before)

    def test_zero_rows_use_confirmed_metric_period_and_never_borrow_a_different_forms_period(self):
        from analysis_extension.display_tables import query_markdown
        context = {'baseContext': {'snapshotId':'v'}, 'target': {'formCode':'F'}, 'period':'2026N0001'}
        data = receipt([], targets=[{'id':'t','label':'销量'}])
        result = data['data']['resultSets'][0]
        result.update(targetId='t', context=context)
        metric = {'name':'m', 'context':copy.deepcopy(context), 'selectedDimensions':{'DATATIME':'2026J0002'}}
        batch = {'batchId':'b','disposition':'SUCCEEDED','metrics':[metric],
                 'data':{'resolvedPeriods':[{'dimCode':'DATATIME','values':['2026J0002']}]}}
        data['data'].update(stepResults=[{'batchResults':[batch]}],
                            valueFilter={'batches':[{'batchId':'b','status':'SUCCEEDED','logicalTargetColumns':{'t':'m'}}]})
        def answer():
            return query_markdown(build_tables(data)[0])
        self.assertIn('| 数据时期 | 2026年第2季度 |', answer())
        metric['context']['target'] = {'formCode':'other'}
        self.assertIn('| 数据时期 | 未返回 |', answer())
        self.assertNotIn('2026年', answer())

    def test_one_requested_member_can_have_two_source_readings(self):
        source = receipt([], resultSets=[], effectiveRequest={'intentDraft': {'dimensionScopes': [
            {'id': 's', 'selector': {'type': 'VALUES', 'members': ['实际']}}]}},
            scopeResolution=[{'id': 's', 'condition': {'dimCode': 'MD_SCENE', 'values': ['actual1', 'actual2']}}])
        for idx, name in enumerate(['实际数', '实际值'], 1):
            row = fact(f'f{idx}', {'MD_SCENE': f'actual{idx}'}, None, valueState='NULL',
                       dimensionNames={'MD_SCENE': name})
            source['data']['resultSets'].append(
                {'resultSetId': f's{idx}', 'context': {'formName': '利润表', 'taskName': '任务甲'}, 'rows': [row]})
        body = business_view(source, 'cap')['markdown']
        self.assertIn('| 项目 | 内容 |', body)
        self.assertEqual(2, body.count('| 空值 |'))
        self.assertIn('情景：实际数 | 空值 | 元 |', body)
        self.assertIn('情景：实际值 | 空值 | 元 |', body)
        source['data']['effectiveRequest']['intentDraft']['dimensionScopes'][0]['selector']['members'] = ['实际数', '实际值']
        self.assertIn('| 行号 |', business_view(source, 'cap')['markdown'])

    def test_singleton_sources_keep_equal_values_and_identity_across_pages(self):
        from analysis_extension.business_tables import encode

        source = receipt([])
        results = []
        for i in range(18):
            results.append({'resultSetId': str(i), 'context': {'formName': f'表{i}', 'taskName': '任务甲'},
                            'rows': [fact(f'f{i}', {'MD_SCENE': 'BUDGET'}, '1.00')]})
        source['data']['resultSets'] = results
        seen = []
        request = {}
        while len(seen) < len(results):
            page = business_view(source, 'cap', max_chars=3000, **request)
            self.assertLessEqual(len(encode(page)), 3000)
            self.assertTrue(page['tables'])
            self.assertEqual(len(page['tables']), page['markdown'].count('1.00'))
            self.assertNotIn('| R1 |', page['markdown'])
            for table in page['tables']:
                seen.append(resolve_cell(source, table['tableRef'], table['rowRef'], table['columns'][0]['id'])['factId'])
            if page['readComplete']:
                break
            request = {key: value for key, value in page['nextRead'].items() if key != 'captureRef'}
        self.assertEqual([f'f{i}' for i in range(18)], seen)
        self.assertTrue(request)

    def test_hierarchy_guidance_is_shared_by_the_result_and_its_reviewer(self):
        from analysis_extension.business_tables import HIERARCHY_SCOPE_GUIDANCE
        from analysis_extension.turn_completion import QUERY_REVIEW_PROMPT

        for selector, hierarchical in [({'hierarchy': {'level': 2}}, True), ({'values': ['01']}, False)]:
            with self.subTest(selector=selector):
                source = receipt([fact('f', {'department': '01'})],
                                 scopeResolution=[{'condition': {'dimCode': 'department', **selector}}])
                view = business_view(source, 'cap')
                self.assertNotIn(HIERARCHY_SCOPE_GUIDANCE, view['markdown'])
                self.assertEqual(HIERARCHY_SCOPE_GUIDANCE in model_text(view), hierarchical)
        self.assertIn(HIERARCHY_SCOPE_GUIDANCE, QUERY_REVIEW_PROMPT)

    def test_scenario_fallback_message_only_describes_values_observed_in_that_scenario(self):
        from analysis_extension.business_tables import state_markdown
        for value, state in [(None, 'NO_DATA'), (None, 'NULL_VALUE'), ('0', 'PRESENT')]:
            with self.subTest(state=state):
                data = receipt([fact('actual', {}, value=value, valueState=state), fact('other', {})])
                data['data']['resultSets'][0]['analysis'] = {'scenarioFallbackFactIds': ['actual']}
                output = state_markdown(data)
                self.assertIn('目标情景取数', output)
                self.assertEqual('已返回的数值可用于对比' in output, state == 'PRESENT')

    def test_same_scenario_code_does_not_split_when_some_names_are_missing(self):
        rows = [fact('a', {'MD_BUDACCOUNT': 'a', 'MD_SCENE': 'BUDGET'}, dimensionNames={}),
                fact('b', {'MD_BUDACCOUNT': 'b', 'MD_SCENE': 'BUDGET'})]
        for ordered in (rows, list(reversed(rows))):
            table = build_tables(receipt(ordered))[0]
            self.assertEqual(1, len(table.columns))
            self.assertEqual('预算数', table.columns[0]['label'])
            self.assertEqual({'a', 'b'}, set(table.locations))
            self.assertNotIn('未返回', table.markdown())

    def test_scenario_columns_and_multiple_accounts_are_lossless(self):
        rows = [fact(str(i), {'MD_BUDACCOUNT': account, 'MD_SCENE': scene, 'DATATIME': '2026N0001'}, str(i))
                for i, (account, scene) in enumerate([('收入', 'BUDGET'), ('收入', 'ACTUAL'), ('利润', 'BUDGET'), ('利润', 'ACTUAL')])]
        original = copy.deepcopy(rows)
        result = business_view(receipt(rows), 'cap1')
        self.assertTrue(result['allRowsIncluded'])
        self.assertNotIn('ZB_JE2', result['markdown'])
        self.assertIn('预算数（元）', result['markdown'])
        self.assertEqual(2, result['tables'][0]['rowCount'])
        self.assertEqual('0', resolve_cell(receipt(rows), 'T1', 'R1', 'C1')['value'])
        self.assertEqual(original, rows)

    def test_single_scenario_stays_a_column_and_null_is_not_zero(self):
        rows = [fact('1', {'MD_SCENE': 'BUDGET'}, '0'), fact('2', {'MD_SCENE': 'BUDGET'}, None, valueState='NULL')]
        view = business_view(receipt(rows), 'cap1')
        self.assertIn('| R1 | 0 |', view['markdown'])
        self.assertIn('| R2 | 空值 |', view['markdown'])
        self.assertEqual(2, view['tables'][0]['rowCount'])
        with self.assertRaises(AnalysisError):
            resolve_cell(receipt(rows), 'T1', 'R2', 'C1')

    def test_tasks_do_not_merge_and_partial_is_not_display_pagination(self):
        row = fact('1', {'MD_SCENE': 'BUDGET'})
        source = receipt([row]); second = copy.deepcopy(source['data']['resultSets'][0]); second['context']['taskName'] = '任务乙'
        source['data']['resultSets'].append(second)
        source.update(status='PARTIAL'); source['data']['complete'] = False
        view = business_view(source, 'cap1')
        self.assertEqual(2, view['tableCount'])
        self.assertTrue(view['allRowsIncluded'])
        self.assertFalse(view['complete'])

    def test_period_coverage_has_no_invented_quarter(self):
        rows = [fact(str(n), {'DATATIME': f'2026J000{n}', 'MD_SCENE': 'BUDGET'}) for n in (1, 3)]
        self.assertEqual('2026年第1季度、2026年第3季度', actual_periods(rows))
        source = receipt(rows)
        source['data']['resultSets'][0]['context']['period'] = '2026N0001'
        markdown = business_view(source, 'cap')['markdown']
        self.assertIn('数据加载时期：2026年', markdown)
        self.assertIn('实际数据时期：2026年第1季度、2026年第3季度', markdown)

    def test_ordinary_measures_pivot_without_arithmetic(self):
        rows = [fact(f'{n}-{m}', {'区域': str(n)}, str(n), measure=m, title=m, unit=None)
                for n in range(4) for m in ('数量', '收入')]
        table = build_tables(receipt(rows))[0]
        self.assertEqual(4, len(table.rows)); self.assertEqual(2, len(table.columns))
        self.assertEqual(set(f['factId'] for f in rows), set(table.locations))

    def test_paging_is_stable_and_wrong_capture_is_rejected(self):
        source = receipt([fact(str(n), {'科目': str(n), 'MD_SCENE': 'BUDGET'}) for n in range(15)])
        first = business_view(source, 'cap1', limit=6)
        nxt = first['nextRead']; nxt.pop('captureRef')
        second = business_view(source, 'cap1', **nxt)
        self.assertIn('| R7 |', second['markdown']); self.assertNotIn('| R1 |', second['markdown'])
        self.assertFalse(first['allRowsIncluded']); self.assertFalse(second['allRowsIncluded'])
        with self.assertRaises(AnalysisError): business_view(source, 'cap2', **nxt)
        filtered = business_view(source, 'cap1', tableRef='T1', filters=[{'column': '科目', 'operator': 'EQ', 'value': '10'}])
        self.assertIn('| R11 |', filtered['markdown'])

    def test_escape_and_markdown_is_not_serialized_body(self):
        row = fact('1', {'科目': 'a|b\nc', 'MD_SCENE': 'BUDGET'})
        row2 = fact('2', {'科目': 'd', 'MD_SCENE': 'BUDGET'})
        view = business_view(receipt([row, row2]), 'cap1')
        body = model_text(view)
        self.assertIn('a\\|b<br>c', body)
        self.assertNotIn('"markdown":', body)
        self.assertIn('\n| 行号', body)

    def test_same_name_member_filter_is_ambiguous(self):
        rows = [fact(str(n), {'科目': str(n)}, dimensionNames={'科目': '同名'}) for n in (1, 2)]
        with self.assertRaises(AnalysisError):
            business_view(receipt(rows), 'cap1', tableRef='T1', filters=[{'column': '科目', 'operator': 'EQ', 'value': '同名'}])

    def test_wide_row_column_pages_never_skip_other_rows_columns(self):
        rows = [fact(f'{r}-{c}', {'区域': str(r)}, str(c), measure=f'M{c}', title='指标'+str(c)) for r in (1, 2) for c in range(30)]
        source = receipt(rows); request = {}; seen = []
        for _ in range(50):
            view = business_view(source, 'cap1', max_chars=3000, **request)
            for table in view['tables']:
                import re
                displayed = re.findall(r'^\| (R\d+) \|', view['markdown'], re.M)
                seen.extend((row, col['id']) for row in displayed for col in table['columns'])
            if view['readComplete']: break
            request = {k: v for k, v in view['nextRead'].items() if k != 'captureRef'}
        self.assertEqual(60, len(seen)); self.assertEqual(60, len(set(seen)))


if __name__ == '__main__':
    unittest.main()
