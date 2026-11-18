"""Public observations, event ordering, cancellation and history regression."""
import asyncio
from copy import deepcopy
import json
from unittest import IsolatedAsyncioTestCase, TestCase

from agentscope.app._service._progress_facts import result_facts
from agentscope.app._service._reply_progress import (
    ReplyProgress, apply_progress, with_reply_progress,
)
from agentscope.event import (
    CustomEvent, ModelCallStartEvent, ModelCallEndEvent, ReplyStartEvent, ReplyEndEvent,
    ThinkingBlockStartEvent, ThinkingBlockDeltaEvent, ToolCallStartEvent, ToolCallDeltaEvent,
    ToolResultStartEvent, ToolResultTextDeltaEvent, ToolResultEndEvent,
    RequireUserConfirmEvent, TextBlockStartEvent, TextBlockDeltaEvent,
    HintBlockEvent,
)
from agentscope.message import AssistantMsg, Msg, ToolCallBlock


def table():
    return {'data': {'columns': [
        {'name': 'category', 'label': '分类', 'type': 'STRING'},
        {'name': 'amount', 'label': '费用', 'unit': '元', 'type': 'DECIMAL_STRING'},
    ], 'rows': [['部门甲', '123456789.1200'], ['部门乙', '-3.40']], 'rowCount': 2}}


def events(two=False):
    yield ReplyStartEvent(session_id='session', reply_id='reply', name='agent')
    yield ModelCallStartEvent(reply_id='reply', model_name='model')
    yield ThinkingBlockStartEvent(reply_id='reply', block_id='private')
    yield ThinkingBlockDeltaEvent(reply_id='reply', block_id='private', delta='secret reasoning 999')
    for key in (['one', 'two'] if two else ['one']):
        yield ToolCallStartEvent(reply_id='reply', tool_call_id=key, tool_call_name='query')
    yield ModelCallEndEvent(reply_id='reply', input_tokens=1, output_tokens=1)
    for key in (['one', 'two'] if two else ['one']):
        yield ToolResultStartEvent(reply_id='reply', tool_call_id=key, tool_call_name='query')
        yield ToolResultTextDeltaEvent(reply_id='reply', tool_call_id=key, delta=json.dumps(table()))
        yield ToolResultEndEvent(reply_id='reply', tool_call_id=key, state='success')


class ProgressFactsTests(TestCase):
    def test_exact_values_units_scope_and_no_arbitrary_prompt_data(self):
        value = table()
        value['instructions'] = {'rows': [{'password': 'sensitive'}], 'rowCount': 999}
        facts = result_facts(value, 'capture')
        self.assertEqual(len(facts), 3)
        self.assertEqual(facts[1]['value'], '123456789.1200')
        self.assertEqual(facts[1]['unit'], '元')
        self.assertEqual(facts[1]['scope'], '部门甲')
        self.assertEqual(facts[2]['value'], '-3.40')
        self.assertNotIn('sensitive', json.dumps(facts))
        self.assertNotIn('999', json.dumps(facts))
        self.assertTrue(all(fact['sourceId'] == 'capture' for fact in facts))

    def test_unknown_unit_does_not_become_money(self):
        value = table()
        del value['data']['columns'][1]['unit']
        self.assertEqual(len(result_facts(value, 'capture')), 1)

    def test_missing_group_is_not_silently_presented_as_a_period_total(self):
        value = table()
        value['data']['rows'][0][0] = None
        facts = result_facts(value, 'capture')
        self.assertIn('未标注', facts[1]['scope'])

    def test_batch_counts_are_not_entity_counts_or_complete_population(self):
        value = {'data': {'stepResults': [
            {'resultType': 'ENTITY_SET<X>', 'rows': [1]},
            {'batchResults': [{'data': {**table()['data'], 'aggregationSummary': {'detailRowCount': 42}}}]},
        ]}}
        facts = result_facts(value, 'capture')
        self.assertEqual(facts[0]['value'], 2)
        self.assertEqual(facts[1]['value'], 42)
        self.assertIn('本次返回', facts[0]['text'])

    def test_empty_batch_envelope_is_not_a_zero_row_business_result(self):
        value = {'data': {'stepResults': [{'rows': [], 'rowCount': 0,
            'batchResults': [{'data': {**table()['data'], 'aggregationSummary': {'detailRowCount': 42}}}]}]}}
        facts = result_facts(value, 'capture')
        self.assertEqual([f['value'] for f in facts], [2, 42, '123456789.1200', '-3.40'])
        self.assertNotIn('0 条', json.dumps(facts, ensure_ascii=False))


