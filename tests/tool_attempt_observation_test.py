"""入口观测覆盖参数拒绝、权限等待和实际执行，不能改变工具行为。"""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from agentscope.agent import Agent
from agentscope.event import ToolResultEndEvent
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk, Toolkit
from utils import MockModel


class ObservedTool(ToolBase):
    name = 'observed'
    description = '观测回归'
    input_schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}
    is_concurrency_safe = True
    is_read_only = True

    async def check_permissions(self, *args, **kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='test')

    async def call(self, value):
        return ToolChunk(content=[TextBlock(text=str(value))])


class Observer(MiddlewareBase):
    def __init__(self):
        self.events = []
        self.fail = False

    async def on_tool_attempt(self, agent, attempt):
        self.events.append(attempt.copy())
        attempt['status'] = 'cannot-change-original'
        if self.fail:
            raise RuntimeError('观测故障不应改变结果')


class ToolAttemptTests(IsolatedAsyncioTestCase):
    async def execute(self, arguments='{"value": 3}', behavior=PermissionBehavior.ALLOW, name='observed', external=False):
        self.observer = Observer()
        tool = ObservedTool()
        tool.is_external_tool = external
        self.agent = Agent(name='attempt-test', system_prompt='test', model=MockModel(), toolkit=Toolkit(tools=[tool]), middlewares=[self.observer])
        self.agent._check_permission = AsyncMock(return_value=PermissionDecision(behavior=behavior, message='test'))
        return [event async for event in self.agent._execute_tool_call(ToolCallBlock(id='call', name=name, input=arguments))]

    async def test_validation_failure_and_missing_tool_are_observed_before_acting(self):
        for name, arguments in [('observed', '{"value":"invalid"}'), ('missing', '{}')]:
            with self.subTest(name=name):
                events = await self.execute(arguments=arguments, name=name)
                self.agent._check_permission.assert_not_awaited()
                self.assertEqual([e['event'] for e in self.observer.events], ['START', 'END'])
                self.assertEqual(self.observer.events[-1]['phase'], 'VALIDATION')
                self.assertEqual(self.observer.events[-1]['status'], 'error')
                self.assertGreaterEqual(self.observer.events[-1]['validationMs'], 0)
                self.assertTrue(any(isinstance(e, ToolResultEndEvent) for e in events))

    async def test_execution_permission_denial_and_waiting_have_distinct_status(self):
        for behavior, external, status, phase in [
            (PermissionBehavior.ALLOW, False, 'success', 'EXECUTION'),
            (PermissionBehavior.DENY, False, 'denied', 'PERMISSION'),
            (PermissionBehavior.ASK, False, 'waiting_permission', 'PERMISSION'),
            (PermissionBehavior.ALLOW, True, 'waiting_external', 'EXECUTION')]:
            with self.subTest(status=status):
                await self.execute(behavior=behavior, external=external)
                self.assertEqual(len(self.observer.events), 2)
                end = self.observer.events[-1]
                self.assertEqual((end['status'], end['phase']), (status, phase))
                self.assertGreaterEqual(end['permissionMs'], 0)
                self.assertNotIn('arguments', end)

    async def test_observer_failure_does_not_change_success_and_cancellation_is_recorded(self):
        await self.execute()
        self.observer.events.clear()
        self.observer.fail = True
        events = [e async for e in self.agent._execute_tool_call(ToolCallBlock(id='next', name='observed', input='{"value":4}'))]
        self.assertEqual(next(e for e in events if isinstance(e, ToolResultEndEvent)).state, 'success')
        self.observer.fail = False
        self.agent._check_permission = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            _ = [e async for e in self.agent._execute_tool_call(ToolCallBlock(id='cancel', name='observed', input='{"value":4}'))]
        self.assertEqual(self.observer.events[-1]['status'], 'interrupted')
