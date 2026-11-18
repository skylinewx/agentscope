from unittest import TestCase, IsolatedAsyncioTestCase
from agentscope.message import AssistantMsg, UserMsg, ToolCallBlock, ToolResultBlock, ToolResultState, ToolCallState
from agentscope.agent._tool_history_recovery import repair_tool_history
from agentscope.agent._tool_history_recovery import restore_persisted_tool_results


class PersistedRecoveryTests(IsolatedAsyncioTestCase):
    async def test_matching_durable_receipt_restores_success_without_reexecution(self):
        from unittest.mock import AsyncMock
        call = ToolCallBlock(id='call', name='Write', input='{"x":1}', state=ToolCallState.FINISHED)
        old = AssistantMsg('agent', [call], id='reply')
        saved = AssistantMsg('agent', [call.model_copy(deep=True),
            ToolResultBlock(id='call', name='Write', output='saved', state=ToolResultState.SUCCESS)], id='reply')
        read = AsyncMock(return_value=saved)
        self.assertEqual(await restore_persisted_tool_results([old], read), ['call'])
        read.assert_awaited_once_with('reply')
        self.assertEqual(repair_tool_history([old]), [])
        self.assertEqual(old.content[-1].output, 'saved')
        self.assertEqual(await restore_persisted_tool_results([old], read), [])

    async def test_changed_call_cannot_borrow_an_old_success_receipt(self):
        from unittest.mock import AsyncMock
        original = ToolCallBlock(id='a', name='Write', input='old')
        current = AssistantMsg('agent', [original.model_copy(update={'input':'new'})], id='r')
        saved = AssistantMsg('agent', [original, ToolResultBlock(id='a', name='Write', output='ok', state='success')], id='r')
        self.assertEqual(await restore_persisted_tool_results([current], AsyncMock(return_value=saved)), [])
        self.assertEqual(repair_tool_history([current]), ['a'])


class HistoryRecoveryTests(TestCase):
    def test_orphan_is_unknown_idempotent_and_never_reexecuted(self):
        call = ToolCallBlock(id='a', name='Write', input='{}', state=ToolCallState.FINISHED)
        message = AssistantMsg('agent', [call])
        history = [message, UserMsg('user', '继续')]
        self.assertEqual(repair_tool_history(history), ['a'])
        self.assertEqual(repair_tool_history(history), [])
        result = message.content[-1]
        self.assertIsInstance(result, ToolResultBlock)
        self.assertEqual(result.state, ToolResultState.ERROR)
        self.assertIn('执行结果未知', result.output)
        self.assertEqual(result.id, call.id)

    def test_complete_and_active_external_calls_are_not_changed(self):
        calls = [ToolCallBlock(id='a', name='Read', input='{}'),
                 ToolCallBlock(id='b', name='Write', input='{}', state=ToolCallState.ASKING),
                 ToolCallBlock(id='c', name='External', input='{}', state=ToolCallState.SUBMITTED),
                 ToolResultBlock(id='a', name='Read', output='ok', state=ToolResultState.SUCCESS)]
        self.assertEqual(repair_tool_history([AssistantMsg('agent', calls)]), [])
