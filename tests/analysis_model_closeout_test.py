"""查询、报告收尾共用现有材料；解释缺失不能伪装成交付成功。"""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.model_closeout import model_closeout, closeout_material
from analysis_extension.review_control import progress
from analysis_extension.turn_completion import TurnCompletion, current_turn, review_query_answer
from analysis_extension.middleware import AnalysisMiddleware
from agentscope.message import UserMsg
from agentscope.state import AgentState


async def summary_response(messages, **kwargs):
    from analysis_extension.review_encoding import unpack_review_records
    material = unpack_review_records(json.loads(messages[-1].get_text_content()))
    records = material['facts'] or material['computed']
    return SimpleNamespace(usage=None, content={
        'answer': '当前已取得的金额为 ' + '、'.join(str(item.get('value')) for item in records) + '，进一步核对尚未完成。',
        'usedRefs': [item.get('factId', item.get('derivedId')) for item in records]})


class ModelCloseoutTests(PresentationCase):
    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '查询本年金额')))
        turn.task(mode='DIRECT', goal='本年金额')
        self.service.turn = turn
        return turn

    def agent(self):
        return SimpleNamespace(model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response)))

    def evidence(self):
        return {'sources': [{'facts': [{'factId': 'fact-zero', 'value': '0.00', 'valueState': 'PRESENT', 'unit': '元'},
                                     {'factId': 'fact-null', 'value': None, 'valueState': 'NO_VALUE'}]}]}

    async def test_direct_without_analysis_ref_uses_existing_data_once(self):
        turn, agent = self.turn(), self.agent()
        middleware = AnalysisMiddleware(self.store, self.service.owner, [], self.service.authorize)
        middleware.service = self.service
        with patch('analysis_extension.middleware.query_evidence', return_value=self.evidence()):
            first = await middleware._closeout('REVIEW_LIMIT', agent)
            second = await middleware._closeout('REVIEW_LIMIT', agent)
        self.assertEqual(first, second)
        self.assertIn('0.00', first)
        self.assertEqual(turn.state['hostCloseout']['deliveryStatus'], 'MODEL_SUMMARY')
        self.assertEqual(turn.state['hostCloseout']['semanticReview'], 'NOT_REVIEWED')
        self.assertNotIn('analysisRef', turn.state)
        agent.model.generate_structured_output.assert_awaited_once()

    async def test_model_failure_is_not_raw_data_or_a_pass(self):
        turn, agent = self.turn(), self.agent()
        agent.model.generate_structured_output.side_effect = TimeoutError()
        with self.assertRaises(AnalysisError) as caught:
            await model_closeout(agent, turn, 'REVIEW_LIMIT', evidence=self.evidence())
        self.assertEqual(caught.exception.code, 'CLOSEOUT_UNAVAILABLE')
        self.assertNotIn('result', progress(turn)['modelCloseout'])

    async def test_closeout_schema_limits_references_to_supplied_material(self):
        turn, agent = self.turn(), self.agent()
        await model_closeout(agent, turn, 'REVIEW_LIMIT', evidence=self.evidence())
        schema = agent.model.generate_structured_output.call_args.kwargs['structured_model']
        self.assertEqual(schema['properties']['usedRefs']['items']['enum'], ['fact-null', 'fact-zero'])

    async def test_invalid_closeout_reference_retains_diagnostic(self):
        turn, agent = self.turn(), self.agent()
        agent.model.generate_structured_output.side_effect = None
        agent.model.generate_structured_output.return_value = SimpleNamespace(
            usage=None, content={'answer': '无法确认', 'usedRefs': ['unknown']})
        with self.assertRaises(AnalysisError) as caught:
            await model_closeout(agent, turn, 'REVIEW_LIMIT', evidence=self.evidence())
        self.assertEqual(caught.exception.code, 'CLOSEOUT_REFERENCE')
        self.assertEqual(progress(turn)['modelCloseout']['invalidRefs'], ['unknown'])

    async def test_query_review_respects_shared_exhausted_budget(self):
        turn = self.turn()
        progress(turn, lambda state: state.update(review={'attempts': 3, 'elapsedMs': 180000, 'unavailable': 0}))
        with patch('analysis_extension.turn_completion._model_review', new_callable=AsyncMock) as model:
            with self.assertRaises(AnalysisError) as caught:
                await review_query_answer(None, turn, self.evidence(), '金额0.00')
            self.assertEqual(caught.exception.code, 'ANALYSIS_RETRY_LIMIT')
            model.assert_not_awaited()

    def test_empty_optional_intent_does_not_clear_inherited_conditions(self):
        turn = self.turn()
        turn.task(mode='DIRECT', goal='查询', intent={'period': '2026年', 'order': ''})
        self.assertEqual(turn.state['queryIntent'], {'period': '2026年'})

    def test_task_schema_requires_task_mode_only_when_declaring(self):
        from jsonschema import Draft202012Validator
        from analysis_extension.tools import AnalysisTask
        validator = Draft202012Validator(AnalysisTask.input_schema)
        self.assertTrue(validator.is_valid({'action': 'status'}))
        self.assertTrue(validator.is_valid({'action': 'declare', 'mode': 'ANALYSIS', 'goal': '分析构成'}))
        self.assertFalse(validator.is_valid({'action': 'declare', 'context': {'mode': 'CONTINUE'}, 'goal': '分析构成'}))
        self.assertFalse(validator.is_valid({'action': 'declare', 'mode': 'ANALYSIS'}))

    def test_lossless_compaction_preserves_missing_and_null(self):
        from analysis_extension.review_revision import compact_query_facts
        facts = [{'factId': str(index), 'value': None if index == 1 else '0.00', 'unit': '元'} for index in range(21)]
        del facts[0]['unit']
        source = compact_query_facts({'sources': [{'facts': facts}]})['sources'][0]
        restored = []
        for index, row in enumerate(source['factRows']):
            value = {**source['fixedFactFields'], **dict(zip(source['factColumns'], row))}
            for key in source['missingFactFields'].get(str(index), []):
                value.pop(key)
            restored.append(value)
        self.assertEqual(restored, facts)

    def test_closeout_compacts_before_capacity_without_losing_later_subjects(self):
        turn = self.turn()
        facts = [{'factId': 'fact-' + str(index), 'objectId': 'object-' + str(index),
                  'value': None if index == 0 else str(index), 'unit': '元',
                  'sourceContext': {'description': '已取得的共同来源说明' * 100}}
                 for index in range(70)]
        self.assertGreater(len(json.dumps(facts, ensure_ascii=False)), 60000)
        material = closeout_material(turn, evidence={'sources': [{'facts': facts}]})
        self.assertEqual(material['facts'], facts)
        self.assertEqual(material['omittedCount'], 0)

    def test_empty_closeout_is_not_an_answer(self):
        with self.assertRaises(AnalysisError) as caught:
            closeout_material(self.turn(), evidence={'sources': []})
        self.assertEqual(caught.exception.code, 'NO_AVAILABLE_FACTS')

    def test_revision_focus_is_invalidated_by_data_change(self):
        from analysis_extension.review_revision import remember_review, focus_revision
        turn = self.turn()
        first = {'documentText': '金额0.00\n解释甲', 'facts': self.evidence()['sources'][0]['facts']}
        remember_review(turn, first, {'issues': []})
        changed = {**first, 'documentText': '金额0.00\n解释乙'}
        self.assertTrue(focus_revision(turn, changed)['revisionFocus']['sameDataAndScope'])
        changed = deepcopy(changed)
        changed['facts'][0]['value'] = '9.00'
        self.assertNotIn('revisionFocus', focus_revision(turn, changed))
