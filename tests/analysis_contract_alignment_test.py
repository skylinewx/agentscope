"""跨入口、存储、计算、完成检查和交付的契约回归；使用合成来源。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from analysis_fixtures import OWNER, saved_fixture
from analysis_closeout_test import mixed_fixture
from analysis_depth_test import chain
from analysis_presentation_fixtures import PresentationCase
import analysis_capture_test as capture_fixture
from analysis_extension.capture import ReceiptCapture
from analysis_extension.query_attempts import prepare, record
from analysis_extension.completion import completion_summary
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.protocol import validator
from analysis_extension.store import AnalysisStore
from analysis_extension.tools import AnalysisBuild, AnalysisPublish
from analysis_extension.turn_completion import TurnCompletion, current_turn
from analysis_extension.validation import executable_field
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.state import AgentState
from agentscope.tool import ToolResponse


class RequestAttemptTests(PresentationCase):
    origin = {'resourceId': 'fixture', 'originalToolName': 'execute_semantic_plan'}
    request = {'contractVersion': '2.0', 'requestId': 'model-placeholder', 'contextId': 'c',
               'querySessionId': 'q', 'plan': {'steps': [], 'outputStepIds': []}}

    def attempt(self, request=None, turn='turn'):
        return prepare(self.store, OWNER, turn, self.origin, request or self.request)

    def finish(self, attempt, status='FAILED', retryable=True, action='RETRY'):
        record(self.store, OWNER, 'turn', attempt, {'contractVersion': '2.0', 'executionId': 'execution',
            'status': status, 'error': {'code': 'QUERY_CONCURRENCY_LIMIT', 'retryable': retryable,
                                       'recoveryAction': action} if status == 'FAILED' else None})

    def test_unknown_running_success_and_partial_preserve_identity(self):
        for status in ('IN_PROGRESS', 'SUCCEEDED', 'PARTIAL'):
            with self.subTest(status=status):
                request = {**self.request, 'querySessionId': status}
                first, attempt = self.attempt(request)
                self.assertEqual(first, self.attempt(request)[0])
                self.finish(attempt, status)
                second, _ = self.attempt({**request, 'requestId': 'ai-changed'})
                self.assertEqual(first, second)

    def test_terminal_retry_survives_restart_and_concurrent_acquisition(self):
        first, attempt = self.attempt()
        self.finish(attempt)
        self.store = AnalysisStore(self.store.root)
        with ThreadPoolExecutor(max_workers=4) as pool:
            acquired = list(pool.map(lambda _: self.attempt(), range(4)))
        self.assertEqual(len({request['requestId'] for request, _ in acquired}), 1)
        self.assertNotEqual(first['requestId'], acquired[0][0]['requestId'])
        self.assertEqual(acquired[0][1]['attempt'], 1)
        # 迟到的旧响应不覆盖新的未知结果；第二次失败后也不无限换身份。
        self.finish(attempt)
        self.assertEqual(acquired[0][0], self.attempt()[0])
        self.finish(acquired[0][1])
        self.assertEqual(acquired[0][0], self.attempt()[0])
        self.assertNotEqual(first['requestId'], self.attempt(turn='another-turn')[0]['requestId'])

    def test_non_retryable_failure_and_unconfirmed_error_do_not_change_identity(self):
        for retryable, action in ((False, 'RETRY'), (True, 'NONE'), ('true', 'RETRY')):
            request = {**self.request, 'querySessionId': str((retryable, action))}
            first, attempt = self.attempt(request)
            self.finish(attempt, retryable=retryable, action=action)
            self.assertEqual(first, self.attempt(request)[0])

    async def test_capture_retries_terminal_failure_once_without_another_model_call(self):
        tool = capture_fixture.CaptureTest.make_tool(self, '{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, OWNER, frozenset({'registered-budget'}), AsyncMock())
        capture.turn_key_provider = lambda: 'capture-turn'
        capture.query_cache_provider = lambda: cache
        sent, cache = [], {}
        async def execute(**kwargs):
            actual = json.loads(kwargs['tool_call'].input)
            sent.append(actual)
            failed = len(sent) == 1
            receipt = {'contractVersion': '2.0', 'executionId': 'e' + str(len(sent)),
                       'status': 'FAILED' if failed else 'SUCCEEDED', 'data': {},
                       'error': {'code': 'QUERY_CONCURRENCY_LIMIT', 'retryable': True,
                                 'recoveryAction': 'RETRY'} if failed else None}
            yield ToolResponse(state=ToolResultState.ERROR if failed else ToolResultState.SUCCESS,
                               content=[TextBlock(text=json.dumps(receipt))])
        call = ToolCallBlock(id='one-model-call', name=tool.name, input=json.dumps(self.request))
        events = [event async for event in capture.run(agent, {'tool_call': call}, execute)]
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]['requestId'], sent[1]['requestId'])
        self.assertEqual({k: v for k, v in sent[0].items() if k != 'requestId'},
                         {k: v for k, v in sent[1].items() if k != 'requestId'})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state, ToolResultState.SUCCESS)
        self.assertEqual(len(events[0].metadata['queryRetries']), 1)
        failed = self.store.get_capture(OWNER, events[0].metadata['queryRetries'][0]['captureRef'])
        self.assertEqual(json.loads(failed['response']['content'][0]['text'])['status'], 'FAILED')

    async def test_repeated_failure_stops_with_visible_host_limit_and_retains_source_error(self):
        tool = capture_fixture.CaptureTest.make_tool(self, '{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, OWNER, frozenset({'registered-budget'}), AsyncMock())
        capture.turn_key_provider = lambda: 'failed-turn'
        sent = []
        async def execute(**kwargs):
            sent.append(json.loads(kwargs['tool_call'].input))
            receipt = {'contractVersion': '2.0', 'executionId': 'failed-execution', 'status': 'FAILED',
                       'error': {'code': 'QUERY_CONCURRENCY_LIMIT', 'retryable': True, 'recoveryAction': 'RETRY'}}
            yield ToolResponse(state=ToolResultState.ERROR, content=[TextBlock(text=json.dumps(receipt))])
        call = ToolCallBlock(id='failed-call', name=tool.name, input=json.dumps(self.request))
        events = [event async for event in capture.run(agent, {'tool_call': call}, execute)]
        self.assertEqual(len(sent), 2)
        self.assertEqual(events[-1].state, ToolResultState.ERROR)
        self.assertEqual(events[-1].metadata['queryRecovery']['code'], 'QUERY_RETRY_LIMIT_REACHED')
        self.assertIn('hostQueryRecovery', events[-1].content[-1].text)
        saved = self.store.get_capture(OWNER, events[-1].metadata['analysisCaptureRef'])
        self.assertTrue(json.loads(saved['response']['content'][0]['text'])['error']['retryable'])
        repeated = [event async for event in capture.run(agent, {'tool_call': call}, execute)]
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1]['requestId'], sent[-2]['requestId'])
        self.assertEqual(repeated[-1].metadata['queryRecovery']['retryCount'], 1)

    async def test_identity_storage_failure_never_sends_source_request(self):
        tool = capture_fixture.CaptureTest.make_tool(self, '{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, OWNER, frozenset({'registered-budget'}), AsyncMock())
        capture.turn_key_provider = lambda: 'store-down'
        async def forbidden(**kwargs):
            raise AssertionError('身份未持久化时不得执行来源请求')
            yield
        call = ToolCallBlock(id='store-down', name=tool.name, input=json.dumps(self.request))
        with patch.object(self.store, 'turn_progress', side_effect=OSError('fixture storage failure')):
            events = [event async for event in capture.run(agent, {'tool_call': call}, forbidden)]
        self.assertEqual(events[-1].state, ToolResultState.ERROR)
        self.assertIn('STORE_UNAVAILABLE', events[-1].content[0].text)


class FormulaCompletionTests(TestCase):
    def result(self, facts, scope='LOCAL'):
        return calculate(facts, {'scopeId': 'scope'}, [{'id': 'f', 'operator': 'FORMULA_CHECK',
            'inputRefs': ['root'], 'checkScope': scope}], RuleRegistry())[0]

    def test_known_null_completes_check_but_is_not_executable_value(self):
        facts = chain()
        facts[1].update(value=None, valueState='NO_VALUE')
        for scope in ('LOCAL', 'RECURSIVE'):
            result = self.result(facts, scope)
            self.assertEqual(result['executionStatus'], 'COMPLETED')
            self.assertEqual(result['calculationValidation'], 'BLOCKED')
            self.assertEqual(result['sourceConsistency'], 'NOT_CHECKED')
            self.assertIsNone(result['value'])
            self.assertFalse(executable_field(result, 'value'))
            self.assertTrue(executable_field(result, 'sourceValue'))
            from analysis_extension.review_revision import formula_review_trace
            projected = formula_review_trace(result['formulaTrace'])
            self.assertEqual(projected['executionStatus'], 'COMPLETED')
            self.assertEqual(projected['completionReason'], 'SOURCE_VALUE_MISSING')
            artifact = {'facts': facts, 'derivedValues': [result], 'relations': [], 'plan': {'workPlan': [
                {'taskId': 'check', 'kind': 'COMPUTATION', 'title': '核对', 'refs': ['derived_f'],
                 'operator': 'FORMULA_CHECK', 'targetRefs': ['root']}]}}
            completion = completion_summary(artifact)
            self.assertEqual(completion['state'], 'READY')
            self.assertEqual(completion['items'][0]['missingTargetRefs'], [])

    def test_null_does_not_hide_missing_dependency_conflict_or_display_precision(self):
        for change in ('missing', 'CONFLICT', 'NOT_QUERIED', 'precision', 'target-conflict', 'null-display'):
            facts = chain()
            facts[1].update(value=None, valueState='NO_VALUE')
            if change == 'missing':
                facts = [fact for fact in facts if fact['factId'] != 'expense']
            elif change == 'precision':
                facts[2]['precision'] = 'display_only'
            elif change == 'target-conflict':
                facts[0]['valueState'] = 'CONFLICT'
            elif change == 'null-display':
                facts[1]['precision'] = 'display_only'
            else:
                facts[2]['valueState'] = change
            for scope in ('LOCAL', 'RECURSIVE'):
                with self.subTest(change=change, scope=scope):
                    self.assertEqual(self.result(facts, scope)['executionStatus'], 'BLOCKED')


class PythonRecoveryTests(IsolatedAsyncioTestCase):
    async def test_decimal_error_keeps_source_null_and_reports_input_repair(self):
        from analysis_extension.models import AnalysisError
        from analysis_extension.python_analysis import execute_python
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('result = D("表头")', [], 5)
        self.assertEqual(caught.exception.details['nextAction'], 'CHECK_DECIMAL_INPUT')
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    async def test_standard_string_numeric_checks_remain_available_in_restricted_python(self):
        from analysis_extension.python_analysis import execute_python
        result = await execute_python('result = [{"digits": "123".isdigit(), "decimal": "123".isdecimal(), "numeric": "123".isnumeric()}]', [], 5)
        self.assertEqual(result, [{'digits': True, 'decimal': True, 'numeric': True}])

    async def test_grid_filter_error_lists_contract_and_continuation_reuses_exact_inputs(self):
        import tempfile
        from pathlib import Path
        from analysis_extension.models import AnalysisError
        from analysis_extension.python_analysis import execute_python, file_continuation
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cells.jsonl'
            path.write_text('', 'utf-8')
            with self.assertRaises(AnalysisError) as caught:
                await execute_python('result = list(grid_rows(inputs[0]["ref"], {"mapType":"ZB"}))',
                    [{'ref': 'grid', 'kind': 'GRID_FILE', '_path': str(path), 'sha256': 'digest', 'cellCount': 0}], 5)
        self.assertEqual(caught.exception.details['nextAction'], 'FIX_GRID_FILTER')
        self.assertIn('mapTypes', caught.exception.details['availableWhereFields'])
        self.assertFalse(caught.exception.details['requiresSourceQuery'])
        source = [{'captureRef': 'capture', 'fileRef': 'grid'}]
        continuation = file_continuation(source, None)
        self.assertEqual(continuation['input'], {'gridInputs': source})
        continuation['input']['gridInputs'][0]['fileRef'] = 'changed'
        self.assertEqual(source[0]['fileRef'], 'grid')

    async def test_unknown_input_name_returns_actual_runtime_contract(self):
        from analysis_extension.models import AnalysisError
        from analysis_extension.python_analysis import execute_python
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('result = INPUT_FILES', [], 5)
        self.assertEqual(caught.exception.details['inputVariable'], 'inputs')
        self.assertEqual(caught.exception.details['nextAction'], 'FIX_CODE_NAME')
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    def test_bad_table_rows_point_to_exact_shape_without_rewriting_values(self):
        from analysis_extension.models import AnalysisError
        from analysis_extension.python_products import products
        output = {'name': 'table', 'label': '表', 'kind': 'TABLE', 'inputRefs': ['file'],
                  'columns': [{'name': 'name', 'label': '项目', 'type': 'STRING'},
                              {'name': 'amount', 'label': '金额', 'type': 'DECIMAL_STRING'}],
                  'rows': [{'name': '项目甲', 'amount': '1.20'}]}
        for row in ({'name': '项目甲', 'amount': '1.20'}, ['项目甲']):
            output['rows'] = [row]
            with self.assertRaises(AnalysisError) as caught:
                products([output], {'file': {}}, ['file'], None, 'python')
            self.assertEqual(caught.exception.details['path'], '/result/0/rows/0')
            self.assertEqual(caught.exception.details['columnNames'], ['name', 'amount'])
            self.assertEqual(caught.exception.details['nextAction'], 'FIX_OUTPUT_ROWS')
            self.assertFalse(caught.exception.details['requiresSourceQuery'])
        output['rows'] = [['项目甲', '1.20']]
        result = products([output], {'file': {}}, ['file'], None, 'python')
        self.assertEqual(result[0]['rows'], [['项目甲', '1.20']])
        self.assertEqual(output['rows'], [['项目甲', '1.20']])


class DeliveryContractTests(PresentationCase):
    def observe(self, turn, name, value, state=ToolResultState.SUCCESS):
        from agentscope.tool import ToolResponse
        turn.observe(ToolCallBlock(id=name, name=name, input='{}'),
                     ToolResponse(state=state, content=[TextBlock(text=json.dumps(value))]))

    async def presentation_fixture(self):
        from analysis_turn_completion_test import TurnCompletionTests
        old = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '展示读数')))
        old.task(mode='ANALYSIS', goal='展示读数', delivery='DOCUMENT')
        original = await TurnCompletionTests.generic_result(self, old)
        refs = [fact['factId'] for fact in original['facts']]
        body = ''.join('<p>{{v' + str(index) + '}}</p>' for index in range(len(refs)))
        request = {'analysisRef': original['analysisRef'], 'requestKey': 'edit-published',
            'document': {'title': '修订后的读数', 'html': '<html><head></head><body><h1>预算比较</h1>' + body + '</body></html>',
                         'bindings': [{'key': f'v{index}', 'valueRef': ref} for index, ref in enumerate(refs)]},
            'renderCheckUnavailableReason': '离线契约测试未检查视觉布局。'}
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '仅修改报告标题')))
        turn.task(mode='ANALYSIS', goal='修改现有标题，数据不变', delivery='DOCUMENT')
        self.service.turn = turn
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        return request, original, turn

    async def test_real_presentation_revision_finalizes_without_build_and_keeps_evidence_checks(self):
        request, original, turn = await self.presentation_fixture()
        result = await self.service.publish(**request, finalize=True)
        self.observe(turn, 'AnalysisPublish', result)
        self.assertEqual(result['automaticFinalization']['status'], 'PUBLISHED')
        self.assertEqual(turn.check()['status'], 'READY')
        self.assertEqual(turn.state['buildRefs'], [])
        revised = self.store.get_analysis(OWNER, result['analysisRef'])
        for field in ('facts', 'derivedValues', 'inputDigest', 'verificationRecords'):
            self.assertEqual(revised[field], original[field])
        self.assertEqual(self.store.get_analysis(OWNER, original['analysisRef']), original)
        turn.state['requiredEvidence'] = 'FORMULAS'
        self.assertIn('FORMULA_TARGETS_REQUIRED', [item['code'] for item in turn.check()['issues']])

    async def test_presentation_revision_survives_preview_repair_and_turn_restoration(self):
        request, original, turn = await self.presentation_fixture()
        preview = await self.service.publish(**request, previewOnly=True)
        self.observe(turn, 'AnalysisPublish', preview)
        self.assertIn('RESULT_NOT_PUBLISHED', [item['code'] for item in turn.check()['issues']])
        restored_state = deepcopy(turn.state)
        restored_state['buildRefs'] = []
        restored = TurnCompletion(self.service, restored_state)
        self.service.turn = restored
        revised = await self.service.publish(**{**preview['confirmPublish'], 'requestKey': 'repair-title',
            'repairs': [{'op': 'replace', 'path': '/document/title', 'value': '修订标题'}]}, previewOnly=True)
        self.observe(restored, 'AnalysisPublish', revised)
        published = await self.service.publish(**revised['confirmPublish'])
        self.observe(restored, 'AnalysisPublish', published)
        self.assertEqual(restored.check()['status'], 'READY')
        self.assertEqual(restored.state['buildRefs'], [])
        fresh = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '执行新的分析')))
        fresh.task(mode='ANALYSIS', goal='新分析', delivery='DOCUMENT')
        self.observe(fresh, 'AnalysisPublish', published)
        self.assertIn('ANALYSIS_NOT_BUILT', [item['code'] for item in fresh.check()['issues']])

    async def test_registered_export_finishes_without_rebuild_but_keeps_review_and_new_work_checks(self):
        from analysis_turn_completion_test import TurnCompletionTests
        from analysis_extension.middleware import AnalysisMiddleware
        old = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '展示原值')))
        old.task(mode='ANALYSIS', goal='展示原值', delivery='DOCUMENT')
        artifact = await TurnCompletionTests.generic_result(self, old)
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '导出刚才的报告')))
        turn.task(mode='ANALYSIS', goal='导出已发布内容', delivery='DOCUMENT')
        export = await self.service.render_report(artifact['analysisRef'], 'existing-export')
        self.observe(turn, 'AnalysisRenderReport', export)
        self.assertEqual(turn.check()['status'], 'READY')
        self.assertEqual(turn.state['buildRefs'], [])
        middleware = AnalysisMiddleware(self.store, OWNER, frozenset(), AsyncMock(), delivery=self.delivery)
        middleware.service = self.service
        self.service.turn = turn
        with patch('analysis_extension.turn_completion.document_review', return_value=({}, None)):
            self.assertIsNone(middleware._approved_delivery())
        with patch('analysis_extension.turn_completion.document_review', return_value=({}, {'issues': []})):
            self.assertEqual(middleware._approved_delivery(), export['markdown'])
        turn.state['requiredEvidence'] = 'FORMULAS'
        self.assertIn('FORMULA_TARGETS_REQUIRED', [item['code'] for item in turn.check()['issues']])
        turn.state.pop('requiredEvidence')
        updated = await self.service.build(baseAnalysisRef=artifact['analysisRef'], operations=[], requestKey='new-draft')
        self.observe(turn, 'AnalysisBuild', updated)
        self.assertIsNone(turn.registered_export(updated['analysisRef']))
        self.assertIn('RESULT_NOT_PUBLISHED', [item['code'] for item in turn.check()['issues']])

    async def test_verification_draft_inherits_only_current_presentation_revision(self):
        from analysis_evidence_closure_test import EvidenceClosureTests
        request, original, turn = await self.presentation_fixture()
        preview = await self.service.publish(**request, previewOnly=True)
        draft = self.store.get_analysis(OWNER, preview['analysisRef'])
        # 模拟另一个真实来源在修订预览后返回核验，宿主只登记核验，不重新计算。
        capture_ref, _ = EvidenceClosureTests.make_capture(self, connected=True)
        updates = {'captureRefs': draft['captureRefs'] + [capture_ref],
                   'receiptVerification': draft['receiptVerification'],
                   'verificationRecords': draft['verificationRecords']}
        with patch.object(self.service, '_captured_verification_updates', return_value=updates):
            verified = self.service._with_captured_verification(draft)
        self.assertNotEqual(verified['analysisRef'], draft['analysisRef'])
        self.assertTrue(turn.current_presentation_revision(verified['analysisRef']))
        self.assertEqual(verified['facts'], original['facts'])
        self.assertFalse(turn.current_presentation_revision(original['analysisRef']))
        fresh = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '新问题')))
        self.assertFalse(fresh.current_presentation_revision(verified['analysisRef']))

    async def test_file_execution_and_idempotent_replay_offer_exact_continuation(self):
        from analysis_grid_files_test import GridFileTest
        from analysis_extension.models import AnalysisError
        capture, file_ref, _, _, _ = await GridFileTest.make_grid(self)
        inputs = {'gridInputs': [{'captureRef': capture, 'fileRef': file_ref}]}
        code = 'result=[{"name":"count","label":"行数","value":sum(1 for cell in grid_rows(inputs[0]["ref"])),"unit":None,"inputRefs":[inputs[0]["ref"]]}]'
        first = await self.service.python(**inputs, code=code, purpose='统计已选网格', requestKey='file-continuation')
        replay = await self.service.python(**inputs, code=code, purpose='统计已选网格', requestKey='file-continuation')
        self.assertEqual(first['pythonRunRef'], replay['pythonRunRef'])
        for result in (first, replay):
            self.assertNotIn('analysisRef', result)
            self.assertEqual(result['continuation']['input'], inputs)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(**inputs, code='result=INPUT_FILES', purpose='复用文件', requestKey='file-error')
        self.assertEqual(caught.exception.details['retryInput'], inputs)

    async def test_python_run_reference_is_not_silently_used_as_analysis(self):
        from analysis_extension.models import AnalysisError
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(analysisRef='python_example', code='result=[]',
                purpose='继续已有文件计算', requestKey='wrong-kind')
        self.assertEqual(caught.exception.code, 'PYTHON_INPUT_REFS')
        self.assertEqual(caught.exception.details['nextAction'], 'CORRECT_REFERENCE_KIND')
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    async def test_business_profile_applies_on_creation_cache_and_reconstruction(self):
        from agentscope.app.workspace_manager import LocalWorkspaceManager
        from business_instructions import WORKSPACE_INSTRUCTIONS, MEMORY_INSTRUCTIONS
        from workspace_manager_local_test import _FakeWorkspace
        with patch('agentscope.app.workspace_manager._local_workspace_manager.LocalWorkspace', _FakeWorkspace):
            manager = LocalWorkspaceManager(str(self.root), instructions=WORKSPACE_INSTRUCTIONS)
            first = await manager.get_workspace('user', 'agent', 'session')
            cached = await manager.get_workspace('user', 'agent', 'other-session')
            self.assertIs(first, cached)
            self.assertEqual(first.kwargs['instructions'], WORKSPACE_INSTRUCTIONS)
            await manager.close_all()
            rebuilt = await manager.get_workspace('user', 'agent', 'session')
            self.assertEqual(rebuilt.kwargs['instructions'], WORKSPACE_INSTRUCTIONS)
            created = await manager.create_workspace('user', 'new-agent', 'session')
            self.assertEqual(created.kwargs['instructions'], WORKSPACE_INSTRUCTIONS)
            generic = LocalWorkspaceManager(str(self.root))
            workspace = await generic.get_workspace('u', 'generic', 's')
            self.assertNotIn('instructions', workspace.kwargs)
        self.assertIn('{memory_dir}', MEMORY_INSTRUCTIONS)
        self.assertNotIn('uv pip', WORKSPACE_INSTRUCTIONS)

    async def test_effective_tool_and_host_guidance_has_no_mandatory_extra_steps(self):
        from analysis_extension.middleware import AnalysisMiddleware
        from numeric_tools import NumericToolsMiddleware
        middleware = AnalysisMiddleware(self.store, OWNER, frozenset(), AsyncMock(), delivery=self.delivery)
        prompt = await middleware.on_system_prompt(None, '业务任务')
        prompt = await NumericToolsMiddleware().on_system_prompt(None, prompt)
        for stale in ('排序用 SortRecords', '再调用 AnalysisRenderReport', '分析须有明确 workPlan',
                      '查询中的排序使用 SortRecords'):
            self.assertNotIn(stale, prompt)
        self.assertIn('仅用户明确下载或导出', prompt)
        self.assertIn('仅展示原值无需形式化 workPlan', prompt)
        self.assertIn('不先抄值逐项 Calculate 再 Build 重算', prompt)
        self.assertNotIn('先用 AnalysisInspect 发现', AnalysisBuild.description)
        from analysis_extension.tools import AnalysisPython
        self.assertIn('rows 必须是二维数组', AnalysisPython.description)
        self.assertIn('纯网格只传工具参数 gridInputs', AnalysisPython.description)
        self.assertIn('ref=inputs[0]["ref"]', AnalysisPython.description)
        self.assertIn('captureRefs+scope', AnalysisPython.description)
        self.assertNotIn('完整三字段', AnalysisPython.description)
        self.assertNotIn('代码分析已创建可发布的 analysisRef', AnalysisPython.description)
        self.assertIn('仅文件执行返回 pythonRunRef，不创建 analysisRef', AnalysisPython.description)

    async def test_display_without_plan_passes_work_check_and_declared_work_is_preserved(self):
        reference, selectors, scope = saved_fixture(self.store)
        built = await self.service.build([reference], selectors, scope, [], 'display')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '画图展示已有数据')))
        turn.task(mode='ANALYSIS', goal='展示原值', delivery='DOCUMENT')
        _, issues, _ = turn.data_issues(artifact)
        self.assertNotIn('WORK_INCOMPLETE', [item['code'] for item in issues])
        declared = await self.service.build([reference], selectors, scope, [], 'declared', workPlan=[
            {'taskId': 'pending', 'title': '必要计算', 'kind': 'COMPUTATION', 'refs': []}])
        rebuilt = await self.service.build(baseAnalysisRef=declared['analysisRef'], operations=[], workPlan=[], requestKey='keep')
        self.assertEqual(rebuilt['completion']['state'], 'INCOMPLETE')

    async def test_null_check_survives_schema_store_and_publication(self):
        reference, selectors, scope = mixed_fixture(self.store, 1)
        capture = self.store.get_capture(OWNER, reference)
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['data']['stepResults'][1]['formResults'][0]['page']['cells'][1].update(rawValueJson='null', displayValue='')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        reference = self.store.capture(OWNER, capture)
        selectors[0]['captureRef'] = scope['population']['captureRef'] = reference
        built = await self.service.build([reference], selectors, scope, [{'id': 'check', 'operator': 'FORMULA_CHECK',
            'select': {'cellKey': '2:1', 'objectId': 'object-0'}}], 'null', workPlan=[
                {'taskId': 'check', 'title': '核对', 'kind': 'COMPUTATION', 'operator': 'FORMULA_CHECK', 'refs': ['check']}])
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        validator().validate(artifact)
        self.assertEqual(built['completion']['state'], 'READY')
        published = await self.service.publish(built['analysisRef'], [], [
            {'viewId': 'values', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
             'valueRefs': [fact['factId'] for fact in artifact['facts']]},
            {'viewId': 'diagnostic', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': ['derived_check']}], 'publish')
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        forged = deepcopy(artifact)
        forged['derivedValues'][0]['value'] = '0'
        self.assertTrue(list(validator().iter_errors(forged)))

    async def test_final_tool_schema_describes_build_alias_and_publish_actual_refs(self):
        build_refs = AnalysisBuild.input_schema['properties']['workPlan']['items']['properties']['refs']['description']
        publish_refs = AnalysisPublish.input_schema['properties']['workRefs']['items']['properties']['refs']['description']
        self.assertIn('本计划运算 id', build_refs)
        self.assertIn('不使用计划内别名', publish_refs)
