"""准备阶段无模型调用的进度通知；故障观测不能中止业务执行。"""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from agentscope.app._service._run_progress import RunProgress


class RunProgressTests(IsolatedAsyncioTestCase):
    async def test_stages_are_correlated_and_timed_before_reply_start(self):
        with patch('agentscope.app._service._run_progress.perf_counter', side_effect=[10, 10, 12, 16]), patch(
            'agentscope.app._service._run_progress.publish_session_event', new_callable=AsyncMock) as publish:
            progress = RunProgress(object(), 'session', 'user-message')
            await progress.advance('PREPARING')
            await progress.advance('TOOLS')
            result = await progress.advance('READY')
        self.assertEqual(result['elapsedMs'], 6000)
        self.assertEqual(result['stages'], {'PREPARING': 2000, 'TOOLS': 4000})
        values = [call.args[2]['value'] for call in publish.await_args_list]
        self.assertEqual({value['messageId'] for value in values}, {'user-message'})
        self.assertEqual(len({value['runId'] for value in values}), 1)

    async def test_progress_notification_failure_is_nonfatal(self):
        with patch('agentscope.app._service._run_progress.publish_session_event', AsyncMock(side_effect=OSError('offline'))):
            result = await RunProgress(object(), 'session', 'user-message').advance('PREPARING')
        self.assertEqual(result['stage'], 'PREPARING')