class ProgressStreamTests(IsolatedAsyncioTestCase):
    def test_reply_completion_does_not_promote_failed_receipt_to_success(self):
        tracker = ReplyProgress()
        tracker.observe(ReplyStartEvent(session_id='session', reply_id='reply', name='agent'))
        tracker.observe(ToolCallStartEvent(reply_id='reply', tool_call_id='failed', tool_call_name='Calculate'))
        tracker.observe(ToolResultTextDeltaEvent(reply_id='reply', tool_call_id='failed',
            delta=json.dumps({'error': {'message': '无效输入'}})))
        # Some tools successfully deliver an error receipt at the transport level.
        tracker.observe(ToolResultEndEvent(reply_id='reply', tool_call_id='failed', state='success'))
        self.assertEqual(tracker.step['status'], 'error')
        tracker.observe(ReplyEndEvent(session_id='session', reply_id='reply', finished_reason='completed'))
        self.assertEqual(tracker.step['status'], 'error')
        self.assertIn('仍未成功', tracker.step['nextAction'])

    def test_parallel_results_coalesce_and_ignore_thinking(self):
        tracker = ReplyProgress()
        summaries = []
        for event in events(two=True):
            updates = tracker.observe(event)
            if event.type == 'TOOL_RESULT_END' and event.tool_call_id == 'one':
                self.assertEqual(updates, [])
            summaries.extend(updates)
        serialized = json.dumps([event.value for event in summaries], ensure_ascii=False)
        self.assertNotIn('secret reasoning', serialized)
        self.assertIn('123456789.1200', serialized)
        self.assertEqual(tracker.step['nextAction'], '')  # No next call selected yet.
        self.assertEqual(len(tracker.value['steps']), 1)

    async def test_live_snapshot_equals_persisted_terminal_and_replay_is_idempotent(self):
        async def source():
            for event in events():
                yield event
            yield ModelCallStartEvent(reply_id='reply', model_name='model')
            yield TextBlockStartEvent(reply_id='reply', block_id='answer')
            yield TextBlockDeltaEvent(reply_id='reply', block_id='answer', delta='final answer')
            yield ReplyEndEvent(session_id='session', reply_id='reply', finished_reason='completed')
        message = AssistantMsg('agent', [], id='reply')
        collected = []
        async for event in with_reply_progress(source()):
            collected.append(event)
            if isinstance(event, CustomEvent):
                apply_progress(message, event)
            elif event.type != 'REPLY_START':
                message.append_event(event)
        restored = Msg.model_validate_json(message.model_dump_json())
        self.assertEqual(restored.metadata, message.metadata)
        self.assertEqual(message.metadata['agentProgress']['steps'][-1]['status'], 'completed')
        self.assertEqual(len(message.metadata['agentProgress']['steps']), 1)
        self.assertEqual(collected[-1].metadata['agentProgress'], message.metadata['agentProgress'])
        before = deepcopy(message.metadata)
        for event in collected:
            if isinstance(event, CustomEvent):
                apply_progress(message, event)
        self.assertEqual(before, message.metadata)
        self.assertEqual(message.get_text_content(), 'final answer')

    def test_next_selected_operation_updates_plan_without_copying_previous_summary(self):
        tracker = ReplyProgress()
        for event in events():
            tracker.observe(event)
        self.assertEqual(tracker.observe(ModelCallStartEvent(reply_id='reply', model_name='model')), [])
        tracker.observe(ToolCallStartEvent(reply_id='reply', tool_call_id='next', tool_call_name='AnalysisReview'))
        tracker.observe(ModelCallEndEvent(reply_id='reply', input_tokens=1, output_tokens=1))
        self.assertEqual(len(tracker.value['steps']), 1)
        self.assertIn('123456789.1200', tracker.step['summary'])
        self.assertIn('检查分析结果', tracker.step['nextAction'])

    def test_repeated_receipts_and_bookkeeping_do_not_add_boilerplate(self):
        tracker = ReplyProgress()
        for event in events():
            tracker.observe(event)
        for call_id, name, receipt in [('repeat', 'query', table()), ('guide', 'Skill', {})]:
            tracker.observe(ModelCallStartEvent(reply_id='reply', model_name='model'))
            tracker.observe(ToolCallStartEvent(reply_id='reply', tool_call_id=call_id, tool_call_name=name))
            tracker.observe(ModelCallEndEvent(reply_id='reply', input_tokens=1, output_tokens=1))
            tracker.observe(ToolResultTextDeltaEvent(reply_id='reply', tool_call_id=call_id, delta=json.dumps(receipt)))
            tracker.observe(ToolResultEndEvent(reply_id='reply', tool_call_id=call_id, state='success'))
        self.assertEqual(len(tracker.value['steps']), 1)
        self.assertEqual(tracker.step['nextAction'], '')
        self.assertNotIn('执行所选操作', json.dumps(tracker.value, ensure_ascii=False))

    def test_next_action_uses_selected_arguments_not_private_reasoning(self):
        tracker = ReplyProgress()
        for event in events():
            tracker.observe(event)
        tracker.observe(ModelCallStartEvent(reply_id='reply', model_name='model'))
        tracker.observe(ToolCallStartEvent(reply_id='reply', tool_call_id='lookup', tool_call_name='discover_query_targets'))
        tracker.observe(ToolCallDeltaEvent(reply_id='reply', tool_call_id='lookup', delta=json.dumps({
            'taskKeyword': '年度预算二上终版', 'keyword': '净利润', 'year': 2026})))
        tracker.observe(ModelCallEndEvent(reply_id='reply', input_tokens=1, output_tokens=1))
        plan = tracker.step['nextAction']
        self.assertIn('2026年', plan)
        self.assertIn('年度预算二上终版', plan)
        self.assertIn('净利润', plan)
        self.assertNotIn('secret reasoning', plan)

    async def test_progress_uses_host_facts_without_auxiliary_tasks(self):
        before = asyncio.all_tasks()
        async def source():
            for event in events():
                yield event
            yield ReplyEndEvent(session_id='session', reply_id='reply', finished_reason='completed')
        result = [event async for event in with_reply_progress(source())]
        self.assertEqual(asyncio.all_tasks(), before)
        self.assertEqual(result[-1].type, 'REPLY_END')
        summary = result[-1].metadata['agentProgress']['steps'][-1]['summary']
        self.assertIn('123456789.1200', summary)
        self.assertNotIn('999', summary)

    async def test_source_error_preserves_observed_progress(self):
        async def source():
            for event in events():
                yield event
            raise RuntimeError('model unavailable')
        result = []
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            async for event in with_reply_progress(source()):
                result.append(event)
        self.assertEqual(result[-1].value['steps'][-1]['status'], 'error')
        self.assertIn('123456789.1200', result[-1].value['steps'][-1]['summary'])

    def test_background_receipt_does_not_claim_data_or_completion(self):
        tracker = ReplyProgress()
        for event in events():
            if event.type == 'TOOL_RESULT_END':
                event.metadata['backgroundToolCallId'] = 'one'
            tracker.observe(event)
        self.assertEqual(tracker.step['facts'], [])
        self.assertEqual(tracker.step['status'], 'waiting')
        end = ReplyEndEvent(session_id='session', reply_id='reply', finished_reason='completed',
                            metadata={'analysisCompletion': {'status': 'WAITING'}})
        tracker.observe(end)
        self.assertEqual(end.metadata['agentProgress']['steps'][-1]['status'], 'waiting')

    def test_interruption_clears_future_plan(self):
        tracker = ReplyProgress()
        for event in events():
            tracker.observe(event)
        tracker.observe(ReplyEndEvent(session_id='session', reply_id='reply', finished_reason='interrupted'))
        self.assertEqual(tracker.step['status'], 'interrupted')
        self.assertNotIn('接下来', tracker.step['nextAction'])

    def test_real_background_result_is_observed_but_unrelated_hints_are_not(self):
        tracker = ReplyProgress()
        tracker.observe(ReplyStartEvent(session_id='session', reply_id='reply', name='agent'))
        self.assertEqual(tracker.observe(HintBlockEvent(reply_id='reply', block_id='other',
            source='{"label":"System"}', hint='secret internal instructions 999')), [])
        update = tracker.observe(HintBlockEvent(reply_id='reply', block_id='background',
            source=json.dumps({'label': 'tool_output', 'backgroundToolCallId': 'actual',
                               'backgroundState': 'success', 'backgroundToolName': 'query'}),
            hint=json.dumps(table())))
        self.assertIn('123456789.1200', update[0].value['steps'][-1]['summary'])
        self.assertNotIn('999', json.dumps(update[0].value))

    def test_resume_reuses_reply_snapshot_without_restarting_revision(self):
        previous = AssistantMsg('agent', [ToolCallBlock(id='one', name='query', input='{}')], id='reply',
            metadata={'agentProgress': {'version': 1, 'replyId': 'reply', 'revision': 20, 'steps': []}})
        tracker = ReplyProgress(previous)
        update = tracker.observe(RequireUserConfirmEvent(reply_id='reply',
            tool_calls=[ToolCallBlock(id='one', name='query', input='{}')]))
        self.assertEqual(update[0].value['revision'], 21)
        self.assertEqual(tracker.step['status'], 'waiting')
