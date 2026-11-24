import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from analysis_business_tables_test import fact, receipt
from analysis_period_trends_test import artifact, observation
from analysis_extension.direct_query_delivery import deliver, source_answer
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.publication import repair_request
from analysis_extension.report_markdown import compile_source
from analysis_extension.review_revision import focus_revision, revision_basis


def query_turn(rows):
    result = receipt(rows, targets=[{'id': 't-physical', 'logicalTargetId': 't', 'label': '查询指标'}])
    result['data']['resultSets'][0]['targetId'] = 't-physical'
    cap = {'captureRef': 'c', 'originalToolName': 'execute_budget_query', 'request': {'question': '本轮问题'},
           'response': result}
    service = NS(owner=None, store=NS(get_capture=lambda *_: cap), delivery=None,
                 sources=NS(for_capture=lambda _: NS(raw_receipt=lambda response: response)))
    return NS(state={'request': '本轮问题', 'mode': 'DIRECT', 'deliveryMode': 'TEXT', 'queryCaptureRefs': ['c'],
                     'queryTargets': ['t']}, service=service), result


class QueryDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_reply_without_target_declaration_gets_a_table_without_early_finish(self):
        turn, raw = query_turn([fact('first', {'MD_ORG':'01'}, '256')])
        del turn.state['queryTargets']
        turn.state['turnKey'] = 'this-turn'
        saved = turn.service.store.get_capture(None, 'c')
        saved['turnKey'] = 'this-turn'
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertIsNone(source_answer(turn))
            final = source_answer(turn, at_reply_end=True)
            self.assertIn('查询条件', final['answer'])
            self.assertIn('查询结果', final['answer'])
            self.assertIn('256', final['answer'])
            turn.state['queryCaptureRefs'].append('independent')
            self.assertIsNone(source_answer(turn, at_reply_end=True))
            turn.state['queryCaptureRefs'] = ['c']
            saved['turnKey'] = 'another-turn'
            self.assertIsNone(source_answer(turn, at_reply_end=True))

    def test_empty_table_uses_verified_member_names_only_from_matching_snapshot_and_domain(self):
        from analysis_extension.business_tables import build_tables
        data = receipt([fact('one', {'MD_TEAM':'01'}, dimensionNames={'MD_TEAM':'甲组'})])
        first = data['data']['resultSets'][0]
        meta = {'sourceId':'a', 'snapshotId':'v1'}
        field = {'code':'MD_TEAM', 'name':'团队', 'value':'01', 'valueModelCode':'teams'}
        first['context'].update(baseContext=meta, period='2026N0001', dimensions=[field])
        empty = {**first, 'resultSetId':'empty', 'rows':[], 'context':{**first['context'],
                 'effectiveDimensions':{'MD_TEAM':'01','MD_UNIT':'002','DATATIME':'2026N0001'},
                 'dimensions':[field, {'code':'MD_UNIT','name':'单位','value':'002','valueLabel':'002 业务部'}]}}
        data['data']['resultSets'].append(empty)
        shown = build_tables(data)[1].scope()
        self.assertIn('数据加载条件（团队）：甲组', shown)
        self.assertIn('数据加载条件（单位）：业务部', shown)
        self.assertNotIn('实际时期', shown)
        self.assertNotIn('dimensionNames', empty['context'])
        for change in ({'baseContext':{**meta,'snapshotId':'v2'}}, {'dimensions':[{**field,'valueModelCode':'other'}]}):
            data['data']['resultSets'][1] = {**empty, 'context':{**empty['context'], **change}}
            self.assertNotIn('甲组', build_tables(data)[1].scope())

    def test_native_scope_empty_results_and_zero_have_distinct_delivery(self):
        for rows, expected in (([], '无数据'), ([fact('f', {}, value='0.00')], '0.00')):
            turn, raw = query_turn(rows)
            with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
                answer = source_answer(turn)
                for label in ('业务表', '利润表', '任务甲', expected):
                    self.assertIn(label, answer['answer'])
                self.assertNotIn('R1', answer['answer'])
                turn.state['queryTargets'].append('second')
                self.assertIsNone(source_answer(turn))
                turn.state.update(queryTargets=['t'], mode='ANALYSIS')
                self.assertIsNone(source_answer(turn))

    async def test_large_table_preview_sends_all_rows_to_the_actual_writer(self):
        rows = [fact(f'f{n}', {'MD_ORG': f'单位{n:04d}'}, measure='vol', title='数量', unit=None) for n in range(1300)]
        turn, raw = query_turn(rows)
        turn.service.delivery = Mock()
        turn.service.delivery.query_rows.return_value = './analysis/query-results/frozen.html'
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            answer = source_answer(turn)
        self.assertIn('单位1299', turn.service.delivery.query_rows.call_args.args[1])
        self.assertIn('单位0000', answer['material'])
        self.assertIn('单位1299', answer['material'])
        self.assertIn('1.20', answer['material'])
        self.assertEqual(answer['rows'], 1300)
        self.assertNotIn('单位1299', answer['answer'])
        self.assertIn('[查看完整查询结果]', answer['answer'])
        from analysis_extension.direct_query_delivery import _write_brief
        turn.service.review_budget = NS(call_timeout_seconds=20)
        agent = Mock()
        agent.model.generate_structured_output = AsyncMock(return_value=NS(content={'summary':'本次查询结果相同。'}, usage=None))
        with patch('analysis_extension.direct_query_delivery.record_timing'):
            await _write_brief(turn, agent, answer)
        prompt = agent.model.generate_structured_output.call_args.args[0][1].content[0].text
        for n in range(1300):
            self.assertIn(f'| 单位{n:04d} |', prompt)
        self.assertNotIn('查看完整查询结果', prompt)

    def test_full_brief_preserves_detail_subtotals_time_and_unit(self):
        rows = []
        for i in range(4):
            for region in ('00000000', 'detail'):
                dims = {'DATATIME': f'2026J000{i+1}', 'X': region}
                names = {'X': '合计' if region == '00000000' else '明细甲'}
                rows.append(fact('key'+str(len(rows)), dims, str(100+i), dimensionNames=names))
        turn, raw = query_turn(rows + [fact(f'd{i}', {'DATATIME':'2026J0001', 'X':f'x{i}'}) for i in range(150)])
        raw['data']['dimensionCatalog'] = [{'dimCode': 'X', 'label': '销售范围'}]
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            answer = source_answer(turn)
        for word in ('合计', '2026年第1季度', '2026年第4季度', '100', '103', '元'):
            self.assertIn(word, answer['material'])
        self.assertIn('销售范围', answer['material'])
        self.assertIn('原表 158 行，其中有值 158 行', answer['material'])
        self.assertIn('明细甲', answer['material'])
        self.assertIn('明细甲', answer['answer'])

    def test_checked_extrema_keep_ties_and_count_empty_rows_in_the_full_table(self):
        amounts = ['3.10', '1.05', '1.05', None]
        rows = [fact(str(i), {'UNIT': str(i)}, value, dimensionNames={'UNIT': name},
                     valueState='NULL' if value is None else 'PRESENT')
                for i, (name, value) in enumerate(zip(['甲部', '乙部', '丙部', '丁部'], amounts))]
        turn, raw = query_turn(rows)
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            brief = source_answer(turn)['material']
        self.assertIn('原表 4 行，其中有值 3 行', brief)
        self.assertIn('| 丁部 | 空值 |', brief)
        self.assertIn('最低 乙部 1.05（并列 2 行', brief)
        self.assertIn('最高 甲部 3.10', brief)

    async def test_empty_sources_end_at_table_without_a_summary_or_summary_timing(self):
        for rows, outcome, expected in (
                ([], 'NO_DATA', 'NO_DATA'),
                ([fact('one', {}, value=None, valueState='NO_DATA')], 'NO_DATA', 'NO_DATA'),
                ([fact('null', {}, value=None, valueState='NULL')], 'ROWS_RETURNED', 'EMPTY_VALUES')):
            with self.subTest(outcome=outcome, expected=expected):
                turn, raw = query_turn(rows)
                raw['data']['failureSummary'] = {'outcome': outcome}
                agent = Mock()
                agent.name = 'agent'
                agent.state.reply_id = 'reply'
                agent.model.generate_structured_output = AsyncMock()
                agent._get_reply_usage.return_value = None
                with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw), \
                     patch('analysis_extension.review_control.progress'), patch('analysis_extension.direct_query_delivery.record_timing') as timing:
                    result = source_answer(turn)
                    events = [event async for event in deliver(turn, agent, result)]
                self.assertEqual(result['emptyStatus'], expected)
                self.assertNotIn('material', result)
                self.assertEqual([e.delta for e in events if hasattr(e, 'delta')], [result['answer']])
                self.assertEqual(events[-1].content[0].text, result['answer'])
                self.assertEqual(turn.state['hostCloseout']['status'], 'DIRECT')
                self.assertEqual(turn.state['hostCloseout']['deliveryStatus'], expected)
                self.assertNotIn('summaryFinishedAt', turn.state['hostCloseout'])
                self.assertIn('利润表', result['answer'])
                self.assertIn('空值' if expected == 'EMPTY_VALUES' else '无数据', result['answer'])
                agent.model.generate_structured_output.assert_not_awaited()
                timing.assert_not_called()

    def test_empty_value_objectives_wait_for_all_receipts_and_preserve_any_present_value(self):
        from analysis_extension.material_plan import request_key
        turn, raw = query_turn([fact('f', {}, value=None, valueState='NULL')])
        first = turn.service.store.get_capture(None, 'c')
        first.update(turnKey='now')
        second = deepcopy(first)
        second.update(captureRef='other', request={**second['request'], 'target':'second'})
        second['response']['data']['resultSets'][0].update(context={'formName':'另一来源'}, rows=[])
        captures = {'c':first, 'other':second}
        turn.service.store.get_capture = lambda _, ref: captures[ref]
        turn.state.update(turnKey='now', queryTargets=[], objectives=[
            {'objectiveRef':ref, 'capability':'VALUES'} for ref in ('alpha','beta')],
            materialQueryObjectives={request_key(first['request']):'alpha', request_key(second['request']):'beta'})
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', side_effect=lambda c:c['response']):
            self.assertIsNone(source_answer(turn))
            turn.state['queryCaptureRefs'].append('other')
            result = source_answer(turn)
            self.assertEqual(result['emptyStatus'], 'EMPTY_VALUES')
            self.assertEqual(result['refs'], ['c','other'])
            self.assertIn('另一来源', result['answer'])
            second['response']['data']['resultSets'][0]['rows'] = [fact('value', {}, value='0')]
            self.assertIsNone(source_answer(turn))
            turn.state['queryTargets'] = ['t']
            turn.state['queryCaptureRefs'] = ['c']
            raw['data']['resultSets'].append({**second['response']['data']['resultSets'][0], 'resultSetId':'s2'})
            self.assertIsNone(source_answer(turn)['emptyStatus'])

    def test_sorting_and_failed_receipts_do_not_take_the_query_shortcut(self):
        turn, raw = query_turn([fact('f', {})])
        turn.state['queryIntent'] = {'order': '由小到大'}
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertIsNone(source_answer(turn))
            raw['data']['presentation'] = {'verified': True, 'valueOrder': 'ASC'}
            self.assertIsNotNone(source_answer(turn))
            raw['status'] = 'PARTIAL'
            self.assertIsNone(source_answer(turn))

    def test_incomplete_rows_cannot_finish_a_query_as_empty_or_as_a_preview(self):
        from analysis_extension.query_empty_result import empty_table_status
        turn, raw = query_turn([])
        result_set = raw['data']['resultSets'][0]
        result_set['rowsComplete'] = False
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertIsNone(source_answer(turn))
            result_set['rows'] = [fact('number', {}, value='0')]
            self.assertIsNone(source_answer(turn))
            del result_set['rowsComplete'], result_set['rows']
            self.assertIsNone(source_answer(turn))
        projected = {'purpose': 'QUERY', 'complete': True, 'failureSummary': {'outcome': 'NO_DATA'},
                     'resultSetCatalog': [{'rowCount': 0, 'rowsComplete': False}]}
        self.assertIsNone(empty_table_status(projected, []))

    def test_only_a_later_complete_receipt_resolves_a_blocked_query(self):
        turn, raw = query_turn([fact('ok', {}, '7')])
        current = turn.service.store.get_capture(None, 'c')
        current.update(turnKey='now', connectionFingerprint='budget-A', capturedAt='2026-11-22T10:01:00+00:00')
        prior = {**current, 'captureRef':'old', 'toolCallId':'failed', 'response':{'status':'FAILED'},
                 'capturedAt':'2026-11-22T10:00:00+00:00'}
        turn.state.update(turnKey='now', queryCaptureRefs=['old','c'], blocked={'failedToolCallIds':['failed']})
        turn.service.store.get_capture = lambda owner, ref: current if ref == 'c' else prior
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertEqual(source_answer(turn)['resolvedBlocker'], turn.state['blocked'])
            for field, wrong in (('turnKey','earlier'),('connectionFingerprint','other'),
                                 ('capturedAt','2026-11-22T10:02:00+00:00')):
                previous = prior[field]
                prior[field] = wrong
                self.assertIsNone(source_answer(turn))
                prior[field] = previous
            turn.state['blocked']['failedToolCallIds'].append('unrelated')
            self.assertIsNone(source_answer(turn))

    def test_reviewed_result_can_include_a_catalog_lookup_bound_by_that_query(self):
        turn, raw = query_turn([fact('answer', {}, '7')])
        turn.state.pop('queryTargets')
        turn.state.update(turnKey='now', queryCaptureRefs=['search', 'c'])
        current = turn.service.store.get_capture(None, 'c')
        current.update(turnKey='now', connectionFingerprint='connection')
        binding = {'contextId': 'ctx', 'querySessionId': 'session', 'executionId': 'found'}
        current['request']['scope'] = {'bindings': [binding]}
        search = {'originalToolName': 'search_entities', 'captureRef': 'search', 'turnKey': 'now',
                  'connectionFingerprint': 'connection', 'response': {'status': 'SUCCEEDED', 'executionId': 'found',
                  'querySessionId': 'session', 'context': {'contextId': 'ctx'}}}
        turn.service.store.get_capture = lambda _, key: {'c': current, 'search': search}[key]
        turn.service.sources.for_capture = lambda _: NS(is_data=lambda _: True, raw_receipt=lambda raw: raw)
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertIn('查询结果', source_answer(turn, at_reply_end=True)['answer'])
            for field in binding:
                before = binding[field]
                binding[field] = 'other'
                self.assertIsNone(source_answer(turn, at_reply_end=True))
                binding[field] = before
            search['connectionFingerprint'] = 'another'
            self.assertIsNone(source_answer(turn, at_reply_end=True))

    def test_two_independent_point_values_reach_both_the_reader_and_writer(self):
        turn, raw = query_turn([fact('one', {}, '15.50')])
        data = raw['data']
        turn.state['queryTargets'].append('second')
        data['targets'].append({'id':'t2-physical', 'logicalTargetId':'second', 'label':'另一指标'})
        data['resultSets'].append({**data['resultSets'][0], 'targetId':'t2-physical',
                                   'resultSetId':'s2', 'rows':[fact('two', {}, '12.00')]})
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            answer = source_answer(turn)
        for field in ('answer', 'material'):
            for expected in ('另一指标', '15.50', '12.00', '查询结果'):
                self.assertIn(expected, answer[field])
            self.assertNotIn('| R1 |', answer[field])

    async def test_tables_are_emitted_before_brief_and_same_answer_is_persisted(self):
        turn, raw = query_turn([fact('f', {}, value='0.00')])
        turn.service.review_budget = NS(call_timeout_seconds=20)
        agent = Mock(name='assistant')
        agent.name = 'assistant'
        agent.state.reply_id = 'reply'
        agent.model.generate_structured_output = AsyncMock(return_value=NS(content={'summary': '**AI 简述**　该指标为零。'}, usage=None))
        agent._get_reply_usage.return_value = None
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            result = source_answer(turn)
        self.assertIsNone(result['emptyStatus'])
        with patch('analysis_extension.review_control.progress'), patch('analysis_extension.direct_query_delivery.record_timing'):
            stream = deliver(turn, agent, result)
            await anext(stream)
            first = await anext(stream)
            self.assertEqual(first.delta, result['answer'])
            agent.model.generate_structured_output.assert_not_awaited()
            rest = [x async for x in stream]
        self.assertEqual(rest[-1].content[0].text, first.delta + rest[0].delta)
        self.assertEqual(turn.state['hostCloseout']['deliveryStatus'], 'SOURCE_WITH_AI')
        self.assertEqual(rest[0].delta, '\n\n该指标为零。')
        self.assertEqual(len(agent.model.generate_structured_output.call_args.args[0]), 2)


