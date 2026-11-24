"""Business evidence must be readable without decoding the host storage format."""
from copy import deepcopy
import unittest
from markdown_it import MarkdownIt
from analysis_extension.review_request import make_review_request


def parsed_tables(markdown):
    tables, row, current = [], None, None
    for token in MarkdownIt('commonmark').enable('table').parse(markdown):
        if token.type == 'table_open': current = []; tables.append(current)
        elif token.type == 'tr_open': row = []; current.append(row)
        elif token.type == 'inline' and row is not None: row.append(token.content)
        elif token.type == 'tr_close': row = None
        elif token.type == 'table_close': current = None
    return tables


class ReviewBusinessMarkdownTests(unittest.TestCase):
    def test_report_names_remain_available_without_python_or_names_in_the_report(self):
        from analysis_extension.review_business_projection import report_source_tables
        from analysis_extension.computation_review import business_material
        pairs = report_source_tables([{'taskName': '审批后版本', 'formName': '经营情况'},
                                     {'taskName': '审批后版本', 'formName': '经营情况'},
                                     {'taskName': '工作稿', 'formName': '经营情况'}, {'value': 2}])
        self.assertEqual(len(pairs), 2)
        source = {'reportSourceTables': pairs, 'facts': [], 'derived': [], 'documentText': '任务为预算'}
        markdown = make_review_request(business_material(source)).markdown
        rows = [row for table in parsed_tables(markdown) for row in table]
        self.assertEqual(rows.count(['审批后版本', '经营情况']), 1)
        self.assertEqual(rows.count(['工作稿', '经营情况']), 1)

    def test_only_measured_blank_columns_are_flagged_without_treating_zero_as_missing(self):
        from analysis_extension.review_business_markdown import render_business_markdown
        text = '单位\t余额\t增幅\n\t0\t—\n\t12.5\t—\n合计\t12.5\t100%'
        for basis in ('STATIC_SOURCE', 'HOST_RENDERED'):
            markdown = render_business_markdown({'documentText': text, 'documentTextBasis': basis}, {})
            self.assertEqual('实测页面中的空列' in markdown, basis == 'HOST_RENDERED')
            if basis == 'HOST_RENDERED':
                self.assertIn('单位（2/3 行', markdown)
                self.assertIn('增幅（2/3 行', markdown)
                self.assertNotIn('余额（', markdown)
                self.assertIn('来源确实为空时按空值接受', markdown)

    def test_only_identical_output_table_bodies_share_a_display_location(self):
        first = {'derivedId': 'v1', 'outputKind': 'TABLE', 'columns': [{'name': 'x', 'label': '数量', 'unit': '件'}],
                 'rows': [{'rowRef': 'row-a', 'values': {'x': '11'}, 'valueStates': {'x': 'PRESENT'}}]}
        second = deepcopy(first)
        second.update(derivedId='v2', label='另一项计算')
        second['rows'][0]['rowRef'] = 'row-b'
        material = {'derived': [first, second]}
        request = make_review_request(material)
        self.assertIn('与结果1相同', request.markdown)
        self.assertEqual(request.wire_references['结果1'], 'v1')
        self.assertEqual(request.wire_references['结果2'], 'v2')
        second['columns'][0]['unit'] = '千件'
        self.assertNotIn('与结果1相同', make_review_request(material).markdown)

    def test_loading_period_is_labeled_in_the_execution_scope_only(self):
        material = {'evidence': {'sources': [{'executionScopes': [
            {'formName': '费用表', 'period': '2026N0001'}]}]},
            'pythonRuns': [{'code': 'result = inputs', 'inputRecords': [
                {'executionPeriod': '2026N0001', 'period': '2025J0004', 'value': '18'}]}]}
        original = deepcopy(material)
        body = make_review_request(material).markdown
        self.assertIn('### 数据加载条件', body)
        rows = [row for table in parsed_tables(body) for row in table]
        self.assertIn(['数据加载时期', '2026N0001（2026年）'], rows)
        self.assertIn(['时期', '2025J0004（2025年第4季度）'], rows)
        self.assertEqual(material, original)

    def test_exact_header_join_is_explained_once_but_different_script_labels_remain_visible(self):
        facts = [dict(factId=f'f{i}', headers=['渠道', str(i)], value=str(i)) for i in range(4)]
        inputs = [dict(ref=row['factId'], headers=row['headers'], value=row['value'],
                       label='_'.join(row['headers'])) for row in facts]
        material = {'facts': facts, 'pythonRuns': [{'code': 'result = inputs', 'inputRecords': inputs}]}
        body = make_review_request(material).markdown
        self.assertIn('按顺序用 `_` 连接', body)
        self.assertNotIn('脚本输入的附加业务数据', body)
        inputs[0]['label'] = '渠道中特殊的指标名'
        body = make_review_request(material).markdown
        self.assertIn('渠道中特殊的指标名', body)
        self.assertNotIn('按顺序用 `_` 连接', body)

    def test_second_script_reuses_an_exact_block_without_losing_extra_inputs(self):
        rows = [{'ref':'one', 'value':'9', 'label':'甲'}, {'ref':'two', 'value':'9', 'label':'乙'}]
        more = [{'ref':'before', 'value':'0', 'label':'前置结果'}, *deepcopy(rows),
                {'ref':'after', 'value':None, 'label':'后置空结果'}]
        runs = [{'runRef':'r1','code':'result = inputs','inputRecords':rows},
                {'runRef':'r2','code':'result = list(inputs)','inputRecords':more}]
        body = make_review_request({'pythonRuns':runs}).markdown
        self.assertIn('输入第 2 至 3 行与第 1 段', body)
        self.assertIn('原输入行号为 1、4', body)
        for text in ('前置结果', '后置空结果', 'result = inputs', 'result = list(inputs)'):
            self.assertIn(text, body)
        for field, value in [('value', 9), ('label', '另一科目'), ('ref','another')]:
            with self.subTest(field=field):
                changed = deepcopy(runs)
                changed[1]['inputRecords'][1][field] = value
                self.assertNotIn('输入第 2 至 3 行与第 1 段', make_review_request({'pythonRuns':changed}).markdown)

    def test_reuse_keeps_metadata_newly_read_by_a_later_script(self):
        rows = [{'ref':'one','value':'7','rowIndex':27}]
        runs = [{'runRef':'r1','code':'result = inputs','inputRecords':rows},
                {'runRef':'r2','code':"result = inputs[0]['rowIndex']",'inputRecords':rows}]
        body = make_review_request({'pythonRuns':runs}).markdown
        self.assertNotIn('输入第 1 至 1 行与第 1 段', body)
        self.assertIn('27', body)

    def test_role_material_choices_remain_model_plan_without_replacing_user_request(self):
        material = {'userRequest': '解释异常对象的形成并与全体比较',
                    'analysisRequirement': {'origin': 'MODEL_PLAN', 'materials': [
                        {'role': 'PRIMARY', 'kinds': ['VALUES']},
                        {'role': 'REFERENCE', 'kinds': ['VALUES']}],
                        'explanation': '内部状态不应混入业务正文'},
                    'agentPlan': {'declaredGoals': ['只列金额占比']}}
        original = deepcopy(material)
        request = make_review_request(material)
        self.assertIn(material['userRequest'], request.markdown)
        table = next(t for t in parsed_tables(request.markdown) if t[0] == ['用途', '已选择材料'])
        self.assertEqual(table[1:], [['主体', '数值'], ['对照', '数值']])
        self.assertNotIn('只列金额占比', request.markdown)
        self.assertNotIn('内部状态', request.markdown)
        self.assertEqual(request.material, original)
        self.assertEqual(material, original)
        self.assertLess(len(request.markdown), 900)

    def test_latest_role_choice_overrides_older_assessment_without_forcing_reference_formulas(self):
        request = make_review_request({'userRequest': '比较两组',
            'materials': [{'role': 'PRIMARY', 'kinds': ['VALUES', 'FORMULAS']},
                          {'role': 'REFERENCE', 'kinds': ['VALUES']}],
            'analysisRequirement': {'materials': [{'role': 'PRIMARY', 'kinds': ['VALUES']}]}})
        table = next(t for t in parsed_tables(request.markdown) if t[0] == ['用途', '已选择材料'])
        self.assertEqual(table[1:], [['主体', '数值、公式构成'], ['对照', '数值']])

    def test_query_without_material_declaration_does_not_invent_analysis_obligations(self):
        request = make_review_request({'userRequest': '只列金额并排序', 'facts': []})
        self.assertFalse(any(t[0] == ['用途', '已选择材料'] for t in parsed_tables(request.markdown)))

    def test_same_actual_values_share_source_despite_richer_host_membership_receipts(self):
        facts=[]; records=[]
        for i in range(4):
            group='first' if i<2 else 'second'
            membership={'inputRef':group,'inputRole':'PRIMARY','grain':['DATATIME','MD_QD']}
            fact={**self.fact('f'+str(i),'2026J0001','1',str(i)),
                  'valueState':'PRESENT','inputMemberships':[membership]}
            facts.append(fact)
            record={k:v for k,v in fact.items() if k!='factId'}
            record.update(ref=fact['factId'],sourceValueState='PRESENT',label='渠道收入',
                          inputMemberships=[{**membership,'memberRef':'member'+str(i),'rowIdentity':{'row':i}}])
            records.append(record)
        material={'facts':facts,'pythonRuns':[{'code':'result = inputs','inputRecords':records}]}
        request=make_review_request(material)
        self.assertEqual(request.markdown.count('### 业务数据表'),1)
        self.assertIn('上方全部 4 条来源数据',request.markdown)
        self.assertIn('第1—2行',request.markdown)
        self.assertIn('第3—4行',request.markdown)
        self.assertNotIn('memberRef',request.markdown)
        self.assertEqual(request.material,material)
        records[0]['value']='99'
        changed=make_review_request(material).markdown
        self.assertIn('99',changed)
        self.assertNotIn('上方全部 4 条来源数据',changed)

    def test_reordered_inputs_preserve_order_and_repeated_refs_with_different_extra_fields(self):
        f1=self.fact('f1','2026J0001','1','5'); f2=self.fact('f2','2026J0001','2','6')
        rows=[]
        for fact,label in ((f2,'second'),(f1,'first'),(f1,'changed')):
            rows.append({**{k:v for k,v in fact.items() if k!='factId'},'ref':fact['factId'],'label':label})
        text=make_review_request({'facts':[f1,f2],'pythonRuns':[{'code':'result = inputs','inputRecords':rows}]}).markdown
        order=next(table for table in parsed_tables(text) if table[0]==['输入次序','来源位置'])
        self.assertEqual(order[1:],[['1','表1第2行'],['2','表1第1行'],['3','表1第1行']])
        for label in ('second','first','changed'): self.assertIn(label,text)

    def fact(self, ref, period, channel, value):
        return {'factId':ref, 'recordTable':'销售预算表', 'sourceContextId':'context',
                'dimensions':{'DATATIME':period,'MD_QD':channel,'MD_QY':'A'},
                'dimensionNames':{'MD_QD':'渠道'+channel,'MD_QY':'华东地区'},
                'headers':['销售收入'], 'value':value, 'unit':'元', 'period':period}

    def test_every_business_row_has_chinese_dimensions_value_and_independent_columns(self):
        facts=[self.fact('f1','2026J0001','1','1.005'),self.fact('f2','2026J0002','2',None),
               self.fact('f3','2026J0002','2',0)]
        material={'facts':facts,'userRequest':'查看各季度渠道收入'}
        before=deepcopy(material)
        request=make_review_request(material)
        tables=parsed_tables(request.markdown)
        rows=[table for table in tables if table[0][0]=='行号'][0]
        self.assertEqual(len(rows),4)
        self.assertTrue(all(len(row)==len(rows[0]) for row in rows))
        self.assertIn('2026年第2季度',rows[2])
        self.assertIn('渠道2',rows[2])
        self.assertIn('空值',rows[2])
        self.assertIn('0',rows[3])
        self.assertNotIn('JSON_CELL_TABLE_V1',request.markdown)
        self.assertNotIn('/recordRow',request.markdown)
        self.assertNotIn('逐行相同的列',request.markdown)
        self.assertEqual(material,before)

    def test_dimension_headers_follow_this_sources_metadata(self):
        rows = [self.fact('f1','2026J0001','1','1'), self.fact('f2','2026J0001','2','2')]
        def headers(material):
            return next(t[0] for t in parsed_tables(make_review_request(material).markdown) if t[0][0]=='行号')
        self.assertIn('MD_QD', headers({'facts': rows}))
        self.assertIn('结算方式', headers({'facts': rows, 'sourceScopes': [{'dimCode': 'MD_QD', 'label': '结算方式'}]}))

    def test_identical_names_and_values_keep_distinct_dimension_codes(self):
        facts=[self.fact('f1','2026J0002','1','5'),self.fact('f2','2026J0002','2','5')]
        for fact in facts: fact['dimensionNames']['MD_QD']='同名渠道'
        request=make_review_request({'facts':facts})
        rows=[table for table in parsed_tables(request.markdown) if table[0][0]=='行号'][0]
        self.assertEqual(len(rows),3)
        self.assertIn('同名渠道（1）',rows[1])
        self.assertIn('同名渠道（2）',rows[2])

    def test_empty_missing_and_typed_fields_are_distinct_and_pipes_do_not_break_table(self):
        material={'facts':[], 'pythonRuns':[{'code':'result = inputs','inputRecords':[
                       {'ref':'a','custom':{'x':'1','note':'x|y'}},
                       {'ref':'b','custom':{'x':1,'note':None}},
                       {'ref':'c','custom':{'x':'','note':'z'}}]}]}
        request=make_review_request(material)
        rows=next(table for table in parsed_tables(request.markdown) if 'custom.x' in table[0])
        self.assertTrue(all(len(row)==len(rows[0]) for row in rows))
        self.assertIn('"1"',rows[1]); self.assertIn('1',rows[2]); self.assertIn('""',rows[3])
        self.assertIn('null（来源空值）',rows[2])
        self.assertIn('"x|y"',rows[1])

    def test_python_source_and_multiline_business_table_remain_independent_blocks(self):
        code='result = [{"value": x["value"]} for x in inputs]\n# ```'
        business='| 科目 | 金额 |\n| --- | --- |\n| 收入 | 12 |'
        material={'reviewStage':'CALCULATION','facts':[],
                  'pythonRuns':[{'runRef':'r','code':code,'inputRecords':[{'value':'1'},{'value':None}]}],
                  'evidence':{'sources':[{'businessTables':business,'executionScopes':[{'period':'2026J0002'}]}]}}
        request=make_review_request(material)
        self.assertIn('````python\n'+code+'\n````',request.markdown)
        self.assertIn(business,request.markdown)
        self.assertIn([['科目','金额'],['收入','12']],parsed_tables(request.markdown))
        self.assertIn('2026年第2季度',request.markdown)

    def test_host_membership_and_empty_processing_states_do_not_become_review_sections(self):
        material={'userRequest':'分析渠道销售','scope':{'label':'销售预算',
                     'memberBindings':[{'memberRef':'@e408','sourceObjectId':'GT00'}],
                     'members':['@e408'],'observedMembers':['@e408'],'pendingMembers':[]},
                  'facts':[self.fact('f1','2026J0002','1','5')]}
        request=make_review_request(material)
        for key in ('memberBindings','observedMembers','pendingMembers','sourceObjectId'):
            self.assertNotIn(key,request.markdown)
        self.assertEqual(request.material,material)
        self.assertEqual([line for line in request.markdown.splitlines() if line.startswith('## ')],
                         ['## 一、用户要求与目标','## 二、查询结果与实际输入','## 三、计算过程','## 四、执行结果与交付内容'])

    def test_query_calculation_dependency_keeps_actual_inputs_code_and_diagnostic_data(self):
        code='result = sum(int(x["value"]) for x in inputs)'
        material={'userRequest':'分析渠道收入','resultReviewPhase':'DEPENDENCY','evidence':{'pythonRuns':[
            {'runRef':'r1','code':code,'inputs':[{'ref':'f1','value':'17','headers':['收入']}],
             'outputs':[{'derivedId':'d1','outputKind':'DIAGNOSTIC','data':{'sum':'17','channel_diff':['0','1.005']}}]}]}}
        request=make_review_request(material,query=True)
        for text in (code,'收入','17','channel_diff','1.005','本次仅审核计算'):
            self.assertIn(text,request.markdown)
        self.assertEqual(request.material,material)

    def test_final_query_output_and_prior_review_are_shown_without_code(self):
        material={'userRequest':'比较收入','resultReviewPhase':'FINAL', 'candidateAnswer':'收入合计 88 元',
            'evidence':{'finalResults':[{'outputKind':'TABLE','columns':[{'name':'amount','label':'金额'}],
                'rows':[{'values':{'amount':'88'}}]}]},'calculationReview':{'status':'PASS','summary':'计算关系一致'}}
        text=make_review_request(material,query=True).markdown
        self.assertIn('88',text); self.assertIn('计算关系一致',text)
        self.assertIn('收入合计 88 元',text); self.assertNotIn('```python',text)

    def test_shared_grid_is_decoded_to_complete_business_rows_and_delivery_flags(self):
        from analysis_extension.query_result_evidence import pack_source_rows
        source={'ref':'file1','complete':True,'context':{'formName':'销售表','period':'2026J0002'},
            'columns':['key','headers','dimensions','value','providedToRun'],
            'rows':[['a',['销售收入'],{'DATATIME':'2026J0002'},'1.005',True],
                    ['b',['销售收入'],{'DATATIME':'2026J0002'},None,False]]}
        pack_source_rows(source)
        material={'evidence':{'sharedSourceData':{'shared1':source},'pythonRuns':[
            {'code':'result = grid_rows("file1")','sourceData':[{'sourceDataRef':'shared1'}]}]}}
        request=make_review_request(material,query=True)
        for text in ('销售收入','2026年第2季度','1.005','空值','providedToRun','true','false'):
            self.assertIn(text,request.markdown)
        self.assertNotIn('SHARED_GRID_FIELDS_V1',request.markdown)
        self.assertNotIn('sourceDataRef',request.markdown)

    def test_metadata_only_query_keeps_names_fields_null_and_empty_values(self):
        material={'evidence':{'sources':[{'entityRows':[['e1',[['f1',[None]],['f2',[]]]]]}],
            'entityDefinitions':{'e1':{'entityKind':'MEMBER','key':'A','label':'华东单位'}},
            'fieldDefinitions':{'f1':{'label':'所属板块'},'f2':{'label':'别名'}}}}
        text=make_review_request(material,query=True).markdown
        for expected in ('华东单位','所属板块','null','别名'):
            self.assertIn(expected,text)
        self.assertNotIn('entityRows',text)

    def test_hidden_report_claims_and_actual_source_failure_cannot_disappear(self):
        material={'documentText':'收入 10 元', 'documentSourceTextProjection':{'changes':[
            {'renderedStartLine':0,'renderedEndLine':0,'sourceLines':['折叠明细：收入 99 元']}]},
            'evidence':{'sources':[{'source':{'businessQuery':{'complete':False,'failureSummary':'管理费用表查询失败'}}}]}}
        text=make_review_request(material,query=True).markdown
        self.assertIn('收入 99 元',text); self.assertIn('收入 10 元',text)
        self.assertIn('管理费用表查询失败',text)

    def test_native_computation_input_selection_is_retained(self):
        material={'facts':[self.fact('f1','2026J0002','1','5'),self.fact('f2','2026J0002','2','7')],
            'derived':[{'operator':'SUM','inputRefs':['f2'],'value':'7','unit':'元'}]}
        request=make_review_request(material)
        self.assertIn('运算方式',request.markdown)
        calculation=request.markdown.split('## 三、计算过程')[1].split('## 四、')[0]
        self.assertIn('SUM',calculation); self.assertIn('采用的数据引用',calculation)

    def test_overlap_between_primary_and_reference_remains_explicit(self):
        fact=self.fact('f1','2026J0002','1','5')
        fact['inputMemberships']=[{'inputRef':'g1','inputRole':'PRIMARY','grain':['MD_ORG']},
                                   {'inputRef':'g2','inputRole':'REFERENCE','grain':['MD_ORG']}]
        text=make_review_request({'facts':[fact]}).markdown
        self.assertIn('业务数据的群体用途',text)
        self.assertIn('主体',text); self.assertIn('对照',text)
        self.assertIn('各组分别计算',text)

    def test_actual_input_distinguishes_absent_value_null_and_empty_dimension_objects(self):
        material={'pythonRuns':[{'code':'result = inputs','inputRecords':[
            {'ref':'a','value':None,'dimensions':None,'scale':'1'},
            {'ref':'b','dimensions':{},'scale':1},
            {'ref':'c','value':'0','scale':None}]}]}
        request=make_review_request(material)
        self.assertIn('（字段不存在）',request.markdown)
        self.assertIn('null（来源空值）',request.markdown)
        self.assertIn('{}',request.markdown)
        self.assertIn('来源缩放因子',request.markdown)

    def test_execution_context_and_selected_delivery_are_distinct_without_hiding_old_values(self):
        material={'reviewTargets':['corrected'],'derived':[
            {'derivedId':'old','value':'20','outputKind':'SCALAR'},
            {'derivedId':'corrected','value':'10','outputKind':'SCALAR'}]}
        text=make_review_request(material).markdown
        self.assertIn('20',text); self.assertIn('10',text)
        first=text.split('### 结果 1')[1].split('### 结果 2')[0]
        self.assertIn('前置计算或探索上下文',first)
        self.assertIn('本次采用',text.split('### 结果 2')[1])

    def test_repeated_membership_details_are_business_roles_not_duplicate_host_records(self):
        rows=[{'ref':str(i),'value':str(i),'inputMemberships':[{'inputRef':'g', 'inputRole':'PRIMARY',
            'grain':['MD_ORG'],'memberRef':'member'+str(i),'rowIdentity':{'MD_ORG':str(i)}}],
            'dimensions':{'MD_ORG':str(i)}} for i in range(4)]
        text=make_review_request({'pythonRuns':[{'code':'result = inputs','inputRecords':rows}]}).markdown
        self.assertIn('本表全部数据',text)
        self.assertNotIn('inputMemberships',text)
        self.assertNotIn('rowIdentity',text)

    def test_error_codes_are_execution_diagnostics_not_python_source_or_failed_queries(self):
        script = 'result = sum(row["value"] for row in inputs)'
        material = {'pythonRuns': [{'code': script, 'status': 'COMPLETED'}],
                    'toolFailures': [{'tool': 'AnalysisPython', 'state': 'error',
                        'error': {'code': 'PYTHON_EXECUTION_FAILED', 'message': '前次计算未执行完成'}}]}
        text = make_review_request(material).markdown
        tokens = MarkdownIt('commonmark').enable('table').parse(text)
        self.assertEqual([token.content.rstrip('\n') for token in tokens if token.type == 'fence'], [script])
        self.assertNotIn('尚未完成的来源查询', text)
        self.assertIn('PYTHON_EXECUTION_FAILED', text.split('## 四、执行结果与交付内容')[1])
        self.assertIn('结合后续执行结果判断', text)

    def test_literal_field_access_omits_unused_metric_map_but_keeps_actual_headers(self):
        fact={**self.fact('f1','2026J0002','1','5'),'measureCode':'metric1','sourceMeasureCode':'XSSR'}
        script='for row in inputs:\n    amount = row["value"]\n    title = row.get("headers")'
        request=make_review_request({'facts':[fact],'pythonRuns':[{'code':script}]})
        self.assertNotIn('指标字段说明',request.markdown)
        self.assertNotIn('metric1',request.markdown)
        self.assertNotIn('XSSR',request.markdown)
        self.assertIn('销售收入',request.markdown)
        used=make_review_request({'facts':[fact],'pythonRuns':[{'code':'for row in inputs:\n    amount = row["sourceMeasureCode"]'}]})
        self.assertIn('XSSR',used.markdown)

    def test_dynamic_record_access_keeps_codes_and_same_name_metrics_stay_distinct(self):
        facts=[{**self.fact('f1','2026J0002','1','5'),'measureCode':'m1'},
               {**self.fact('f2','2026J0002','1','5'),'measureCode':'m2'}]
        text=make_review_request({'facts':facts}).markdown
        self.assertIn('m1',text); self.assertIn('m2',text)
        for script in ('for row in inputs:\n    amount=row[field]', 'for row in inputs:\n    result=helper(row)', 'result=inputs'):
            text=make_review_request({'facts':facts[:1],'pythonRuns':[{'code':script}]}).markdown
            self.assertIn('m1',text)

    def test_scope_dedup_keeps_period_pairs_actual_filters_and_exclusions(self):
        scopes=[{'fixedDimValMap':{'DATATIME':'2026J0001','MD_QY':'A'},'returnedRowCount':1,'selectedRowCount':1,
                 'request':{'budTaskId':'internal-task-id'},'rangeSummary':{'alignedRowCount':1,'filteredOutRowCount':0}},
                {'fixedDimValMap':{'DATATIME':'2026J0002','MD_QY':'B'},'valueFilter':{'operator':'LT','value':'0'},
                 'excludedRows':['unit-X'],'rangeSummary':{'filteredOutRowCount':2}}]
        text=make_review_request({'sourceScopes':[scopes[0],scopes[0],scopes[1]]}).markdown
        self.assertNotIn('internal-task-id',text)
        self.assertNotIn('对齐后行数',text)
        self.assertEqual(text.count('2026年第1季度'),1)
        self.assertIn('2026年第2季度',text)
        self.assertIn('unit-X',text); self.assertIn('LT',text)
        self.assertIn('被条件排除行数',text)
