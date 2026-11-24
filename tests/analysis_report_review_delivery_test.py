from analysis_extension.review_request import review_material
"""A real saved draft survives review unavailability without changing its data or HTML."""
from copy import deepcopy

from agentscope.message import UserMsg
from agentscope.state import AgentState
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.report_review import begin_review, save_review
from analysis_extension.store import AnalysisStore
from analysis_extension.turn_completion import TurnCompletion, current_turn, REVIEW_PROMPT
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
import analysis_closeout_recovery_test as recovery_fixtures
import analysis_source_boundary_test as source_fixtures


class ReportReviewDeliveryTests(PresentationCase):
    async def prepare(self):
        capture, selectors, scope = source_fixtures.SourceBoundaryTests.json_capture(self)
        built = await self.service.build([capture], selectors, scope, [], 'data')
        self.ref = built['analysisRef']
        artifact = self.store.get_analysis(OWNER, self.ref)
        request = {'analysisRef': self.ref, 'requestKey': 'preview', 'document': {'title': '分析',
                   'html': '<html><head></head><body><p>完整内容</p><p>{{value}}</p></body></html>',
                   'bindings': [{'key': 'value', 'valueRef': artifact['facts'][0]['factId']}]}}
        self.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '分析当前数据')))
        self.turn.task(mode='ANALYSIS', goal='分析当前数据', delivery='DOCUMENT')
        self.service.turn = self.turn
        recovery_fixtures.CloseoutRecoveryTests.observe(self, self.turn, 'AnalysisBuild', built)
        request['renderCheckUnavailableReason'] = '冻结状态测试不启动浏览器，页面测量单独回归。'
        return request

    async def partial_review(self, artifact):
        record = begin_review(self.turn, self.ref, 'content-1', ['{"facts":[]}', '{"calculation":2}'], REVIEW_PROMPT)
        record['parts'][0].update(status='PASS', issues=[])
        record['parts'][1].update(status='UNAVAILABLE', failure={'code': 'HOST_WAIT_TIMEOUT'})
        save_review(self.turn, record)
        raise AnalysisError('REVIEW_UNAVAILABLE', '测试注入审查超时')

    async def test_pending_report_preserves_full_content_reload_and_export(self):
        request = await self.prepare()
        before = deepcopy(self.store.get_analysis(OWNER, self.ref))
        self.service.preview_reviewer = self.partial_review
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'UNAVAILABLE')
        delivered = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        self.assertIsNotNone(delivered)
        self.assertEqual(delivered['reportDelivery']['mode'], 'PENDING_REVIEW')
        self.assertEqual(delivered['reportDelivery']['semanticReview'], 'PARTIAL')
        self.assertTrue(delivered['delivery']['registered'])
        saved = self.store.get_analysis(OWNER, self.ref)
        self.assertEqual(saved['facts'], before['facts'])
        self.assertEqual(saved['derivedValues'], before['derivedValues'])
        self.assertIn('内容', saved['freeDocument']['html'])
        recovery_fixtures.CloseoutRecoveryTests.observe(self, self.turn, 'AnalysisPublish', delivered)
        self.assertEqual(self.turn.check()['status'], 'PARTIAL')
        reopened = AnalysisStore(self.store.root)
        self.assertEqual(reopened.get_report_review(OWNER, delivered['reportDelivery']['reviewRef'])['completedPartCount'], 1)
        other = Owner('other', OWNER.agent_id, OWNER.session_id)
        self.assertIsNone(reopened.get_report_review(other, delivered['reportDelivery']['reviewRef']))
        view = self.delivery.view(delivered['delivery']['path'])
        self.assertEqual(view['reportDelivery']['semanticReview'], 'PARTIAL')
        export = self.delivery.render_report(self.ref, 'pending-export')
        report = self.delivery.report(self.ref, export['exportRef'])
        self.assertIn('AI 审核尚未全部完成', report['html'])
        self.assertEqual(self.delivery.restore(delivered['delivery']['path'])['freeDocument'], view['freeDocument'])
        record = reopened.get_report_review(OWNER, delivered['reportDelivery']['reviewRef'])
        record['parts'][1].update(status='REVISE', issues=[{'severity':'BLOCKING', 'message':'数值范围错误'}])
        save_review(self.turn, record)
        self.assertEqual(self.delivery.view(delivered['delivery']['path'])['reportDelivery']['mode'], 'REQUIRES_REVISION')
        revised = self.delivery.report(self.ref, export['exportRef'])
        self.assertIn('数值范围错误', revised['html'])
        self.assertNotIn('AI 审核尚未全部完成', revised['html'])

    async def test_known_issue_is_visible_and_never_certified_as_pending_or_pass(self):
        request = await self.prepare()
        async def blocked(artifact):
            return with_coverage({'issues': [{'code':'ANSWER_MISMATCH', 'severity':'BLOCKING', 'message':'金额错误'}]})
        self.service.preview_reviewer = blocked
        await self.service.publish(**request, previewOnly=True)
        result = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        self.assertEqual(result['reportDelivery']['mode'], 'REQUIRES_REVISION')
        self.assertEqual(result['reportDelivery']['semanticReview'], 'REVISE')
        self.assertIn('不能作为确定结果', result['reviewNotice'])
        recovery_fixtures.CloseoutRecoveryTests.observe(self, self.turn, 'AnalysisPublish', result)
        from analysis_extension.middleware import AnalysisMiddleware
        from types import SimpleNamespace
        middleware = object.__new__(AnalysisMiddleware)
        middleware.service = self.service
        ended = await middleware._check_end(SimpleNamespace(state=AgentState()))
        self.assertEqual(ended['status'], 'PARTIAL')
        self.assertEqual(ended['semanticReview'], 'REVISE')
        self.assertIn('不能作为确定结果', self.turn.state['hostCloseout']['answer'])

    async def test_changed_scope_or_question_cannot_reuse_candidate(self):
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        self.turn.state['request'] = '改查另一情景'
        self.assertIsNone(await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT'))

    async def test_host_closeout_keeps_current_report_with_explicit_blocking_findings(self):
        from analysis_extension.middleware import AnalysisMiddleware
        from analysis_extension.review_control import progress
        request = await self.prepare()
        async def blocked(artifact):
            return with_coverage({'issues': [{'code': 'ANSWER_MISMATCH', 'severity': 'BLOCKING',
                                'message': '合计与明细重复相加，当前合计不能作为确定结果。'}]})
        self.service.preview_reviewer = blocked
        await self.service.publish(**request, previewOnly=True)
        progress(self.turn, lambda state: state.update(lastReviewBlocked=True))
        middleware = object.__new__(AnalysisMiddleware)
        middleware.service = self.service
        answer = await middleware._closeout('REPAIR_TIME_LIMIT')
        result = self.turn.state['hostCloseout']
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['semanticReview'], 'REVISE')
        self.assertEqual(result['reportDelivery']['mode'], 'REQUIRES_REVISION')
        self.assertIn('不能作为确定结果', answer)
        self.assertNotEqual(result.get('deliveryStatus'), 'FACT_SUMMARY')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertIn('完整内容', artifact['freeDocument']['html'])
        exported = self.delivery.render_report(result['analysisRef'], 'known-issue-closeout')
        self.assertIn('合计与明细重复相加', self.delivery.report(result['analysisRef'], exported['exportRef'])['html'])

    async def test_current_content_is_retained_when_its_review_times_out(self):
        request = await self.prepare()
        async def approved(artifact): return with_coverage({'issues': []})
        self.service.preview_reviewer = approved
        first = await self.service.publish(**request, previewOnly=True)
        self.service.preview_reviewer = self.partial_review
        request['document']['html'] = request['document']['html'].replace('完整内容', '后来修改的内容')
        request['requestKey'] = 'preview-second'
        latest = await self.service.publish(**request, previewOnly=True)
        result = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        self.assertEqual(result['reportDelivery']['mode'], 'PENDING_REVIEW')
        self.assertEqual(result['reportDelivery']['selectedPreviewRef'], latest['repairRef'])
        self.assertNotEqual(latest['repairRef'], first['repairRef'])
        self.assertEqual(result['reportDelivery']['semanticReview'], 'PARTIAL')
        self.assertNotIn('newerCandidate', result['reportDelivery'])
        exported = self.delivery.render_report(self.ref, 'current-status')
        html = self.delivery.report(self.ref, exported['exportRef'])['html']
        self.assertIn('后来修改的内容', html)
        self.assertIn('调用未完成', html)
        self.assertNotIn('报告版本：', html)

    async def test_resume_only_submits_missing_frozen_part_and_does_not_change_document(self):
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        result = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        before = deepcopy(self.store.get_analysis(OWNER, self.ref))
        calls = []
        async def reviewer(prompt, payload):
            calls.append((prompt, payload)); return with_coverage({'issues': []})
        self.service.material_reviewer = reviewer
        self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '继续审查上次报告')))
        updated = await self.service.publish(analysisRef=self.ref, requestKey='resume', resumeReview=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], REVIEW_PROMPT)
        self.assertEqual(review_material(calls[0][1]), {'calculation': 2})
        self.assertTrue(calls[0][1].markdown.startswith('# '))
        self.assertEqual(updated['reportDelivery']['semanticReview'], 'PASS')
        self.assertEqual(updated['reportDelivery']['mode'], 'REVIEWED_AFTER_RESUME')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), before)
        self.assertEqual(self.delivery.view(result['delivery']['path'])['reportDelivery']['semanticReview'], 'PASS')
        await self.service.publish(analysisRef=self.ref, requestKey='resume-again', resumeReview=True)
        self.assertEqual(len(calls), 1)

    async def test_view_resume_and_fresh_business_review_have_distinct_costs_and_goals(self):
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        calls = []
        async def reviewer(prompt, payload):
            calls.append(payload)
            return with_coverage({'issues': []})
        self.service.material_reviewer = reviewer
        self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '继续未完成的审查')))
        for n, action in enumerate(('VIEW', 'RESUME', 'RESUME')):
            await self.service.publish(analysisRef=self.ref, requestKey=str(n), reviewAction=action)
        self.assertEqual(len(calls), 1)
        for goal in ('复核整篇报告的趋势', '复核全年金额'):
            self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', goal)))
            before = len(calls)
            await self.service.publish(analysisRef=self.ref, requestKey='fresh', reviewAction='RECHECK')
            fresh = calls[before:]
            self.assertEqual(len(fresh), 2)
            for payload in fresh:
                self.assertEqual(review_material(payload)['businessReviewRequest'], goal)
                self.assertIn(goal, payload.markdown)
                self.assertEqual(payload.markdown.count('本次复核要求'), 1)
            await self.service.publish(analysisRef=self.ref, requestKey='retry', reviewAction='RECHECK')
            self.assertEqual(len(calls), before + 2)

    async def test_view_of_another_stored_revision_cannot_deliver_a_recheck_of_the_first(self):
        from analysis_extension.review_control import progress
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        async def reviewer(prompt, payload):
            return with_coverage({'issues': []})
        self.service.material_reviewer = reviewer
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '复核已交付报告')))
        self.service.turn = turn
        turn.task(mode='REPORT', goal='复核已交付报告', delivery='DOCUMENT')
        turn.state['adoptedReportRef'] = self.ref
        sibling = self.ref.partition(':')[0] + ':99'
        self.assertIsNone(turn.review_target_issue(sibling))
        for action in ('RECHECK', 'VIEW', 'RESUME'):
            result = await self.service.publish(analysisRef=self.ref, requestKey=action, reviewAction=action)
            self.assertEqual(result['reviewAction'], action)
        saved = progress(turn)['resumedReportRefs'][self.ref]
        self.assertEqual(saved['action'], 'RECHECK')
        self.assertIsNone(turn.review_target_issue(self.ref))
        self.assertIsNone(turn.review_target_issue('another-report:1'))
        for action in ('VIEW', 'RESUME', 'RECHECK'):
            progress(turn, lambda state: state['resumedReportRefs'].update({sibling: {'action': action}}))
            if action == 'RECHECK':
                self.assertIsNone(turn.review_target_issue(sibling))
            else:
                finding, = turn.check(analysis_ref=sibling)['issues']
                self.assertEqual(finding['code'], 'REPORT_REVIEW_TARGET_MISMATCH')
                self.assertEqual(finding['reviewedAnalysisRefs'], [self.ref])
                self.assertEqual(finding['deliveryAnalysisRef'], sibling)
        progress(turn, lambda state: state['resumedReportRefs'].pop(sibling))
        progress(turn, lambda state: state.setdefault('presentationRevisionRefs', {}).update({sibling: self.ref}))
        self.assertIsNone(turn.review_target_issue(sibling))

    async def test_semantically_same_material_has_distinct_frozen_bytes(self):
        await self.prepare()
        first = begin_review(self.turn, self.ref, 'first', ['{"documentText":"same text","reviewHistory":[]}'], 'prompt')
        second = begin_review(self.turn, self.ref, 'second', ['{"documentText":"same   text","reviewHistory":["prior"]}'], 'prompt')
        a, b = first['parts'][0], second['parts'][0]
        self.assertEqual(a['key'], b['key'])
        self.assertNotEqual(a['materialRef'], b['materialRef'])
        self.assertNotEqual(self.store.get_review_material(OWNER, a['materialRef']), self.store.get_review_material(OWNER, b['materialRef']))

    async def test_reviewed_report_in_report_mode_registers_the_real_export_before_finishing(self):
        from analysis_extension.middleware import AnalysisMiddleware
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '复核刚才报告')))
        self.service.turn = turn
        turn.task(mode='REPORT', goal='复核已有报告', delivery='DOCUMENT')
        turn.state['adoptedReportRef'] = self.ref
        middleware = object.__new__(AnalysisMiddleware)
        middleware.service = self.service
        async def reviewer(prompt, payload):
            return with_coverage({'issues': []})
        self.service.material_reviewer = reviewer
        result = await self.service.publish(analysisRef=self.ref, requestKey='fresh-review', reviewAction='RECHECK')
        recovery_fixtures.CloseoutRecoveryTests.observe(self, turn, 'AnalysisPublish', result)
        self.assertEqual([i['code'] for i in turn.check()['issues']], ['REPORT_NOT_DELIVERED'])
        await middleware._register_reviewed_export()
        report = turn.registered_export(self.ref)
        self.assertTrue(report and self.delivery.report(self.ref, report['exportRef'])['html'])
        self.assertEqual(turn.check()['status'], 'READY')
        self.assertIn(report['markdown'], middleware._approved_delivery())
        self.assertEqual(turn.state['hostCloseout']['semanticReview'], 'PASS')
        self.assertEqual(turn.state['buildRefs'], [])

    async def test_resumed_review_keeps_full_report_when_new_turn_times_out_and_recovers(self):
        import json
        from unittest.mock import AsyncMock, patch
        from agentscope.message import ToolCallBlock, TextBlock
        from agentscope.tool import ToolResponse
        from analysis_extension.middleware import AnalysisMiddleware
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        frozen = deepcopy(self.store.get_analysis(OWNER, self.ref))
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '只继续审核原报告')))
        self.service.turn = turn
        turn.task(mode='ANALYSIS', goal='只继续审核原报告', delivery='DOCUMENT')
        before_call = deepcopy(turn.state)
        self.service.material_reviewer = AsyncMock(side_effect=AnalysisError('REVIEW_UNAVAILABLE', '续审超时'))
        result = await self.service.publish(analysisRef=self.ref, requestKey='resume-timeout', resumeReview=True)
        call = ToolCallBlock(id='resume-background', name='AnalysisPublish', input=json.dumps(
            {'analysisRef': self.ref, 'requestKey': 'resume-timeout', 'resumeReview': True}))
        turn.record_tool_result(call, ToolResponse(id=call.id, content=[TextBlock(text=json.dumps(result))]), turn.start_tool(), 0)
        recovered = TurnCompletion(self.service, before_call)
        self.service.turn = recovered
        middleware = object.__new__(AnalysisMiddleware)
        middleware.service = self.service
        with patch('analysis_extension.model_closeout.model_closeout', AsyncMock(side_effect=AssertionError('Do not replace an explicitly resumed report with a source summary'))):
            answer = await middleware._closeout('REVIEW_LIMIT')
        status = recovered.state['hostCloseout']
        self.assertEqual(status['analysisRef'], self.ref)
        self.assertEqual(status['status'], 'PARTIAL')
        self.assertEqual(status['reportDelivery']['mode'], 'PENDING_REVIEW')
        self.assertIn(result['delivery']['markdown'], answer)
        self.assertEqual(recovered.state['buildRefs'], [])
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), frozen)
        other = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '开始另一个分析')))
        self.service.turn = other
        other.task(mode='ANALYSIS', goal='另一个分析', delivery='DOCUMENT')
        other.state['analysisRef'] = self.ref
        self.assertEqual(other.check()['issues'][0]['code'], 'ANALYSIS_NOT_BUILT')

    async def test_budget_exhausted_before_first_review_still_freezes_resumable_report(self):
        import json
        from unittest.mock import AsyncMock
        from analysis_extension.review_control import request_closeout
        request = await self.prepare()
        self.service.preview_reviewer = AsyncMock(side_effect=AssertionError('No review call after closeout'))
        request_closeout(self.turn, 'REPAIR_TIME_LIMIT')
        preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'UNAVAILABLE')
        self.service.preview_reviewer.assert_not_awaited()
        delivered = await self.service.publish_available_report(self.ref, 'REPAIR_TIME_LIMIT')
        self.assertEqual(delivered['reportDelivery']['mode'], 'PENDING_REVIEW')
        record = self.store.get_report_review(OWNER, delivered['reportDelivery']['reviewRef'])
        self.assertEqual(record['status'], 'PENDING')
        materials = [self.store.get_review_material(OWNER, part['materialRef']) for part in record['parts']]
        self.assertTrue(materials)
        self.assertIn('完整内容', materials[0]['payload'].markdown)
        self.assertIn('分析当前数据', materials[0]['payload'].markdown)
        self.assertTrue(review_material(materials[0]['payload'])['facts'])
        frozen = deepcopy(self.store.get_analysis(OWNER, self.ref))
        self.service.material_reviewer = AsyncMock(return_value=with_coverage({'issues': []}))
        self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '只继续审查')))
        resumed = await self.service.publish(analysisRef=self.ref, requestKey='resume-after-limit', resumeReview=True)
        self.assertEqual(resumed['reportDelivery']['semanticReview'], 'PASS')
        self.assertEqual(self.service.material_reviewer.await_args_list[0].args,
                         (materials[0]['prompt'], materials[0]['payload']))
        self.assertEqual(self.service.material_reviewer.await_count, len(materials))
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), frozen)

    async def test_maintenance_retains_published_review_history_and_resumable_inputs(self):
        import os, time
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        delivered = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        review_ref = delivered['reportDelivery']['reviewRef']
        before = self.store.get_report_review(OWNER, review_ref)
        materials = {part['materialRef']: self.store.get_review_material(OWNER, part['materialRef']) for part in before['parts']}
        old = time.time() - self.store.limits.retention_seconds - 100
        for path in self.store.blobs.glob('*.json'): os.utime(path, (old, old))
        self.store.collect_expired()
        reopened = AnalysisStore(self.store.root)
        self.assertEqual(reopened.get_report_review(OWNER, review_ref), before)
        for ref, value in materials.items(): self.assertEqual(reopened.get_review_material(OWNER, ref), value)
        self.assertEqual(self.delivery.view(delivered['delivery']['path'])['reportDelivery']['semanticReview'], 'PARTIAL')

    async def test_deferred_preparation_failure_cannot_borrow_prior_document_review(self):
        from unittest.mock import patch
        from analysis_extension.review_control import request_closeout
        request = await self.prepare()
        self.service.preview_reviewer = self.partial_review
        await self.service.publish(**request, previewOnly=True)
        previous_ref = self.turn.state['currentReportReviewRef']
        request_closeout(self.turn, 'REPAIR_TIME_LIMIT')
        request['document']['html'] = request['document']['html'].replace('完整内容', '新候选内容')
        request['requestKey'] = 'capacity-candidate'
        with patch('analysis_extension.turn_completion.analysis_review_payloads',
                   side_effect=AnalysisError('REVIEW_CAPACITY', '完整材料超过处理容量')):
            preview = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'UNAVAILABLE')
        self.assertEqual(preview['diagnostics']['semanticReview']['error']['code'], 'REVIEW_CAPACITY')
        self.assertNotEqual(self.turn.state['currentReportReviewRef'], previous_ref)
        record = self.store.get_report_review(OWNER, self.turn.state['currentReportReviewRef'])
        self.assertEqual(record['completedPartCount'], 0)

    def test_resume_schema_forbids_replacing_report_while_resuming(self):
        from jsonschema import validate, ValidationError
        from analysis_extension.tools import AnalysisPublish
        request = {'analysisRef': 'a', 'requestKey': 'resume', 'resumeReview': True}
        validate(request, AnalysisPublish.input_schema)
        with self.assertRaises(ValidationError):
            validate({**request, 'document': {'title': 't', 'html': '<p>x</p>', 'bindings': []}}, AnalysisPublish.input_schema)

    async def test_current_candidate_preserves_report_and_source_findings_without_old_fallback(self):
        request = await self.prepare()
        async def approved(artifact): return with_coverage({'issues': []})
        self.service.preview_reviewer = approved
        first = await self.service.publish(**request, previewOnly=True)
        async def report_error(artifact):
            return with_coverage({'issues': [{'code': 'ANSWER_MISMATCH', 'severity': 'BLOCKING', 'impact': 'REPORT',
                               'statement': '新增错误断言', 'message': '新增句子与已有数值矛盾'}]})
        self.service.preview_reviewer = report_error
        request['document']['html'] = request['document']['html'].replace('完整内容', '新增错误断言')
        request['requestKey'] = 'new-report-error'
        latest = await self.service.publish(**request, previewOnly=True)
        from analysis_extension.report_review import choose_candidate
        self.assertEqual(choose_candidate(self.service, self.store.get_analysis(OWNER, self.ref))['previewRef'], latest['repairRef'])
        async def source_error(artifact):
            return with_coverage({'issues': [{'code': 'ANSWER_MISMATCH', 'severity': 'BLOCKING', 'impact': 'SOURCE',
                               'statement': '新增错误断言', 'message': '来源范围错误也影响旧报告'}]})
        self.service.preview_reviewer = source_error
        request['requestKey'] = 'new-source-error'
        await self.service.publish(**request, previewOnly=True)
        self.assertEqual(choose_candidate(self.service, self.store.get_analysis(OWNER, self.ref))['mode'], 'REQUIRES_REVISION')
        from analysis_extension.review_control import progress
        progress(self.turn, lambda state: state['publicationCandidates'][-1].update(programChecks='FAILED'))
        self.assertIsNone(choose_candidate(self.service, self.store.get_analysis(OWNER, self.ref)),
                          'Presentation failure cannot erase a known shared-source problem')

from analysis_review_fixtures import with_coverage
