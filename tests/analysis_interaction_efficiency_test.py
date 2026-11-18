"""减少机械往返，但保留来源失败、空值、修订边界及真正的发布检查。"""
import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, patch

from jsonschema import Draft202012Validator, ValidationError
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolResponse
import analysis_capture_test as capture_fixture
import analysis_free_document_test as document_fixture
import analysis_local_render_test as render_fixture
import analysis_turn_completion_test as turn_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER
from analysis_extension.capture import ReceiptCapture
from analysis_extension.models import AnalysisError
from analysis_extension.query_continuation import continuation_request
from analysis_extension.tools import AnalysisPublish


def page(status='PARTIAL', cursor='next', reason='PAGE_BOUNDARY'):
    return {'contractVersion': '2.0', 'status': status, 'context': {'contextId': 'context'},
            'querySessionId': 'query', 'resumeRef': cursor, 'data': {'stepResults': []},
            'branches': [{'enumeration': {'stopReasons': [reason]}}] if reason else []}


class ContinuationTests(IsolatedAsyncioTestCase):
    asyncSetUp = capture_fixture.CaptureTest.asyncSetUp
    make_tool = capture_fixture.CaptureTest.make_tool
    async def execute_pages(self, pages, *, action=None):
        sent = []
        tool = self.make_tool('{}')
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        capture = ReceiptCapture(self.store, self.owner, frozenset({'registered-budget'}), self.allowed)
        capture.turn_key_provider = lambda: 'one'
        capture.action_ref_provider = lambda: action
        async def execute(**kwargs):
            sent.append(json.loads(kwargs['tool_call'].input))
            value = pages[min(len(sent) - 1, len(pages) - 1)]
            yield ToolResponse(id='one-call', content=[TextBlock(text=json.dumps(value))])
        request = {'contractVersion': '2.0', 'contextId': 'context', 'querySessionId': 'query',
                   'requestId': 'first', 'plan': {'steps': []}}
        call = ToolCallBlock(id='one-call', name=tool.name, input=json.dumps(request))
        responses = [event async for event in capture.run(agent, {'tool_call': call}, execute)]
        return responses, sent

    async def test_two_pages_one_response_and_all_evidence_survives_background_restore(self):
        responses, sent = await self.execute_pages([page(), page('SUCCEEDED', None, None)])
        self.assertEqual(len(responses), 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(set(sent[1]), {'contractVersion', 'contextId', 'querySessionId', 'requestId', 'resumeRef'})
        response = responses[0]
        refs = response.metadata['analysisCaptureRefs']
        self.assertEqual(len(refs), 2)
        for ref in refs:
            saved = self.store.get_capture(self.owner, ref)
            self.assertEqual(saved['toolCallId'], 'one-call')
            self.assertEqual(saved['turnKey'], 'one')
        payload = json.loads(response.content[0].text)
        self.assertEqual(payload['captureRefs'], refs)
        self.assertEqual(payload['status'], 'SUCCEEDED')
        self.assertIsNone(payload['resumeRef'])
        from analysis_extension.turn_completion import TurnCompletion
        turn = TurnCompletion(SimpleNamespace(store=self.store, owner=self.owner), {'turnKey': 'one'})
        turn.observe_background({'backgroundToolCallId': 'one-call', 'toolResponseMetadata': response.metadata})
        self.assertEqual(turn.state['queryCaptureRefs'], refs)
        self.assertFalse(turn.state.get('backgroundEvidenceErrors'))

    async def test_business_failure_and_background_pending_are_not_retried(self):
        for reason in ('BUSINESS_ERROR', 'BACKGROUND_EXECUTION', 'CAPACITY_LIMIT'):
            with self.subTest(reason=reason):
                responses, sent = await self.execute_pages([page(reason=reason)])
                self.assertEqual(len(sent), 1)
                self.assertNotIn('analysisCaptureRefs', responses[0].metadata)
        request = {'contextId': 'context', 'querySessionId': 'query'}
        for changed in ({'error': {'code': 'FAILED'}}, {'data': {'problems': ['invalid']}},
                        {'context': {'contextId': 'changed'}}, {'querySessionId': 'changed'}):
            self.assertIsNone(continuation_request({**page(), **changed}, request))

    async def test_repeated_cursor_and_page_budget_return_recoverable_partial(self):
        responses, sent = await self.execute_pages([page(), page()])
        self.assertEqual(len(sent), 2)
        payload = json.loads(responses[0].content[0].text)
        self.assertEqual(payload['automaticContinuation']['stopReason'], 'REPEATED_CURSOR')
        responses, sent = await self.execute_pages([page(cursor=str(i)) for i in range(8)])
        self.assertEqual(len(sent), 4)
        payload = json.loads(responses[0].content[0].text)
        self.assertEqual(payload['automaticContinuation']['stopReason'], 'CONTINUATION_BUDGET')
        final = self.store.get_capture(self.owner, responses[0].metadata['analysisCaptureRef'])
        self.assertIn('"resumeRef": "3"', final['response']['content'][0]['text'])

    async def test_action_workflow_never_auto_continues(self):
        # 自动续页仅适用于一般只读取数；行动工作流维持原有捕获与调度边界。
        responses, sent = await self.execute_pages([page()], action='not-registered')
        self.assertEqual(len(sent), 1)
        self.assertNotIn('analysisCaptureRefs', responses[0].metadata)

    async def test_oversized_navigation_does_not_repeat_payload_and_keeps_source_error(self):
        failure = page('FAILED', None, None)
        failure['data']['stepResults'] = [{'stepId': 'tail', 'error': {'code': 'UNAVAILABLE', 'message': '来源未返回'}}]
        with patch('analysis_extension.query_results.compact_receipt', return_value=None):
            responses, sent = await self.execute_pages([page(), failure])
        payload = json.loads(responses[0].content[0].text)
        self.assertEqual(payload['status'], 'FAILED')
        self.assertNotIn('content', payload['pages'][0])
        navigation = payload['pages'][1]['receipt']
        self.assertEqual(navigation['read']['captureRef'], payload['captureRefs'][1])
        self.assertTrue(any(item.get('value', {}).get('code') == 'UNAVAILABLE'
                            for item in navigation['diagnostics'] if isinstance(item.get('value'), dict)))


class FinalizeTests(PresentationCase):
    async def request(self):
        request = await document_fixture.FreeDocumentTests.request(self)
        request['renderCheckUnavailableReason'] = '契约测试不启动浏览器；页面另有真实浏览器回归。'
        return request

    async def test_finalize_pass_registers_only_after_review_and_keeps_checks(self):
        request = await self.request()
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        result = await self.service.publish(**request, finalize=True)
        self.assertTrue(result['delivery']['registered'])
        self.assertEqual(result['automaticFinalization']['status'], 'PUBLISHED')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(artifact['publicationState'], 'PUBLISHED')
        self.assertTrue(self.service.preview_reviewer.await_count)

    async def test_revise_and_unreviewed_preserve_draft_instead_of_publishing(self):
        request = await self.request()
        for review in (None, AsyncMock(return_value={'issues': [{'code': 'VALUE_MISMATCH', 'message': '数值不符'}]})):
            self.service.preview_reviewer = review
            result = await self.service.publish(**request, finalize=True)
            self.assertEqual(result['automaticFinalization']['status'], 'AWAITING_REPAIR_OR_REVIEW')
            self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')
            self.assertTrue(result['repairAndFinalize']['finalize'])

    async def test_file_repair_reuses_all_bindings_and_can_finalize_in_same_call(self):
        request = await self.request()
        self.delivery.root.mkdir(parents=True)
        path = self.delivery.root / 'report.html'
        path.write_text(request['document'].pop('html'), 'utf-8')
        request['document']['path'] = 'report.html'
        self.service.preview_reviewer = AsyncMock(return_value={'issues': [{'code': 'VALUE_MISMATCH', 'message': '改正文'}]})
        preview = await self.service.publish(**request, finalize=True)
        path.write_text(path.read_text('utf-8').replace('<h1>比较</h1>', '<h1>修正比较</h1>'), 'utf-8')
        self.service.preview_reviewer = AsyncMock(return_value={'issues': []})
        result = await self.service.publish(**{**preview['repairAndFinalize'],
            'repairs': [{'op': 'replace', 'path': '/document/path', 'value': 'report.html'}]})
        self.assertTrue(result['delivery']['registered'])
        frozen = self.store.get_publication_proposal(OWNER, result['automaticFinalization']['previewRef'])
        self.assertEqual(frozen['document']['bindings'], request['document']['bindings'])
        self.assertIn('修正比较', frozen['document']['html'])

    def test_finalize_and_preview_are_mutually_exclusive(self):
        validator = Draft202012Validator(AnalysisPublish.input_schema)
        request = {'analysisRef': 'a', 'requestKey': 'r', 'repairRef': 'p', 'repairs': [], 'finalize': True}
        validator.validate(request)
        with self.assertRaises(ValidationError):
            validator.validate({**request, 'previewOnly': True})

    async def test_reviewed_component_explanation_is_delivered_without_another_model_summary(self):
        from analysis_extension.middleware import AnalysisMiddleware
        from analysis_extension.turn_completion import review_answer
        turn = turn_fixture.TurnCompletionTests.turn(self)
        self.observe = turn_fixture.TurnCompletionTests.observe.__get__(self)
        self.service.turn = turn
        artifact = await turn_fixture.TurnCompletionTests.generic_result(self, turn, published=False)
        refs = [fact['factId'] for fact in artifact['facts']]
        self.service.preview_reviewer = lambda artifact: review_answer(None, turn, artifact, '')
        reviewer = AsyncMock(return_value={'issues': []})
        with patch('analysis_extension.turn_completion._model_review', reviewer):
            result = await self.service.publish(analysisRef=artifact['analysisRef'], requestKey='component-finalize',
                claims=[{'claimId': 'meaning', 'scopeId': artifact['scope']['scopeId'], 'kind': 'fact',
                         'parts': [{'text': '这是所选对象的来源读数，范围保持不变。'},
                                   *[{'valueRef': ref} for ref in refs]]}],
                viewPlan=[{'viewId': 'detail', 'type': 'TABLE', 'scopeId': artifact['scope']['scopeId'], 'valueRefs': refs}],
                workRefs=[{'taskId': 'values', 'refs': refs}], finalize=True)
            self.observe(turn, 'AnalysisPublish', result)
            middleware = AnalysisMiddleware(self.store, OWNER, [], self.service.authorize)
            middleware.service = self.service
            self.assertEqual(middleware._approved_delivery(), result['delivery']['markdown'])
            await review_answer(None, turn, self.store.get_analysis(OWNER, result['analysisRef']), result['delivery']['markdown'])
        self.assertEqual(reviewer.await_count, 1)
        frozen = self.store.get_analysis(OWNER, result['analysisRef'])
        with patch.object(self.store, 'get_analysis', return_value={**frozen, 'hostSummary': {'content': 'SOURCE_FACTS_ONLY'}}):
            self.assertIsNone(middleware._approved_delivery())
        turn.state['queryIntent'] = {'population': 'different'}
        self.assertIsNone(middleware._approved_delivery())


@skipUnless(os.environ.get('RUN_ANALYSIS_BROWSER_TESTS') == '1', '显式启用真实浏览器测试')
class TabularChartTests(PresentationCase):
    async def test_describe_table_binds_real_bars_and_checks_two_widths(self):
        request = await render_fixture.LocalRenderTests.prepare(self)
        built = await self.service.build(baseAnalysisRef=request['analysisRef'],
            operations=[{'id': 'stats', 'operator': 'DESCRIBE'}], requestKey='native-stats')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        request['analysisRef'] = built['analysisRef']
        request['document']['datasets'] = [{'key': 'statistics', 'valueRef': 'derived_stats'}]
        refs = [fact['factId'] for fact in artifact['facts']]
        request['document']['html'] += '''<div id="track" style="width:100%"><div id="one" style="height:14px;background:teal"></div><div id="two" style="height:14px;background:orange"></div></div><script>
analysisCharts.bindLinearBars(REFS.map((ref,i)=>({element:document.getElementById(i?'two':'one'),setKey:'statistics',factRef:ref})),{extent:document.getElementById('track')});</script>'''.replace('REFS', json.dumps(refs))
        result = await self.service.publish(**request, previewOnly=True)
        self.assertEqual(result['nextAction'], 'REVIEW_AND_CONFIRM', result['diagnostics'])
        for sample in result['diagnostics']['renderChecks']['viewports']:
            self.assertEqual(sample['numericChecks']['linearBars']['measured'], 2)
            self.assertEqual(sample['numericChecks']['linearBars']['status'], 'PASS')


class RevisionTests(PresentationCase):
    async def test_build_retains_source_aggregation_counts_at_their_own_grain(self):
        turn = turn_fixture.TurnCompletionTests.turn(self)
        self.observe = turn_fixture.TurnCompletionTests.observe.__get__(self)
        artifact = await turn_fixture.TurnCompletionTests.generic_result(self, turn, published=False)
        # 来源计数可能合法不同；只透传，不把叶子去重推断成漏查或改写来源数值。
        scope = {'returnedRowCount': 5, 'selectedRowCount': 5,
                 'rangeSummary': {'targetCombinationCount': 51, 'matchedTargetCount': 42},
                 'aggregationSummary': {'leafTargetCombinationCount': 42, 'groupCountBeforeHaving': 5,
                                        'filteredOutGroupCount': 0}}
        artifact['scope']['sourceScopes'] = [scope]
        self.assertEqual(self.service._built(artifact)['scope']['sourceRangeSummary'], [scope])

    def test_revision_keeps_all_values_and_skips_only_verified_unchanged_background(self):
        from analysis_extension.turn_completion import TurnCompletion
        from analysis_extension.review_revision import remember_review, focus_revision
        turn = TurnCompletion(self.service, {'turnKey': 'test'})
        material = {'documentText': '说明甲', 'documentSourceText': '隐藏说明甲', 'facts': [{'value': None}],
                    'derived': [{'difference': '-1'}], 'sourceScopes': ['背景' * 100], 'toolFailures': []}
        remember_review(turn, material, {'issues': [{'code': 'VALUE_MISMATCH'}]})
        changed = {**material, 'documentText': '说明乙'}
        focused = focus_revision(turn, changed)
        self.assertEqual(focused['facts'], material['facts'])
        self.assertEqual(focused['derived'], material['derived'])
        self.assertNotIn('sourceScopes', focused)
        for key, value in (('facts', [{'value': '1'}]), ('sourceScopes', ['新范围']), ('toolFailures', ['新错误'])):
            self.assertNotIn('revisionFocus', focus_revision(turn, {**changed, key: value}))
        hidden = focus_revision(turn, {**material, 'documentSourceText': '隐藏说明乙'})
        self.assertEqual(hidden['revisionFocus']['changes'][0]['field'], 'documentSourceText')
