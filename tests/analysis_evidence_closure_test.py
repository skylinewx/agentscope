"""来源凭据增量登记、局部事实完整性与交付前检查的回归。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types
from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters.budget_verification import verification
from analysis_extension.middleware import AnalysisMiddleware
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn
from agentscope.app._service._resources import _LeasedTool
from agentscope.message import UserMsg
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.tool._adapters import MCPTool


class EvidenceClosureTests(PresentationCase):
    async def test_protocol_and_tool_schema_agree_on_local_formula_default(self):
        from analysis_extension.tools import OPERATION
        protocol = await self.service.inspect(section='protocol')
        self.assertEqual(protocol['formulaCheck']['default'], 'LOCAL')
        self.assertEqual(OPERATION['properties']['checkScope']['default'], protocol['formulaCheck']['default'])

    def make_capture(self, index=0, connected=False, partial=False):
        capture, selectors, _ = fixture(values=('10',))
        # 每次执行保留自己的对象和观察，不能把不同观察伪装成同一事实。
        serialized = json.dumps(capture).replace('object-0', f'object-{index}').replace(
            'observation-0', f'observation-{index}').replace('execution-A', f'execution-{index}')
        capture = json.loads(serialized)
        if connected:
            capture.update(serverName='budget-source', connectionFingerprint='authorized-connection')
        if partial:
            receipt = json.loads(capture['response']['content'][0]['text'])
            receipt['status'] = 'PARTIAL'
            receipt['completeness']['enumerationExhausted'] = False
            page = receipt['data']['stepResults'][1]['formResults'][0]['page']
            page.update(totalRows=8, nextCursor='next-page', observationExhausted=False)
            capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        selectors[0]['captureRef'] = ref
        return ref, selectors[0]

    def verify_capture(self, source_ref, status='VALID', context_change=None):
        capture = self.store.get_capture(OWNER, source_ref)
        receipt = json.loads(capture['response']['content'][0]['text'])
        execution = receipt['executionId']
        capture['originalToolName'] = 'validate_evidence'
        capture['request'] = {'action': 'verify', 'executionId': execution,
            'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId']}
        receipt.update(executionId='verify-' + execution, status='SUCCEEDED', data={
            'action': 'verify', 'executionId': execution,
            'verification': {'status': status, 'evidenceIds': [item['evidenceId'] for item in receipt['evidence']]}})
        if context_change:
            receipt['context'].update(context_change)
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        return self.store.capture(OWNER, capture)

    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '分析所选对象')))
        turn.task(mode='ANALYSIS', goal='分析所选对象', delivery='DOCUMENT')
        self.service.turn = turn
        return turn

    async def build_many(self, count=6, connected=False, partial=False):
        captures, selections = zip(*(self.make_capture(index, connected, partial) for index in range(count)))
        scope = {'label': '所选对象', 'selectionReason': '用户选择', 'members': [f'object-{index}' for index in range(count)],
                 'memberDimension': 'MD_ORG', 'expectedDimensions': {'DATATIME': '2026N0001'}}
        built = await self.service.build(list(captures), list(selections), scope, [], 'selected')
        return self.store.get_analysis(OWNER, built['analysisRef'])

    async def test_original_authorized_receipt_avoids_remote_verification(self):
        artifact = await self.build_many(2, connected=True, partial=True)
        self.assertEqual(artifact['receiptVerification'], 'VALID')
        self.assertTrue(artifact['scope']['selectedDataComplete'])
        self.assertFalse(artifact['scope']['sourcePagesComplete'])
        self.assertFalse(artifact['scope']['populationComplete'])
        self.assertEqual(self.service._built(artifact)['verificationTaskCount'], 0)
        self.assertEqual({record['basis'] for record in artifact['verificationRecords']}, {'CAPTURE_INTEGRITY'})

    async def test_four_valid_receipts_leave_only_two_tasks_and_survive_rebuild(self):
        turn = self.turn()
        artifact = await self.build_many()
        turn.state['queryCaptureRefs'] = [self.verify_capture(ref) for ref in artifact['captureRefs'][:4]]
        progress = self.service._with_captured_verification(artifact)
        self.assertEqual(progress['receiptVerification'], 'UNVERIFIED')
        self.assertEqual(len(progress['verificationRecords']), 4)
        self.assertEqual({task['executionId'] for task in self.service._built(progress)['verificationTasks']},
                         {'execution-4', 'execution-5'})
        rebuilt = await self.service.build(artifact['captureRefs'], artifact['plan']['selections'],
            artifact['plan']['scope'], [], 'continue', baseAnalysisRef=progress['analysisRef'])
        saved = self.store.get_analysis(OWNER, rebuilt['analysisRef'])
        self.assertEqual(len(saved['verificationRecords']), 4)
        self.assertEqual(rebuilt['verificationTaskCount'], 2)

    async def test_other_snapshot_verification_does_not_satisfy_current_fact(self):
        turn = self.turn()
        artifact = await self.build_many(1)
        turn.state['queryCaptureRefs'] = [self.verify_capture(artifact['captureRefs'][0], context_change={'snapshotId': 'other'})]
        self.assertEqual(self.service._built(artifact)['verificationTaskCount'], 1)

    async def test_remote_invalid_overrides_locally_trusted_capture(self):
        turn = self.turn()
        artifact = await self.build_many(1, connected=True)
        turn.state['queryCaptureRefs'] = [self.verify_capture(artifact['captureRefs'][0], 'INVALID')]
        effective = self.service.effective_evidence(artifact)
        self.assertEqual(effective['receiptVerification'], 'INVALID')
        self.assertEqual(effective['facts'][0]['valueState'], 'CONFLICT')
        self.assertEqual(self.store.get_analysis(OWNER, artifact['analysisRef'])['receiptVerification'], 'VALID')

    async def test_missing_work_is_rejected_before_expensive_preview_review(self):
        turn = self.turn()
        artifact = await self.build_many(1, connected=True)
        built = await self.service.build(baseAnalysisRef=artifact['analysisRef'], operations=[],
            requestKey='declare-pending-work', workPlan=[{
                'taskId': 'selected-values', 'title': '尚待登记的所选读数', 'kind': 'VALUES', 'refs': []}])
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(artifact['analysisRef'], requestKey='premature',
                previewOnly=True, document={'title': '结果', 'html': '<html><head></head><body>{{value}}</body></html>',
                    'bindings': [{'key': 'value', 'valueRef': artifact['facts'][0]['factId']}]})
        self.assertIn('WORK_INCOMPLETE', str(caught.exception.details))
        self.service.preview_reviewer.assert_not_awaited()

    async def test_pending_tasks_use_evidence_difference_not_whole_execution(self):
        artifact = await self.build_many(1)
        captures = {ref: self.store.get_capture(OWNER, ref) for ref in artifact['captureRefs']}
        ref = self.verify_capture(artifact['captureRefs'][0])
        captures[ref] = self.store.get_capture(OWNER, ref)
        facts = deepcopy(artifact['facts'])
        facts[0]['evidenceIds'].append('new-evidence')
        artifact.update(facts=facts, captureRefs=list(captures))
        tasks = self.service.pending_verifications(artifact)
        self.assertEqual(tasks[0]['evidenceIds'], ['new-evidence'])

    async def test_explicit_followup_verification_does_not_use_local_capture_as_remote_check(self):
        artifact = await self.build_many(1, connected=True)
        captures = {ref: self.store.get_capture(OWNER, ref) for ref in artifact['captureRefs']}
        state, _ = verification(artifact['facts'], captures, verifier_refs=set())
        self.assertEqual(state, 'UNVERIFIED')

    def remote_runner(self, turn, artifact, behavior=PermissionBehavior.ALLOW, fingerprint='authorized-connection'):
        """使用真实 MCP 适配、工具箱和回执捕获，只替换远端传输与权限决定。"""
        sent = []
        source = self.store.get_capture(OWNER, artifact['captureRefs'][0])
        receipt = json.loads(source['response']['content'][0]['text'])

        async def remote_call(name, arguments, **kwargs):
            sent.append(arguments)
            verified = {**receipt, 'executionId': 'verification-execution', 'data': {
                'action': 'verify', 'executionId': arguments['executionId'],
                'verification': {'status': 'VALID', 'evidenceIds': arguments['evidenceIds']}}}
            return mcp.types.CallToolResult(content=[mcp.types.TextContent(type='text', text=json.dumps(verified))])

        tool = MCPTool('budget-source', mcp.types.Tool(name='validate_evidence', inputSchema={
            'type': 'object', 'properties': {'contractVersion': {'const': '2.0'}}, 'required': ['contractVersion']},
            annotations=mcp.types.ToolAnnotations(readOnlyHint=True),
            _meta={'version': '2.0', 'exactContractResource': 'semantic://contracts/external-query'}),
            session=SimpleNamespace(call_tool=remote_call))
        leased = _LeasedTool(tool, SimpleNamespace(resource_id=source['resourceId'], fingerprint=fingerprint,
                                                  users=1, release=AsyncMock()))
        agent = SimpleNamespace(toolkit=Toolkit(tools=[leased]), state=AgentState(),
            _check_permission=AsyncMock(return_value=PermissionDecision(behavior=behavior, message='测试权限')))
        middleware = AnalysisMiddleware(self.store, OWNER, [source['resourceId']], AsyncMock())
        middleware.service = self.service
        self.service.verification_runner = lambda tasks: middleware._verify_sources(agent, turn, tasks)
        # 模拟显式跟进必须取得远程证明；成功后凭据只从宿主已捕获列表中读取。
        self.service._verifier_refs = lambda unused: set(turn.state.get('queryCaptureRefs', []))
        return agent, sent

    async def test_host_remote_verification_is_captured_and_not_repeated(self):
        turn = self.turn()
        artifact = await self.build_many(1, connected=True)
        agent, sent = self.remote_runner(turn, artifact)
        checked = await self.service.ensure_verification(artifact)
        self.assertEqual(checked['receiptVerification'], 'VALID')
        self.assertEqual(checked['verificationRecords'][0]['basis'], 'REMOTE_VERIFICATION')
        self.assertEqual(len(turn.state['queryCaptureRefs']), 1)
        await self.service.ensure_verification(artifact)
        self.assertEqual(len(sent), 1)
        agent._check_permission.assert_awaited_once()
        self.assertEqual(sent[0]['executionId'], 'execution-0')

    async def test_host_remote_verification_respects_permission_and_source_connection(self):
        turn = self.turn()
        artifact = await self.build_many(1, connected=True)
        for behavior in (PermissionBehavior.DENY, PermissionBehavior.ASK):
            agent, sent = self.remote_runner(turn, artifact, behavior=behavior)
            with self.assertRaises(AnalysisError) as caught:
                await self.service.ensure_verification(artifact)
            self.assertEqual(caught.exception.code, 'VERIFICATION_PERMISSION_REQUIRED')
            self.assertEqual(sent, [])
            agent._check_permission.assert_awaited_once()
        agent, sent = self.remote_runner(turn, artifact, fingerprint='another-connection')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.ensure_verification(artifact)
        self.assertEqual(caught.exception.code, 'VERIFICATION_TOOL_UNAVAILABLE')
        agent._check_permission.assert_not_awaited()
        self.assertEqual(sent, [])

    async def test_closeout_preserves_evidence_failure_instead_of_reporting_review_failure(self):
        turn = self.turn()
        artifact = await self.build_many(1, connected=True)
        turn.state['analysisRef'] = artifact['analysisRef']
        self.service.ensure_verification = AsyncMock(side_effect=AnalysisError(
            'EVIDENCE_INVALID', '所选数据的来源不一致。', {'executionId': 'execution-0'}))
        middleware = AnalysisMiddleware(self.store, OWNER, [], AsyncMock())
        middleware.service = self.service
        answer = await middleware._closeout('REPAIR_LIMIT')
        status = turn.state['hostCloseout']
        self.assertEqual(status['semanticReview'], 'NOT_REVIEWED')
        self.assertEqual(status['reason'], 'EVIDENCE_INVALID')
        self.assertEqual(status['issues'][0]['details']['executionId'], 'execution-0')
        self.assertIn('来源不一致', answer)
