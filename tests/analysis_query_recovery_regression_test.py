"""真实退化路径回归：追问标识笔误、原生聚合及已取得核验的交付。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import analysis_budget_batch_test as batch_fixtures
import analysis_capture_test as capture_fixtures
from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.capture import ReceiptCapture
from analysis_extension.followup_scope import prepare_request, remember_subject
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion
from agentscope.message import ToolCallBlock, ToolResultState


BINDING = 'urn:example:budget:source/source-A/resource/BUDGET_TASK_FORM_BINDING/task%2Fform'
MISTYPED = BINDING.replace('source/', 'source:', 1)


class QueryReferenceRecoveryTests(PresentationCase):
    def setUp(self):
        super().setUp()
        capture, selection, _, receipt = batch_fixtures.batch_fixture(False)
        capture = json.loads(json.dumps(capture).replace('binding-A', BINDING))
        receipt = json.loads(json.dumps(receipt).replace('binding-A', BINDING))
        batch_fixtures.update(capture, receipt)
        capture['turnKey'] = 'first'
        self.capture = capture
        self.ref = self.store.capture(OWNER, capture)
        previous = {'turnKey': 'first', 'mode': 'DIRECT', 'queryCaptureRefs': [self.ref],
                    'queryIntent': {'population': '所有单位', 'period': '2026年'}}
        remember_subject(TurnCompletion(self.service, previous), {'status': 'DIRECT'})
        self.turn = TurnCompletion(self.service, {'turnKey': 'next', 'request': '按板块分析',
            'buildRefs': [], 'inheritedScope': previous['resolvedSubjectScope'],
            'previousQueryIntent': previous['queryIntent']})
        self.service.turn = self.turn
        self.task = self.turn.task(mode='ANALYSIS', goal='按板块分析', intent={'grouping': '板块'})

    def test_task_returns_complete_source_grouping_arguments_without_rewriting_identifier(self):
        anchor = self.task['queryAnchors'][0]
        arguments = anchor['discoverGroupingArguments']
        self.assertEqual(anchor['bindingIri'], BINDING)
        self.assertEqual(arguments['bindingIri'], BINDING)
        self.assertEqual(arguments['groupingKeyword'], '板块')
        self.assertEqual(arguments['contextId'], self.capture['request']['contextId'])
        self.assertEqual(arguments['querySessionId'], self.capture['request']['querySessionId'])
        self.assertEqual(prepare_request(self.turn, {**self.capture, 'originalToolName': 'discover_query_targets'}, arguments), arguments)

    def test_mistyped_known_reference_has_exact_retry_arguments_not_grouping_guesses(self):
        request = deepcopy(self.capture['request'])
        request['plan']['steps'][0]['resourceIris'] = [MISTYPED]
        request['plan']['steps'][1]['batchQuery']['metrics'][0]['bindingIri'] = MISTYPED
        before = deepcopy(request)
        with self.assertRaises(AnalysisError) as caught:
            prepare_request(self.turn, self.capture, request)
        self.assertEqual(caught.exception.code, 'QUERY_REFERENCE_MISMATCH')
        expected = json.loads(json.dumps(request).replace(MISTYPED, BINDING))
        self.assertEqual(caught.exception.details['retryArguments'], expected)
        self.assertEqual(len(caught.exception.details['replacements']), 2)
        self.assertEqual(request, before)
        self.assertEqual(prepare_request(self.turn, self.capture, expected), expected)

    def test_reference_suggestions_do_not_cross_source_context_session_or_task(self):
        arguments = deepcopy(self.task['queryAnchors'][0]['discoverGroupingArguments'])
        arguments['bindingIri'] = MISTYPED
        origin = {**self.capture, 'originalToolName': 'discover_query_targets'}
        for changed_origin, changed_request in [({**origin, 'resourceId': 'other'}, arguments),
            (origin, {**arguments, 'contextId': 'other'}), (origin, {**arguments, 'querySessionId': 'other'}),
            (origin, {**arguments, 'bindingIri': MISTYPED + '-different-resource'})]:
            with self.subTest(request=changed_request, origin=changed_origin.get('resourceId')):
                self.assertEqual(prepare_request(self.turn, changed_origin, changed_request), changed_request)
        self.turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['task']}
        self.assertEqual(prepare_request(self.turn, origin, arguments), arguments)
        self.turn.state['followupContext'] = {'mode': 'NEW'}
        self.assertEqual(prepare_request(self.turn, origin, arguments), arguments)

    async def test_preflight_returns_actionable_tool_error_without_external_call(self):
        tool = capture_fixtures.CaptureTest.make_tool(self, '{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, OWNER, frozenset({'registered-budget'}), AsyncMock())
        capture.turn_key_provider = lambda: 'next'
        def reject(origin, request):
            raise AnalysisError('QUERY_REFERENCE_MISMATCH', '修复标识', {'retryArguments': {'bindingIri': BINDING}})
        capture.request_preparer = reject
        executed = []
        async def execute(**kwargs):
            executed.append(kwargs)
            yield None
        call = ToolCallBlock(id='typo', name=tool.name, input='{"contractVersion":"2.0"}')
        responses = [item async for item in capture.run(agent, {'tool_call': call}, execute)]
        self.assertEqual(executed, [])
        self.assertEqual(responses[0].state, ToolResultState.ERROR)
        self.assertEqual(responses[0].metadata['analysisTurnKey'], 'next')
        self.assertEqual(json.loads(responses[0].content[0].text)['error']['details']['retryArguments']['bindingIri'], BINDING)


class CapturedVerificationRecoveryTests(PresentationCase):
    async def setup_draft(self):
        capture, selections, scope = fixture(('7', '9'))
        self.data_capture = capture
        self.data_receipt = json.loads(capture['response']['content'][0]['text'])
        ref = self.store.capture(OWNER, capture)
        for selection in selections:
            selection['captureRef'] = ref
        scope['population']['captureRef'] = ref
        self.turn = TurnCompletion(self.service, {'turnKey': 'analysis', 'request': '分析', 'buildRefs': []})
        self.service.turn = self.turn
        self.turn.task(mode='ANALYSIS', goal='分析已查得数据')
        built = await self.service.build([ref], selections, scope, [{'id': 'n', 'operator': 'COUNT'}], 'build',
            workPlan=[{'taskId': 'count', 'title': '所选对象数量', 'kind': 'COMPUTATION', 'refs': ['n']}])
        self.artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.turn.state.update(analysisRef=built['analysisRef'], buildRefs=[built['analysisRef']])
        self.view = {'viewId': 'details', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
                     'valueRefs': [fact['factId'] for fact in self.artifact['facts']]}
        return built

    def verifier(self, status='VALID', source=None, context=None, execution=None, session=None, evidence=None):
        receipt = deepcopy(self.data_receipt)
        receipt['context'].update(context or {})
        receipt['querySessionId'] = session or receipt['querySessionId']
        target = execution or receipt['executionId']
        receipt['data'] = {'action': 'verify', 'executionId': target,
            'verification': {'status': status, 'evidenceIds': evidence if evidence is not None else
                             [item['evidenceId'] for item in receipt['evidence']]}}
        capture = deepcopy(self.data_capture)
        capture.update(originalToolName='validate_evidence', resourceId=source or capture['resourceId'],
                       request={'action': 'verify', 'executionId': target},
                       response={'content': [{'type': 'text', 'text': json.dumps(receipt)}]})
        ref = self.store.capture(OWNER, capture)
        self.turn._add_query_capture(ref)
        return ref

    async def test_preview_carries_existing_verification_and_publishes_without_rebuild_or_calculation(self):
        built = await self.setup_draft()
        verifier = self.verifier()
        with patch('analysis_extension.service.calculate', side_effect=AssertionError('不应重新计算')):
            preview = await self.service.publish(built['analysisRef'], [], [self.view], 'preview',  previewOnly=True)
        self.assertNotEqual(preview['analysisRef'], built['analysisRef'])
        verified = self.store.get_analysis(OWNER, preview['analysisRef'])
        self.assertEqual(verified['receiptVerification'], 'VALID')
        self.assertIn(verifier, verified['captureRefs'])
        self.assertEqual(verified['baseAnalysisRef'], built['analysisRef'])
        for field in ('facts', 'derivedValues', 'plan', 'scope'):
            self.assertEqual(verified[field], self.artifact[field])
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef']), self.artifact)
        repeat = self.service._with_captured_verification(self.artifact)
        self.assertEqual(repeat['analysisRef'], verified['analysisRef'])
        published = await self.service.publish(verified['analysisRef'], requestKey='confirm',
             repairRef=preview['repairRef'], repairs=[])
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        self.assertEqual(self.store.get_analysis(OWNER, published['analysisRef'])['receiptVerification'], 'VALID')

    async def test_other_source_snapshot_execution_session_and_incomplete_evidence_do_not_verify(self):
        await self.setup_draft()
        for options in ({'source': 'other'}, {'context': {'snapshotId': 'other'}},
                        {'context': {'contextId': 'other'}}, {'context': {'schemaHash': 'other'}},
                        {'context': {'asOf': '2025-01-01T00:00:00Z'}},
                        {'execution': 'other'}, {'session': 'other'}, {'evidence': []}):
            with self.subTest(options=options):
                self.turn.state['queryCaptureRefs'] = []
                self.verifier(**options)
                self.assertEqual(self.service._with_captured_verification(self.artifact), self.artifact)

    async def test_matching_invalid_evidence_cannot_be_covered_by_valid_evidence(self):
        await self.setup_draft()
        self.verifier()
        self.verifier(status='INVALID')
        with self.assertRaises(AnalysisError) as caught:
            self.service._with_captured_verification(self.artifact)
        self.assertEqual(caught.exception.code, 'EVIDENCE_INVALID')
        self.assertEqual(self.store.get_analysis(OWNER, self.artifact['analysisRef']), self.artifact)

    async def test_proof_not_registered_in_current_turn_is_not_used(self):
        await self.setup_draft()
        self.verifier()
        self.turn.state['queryCaptureRefs'] = []
        self.assertEqual(self.service._with_captured_verification(self.artifact), self.artifact)

    async def test_verified_facts_survive_host_closeout_even_when_old_draft_was_unverified(self):
        await self.setup_draft()
        self.verifier()
        result = await self.service.publish_available_facts(self.artifact['analysisRef'], 'REPAIR_LIMIT')
        self.assertEqual(result['publicationState'], 'PUBLISHED')
        published = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(published['receiptVerification'], 'VALID')
        self.assertEqual(published['facts'], self.artifact['facts'])

    async def test_document_preview_and_confirmation_use_the_verified_revision(self):
        built = await self.setup_draft()
        self.verifier()
        document = {'title': '既有数据展示', 'html': '<html><head><meta charset="utf-8"></head><body><p>结果 {{value}}</p></body></html>',
                    'bindings': [{'key': 'value', 'valueRef': self.artifact['facts'][0]['factId']}]}
        preview = await self.service.publish(built['analysisRef'], requestKey='document-preview',
             previewOnly=True, document=document)
        self.assertNotEqual(preview['analysisRef'], built['analysisRef'])
        self.assertEqual(preview['confirmPublish']['analysisRef'], preview['analysisRef'])
        proposal = self.store.get_publication_proposal(OWNER, preview['repairRef'])
        self.assertEqual(proposal['analysisRef'], preview['analysisRef'])
        self.assertEqual(proposal['document'], document)
        published = await self.service.publish(**preview['confirmPublish'])
        saved = self.store.get_analysis(OWNER, published['analysisRef'])
        self.assertEqual(saved['receiptVerification'], 'VALID')
        self.assertEqual(saved['freeDocument']['title'], document['title'])

    async def test_late_verification_is_included_when_revising_a_published_analysis(self):
        built = await self.setup_draft()
        # 模拟历史上先发布、后关联核验的版本；新的活动分析在预览前已检查凭据。
        self.service.turn = None
        published = await self.service.publish(built['analysisRef'], [], [self.view], 'early-publish')
        self.service.turn = self.turn
        original = self.store.get_analysis(OWNER, published['analysisRef'])
        self.assertEqual(original['receiptVerification'], 'UNVERIFIED')
        self.verifier()
        preview = await self.service.publish(published['analysisRef'], [], [self.view], 'verified-preview',
                                              previewOnly=True)
        verified = self.store.get_analysis(OWNER, preview['analysisRef'])
        self.assertEqual(verified['receiptVerification'], 'VALID')
        self.assertEqual(verified['baseAnalysisRef'], original['analysisRef'])
        self.assertEqual(verified['revision'], original['revision'] + 1)
        self.assertEqual(verified['facts'], original['facts'])
        self.assertEqual(self.store.get_analysis(OWNER, original['analysisRef']), original)
        confirmed = await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(confirmed['publicationState'], 'PUBLISHED')

    async def test_missing_native_grouping_returns_query_gap_before_model_review(self):
        built = await self.setup_draft()
        capture = deepcopy(self.data_capture)
        capture.update(originalToolName='discover_query_targets', request={'bindingIri': 'binding-A', 'groupingKeyword': '类别'})
        receipt = {**self.data_receipt, 'data': {'resolutionKind': 'GROUPING', 'bindingIri': 'binding-A',
            'status': 'SUPPORTED', 'metadataComplete': True, 'organizationType': 'category-A',
            'candidates': [{'supported': True, 'groupBy': {'dimCode': 'MD_ORG', 'field': 'category'}}]}}
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        discovery_ref = self.store.capture(OWNER, capture)
        self.turn._add_query_capture(discovery_ref)
        # 已明确采用的口径缺少原生结果仍应阻断，单纯探索候选不产生必做项。
        self.turn.state['selectedGroupingCaptureRefs'] = [discovery_ref]
        reviewer = AsyncMock(return_value={'issues': []})
        self.service.preview_reviewer = reviewer
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(built['analysisRef'], [], [self.view], 'preview',  previewOnly=True)
        self.assertEqual(caught.exception.code, 'GROUPING_RESULT_REQUIRED')
        self.assertEqual(caught.exception.details['issues'][0]['groupByCandidates'], [{'dimCode': 'MD_ORG', 'field': 'category'}])
        reviewer.assert_not_called()
