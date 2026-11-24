"""Verify the public projection independently from tool navigation and source arithmetic."""
from copy import deepcopy
from unittest import TestCase

from analysis_extension.business_tables import BusinessTable
from analysis_extension.budget_dimensions import dimension_labels
from analysis_extension.completion import completion_summary
from analysis_extension.display_tables import aligned_filter_view, appendix_tables, comparison_labels, display_facts, field_name, link_copied_source_table, member_name, visible_table
from analysis_extension.formula_labels import component_labels
from analysis_extension.formatting import formula_text
from analysis_extension.formulas import Expression
from analysis_extension.models import AnalysisError
from analysis_extension.publication import repair_request
from analysis_extension.report_markdown import appendix, dataset_html, parser


def fact(ref, period, member, measure, value, role='PRIMARY'):
    return {'factId': ref, 'sourceContextId': 'snapshot-A', 'bindingRef': 'form-A', 'captureRef': 'capture-A',
            'formName': '运营表', 'taskName': '计划', 'inputRole': role, 'batchId': 'batch-A',
            'dimensions': {'DATATIME': period, 'axis': member}, 'dimensionNames': {'axis': '合计' if member == '00000000' else member},
            'headers': [measure], 'sourceMeasureCode': measure, 'value': value,
            'valueState': 'PRESENT' if value is not None else 'NULL', 'valueType': 'DECIMAL_STRING', 'unit': '元'}


