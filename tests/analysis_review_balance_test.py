"""分析建议不阻断，实质错误仍拦截，后台恢复复用真实审查结果。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.delivery import AnalysisDelivery
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.service import AnalysisService
from analysis_extension.store import AnalysisStore
from analysis_extension.turn_completion import review_answer


class AnalysisReviewBalanceTests(PresentationCase):
    async def request(self):
        from analysis_free_document_test import FreeDocumentTests
        request = await FreeDocumentTests.request(self)
        request['renderCheckUnavailableReason'] = '本测试核对审查与交付，不执行浏览器检查。'
        return request

    def turn(self, service=None, state=None):
        return SimpleNamespace(service=service or self.service, state=state or {
            'request': '分析预算情况', 'goal': '解释当前数据', 'mode': 'ANALYSIS'})

    async def artifact(self):
        request = await self.request()
        artifact = self.store.get_analysis(OWNER, self.ref)
        artifact['freeDocument'] = compile_document(artifact, request['document'])
        return artifact

    async def test_advice_does_not_become_a_repair_task(self):
        artifact, turn = await self.artifact(), self.turn()
        advice = {'severity': 'ADVISORY', 'code': 'UNSUPPORTED_CLAIM',
                  'message': '可以换个标题，并增加一个分析角度。'}
        with patch('analysis_extension.turn_completion._model_review',
                   AsyncMock(return_value={'issues': [advice]})):
            verdict = await review_answer(None, turn, artifact, '')
        self.assertEqual(verdict, {'issues': []})
        self.assertEqual(turn.state['analysisReviewAdvisories'], [advice])

    async def test_material_error_is_kept_without_optional_advice(self):
        artifact, turn = await self.artifact(), self.turn()
        wrong_value = {'severity': 'BLOCKING', 'code': 'ANSWER_MISMATCH',
                       'message': '正文金额与当前来源事实不符。'}
        advice = {'severity': 'ADVISORY', 'code': 'MISSING_CHECK',
                  'message': '可以再解释未来走势，但用户未要求预测。'}
        with patch('analysis_extension.turn_completion._model_review',
                   AsyncMock(return_value={'issues': [advice, wrong_value]})):
            verdict = await review_answer(None, turn, artifact, '')
        self.assertEqual(len(verdict['issues']), 1)
        self.assertEqual(verdict['issues'][0]['code'], 'ANSWER_MISMATCH')
        self.assertEqual(verdict['issues'][0]['message'], wrong_value['message'])
        self.assertEqual(verdict['issues'][0]['locationStatus'], 'UNLOCATED')

    async def test_preview_pass_survives_stale_turn_restore_and_publication(self):
        request = await self.request()
        first_turn = self.turn()
        # 模拟转后台前已保存的会话：随后审查完成时，缓存尚未写回这份旧会话。
        persisted_state = json.loads(json.dumps(first_turn.state))
        self.service.preview_reviewer = lambda artifact: review_answer(None, first_turn, artifact, '')
        advice = {'severity': 'ADVISORY', 'code': 'UNSUPPORTED_CLAIM', 'message': '标题可以更简洁。'}
        with patch('analysis_extension.turn_completion._model_review',
                   AsyncMock(return_value={'issues': [advice]})) as model:
            preview = await self.service.publish(**request, previewOnly=True)
            self.assertEqual(preview['diagnostics']['semanticReview']['status'], 'PASS')
            self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM')
            reopened = AnalysisStore(self.store.root)
            delivery = AnalysisDelivery(reopened, OWNER, self.delivery.root, self.rules)
            resumed = AnalysisService(reopened, OWNER, self.service.authorize, self.rules, delivery=delivery)
            resumed_turn = self.turn(resumed, persisted_state)
            resumed.preview_reviewer = lambda artifact: review_answer(None, resumed_turn, artifact, '')
            published = await resumed.publish(**preview['confirmPublish'])
            self.assertEqual(published['publicationState'], 'PUBLISHED')
            self.assertTrue(published['delivery']['registered'])
            self.assertEqual(model.await_count, 1)
            self.assertEqual(resumed_turn.state['analysisReviewProgress']['reusedPartCount'], 1)
            resumed_turn.state['delivery'] = published['delivery']
            artifact = reopened.get_analysis(OWNER, published['analysisRef'])
            await review_answer(None, resumed_turn, artifact, published['delivery']['markdown'])
            self.assertEqual(model.await_count, 1)

    async def test_content_or_evidence_change_invalidates_pass(self):
        artifact, turn = await self.artifact(), self.turn()
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            await review_answer(None, turn, artifact, '')
            changed_text = deepcopy(artifact)
            changed_text['freeDocument']['html'] += '<p>增加新的业务结论。</p>'
            await review_answer(None, self.turn(), changed_text, '')
            changed_value = deepcopy(artifact)
            changed_value['facts'][0]['value'] = '99999'
            await review_answer(None, self.turn(), changed_value, '')
            self.assertEqual(model.await_count, 3)

    async def test_publication_argument_retry_reuses_review_but_source_failure_invalidates_it(self):
        request = await self.request()
        first_turn = self.turn()
        self.service.preview_reviewer = lambda artifact: review_answer(None, first_turn, artifact, '')
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value={'issues': []})) as model:
            preview = await self.service.publish(**request, previewOnly=True)
            # 真实回归曾把 confirmPublish 作为额外外层字段。错误必须保留，
            # 修正参数后同一页面的已通过结论仍应被复用，而不是再次审全文。
            await self.service.validate_publication_input(preview['confirmPublish'])
            with self.assertRaises(AnalysisError) as caught:
                await self.service.validate_publication_input({'analysisRef': self.ref,
                                                              'confirmPublish': preview['confirmPublish']})
            self.assertEqual(caught.exception.code, 'INVALID_PUBLICATION')
            resumed_state = deepcopy(first_turn.state)
            resumed_state['documentReviews'] = {}
            resumed_state['analysisReviewParts'] = {}
            resumed_state['failures'] = {'bad-confirm': {'tool': 'AnalysisPublish',
                                                       'error': caught.exception.as_dict()}}
            resumed_turn = self.turn(state=resumed_state)
            self.service.preview_reviewer = lambda artifact: review_answer(None, resumed_turn, artifact, '')
            published = await self.service.publish(**preview['confirmPublish'])
            self.assertEqual(published['publicationState'], 'PUBLISHED')
            self.assertEqual(model.await_count, 1)
            self.assertIn('bad-confirm', resumed_turn.state['failures'])
            artifact = self.store.get_analysis(OWNER, self.ref)
            resumed_turn.state['failures']['source-failed'] = {
                'tool': 'mcp__budget__execute_semantic_plan',
                'error': {'code': 'QUERY_OPERATION_FAILED', 'message': '来源查询失败'}}
            await review_answer(None, resumed_turn, artifact, '')
            self.assertEqual(model.await_count, 2)

    async def test_review_timeout_is_not_saved_as_pass(self):
        artifact = await self.artifact()
        with patch('analysis_extension.turn_completion._model_review',
                   AsyncMock(side_effect=[TimeoutError(), TimeoutError(), {'issues': []}])) as model:
            with self.assertRaises(TimeoutError):
                await review_answer(None, self.turn(), artifact, '')
            self.assertEqual(await review_answer(None, self.turn(), artifact, ''), {'issues': []})
            self.assertEqual(model.await_count, 3)

    async def test_material_error_still_blocks_publication(self):
        request, turn = await self.request(), self.turn()
        self.service.preview_reviewer = lambda artifact: review_answer(None, turn, artifact, '')
        failure = {'issues': [{'severity': 'BLOCKING', 'code': 'UNSUPPORTED_UNIT',
                               'message': '原值为元，展示成万元时没有换算。'}]}
        with patch('analysis_extension.turn_completion._model_review', AsyncMock(return_value=failure)) as model:
            preview = await self.service.publish(**request, previewOnly=True)
            self.assertEqual(preview['nextAction'], 'REPAIR_DOCUMENT')
            with self.assertRaises(AnalysisError) as caught:
                await self.service.publish(**preview['confirmPublish'])
            self.assertEqual(caught.exception.code, 'DOCUMENT_SEMANTIC_ERRORS')
            self.assertEqual(model.await_count, 1)

    def test_review_cache_is_isolated_by_owner(self):
        self.store.save_semantic_review(OWNER, 'material', {'issues': []})
        other = Owner(OWNER.user_id, OWNER.agent_id, 'another-session')
        self.assertIsNone(self.store.get_semantic_review(other, 'material'))
        self.assertEqual(AnalysisStore(self.store.root).get_semantic_review(OWNER, 'material'), {'issues': []})

    def test_completed_verdict_is_not_overwritten_by_a_late_duplicate(self):
        passed = {'issues': []}
        self.store.save_semantic_review(OWNER, 'same-material', passed)
        late = {'issues': [{'severity': 'BLOCKING', 'code': 'UNSUPPORTED_CLAIM',
                            'message': '另一次审查提出不同意见。'}]}
        self.assertEqual(self.store.save_semantic_review(OWNER, 'same-material', late), passed)
        self.assertEqual(self.store.get_semantic_review(OWNER, 'same-material'), passed)
