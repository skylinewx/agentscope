"""最终结果选择、完整依赖核对及容量兜底的功能回归。"""
from copy import deepcopy
from analysis_model_closeout_test import summary_response
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from agentscope.message import UserMsg, Msg, TextBlock
from agentscope.state import AgentState
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_grid_files_test import GridFileTest
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.turn_completion import TurnCompletion, current_turn, query_evidence, query_review_payloads, review_query_answer
from analysis_extension.query_result_evidence import select_results
from analysis_extension.middleware import AnalysisMiddleware


class ResultEvidenceTests(PresentationCase):
    make_grid = GridFileTest.make_grid

    def test_cancelled_permission_check_is_not_counted_as_denied(self):
        turn = self.turn()
        turn.record_tool_attempt({'attemptId': 'cancelled', 'callId': 'cancelled', 'toolName': 'Read',
            'status': 'interrupted', 'phase': 'PERMISSION', 'elapsedMs': 1})
        stats = turn.state['toolPerformance']['Read']
        self.assertEqual(stats['interrupted'], 1)
        self.assertNotIn('permissionDenied', stats)

    async def test_tool_metrics_include_schema_rejection_without_double_counting_success(self):
        from analysis_turn_completion_test import TurnLoopTests, call, text, STATE_KEY
        await TurnLoopTests.run_agent(self, [
            call('AnalysisTask', action='declare', mode='invalid', goal='读取'),
            call('AnalysisTask', action='declare', mode='DIRECT', goal='读取'), text('完成')])
        stats = self.agent.state.middle_context[STATE_KEY]['toolPerformance']['AnalysisTask']
        self.assertEqual(stats['attempts'], 2)
        self.assertEqual(stats['calls'], 2)
        self.assertEqual(stats['failures'], 1)
        self.assertEqual(stats['validationRejected'], 1)

    async def test_dependency_reuse_keeps_new_answer_review_and_selected_outputs(self):
        turn = self.turn()
        evidence = ResultPartitionTests().evidence()
        model = AsyncMock(return_value={'issues': []})
        with patch('analysis_extension.turn_completion._model_review', new=model):
            await review_query_answer(None, turn, evidence, '首次两个结果')
            self.assertEqual(model.await_count, 1)
            self.assertEqual(turn.state['queryReviewProgress']['dependencyReviewedCount'], 2)
            selected = deepcopy(evidence)
            selected['pythonRuns'] = selected['pythonRuns'][1:]
            selected['resultRoots'] = ['d1']
            turn.state['request'] = '只看第二个结果'
            model.return_value = {'issues': [{'code': 'ANSWER_MISMATCH', 'message': '新答复金额错误'}]}
            result = await review_query_answer(None, turn, selected, '第二个结果是999')
        self.assertEqual(model.await_count, 2, '复用计算核对仍必须审查新的答复')
        payload = json.loads(model.call_args.args[-1])
        self.assertEqual(payload['resultReviewPhase'], 'FINAL')
        self.assertEqual(payload['candidateAnswer'], '第二个结果是999')
        self.assertNotIn('pythonRuns', payload['evidence'])
        self.assertEqual([v['derivedId'] for v in payload['evidence']['finalResults']], ['d1'])
        self.assertEqual(result['issues'][0]['code'], 'ANSWER_MISMATCH')
        self.assertEqual(turn.state['queryReviewProgress']['dependencyReusedCount'], 1)
        self.assertLess(len(model.call_args.args[-1]), len(model.await_args_list[0].args[-1]) / 2)

    def test_dependency_cache_binds_all_material_and_never_unreviewed_outputs(self):
        from analysis_extension.query_result_evidence import dependency_units, cached_dependencies, save_dependency_verdict
        turn = self.turn()
        evidence = ResultPartitionTests().evidence()
        evidence['pythonRuns'] = evidence['pythonRuns'][:1]
        evidence['resultRoots'] = ['d0']
        evidence['pythonRuns'][0]['allOutputsDigest'] = 'all-outputs-original'
        units = dependency_units(evidence)
        save_dependency_verdict(turn, units[0], 'rules-1')
        self.assertEqual(cached_dependencies(turn, units, 'rules-1'), [units[0]['unitRef']])
        self.assertEqual(cached_dependencies(turn, units, 'rules-2'), [])
        for field, value in [('code', 'other code'), ('inputs', [{'value': '9'}]),
                             ('sourceData', [{'complete': True, 'rows': [['9']]}]),
                             ('fileInputs', [{'sha256': 'changed'}]), ('allOutputsDigest', 'changed'),
                             ('outputs', [{'derivedId': 'd0', 'value': '9'}]),
                             ('outputs', [{'derivedId': 'exploratory', 'value': '0'}])]:
            with self.subTest(field=field, value=value):
                changed = deepcopy(evidence)
                changed['pythonRuns'][0][field] = value
                changed['resultRoots'] = [changed['pythonRuns'][0]['outputs'][0]['derivedId']]
                self.assertEqual(cached_dependencies(turn, dependency_units(changed), 'rules-1'), [])

    async def test_failed_combined_review_does_not_mark_dependencies_verified(self):
        turn = self.turn()
        evidence = ResultPartitionTests().evidence()
        model = AsyncMock(side_effect=[{'issues': [{'code': 'ANSWER_MISMATCH', 'message': '错误'}]}, {'issues': []}])
        with patch('analysis_extension.turn_completion._model_review', new=model):
            await review_query_answer(None, turn, evidence, '第一版')
            await review_query_answer(None, turn, evidence, '第二版')
        self.assertEqual(turn.state['queryReviewProgress']['dependencyReusedCount'], 0)
        self.assertIn('pythonRuns', json.loads(model.call_args.args[-1])['evidence'])

    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '查询当前范围的数值')))
        turn.task(mode='DIRECT', goal='查询当前范围的数值')
        self.service.turn = turn
        return turn

    async def compute(self, capture, file, key, code, **kwargs):
        result = await self.service.python(gridInputs=[{'captureRef': capture, 'fileRef': file}],
            code=code, purpose='按已选来源计算', requestKey=key, **kwargs)
        self.service.turn.state.setdefault('pythonRunRefs', []).append(result['pythonRunRef'])
        return result

    async def test_exploration_does_not_drown_selected_diagnostic_and_source_data(self):
        turn = self.turn()
        capture, file, _, _, _ = await self.make_grid(3)
        exploration = await self.compute(capture, file, 'explore',
            "result=[{'name':'debug','label':'探索','kind':'DIAGNOSTIC','data':{'text':'x'*200000},'inputRefs':[inputs[0]['ref']]}]")
        final = await self.compute(capture, file, 'final',
            "rows=list(grid_rows(inputs[0]['ref']))\nresult=[{'name':'total','label':'合计','kind':'DIAGNOSTIC','data':{'total':sum(D(c['value']) for c in rows)},'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        # 后续探索不应偷偷替换已明确选择的答复依据。
        await self.compute(capture, file, 'later',
            "result=[{'name':'later','label':'另一次探索','value':'999','unit':None,'inputRefs':[inputs[0]['ref']]}]")
        evidence = query_evidence(turn)
        self.assertEqual([r['runRef'] for r in evidence['pythonRuns']], [final['pythonRunRef']])
        self.assertEqual(evidence['pythonRuns'][0]['outputs'][0]['data']['total'], '0.3')
        self.assertEqual(evidence['pythonRuns'][0]['sourceData'][0]['rowCount'], 3)
        self.assertTrue(evidence['pythonRuns'][0]['sourceData'][0]['complete'])
        self.assertEqual(len(query_review_payloads({'candidateAnswer': '0.3'}, evidence)), 1)
        self.assertEqual(len(self.store.get_python_run(OWNER, exploration['pythonRunRef'])['outputs'][0]['data']['text']), 200000)
        self.assertEqual(self.store.list_results(OWNER, 0, 100)['total'], 0)

    async def test_earlier_run_dependency_is_restored_with_original_input(self):
        turn = self.turn()
        capture, selectors, scope = saved_fixture(self.store, ('2',))
        built = await self.service.build([capture], selectors, scope, [], 'base')
        first = await self.service.python(analysisRef=built['analysisRef'],
            code="result=[{'name':'first','label':'第一步','value':D(inputs[0]['value'])*2,'unit':'元','inputRefs':[inputs[0]['ref']]}]",
            purpose='第一步', requestKey='first')
        second = await self.service.python(analysisRef=first['analysisRef'], inputRefs=[first['derivedValues'][0]['derivedId']],
            code="result=[{'name':'second','label':'第二步','value':D(inputs[0]['value'])+1,'unit':'元','inputRefs':[inputs[0]['ref']]}]",
            purpose='第二步', requestKey='second', answerOutputs=['second'])
        evidence = query_evidence(turn)
        runs = {r['runRef']: r for r in evidence['pythonRuns']}
        self.assertEqual(set(runs), {first['pythonRunRef'], second['pythonRunRef']})
        self.assertEqual(runs[second['pythonRunRef']]['dependencies'], [first['pythonRunRef']])
        self.assertEqual(runs[first['pythonRunRef']]['inputs'][0]['value'], '2')
        self.assertEqual(runs[second['pythonRunRef']]['outputs'][0]['value'], '5')

    async def test_result_selection_rejects_fabrication_and_other_session(self):
        turn = self.turn()
        capture, file, _, _, _ = await self.make_grid()
        result = await self.compute(capture, file, 'one',
            "result=[{'name':'one','label':'一个值','value':'1','unit':None,'inputRefs':[inputs[0]['ref']]}]")
        with self.assertRaises(AnalysisError):
            select_results(turn, [{'pythonRunRef': result['pythonRunRef'], 'outputRefs':['invented']}])
        self.service.owner = Owner('other', 'other', 'other')
        with self.assertRaises(AnalysisError):
            select_results(turn, [{'pythonRunRef': result['pythonRunRef'], 'outputRefs':[result['derivedValues'][0]['derivedId']]}])

    async def test_partial_read_preserves_real_extent_and_tampering_blocks(self):
        turn = self.turn()
        capture, file, _, _, _ = await self.make_grid(20)
        result = await self.compute(capture, file, 'sample',
            "for cell in grid_rows(inputs[0]['ref']):\n    value=cell['value']\n    break\nresult=[{'name':'sample','label':'样例','value':value,'unit':None,'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['sample'])
        evidence = query_evidence(turn)
        self.assertFalse(evidence['pythonRuns'][0]['fileReads'][0]['exhausted'])
        self.assertEqual(evidence['pythonRuns'][0]['sourceData'][0]['rowCount'], 1)
        from analysis_extension.grid_files import resolve_input
        from pathlib import Path
        path = resolve_input(self.service, {'captureRef':capture,'fileRef':file})['_path']
        Path(path).write_text('{}\n', encoding='utf-8')
        with self.assertRaises(AnalysisError) as failed:
            query_evidence(turn)
        self.assertEqual(failed.exception.code, 'GRID_FILE_INTEGRITY')

    async def test_capacity_records_progress_before_any_model_call(self):
        turn = self.turn()
        evidence = {'sources':[{'facts':[{'value':'1'}]}], 'relations':[]}
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 20), patch('analysis_extension.turn_completion._model_review', new_callable=AsyncMock) as model:
            with self.assertRaises(AnalysisError):
                await review_query_answer(None, turn, evidence, 'answer')
            model.assert_not_awaited()
        saved = self.store.turn_progress(OWNER, turn.state['turnKey'])['queryReview']
        self.assertEqual(saved['errorCode'], 'REVIEW_CAPACITY')
        self.assertEqual(saved['completedPartCount'], 0)
        self.assertGreaterEqual(saved['prepareMs'], 0)

    async def test_direct_capacity_interprets_existing_values_and_data_error_still_blocks(self):
        turn = self.turn()
        capture, _, _ = saved_fixture(self.store, ('0', '-2'))
        turn._add_query_capture(capture)
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        middleware.query_reviewer = AsyncMock(side_effect=AnalysisError('REVIEW_CAPACITY','容量不足'))
        state = AgentState(reply_id='answer')
        state.context = [Msg(id='answer', name='agent', role='assistant', content=[TextBlock(text='未经核对的合计是999999')])]
        verdict = await middleware._check_end(SimpleNamespace(state=state, name='agent', model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response))))
        self.assertEqual(verdict['status'], 'PARTIAL')
        self.assertEqual(verdict['semanticReview'], 'NOT_REVIEWED')
        self.assertEqual(verdict['deliveryStatus'], 'MODEL_SUMMARY')
        self.assertNotIn('999999', turn.state['hostCloseout']['answer'])
        self.assertIn('-2', turn.state['hostCloseout']['answer'])
        turn.state.pop('hostCloseout')
        middleware.query_reviewer = AsyncMock(return_value={'issues':[{'code':'ANSWER_MISMATCH','message':'实际数值不符'}]})
        verdict = await middleware._check_end(SimpleNamespace(state=state, name='agent', model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response))))
        self.assertEqual(verdict['status'], 'INCOMPLETE')
        self.assertEqual(verdict['semanticReview'], 'REVISE')

    async def test_interpreted_closeout_stream_and_saved_message_are_identical(self):
        from analysis_turn_completion_test import TurnLoopTests, call, text
        from agentscope.types import ReplyFinishedReason
        turn = self.turn()
        capture, _, _ = saved_fixture(self.store, ('0', '-2'))
        turn._add_query_capture(capture)
        evidence = query_evidence(turn)
        from utils import MockModel
        with patch.object(MockModel, 'generate_structured_output', AsyncMock(side_effect=summary_response)), patch('analysis_extension.middleware.query_evidence', return_value=evidence), patch(
                'analysis_extension.middleware.review_query_answer', new=AsyncMock(side_effect=AnalysisError('REVIEW_CAPACITY','容量不足'))):
            events, ends = await TurnLoopTests.run_agent(self, [call('AnalysisTask', action='declare', mode='DIRECT', goal='查询金额'), text('未经核对的合计是999999')])
        delivered = ''.join(event.delta for event in events if getattr(event, 'type', '') == 'TEXT_BLOCK_DELTA')
        saved = [event for event in events if isinstance(event, Msg)][-1]
        self.assertEqual(delivered, saved.get_text_content())
        self.assertNotIn('999999', delivered)
        self.assertIn('-2', delivered)
        self.assertEqual(saved.metadata['analysisCompletion'], ends[0].metadata['analysisCompletion'])
        self.assertEqual(saved.metadata['analysisCompletion']['deliveryStatus'], 'MODEL_SUMMARY')
        self.assertEqual(ends[0].finished_reason, ReplyFinishedReason.COMPLETED)

    async def test_failed_dependency_review_never_runs_global_approval(self):
        turn = self.turn()
        evidence = ResultPartitionTests().evidence()
        failure = {'issues':[{'code':'ANSWER_MISMATCH','message':'输入与合计不符'}]}
        reviewer = AsyncMock(side_effect=[failure, {'issues':[]}])
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 10000), patch(
                'analysis_extension.turn_completion._model_review', new=reviewer):
            result = await review_query_answer(None, turn, evidence, '两个结果比较')
        self.assertEqual(result, failure)
        self.assertEqual(reviewer.await_count, 2)
        self.assertTrue(all(json.loads(call.args[-1])['resultReviewPhase']=='DEPENDENCY' for call in reviewer.await_args_list))

    async def test_saved_grid_focus_once_preserves_result_without_source_query(self):
        from analysis_extension.query_result_evidence import focus_saved_reads, focused_results_preserved
        turn=self.turn()
        capture,file,_,_,_=await self.make_grid(3)
        await self.compute(capture,file,'unfocused',
            "result=[{'name':'total','label':'总额','value':sum(D(c['value']) for c in grid_rows(inputs[0]['ref'])),'unit':None,'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        evidence=query_evidence(turn)
        recovery=focus_saved_reads(turn,evidence)
        self.assertEqual(recovery['semanticReview'],'NOT_REVIEWED')
        self.assertFalse(recovery['issues'][0]['details']['requiresSourceQuery'])
        self.assertEqual(recovery['issues'][0]['details']['outputs'],['total'])
        # 本地整理次数在宿主存储中保留，新的 TurnCompletion 实例不能重置限额。
        restored=TurnCompletion(self.service,deepcopy(turn.state))
        self.assertIsNone(focus_saved_reads(restored,evidence))
        await self.compute(capture,file,'focused',
            "result=[{'name':'total','label':'总额','value':sum(D(c['value']) for c in grid_rows(inputs[0]['ref'],{'startRow':0})),'unit':None,'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        focused=query_evidence(turn)
        self.assertTrue(focused_results_preserved(turn,focused))
        focused['pythonRuns'][0]['outputs'][0]['value']='0.300'
        self.assertTrue(focused_results_preserved(turn,focused))
        focused['pythonRuns'][0]['outputs'][0]['value']='999'
        self.assertFalse(focused_results_preserved(turn,focused))

    async def test_capacity_grid_gets_one_recovery_then_source_closeout(self):
        turn=self.turn()
        capture,file,_,_,_=await self.make_grid(3)
        await self.compute(capture,file,'grid',
            "result=[{'name':'total','label':'总额','value':sum(D(c['value']) for c in grid_rows(inputs[0]['ref'])),'unit':None,'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        middleware=AnalysisMiddleware(self.store,OWNER,[],self.service.authorize)
        middleware.service=self.service
        middleware.query_reviewer=AsyncMock(side_effect=AnalysisError('REVIEW_CAPACITY','容量不足'))
        state=AgentState(reply_id='answer')
        state.context=[Msg(id='answer',name='agent',role='assistant',content=[TextBlock(text='总额0.3')])]
        agent=SimpleNamespace(state=state,name='agent',model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response)))
        first=await middleware._check_end(agent)
        self.assertEqual(first['recoveryAction'],'FOCUS_SAVED_GRID')
        self.assertNotIn('hostCloseout',turn.state)
        second=await middleware._check_end(agent)
        self.assertEqual(second['deliveryStatus'],'MODEL_SUMMARY')
        self.assertEqual(second['semanticReview'],'NOT_REVIEWED')

    async def test_focus_diagnostic_change_is_reviewed_instead_of_forced_closeout(self):
        from analysis_extension.query_result_evidence import focus_saved_reads
        turn = self.turn()
        capture, file, _, _, _ = await self.make_grid(3)
        await self.compute(capture, file, 'before-focus',
            "rows=list(grid_rows(inputs[0]['ref']))\nresult=[{'name':'total','label':'合计','kind':'DIAGNOSTIC',"
            "'data':{'total':sum(D(c['value']) for c in rows),'scan':'structure'},'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        focus_saved_reads(turn, query_evidence(turn))
        await self.compute(capture, file, 'after-focus',
            "rows=list(grid_rows(inputs[0]['ref'],{'startRow':0}))\nresult=[{'name':'total','label':'合计','kind':'DIAGNOSTIC',"
            "'data':{'total':sum(D(c['value']) for c in rows),'count':len(rows)},'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        middleware.query_reviewer = AsyncMock(return_value={'issues': []})
        state = AgentState(reply_id='answer')
        state.context = [Msg(id='answer', name='agent', role='assistant', content=[TextBlock(text='合计0.3')])]
        status = await middleware._check_end(SimpleNamespace(state=state, name='agent'))
        self.assertEqual(status['semanticReview'], 'PASS')
        self.assertNotIn('hostCloseout', turn.state)
        middleware.query_reviewer.assert_awaited_once()
        evidence = middleware.query_reviewer.call_args.args[2]
        comparison = evidence['focusComparison']
        self.assertEqual(comparison['changedOutputNames'], ['total'])
        self.assertEqual(comparison['previousCalculations'][0]['outputs'][0]['data']['scan'], 'structure')
        self.assertEqual(evidence['pythonRuns'][0]['outputs'][0]['data']['count'], 3)
        payload = json.loads(query_review_payloads({'candidateAnswer': '合计0.3'}, evidence)[0])
        self.assertEqual(payload['evidence']['focusComparison'], comparison)

    async def test_focus_changed_amount_remains_visible_and_can_be_rejected(self):
        from analysis_extension.query_result_evidence import focus_saved_reads, focused_review_context
        turn = self.turn()
        capture, file, _, _, _ = await self.make_grid(3)
        await self.compute(capture, file, 'original-total',
            "result=[{'name':'total','label':'合计','value':sum(D(c['value']) for c in grid_rows(inputs[0]['ref'])),"
            "'unit':None,'inputRefs':[inputs[0]['ref']]}]", answerOutputs=['total'])
        focus_saved_reads(turn, query_evidence(turn))
        await self.compute(capture, file, 'wrong-total',
            "rows=list(grid_rows(inputs[0]['ref']))\nresult=[{'name':'total','label':'合计','value':'999',"
            "'unit':None,'inputRefs':[inputs[0]['ref']]}]", answerOutputs=['total'])
        evidence = query_evidence(turn)
        comparison = focused_review_context(turn, evidence)
        self.assertEqual(comparison['previousCalculations'][0]['outputs'][0]['value'], '0.3')
        self.assertEqual(evidence['pythonRuns'][0]['outputs'][0]['value'], '999')
        middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
        middleware.service = self.service
        middleware.query_reviewer = AsyncMock(return_value={'issues': [{'code': 'ANSWER_MISMATCH', 'message': '合计与原值不符'}]})
        state = AgentState(reply_id='answer')
        state.context = [Msg(id='answer', name='agent', role='assistant', content=[TextBlock(text='合计999')])]
        status = await middleware._check_end(SimpleNamespace(state=state, name='agent'))
        self.assertEqual(status['status'], 'INCOMPLETE')
        self.assertEqual(status['semanticReview'], 'REVISE')
        self.assertNotIn('hostCloseout', turn.state)

    async def test_grid_focus_continues_same_reply_without_publishing_first_draft(self):
        from analysis_turn_completion_test import TurnLoopTests, call, text
        from agentscope.types import ReplyFinishedReason
        turn=self.turn()
        capture,file,_,_,_=await self.make_grid(3)
        await self.compute(capture,file,'grid-loop',
            "result=[{'name':'total','label':'总额','value':sum(D(c['value']) for c in grid_rows(inputs[0]['ref'])),'unit':None,'inputRefs':[inputs[0]['ref']]}]",
            answerOutputs=['total'])
        evidence=query_evidence(turn)
        reviewer=AsyncMock(side_effect=[AnalysisError('REVIEW_CAPACITY','容量不足'),{'issues':[]}])
        with patch('analysis_extension.middleware.query_evidence',return_value=evidence), patch(
                'analysis_extension.middleware.review_query_answer',new=reviewer):
            events,ends=await TurnLoopTests.run_agent(self,[
                call('AnalysisTask',action='declare',mode='DIRECT',goal='查询金额'),
                text('初次候选0.3'),text('最终数值0.3')])
        delivered=''.join(event.delta for event in events if getattr(event,'type','')=='TEXT_BLOCK_DELTA')
        self.assertNotIn('初次候选',delivered)
        self.assertIn('最终数值0.3',delivered)
        self.assertEqual(len(ends),1)
        self.assertEqual(ends[0].finished_reason,ReplyFinishedReason.COMPLETED)
        self.assertEqual(ends[0].metadata['analysisCompletion']['semanticReview'],'PASS')


class ResultPartitionTests(TestCase):
    def evidence(self):
        runs = []
        for index in range(2):
            runs.append({'runRef':str(index),'code':'x'*8000,'dependencies':[],
                         'sourceData':[{'complete':True,'rows':[['0'],[None]],'columns':['value']}],
                         'outputs':[{'derivedId':'d'+str(index),'value':str(index),'inputRefs':['source']} ]})
        return {'sources':[], 'pythonRuns':runs, 'resultRoots':['d0','d1'],
                'entityDefinitions':{},'fieldDefinitions':{},'identityPrefixes':{}}

    def test_complete_dependencies_then_global_result_review(self):
        evidence = self.evidence()
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 10000):
            payloads = [json.loads(p) for p in query_review_payloads({'candidateAnswer':'两个结果比较'}, evidence)]
        self.assertEqual([p['resultReviewPhase'] for p in payloads], ['DEPENDENCY','DEPENDENCY','FINAL'])
        self.assertEqual(len(payloads[-1]['evidence']['finalResults']), 2)
        for part in payloads[:-1]:
            self.assertEqual(part['evidence']['pythonRuns'][0]['sourceData'][0]['rows'], [['0'],[None]])
            self.assertNotIn('candidateAnswer', part)
            self.assertNotIn('userRequest', part)

    def test_one_global_calculation_is_never_split_to_claim_full_pass(self):
        evidence = self.evidence()
        evidence['pythonRuns'][0]['code'] *= 2
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 10000):
            with self.assertRaises(AnalysisError) as result:
                query_review_payloads({'candidateAnswer':'全局合计'}, evidence)
        self.assertEqual(result.exception.code,'REVIEW_CAPACITY')

    def test_normalized_number_uses_configured_format_not_guessing(self):
        from analysis_extension.python_worker import grid_value
        cell = {'measureCode':'amount','rawValueJson':'"1,234.50"'}
        self.assertEqual(grid_value(cell,'grouped-dot'),'1234.50')
        self.assertEqual(grid_value(cell),'1,234.50')
        self.assertEqual(grid_value({'rawValueJson':'null'}),None)
        self.assertEqual(grid_value({'rawValueJson':'0'}),0)

    def test_grid_compression_is_lossless_for_missing_null_zero_and_dimensions(self):
        from analysis_extension.query_result_evidence import pack_source_rows, source_cells
        for dimensions in ([{'period':'y','org':'a'}, {'period':'y','org':'b'}],
                           [{'period':'y','org':None}, {'period':'y'}]):
            data={'columns':['key','headers','dimensions','value','unit','formula','issue'],
                  'rows':[['1:1',['标题','收入'],dimensions[0],'0',None,None,None],
                          ['2:1',None,dimensions[1],None,None,None,None]]}
            original=list(source_cells(data))
            pack_source_rows(data)
            self.assertEqual(list(source_cells(data)),original)

    def test_failed_exploration_does_not_hide_independent_valid_source(self):
        from analysis_extension.query_closeout import source_only_answer
        evidence={'sources':[{'captureRef':'failed','diagnostic':{'code':'BAD_PARAMETER'}},
                 {'captureRef':'valid','facts':[{'value':'0','valueState':'PRESENT','headers':['实际值']}]}]}
        result=source_only_answer(evidence,'REVIEW_CAPACITY')
        self.assertEqual(result['status'],'PARTIAL')
        self.assertIn('实际值',result['answer'])
        self.assertEqual(result['sourceDiagnostics'][0]['captureRef'],'failed')
        with self.assertRaises(AnalysisError):
            source_only_answer({'sources':evidence['sources'][:1]},'REVIEW_CAPACITY')

    def test_sparse_header_does_not_expand_every_data_row_dimensions(self):
        from analysis_extension.query_result_evidence import pack_source_rows, source_cells
        data={'columns':['key','headers','dimensions','value','unit','formula','issue'],
              'rows':[['header',[],{},'金额',None,None,None]] + [
                  [str(index),['金额'],{'period':'year','object':'member'+str(index),'category':None},
                   str(index),None,None,None] for index in range(1000)]}
        original=list(source_cells(data))
        pack_source_rows(data)
        self.assertEqual(list(source_cells(data)),original)
        dimension_index=data['columns'].index('dimensions')
        self.assertTrue(all(isinstance(row[dimension_index],list) for row in data['rows']))
        self.assertEqual(data['rows'][0][dimension_index],[-1,-1,-1])
        self.assertLess(len(json.dumps(data)), len(json.dumps(original))*0.5)