class PublicDisplayTests(TestCase):
    def test_shared_coordinate_moves_to_caption_only_if_objects_remain_distinguishable(self):
        def project(coordinates):
            facts, rows = {}, []
            for i, (product, scene) in enumerate(coordinates):
                name = '甲类' if product == 'A' else '乙类'
                ref = str(i)
                facts[ref] = {'grain': ['category', 'MD_SCENE'], 'dimensions': {'category': product, 'MD_SCENE': scene},
                              'dimensionNames': {'category': name, 'MD_SCENE': scene}}
                rows.append({'values': {'factRef': ref, 'object': name + ' / ' + scene, 'value': str(i)}})
            dataset = {'kind': 'TABLE', 'rows': rows}
            comparison_labels(dataset, facts)
            return dataset
        uniform = project([('A', '计划'), ('B', '计划')])
        self.assertEqual(uniform['commonMemberNames'], ['计划'])
        self.assertEqual([r['values']['object'] for r in uniform['rows']], ['甲类', '乙类'])
        mixed = project([('A', '计划'), ('B', '执行')])
        self.assertNotIn('commonMemberNames', mixed)
        self.assertEqual(mixed['rows'][1]['values']['object'], '乙类 / 执行')

    def test_batch_appendix_uses_the_scenario_contract_and_retains_raw_measure(self):
        f = fact('f', '2026N0001', 'a', '内部格式名称', '3')
        f.update(sourceMeasureCode='ZB_JE2', measureCode='metric1')
        f['dimensions']['MD_SCENE'] = 'scenario-a'
        f['dimensionNames']['MD_SCENE'] = '计划'
        (t, _), = appendix_tables({'facts': [f]})
        self.assertEqual(visible_table(t), (['计划（元）'], [['3']]))
        self.assertEqual(f['headers'], ['内部格式名称'])

    def test_joint_filter_joins_by_entity_and_keeps_other_scopes_separate(self):
        from analysis_extension.business_tables import build_tables
        data = {'purpose': 'QUERY', 'targets': [], 'resultSets': [], 'valueFilter': {
            'basis': 'BUDGET_NATIVE_FILTER', 'executionStatus': 'EXECUTED', 'batches': [
                {'status': 'SUCCEEDED', 'rowCount': 2, 'logicalTargetColumns': {'a': 'v1', 'b': 'v2'}}]}}
        for target, label, pairs in [('a', '用量', [('甲', '1'), ('乙', '2')]),
                                     ('b', '库存', [('乙', '4'), ('甲', '3')])]:
            data['targets'].append({'id': target, 'label': label})
            data['resultSets'].append({'resultSetId': target, 'targetId': target, 'grain': ['entity', 'item'],
                'context': {'baseContext': {'snapshotId': 's'}, 'target': {'formBindingKey': 't/f'}, 'period': '2026N0001'},
                'resolvedTerms': [{'dimensionCode': 'item', 'objectCode': target}],
                'rows': [{'factId': target + entity, 'dimensions': {'entity': entity, 'item': target},
                          'dimensionNames': {'entity': entity, 'item': label}, 'measureCode': 'quantity',
                          'measureTitle': '数值', 'unit': '吨', 'value': value, 'valueState': 'PRESENT'}
                         for entity, value in pairs]})
        def view(d):
            return aligned_filter_view(d, build_tables({'data': d}))
        self.assertEqual(view(data)[2], [['甲', '1', '3'], ['乙', '2', '4']])
        for key, value in [('target', {'formBindingKey': 'other'}), ('baseContext', {'snapshotId': 'other'}),
                           ('effectiveDimensions', {'unit': 'other'})]:
            changed = deepcopy(data)
            changed['resultSets'][1]['context'][key] = value
            self.assertIsNone(view(changed))
        changed = deepcopy(data)
        changed['resultSets'][1]['rows'][0]['dimensions']['entity'] = '甲'
        self.assertIsNone(view(changed))
        changed = deepcopy(data)
        changed['resultSets'][0]['resolvedTerms'] = []
        self.assertIsNone(view(changed))
        changed = deepcopy(data)
        for index, result in enumerate(changed['resultSets']):
            for row in result['rows']:
                row['modelCode'] = 'model-' + str(index)
        self.assertIsNone(view(changed))
        from unittest.mock import Mock
        tables = build_tables({'data': data})
        projection = view(data)
        headings = '|序号|' + '|'.join(projection[1]) + '|说明|\n'
        copied = headings + '|---|---|---|---|---|\n|1|甲|1|3|均满足|\n|2|乙|2|4|均满足|\n'
        save = Mock(return_value='./detail')
        self.assertIn('[查看说明中引用的原表](./detail)', link_copied_source_table(copied, tables[0], save, aligned=projection))
        for other in (copied.replace('|2|4|', '|2|5|'), copied.replace('均满足', '特别关注', 1)):
            self.assertEqual(link_copied_source_table(other, tables[0], save, aligned=projection), other)
        self.assertEqual(save.call_count, 1)

    def test_grid_cells_of_one_native_measure_are_rows_with_actual_source_values(self):
        fs = []
        for ref, caption, value in [('A1', '收入', '300.00'), ('A2', '费用', None), ('A3', '利润', '0')]:
            item = fact(ref, '2026N0001', ref, caption, value)
            item.update(cellAddress=ref, observationId='one-grid', sourceMeasureCode='AMT')
            fs.append(item)
        (table, _), = appendix_tables({'facts': fs})
        headers, rows = visible_table(table)
        self.assertEqual(len(table.columns), 1)
        self.assertIn('来源标题', headers)
        self.assertEqual([(r[0], r[-1]) for r in rows], [('收入', '300.00'), ('费用', '空值'), ('利润', '0')])

    def test_stale_work_refs_repair_only_the_saved_request_field(self):
        artifact = {'plan': {'workPlan': []}, 'facts': [], 'derivedValues': [], 'relations': [],
                    'scope': {}, 'workRefs': [{'taskId': 'from-an-earlier-build', 'refs': []}]}
        with self.assertRaises(AnalysisError) as caught:
            completion_summary(artifact)
        self.assertEqual(caught.exception.details['path'], '/workRefs')
        self.assertFalse(caught.exception.details['requiresBuild'])
        saved = {'analysisRef': 'a:2', 'workRefs': artifact['workRefs'], 'document': {'markdown': '正文'}}
        from json import loads
        import re
        edits = loads(re.search(r'repairs=(\[.*?\])', caught.exception.details['repairHint']).group(1))
        repaired = repair_request(saved, 'a:2', edits)
        self.assertEqual(repaired, {'analysisRef': 'a:2', 'document': {'markdown':'正文'}})
        del artifact['workRefs']
        completion_summary(artifact)

    def test_dimension_caption_prefers_current_chinese_and_retains_valid_source_abbreviations(self):
        catalog = [{'dimCode':'A','label':'申请部门','modelCode':'book'}]
        self.assertEqual(dimension_labels(catalog, models=['book'], context={'dimensions':[{'code':'A','name':'Department'}]})['A'], '申请部门')
        self.assertEqual(dimension_labels(catalog, models=['book'], context={'dimensions':[{'code':'A','name':'填报部门'}]})['A'], '填报部门')
        self.assertEqual(dimension_labels(context={'dimensionLabels':{'metric':'EVA'}})['metric'], 'EVA')
        self.assertEqual(field_name({}, 'COST_CENTER'), '维度名称待确认')
        self.assertEqual(field_name({'metric':'EVA'}, 'metric'), 'EVA')

    def test_every_selected_cell_survives_pivot_with_roles_duplicates_and_empty_values(self):
        fs = [fact(f'f{n}', p, '00000000', m, v) for n, (p, m, v) in enumerate([
            ('2026J0001', '收入', '8'), ('2026J0001', '成本', None),
            ('2026J0002', '收入', '0'), ('2026J0002', '成本', '-2.20')])]
        fs += [dict(fs[0], factId='duplicate'), dict(fs[0], factId='reference', inputRole='REFERENCE')]
        art = {'facts': fs, 'scope': {'sourceScopes': [{'captureRef': 'capture-A', 'batchId': 'batch-A',
                                                       'dimensionLabels': {'axis': '销售渠道'}}]}}
        original = deepcopy(art)
        tables = appendix_tables(art)
        tables_and_roles = [(len(t.rows), roles) for t, roles in tables]
        self.assertEqual(tables_and_roles, [(3, ('PRIMARY',)), (1, ('REFERENCE',))])
        cells = [f for table, _ in tables for row in table.rows for f in row['cells'].values()]
        self.assertCountEqual([(f['factId'], f['value'], f['dimensions']) for f in fs],
                              [(f['factId'], f['value'], f['dimensions']) for f in cells])
        counts = {}
        html = appendix(art, counts)
        self.assertEqual(counts, dict(tables=2, rows=4, records=6, numeric=5))
        for text in ['<details class="source-data"><summary>', '销售渠道：合计', '2026年第2季度', '空值', '>0<', 'business-negative']:
            self.assertIn(text, html)
        self.assertEqual(art, original)

    def test_member_names_are_bound_to_role_member_source_and_form(self):
        f = fact('one', '2026N0001', '01', '成本', '2')
        f.update(dimensions={'A': '01', 'B': '01'}, dimensionNames={'A': '采购处', 'B': '财务处'})
        missing = dict(f, factId='two', dimensionNames={}, grain=['B'], objectLabel='01')
        other = dict(missing, factId='three', bindingRef='form-B')
        foreign = dict(missing, factId='four', sourceContextId='snapshot-B')
        projected = display_facts([f, missing, other, foreign])
        self.assertEqual(projected[1]['objectLabel'], '财务处')
        self.assertEqual(projected[1]['dimensionNames'], f['dimensionNames'])
        self.assertTrue(all(member_name(row, 'B') == '名称待确认' for row in projected[2:]))
        conflicting = dict(f, factId='five', dimensionNames={'B':'其他处'})
        self.assertNotIn('B', display_facts([f, conflicting, missing])[-1]['dimensionNames'])
        self.assertEqual(missing['dimensionNames'], {})

    def test_formula_names_keep_differing_periods_and_parentheses(self):
        a, b = [fact(ref, p, '1', '四、利润总额（亏损总额以“-”号填列）', '10') for ref, p in
                [('a', '2026J0001'), ('b', '2026J0002')]]
        labels = component_labels([a, b], {})
        self.assertEqual(labels, {'a': '利润总额 / 2026年第1季度', 'b': '利润总额 / 2026年第2季度'})
        names = {'A1': '甲', 'A2': '乙', 'A3': '丙'}
        self.assertEqual(formula_text(Expression('A1-(A2-A3)').tree, names), '甲 − (乙 − 丙)')
        self.assertEqual(formula_text(Expression('A1/(A2/A3)').tree, names), '甲 ÷ (乙 ÷ 丙)')
        self.assertEqual(component_labels([a], {'a': '已确认简称'})['a'], '已确认简称')

    def test_attention_and_signed_numeric_style_cover_markdown_and_bound_tables(self):
        html = parser().render('| 编号 | 金额（元） | 比率 | 说明 |\n|---|---|---|---|\n|-10|-3.20|-2.5%|关注：连续下降|\n|-5|-0.00|4%|正常|')
        self.assertEqual(html.count('business-negative'), 2)
        self.assertEqual(html.count('business-attention'), 1)
        table = {'kind':'TABLE', 'columns':[{'name':'code','type':'STRING'}, {'name':'n','type':'DECIMAL_STRING','unit':'元'}],
                 'rows':[{'values':{'code':'-01','n':'-2.00'}}]}
        self.assertEqual(dataset_html('table','t',table).count('business-negative'), 1)

    def test_public_grouping_preserves_scale_codes_periods_and_source_values(self):
        from analysis_extension.formatting import group_written_amounts
        from analysis_extension.business_tables import cell_text
        examples = [('DECIMAL_STRING', '1000.0000', '1,000.0000'), ('DECIMAL_STRING','1.23450e4','12,345.0'), ('INTEGER', -1000, '-1,000'),
                    ('DECIMAL_STRING', '0.00', '0.00'), ('STRING', '00012345', '00012345')]
        for kind, value, expected in examples:
            cell = {'valueType':kind, 'valueState':'PRESENT', 'value':value}
            self.assertEqual(cell_text(cell, grouped=True), expected)
            self.assertEqual(cell_text(cell), str(value))
        self.assertEqual(cell_text({'valueState':'NULL'}, grouped=True), '空值')
        md = '| 编号 | 收入（元） | 年度 |\n|---|---|---|\n|001234|-12345.0600|2026|'
        html = parser().render(md)
        for expected in ('001234', '-12,345.0600', '<td>2026</td>'):
            self.assertIn(expected, html)
        self.assertEqual(group_written_amounts('2026年 A12000 项收入12345.0600元'),
                         '2026年 A12000 项收入12,345.0600元')

    def test_partial_delivery_groups_financial_values_and_uses_verified_data_period(self):
        from analysis_extension.fact_closeout import summarize
        a = {'factId':'a', 'value':'12345.0600', 'valueState':'PRESENT', 'dataPeriod':'2026J0003',
             'executionPeriod':'2026N0001', 'effectiveDimensions':{'DATATIME':'2026N0001'}}
        b = {**a, 'factId':'b','valueType':'STRING','value':'00012345','dataPeriod':None}
        answer = summarize({'facts':[a,b]},'TEST')['answer']
        self.assertIn('数据时期',answer)
        self.assertIn('2026年第3季度',answer)
        self.assertNotIn('2026年 |',answer)
        self.assertIn('12,345.0600',answer)
        self.assertIn('00012345',answer)
        self.assertIn('未返回',answer)

    def test_same_name_distinct_members_are_kept_without_displaying_the_codes(self):
        facts = [fact(f'f{i}', '2026N0001', str(i), '金额', '3') for i in (1, 2)]
        for f in facts:
            f['dimensionNames']['axis'] = '本部'
        table = BusinessTable('T', {}, '', '', {'axis':'执行部门'}, False, facts)
        _, rows = visible_table(table)
        self.assertEqual([r[0] for r in rows], ['本部（同名成员1）', '本部（同名成员2）'])

    def test_bound_table_renders_observed_periods_and_declared_money_without_changing_python_values(self):
        columns = [{'name': name, 'label': label, 'type': kind} for name, label, kind in (
            ('peak', '收入最高季度', 'STRING'), ('item', '产品', 'STRING'), ('id', '时期编码', 'STRING'),
            ('other', '对比时期', 'STRING'), ('margin', '毛利（元）', 'DECIMAL_STRING'))]
        row = dict(peak='2031J0002', item='2031J0002', id='2031J0002', other='2032J0001', margin='-1.00')
        source = {'kind':'TABLE', 'columns':columns, 'rows':[{'values':row}]}
        before = deepcopy(source)
        html = dataset_html('table', 't', source, {'2031J0002':'2031年第2季度'})
        self.assertIn('<td>2031年第2季度</td><td>2031J0002</td><td>2031J0002</td><td>2032J0001</td>', html)
        self.assertIn('<td class="business-negative">-1.00</td>', html)
        self.assertEqual(source, before)
        columns = [{'name':name,'label':label,'type':'INTEGER'} for name,label in
                   [('year','年度'), ('id','站点编码'), ('n','年度销量')]]
        html = dataset_html('table','t',{'kind':'TABLE','columns':columns,
                                        'rows':[{'values':{'year':2026,'id':12000,'n':12000}}]})
        self.assertIn('<td>2026</td><td>12000</td><td>12,000</td>',html)

    def test_only_full_source_copy_with_original_row_refs_and_coordinates_can_be_folded(self):
        from unittest.mock import Mock
        fs = [fact(str(i), '2026N0001', label, '收入', value) for i,label,value in
              [(1,'甲','1234.00'), (2,'乙','0.00')]]
        table = BusinessTable('T', {}, '', '', {'axis':'部门'}, False, fs)
        header, _ = visible_table(table)
        text = '| 行号 | '+ ' | '.join(header) +' |\n|---|---|---|\n|R1|甲|1,234.00|\n|R2|乙|0.00|\n'
        save = Mock(return_value='./original.html')
        self.assertEqual(link_copied_source_table('说明\n\n'+text+'\n结论', table, save),
                         '说明\n\n[查看说明中引用的原表](./original.html)\n\n结论')
        save.assert_called_once_with(text)
        for other in (text.replace('R1','R9'), text.replace('甲','丙'), text.replace('部门','渠道'),
                      text.replace('1,234.00','1,234.01'),text.replace('|R2|乙|0.00|\n','')):
            self.assertEqual(link_copied_source_table(other, table, save),other)
        self.assertEqual(save.call_count,1)
        self.assertIn('查看说明中引用的原表', link_copied_source_table(text.replace('1,234.00','1234.00'), table, save))

    def test_source_title_indentation_does_not_create_a_second_metric(self):
        dims = {'axis':'部门'}
        fs = [fact(str(i), '2031N0001', label, '额', value) for i, label, value in
              [(1,'甲部门','3'),(2,'乙部门','4')]]
        for f, title in zip(fs, ('甲部门','    乙部门')):
            f.pop('measureTitle', None)
            f.update(title=title, measureCode='AMT')
        table = BusinessTable('T', {}, '', '', dims, False, fs)
        headers, rows = visible_table(table)
        self.assertEqual(headers, ['部门','查询值（元）'])
        self.assertEqual(rows, [['甲部门','3'],['乙部门','4']])

    def test_large_comparison_preserves_both_complete_tables_in_order_for_the_writer(self):
        from analysis_extension.direct_query_delivery import _combine
        from analysis_extension.display_tables import query_markdown
        materials = []
        for title in ('表甲', '表乙'):
            fs = [fact(str(i), '2031N0001', f'单位{i}', '金额', str(1000-i)) for i in range(400)]
            table = BusinessTable('T', {'formName':title}, '', '', {'axis':'部门'}, False, fs)
            materials.append(query_markdown(table))
        parts = [{'rows':400, 'answer':'预览', 'material':m, 'emptyStatus':None} for m in materials]
        combined = _combine(parts)
        self.assertEqual(combined['material'], '\n\n'.join(materials))
        self.assertEqual(combined['rows'], 800)

    def test_empty_adopted_source_keeps_identity_and_conditions(self):
        art = {'facts':[], 'scope':{'sourceScopes':[{'resultSetId':'s','emptyContext':{'formName':'业务表甲','period':'2026N0001',
                'effectiveDimensions':{'A':'01'},'dimensionLabels':{'A':'财务部门'},'dimensionNames':{'A':'甲部门'}}, 'label':'支出'}]}}
        counts = {}
        html = appendix(art, counts)
        self.assertEqual(counts['rows'], 0)
        self.assertIn('财务部门：甲部门', html)
        self.assertIn('业务表甲',html)
        self.assertIn('没有数据', html)