class MarkdownReportTests(unittest.TestCase):
    def test_reviewed_query_numbers_keep_markdown_identifiers_and_precision(self):
        from analysis_extension.report_markdown import format_query_markdown
        evidence = {'sources': [{'facts': [{'title':'发运量','valueType':'DECIMAL_STRING','unit':'吨'}]}]}
        draft = ('| 组织编码 | 年份 | 发运量 | 金额（元） |\n|---|---|---|---|\n'
                 '|001234|2026|12345.0600|**-12497000.00**|\n'
                 '|研发\\|一组|2026|0|1,525.00|\n\n'
                 '收入 12497000.00 元，区间为 1183.69～2236.02 元。\n\n`12345 元` [资料](./2026.html)\n\n'
                 '```text\n12345 元\n```')
        expected = (draft.replace('12345.0600', '12,345.0600').replace('12497000.00', '12,497,000.00')
                    .replace('1183.69～2236.02', '1,183.69～2,236.02'))
        self.assertEqual(format_query_markdown(draft, evidence), expected)
        self.assertEqual(format_query_markdown(expected, evidence), expected)

    def test_formatting_stream_preserves_other_blocks_and_original_block_ids(self):
        from agentscope.event import DataBlockStartEvent, TextBlockDeltaEvent
        from analysis_extension.middleware import _format_query_events
        other = DataBlockStartEvent(reply_id='response', block_id='attachment', media_type='application/json')
        stream = [TextBlockDeltaEvent(reply_id='response', block_id='text', delta='| 金额（元） |\n|---|\n|12'),
                  other, TextBlockDeltaEvent(reply_id='response', block_id='text', delta='345.60|')]
        _format_query_events(stream, {})
        self.assertEqual([item.block_id for item in stream], ['text','attachment'])
        self.assertIn('|12,345.60|', stream[0].delta)
        self.assertIs(stream[1], other)

    def test_authored_quantity_table_uses_declared_units_and_respects_identifier_columns(self):
        a = artifact(observation('2026J0001','12345.0600',unit='吨',measureTitle='发运量'))
        draft = '| 渠道编码 | 发运量 | 净重（吨） |\n|---|---|---|\n|001234|12345.0600|12345.0600|'
        body,_,_ = compile_source(a, {'title':'配送预算','markdown':draft})
        self.assertIn('<td>001234</td>',body)
        self.assertGreaterEqual(body.count('<td>12,345.0600</td>'),2)

    def test_report_owns_one_title_while_section_headings_keep_their_text(self):
        a = artifact(observation('2026J0001', '7'))
        for opening in ('# 2026 年销售预算：分渠道分析\n\n', '', '## 摘要\n\n'):
            body, _, _ = compile_source(a, {'title':'2026年分渠道销售预算分析', 'markdown':opening + '正文。\n\n## 渠道差异\n\n说明。'})
            self.assertEqual(body.count('<h1>'), 1)
            self.assertNotIn('2026 年销售预算：分渠道分析', body)
            self.assertIn('<h2>渠道差异</h2>', body)
            self.assertIn('正文。', body)
            if opening.startswith('##'):
                self.assertIn('<h2>摘要</h2>', body)
        body, _, _ = compile_source(a, {'title':'报告', 'markdown':'# 口径\n\n说明。\n\n# 发现\n\n结论。'})
        self.assertIn('<h2>口径</h2>', body)
        self.assertIn('<h2>发现</h2>', body)
        body, _, _ = compile_source(a, {'title':'销售预算：渠道分析', 'markdown':'## 销售预算 渠道分析\n\n结果。'})
        self.assertNotIn('<h2>销售预算 渠道分析</h2>', body)

    def test_change_review_retains_exact_facts_and_new_failures_force_full_review(self):
        old = '\n'.join(f'第{i}段的报告正文。' for i in range(60))
        common = {'documentTextBasis': 'VERIFIED_MARKDOWN', 'facts': [{'value': '1.005'}], 'failures': []}
        material = {**common, 'documentText': old.replace('第30段', '已修改的第30段')}
        previous = {'lastContentReview': {'reviewIdentity': 'same-policy', 'basis': revision_basis(common),
                                          'text': old, 'issues': [{'impact':'REPORT', 'message':'核对表述'}]}}
        with patch('analysis_extension.review_revision.progress', return_value=previous), \
             patch('analysis_extension.review_revision.review_identity', return_value='same-policy'):
            delta = focus_revision(None, material)
            self.assertEqual(delta['revisionFocus']['scope'], 'MARKDOWN_DELTA')
            self.assertEqual(delta['facts'], common['facts'])
            self.assertNotIn('第50段', delta['documentText'])
            changed = {**material, 'failures':['来源核对未完成']}
            self.assertIs(focus_revision(None, changed), changed)
            changed = {**material, 'facts':[{'value':'1.01'}]}
            self.assertIs(focus_revision(None, changed), changed)

    def test_missing_trend_gives_working_markdown_only_edit(self):
        a = artifact(*(observation(p, '2', sourceMeasureCode=m, headers=[m], factId=m+p)
                       for p in ('2026J0001', '2026J0002') for m in ('数量', '金额')))
        draft = {'analysisRef': 'a', 'document': {'title': '比较', 'markdown': '## 发现\n\n原有说明。'}}
        with self.assertRaises(AnalysisError) as e:
            compile_document(a, draft['document'])
        details = e.exception.details
        self.assertEqual(details['path'], '/document/markdown')
        choice = next(c for c in details['trendChoices'] if c['指标'] == '金额')
        fixed = repair_request(draft, 'a', [{'op': 'appendMarkdown', 'path': details['path'], 'value': choice['Markdown']}])
        doc = compile_document(a, fixed['document'])
        self.assertEqual(doc['periodTrendRefs'], [choice['trendRef']])
        self.assertIn('原有说明。', doc['markdown'])
        self.assertNotIn('charts/', draft['document']['markdown'])
        self.assertIn('查询数据', doc['html'])

    def test_unknown_chart_is_rejected_and_draft_markup_cannot_change_template(self):
        a = artifact(observation('2026J0001', '7'))
        with self.assertRaises(AnalysisError) as e:
            compile_source(a, {'title': '分析', 'markdown': '![图](charts/unknown.svg)'})
        self.assertEqual(e.exception.code, 'UNKNOWN_REPORT_FIGURE')
        body, _, _ = compile_source(a, {'title': '分析', 'markdown': '## 发现\n\n<script>report()</script>'})
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>report()', body)
        self.assertIn('class="overview"', body)

    def test_complete_analysis_precedes_its_source_appendix(self):
        a = artifact(observation('2026J0001', '7'))
        body, _, _ = compile_source(a, {'title':'报告', 'markdown':'## 发现\n\n首段。\n\n## 结论\n\n最后的分析结论。'})
        self.assertLess(body.index('最后的分析结论。'), body.index('<h2>查询数据</h2>'))
