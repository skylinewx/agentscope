"""查询、报告收尾共用现有材料；解释缺失不能伪装成交付成功。"""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    def test_formula_closeout_keeps_all_facts_and_layer_status_without_provenance_overflow(self):
        turn = self.turn()
        facts = [{'factId': f'f{index}', 'objectLabel': '单位', 'value': None if index == 1 else '1.00',
                  'valueState': 'NULL' if index == 1 else 'PRESENT', 'headers': ['净利润'],
                  'period': '2026N0001', 'sourceOrigins': [{'transport': 'x' * 10000}]} for index in range(28)]
        trace = {'status': 'BLOCKED', 'targetFactRef': 'f0', 'targetLocalCheck': {'status': 'MATCH'},
                 'nodes': [{'factRef': 'f0', 'localCheck': {'status': 'MATCH'}, 'originalEvidence': 'x' * 60000},
                           {'factRef': 'f1', 'localCheck': {'status': 'BLOCKED'}}]}
        artifact = {'facts': facts, 'derivedValues': [{'derivedId': 'check', 'operator': 'FORMULA_CHECK', 'formulaTrace': trace}]}
        material = closeout_material(turn, artifact=artifact)
        self.assertEqual(len(material['facts']), 28)
        self.assertEqual(material['omittedCount'], 0)
        self.assertIsNone(material['facts'][1]['value'])
        self.assertEqual(material['computed'][0]['checkSummary']['capturedChainStatus'], 'INCOMPLETE')
        self.assertEqual(material['computed'][0]['formulaTrace']['nodes'][1]['localCheck']['status'], 'BLOCKED')
        self.assertIn('sourceOrigins', artifact['facts'][0])

    async def test_empty_business_receipt_survives_real_evidence_projection(self):
        from analysis_extension.turn_completion import query_evidence
        from analysis_extension.query_empty_result import empty_query_answer
        turn = self.turn()
        query = {'purpose': 'QUERY', 'selection': 'CELLS', 'complete': True, 'resultSets': [],
                 'summary': {'rowCount': 0, 'candidateCount': 1},
                 'failureSummary': {'outcome': 'NO_MATCH', 'matchDiagnostics': []}}
        capture = {'captureRef': 'empty', 'originalToolName': 'execute_budget_query', 'response': {}}
        adapter = Mock()
        adapter.is_data.return_value = True
        adapter.entities.return_value = []
        adapter.raw_receipt.return_value = {'data': query, 'status': 'SUCCEEDED'}
        adapter.receipt_info.return_value = {'businessQuery': query}
        registry = Mock()
        registry.for_capture.return_value = adapter
        registry.catalog.return_value = []
        registry.relations.return_value = []
        turn.state['queryCaptureRefs'] = ['empty']
        with patch.object(self.service, 'sources', registry), patch.object(self.store, 'get_capture', return_value=capture):
            evidence = query_evidence(turn)
        self.assertIsNotNone(evidence)
        self.assertEqual(empty_query_answer(evidence)['deliveryStatus'], 'NO_MATCH')

    async def test_confirmed_empty_query_closes_without_review_and_survives_reentry(self):
        turn, agent = self.turn(), self.agent()
        agent.state = AgentState()
        middleware = AnalysisMiddleware(self.store, self.service.owner, [], self.service.authorize)
        middleware.service = self.service
        evidence = {'sources': [{'facts': [], 'source': {'businessQuery': {
            'purpose': 'QUERY', 'complete': True, 'summary': {'rowCount': 0, 'candidateCount': 1},
            'failureSummary': {'outcome': 'NO_MATCH', 'matchDiagnostics': []}}}}]}
        with patch('analysis_extension.middleware.query_evidence', return_value=evidence):
            first = await middleware._check_end(agent)
            second = await middleware._check_end(agent)
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'DIRECT')
        self.assertEqual(first['deliveryStatus'], 'NO_MATCH')
        self.assertIn('没有取得数值', turn.state['hostCloseout']['answer'])
        self.assertEqual(progress(turn)['closeout']['reason'], 'NO_MATCH')
        agent.model.generate_structured_output.assert_not_awaited()

    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '查询本年金额')))
        turn.task(mode='DIRECT', goal='本年金额')
        self.service.turn = turn
        return turn

    async def test_verified_empty_scenario_analysis_finishes_before_another_model_attempt(self):
        turn, agent = self.turn(), self.agent()
        turn.state.update(mode='ANALYSIS', queryCaptureRefs=['rejected-draft', 'scenario-empty'])
        turn.recover_background_evidence = Mock()
        agent.state = AgentState(); agent.name = 'test'
        agent._save_to_context = Mock(); agent._get_reply_usage = Mock(return_value=None)
        middleware = AnalysisMiddleware(self.store, self.service.owner, [], self.service.authorize)
        middleware.service = self.service
        evidence = {'sources': [{'facts': [{'valueState': 'NO_DATA', 'formulaStatus': 'NO_TARGET_CELL',
                    'dimensions': {'MD_SCENE': 'ACTUAL'}}], 'source': {'businessQuery': {
                    'purpose': 'ANALYZE', 'complete': True, 'failureSummary': {'outcome': 'NO_DATA'}}}}]}
        next_handler = Mock(side_effect=AssertionError('The completed empty scenario must not ask the model to retry.'))
        captures = {'rejected-draft': {'originalToolName': 'execute_budget_query',
                    'request': {'question': turn.state['request']}, 'response': {'status': 'FAILED'}},
                    'scenario-empty': {'originalToolName': 'execute_budget_query',
                    'request': {'question': turn.state['request']}, 'response': {'status': 'SUCCEEDED',
                    'data': {'complete': True, 'failureSummary': {'outcome': 'NO_DATA'}}}}}
        adapter = SimpleNamespace(raw_receipt=lambda response: response)
        def verified_evidence(current):
            self.assertEqual(current.state['queryCaptureRefs'], ['scenario-empty'])
            return evidence
        with patch('analysis_extension.middleware.query_evidence', side_effect=verified_evidence), \
             patch.object(self.store, 'get_capture', side_effect=lambda owner, ref: captures[ref]), \
             patch.object(self.service.sources, 'for_capture', return_value=adapter):
            events = [event async for event in middleware.on_reasoning(agent, {}, next_handler)]
            checked = await middleware._check_end(agent)
        self.assertEqual(checked['deliveryStatus'], 'NO_DATA')
        self.assertTrue(events)
        self.assertEqual(progress(turn)['closeout']['reason'], 'NO_DATA')
        next_handler.assert_not_called()
        agent.model.generate_structured_output.assert_not_awaited()
        self.assertEqual(turn.state['queryCaptureRefs'], ['rejected-draft', 'scenario-empty'])

    def agent(self):
        return SimpleNamespace(model=SimpleNamespace(generate_structured_output=AsyncMock(side_effect=summary_response)))

    async def test_empty_scenario_analysis_reuses_confirmed_previous_scope_without_a_new_query(self):
        turn, agent = self.turn(), self.agent()
        turn.state.update(mode='ANALYSIS', followupContext={'mode': 'CONTINUE'}, queryCaptureRefs=[])
        turn.recover_background_evidence = Mock()
        agent.state = AgentState(); agent.name = 'test'
        agent._save_to_context = Mock(); agent._get_reply_usage = Mock(return_value=None)
        middleware = AnalysisMiddleware(self.store, self.service.owner, [], self.service.authorize)
        middleware.service = self.service
        previous = SimpleNamespace(service=self.service, state={**turn.state, 'queryCaptureRefs': ['prior-actual']})
        capture = {'originalToolName': 'execute_budget_query', 'request': {'question': '上一轮实际数查询'},
                   'response': {'status': 'SUCCEEDED', 'data': {'complete': True, 'failureSummary': {'outcome': 'NO_DATA'}}}}
        evidence = {'sources': [{'facts': [{'valueState': 'NO_DATA', 'formulaStatus': 'NO_TARGET_CELL'}],
                    'source': {'businessQuery': {'purpose': 'QUERY', 'complete': True,
                                                'failureSummary': {'outcome': 'NO_DATA'}}}}]}
        next_handler = Mock(side_effect=AssertionError('No new model/query is needed for the same confirmed empty scope.'))
        with patch('analysis_extension.conversation_context.evidence_turn', return_value=previous), \
             patch('analysis_extension.middleware.query_evidence', return_value=evidence), \
             patch.object(self.store, 'get_capture', return_value=capture), \
             patch.object(self.service.sources, 'for_capture', return_value=SimpleNamespace(raw_receipt=lambda response: response)):
            events = [event async for event in middleware.on_reasoning(agent, {}, next_handler)]
        self.assertTrue(events)
        self.assertIn('本轮未重复取数', turn.state['hostCloseout']['answer'])
        self.assertEqual(turn.state['queryCaptureRefs'], [])
        next_handler.assert_not_called()
        agent.model.generate_structured_output.assert_not_awaited()
        turn.state.pop('hostCloseout')
        turn.state['followupContext']['changedFields'] = ['period']
        with patch('analysis_extension.middleware.query_evidence') as evidence_reader:
            self.assertIsNone(middleware._empty_scenario_analysis())
            evidence_reader.assert_not_called()

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
        self.assertEqual(turn.state['hostCloseout']['deliveryStatus'], 'FACT_SUMMARY')
        self.assertEqual(turn.state['hostCloseout']['semanticReview'], 'NOT_REVIEWED')
        self.assertNotIn('analysisRef', turn.state)
        agent.model.generate_structured_output.assert_not_awaited()

    async def test_model_outage_cannot_prevent_source_only_closeout(self):
        turn, agent = self.turn(), self.agent()
        agent.model.generate_structured_output.side_effect = TimeoutError()
        result = await model_closeout(agent, turn, 'REVIEW_LIMIT', evidence=self.evidence())
        self.assertEqual(result['deliveryStatus'], 'FACT_SUMMARY')
        self.assertIn('0.00', result['answer'])
        self.assertIn('空值', result['answer'])
        self.assertEqual(result['usedRefs'], ['fact-zero', 'fact-null'])
        agent.model.generate_structured_output.assert_not_awaited()

    async def test_rejected_narrative_and_unreviewed_python_are_not_reintroduced(self):
        turn, agent = self.turn(), self.agent()
        turn.state['reviews'] = [{'issues': [{'message': '已知支出导致亏损这句话与复算正值矛盾'}]}]
        evidence = self.evidence()
        evidence.update(resultRoots=['unsafe'], pythonRuns=[{'outputs': [
            {'derivedId': 'unsafe', 'value': '已知支出导致亏损'}]}])
        result = await model_closeout(agent, turn, 'REVIEW_UNAVAILABLE', evidence=evidence)
        self.assertNotIn('导致亏损', result['answer'])
        self.assertNotIn('unsafe', result['usedRefs'])
        agent.model.generate_structured_output.assert_not_awaited()

    async def test_query_review_respects_shared_exhausted_budget(self):
        turn = self.turn()
        progress(turn, lambda state: state.update(review={'attempts': 3, 'elapsedMs': 180000, 'unavailable': 0}))
        with patch('analysis_extension.turn_completion._model_review', new_callable=AsyncMock) as model:
            with self.assertRaises(AnalysisError) as caught:
                await review_query_answer(None, turn, self.evidence(), '金额0.00')
            self.assertEqual(caught.exception.code, 'ANALYSIS_RETRY_LIMIT')
            model.assert_not_awaited()

    def test_null_order_clears_only_order(self):
        turn = self.turn()
        turn.task(mode='DIRECT', goal='查询', intent={'period': '2026年', 'order': None})
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

    def test_oversized_closeout_covers_later_contexts_and_full_state_counts(self):
        facts = [{'factId': f'f-{index}', 'contextRef': f'context-{index // 150}',
                  'headers': [f'项目{index} ' + '来源说明' * 70],
                  'value': None if index < 750 else '0.00' if index % 2 else '150.25',
                  'valueState': 'NO_VALUE' if index < 750 else 'PRESENT'} for index in range(1500)]
        original = deepcopy(facts)
        material = closeout_material(self.turn(), evidence={'sources': [{'facts': facts}]})
        self.assertGreater(material['omittedCount'], 0)
        self.assertEqual(len(material['facts']) + material['omittedCount'], len(facts))
        self.assertEqual({fact['contextRef'] for fact in material['facts']}, {f'context-{i}' for i in range(10)})
        counts = {row['contextRef']: row for row in material['factCoverage']}
        self.assertEqual(counts['context-0']['valueStates'], {'NO_VALUE': 150})
        self.assertEqual(counts['context-9']['valueStates'], {'PRESENT': 150})
        self.assertTrue(all(row['includedCount'] > 0 for row in counts.values()))
        self.assertEqual(facts, original)

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
