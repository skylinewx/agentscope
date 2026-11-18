import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from agentscope.event import CustomEvent
from agentscope.message import AssistantMsg
from agentscope.app._service._reply_progress import apply_progress
from analysis_extension.execution_progress import phase, stream_progress


class ExecutionProgressTests(IsolatedAsyncioTestCase):
    async def test_nested_review_is_streamed_before_it_finishes_and_persisted_in_order(self):
        service = SimpleNamespace()
        turn = SimpleNamespace(state={'turnKey': 't'}, service=service)
        agent = SimpleNamespace(state=SimpleNamespace(reply_id='r'))
        proceed = asyncio.Event()
        async def source():
            outer = await phase(turn, 'PUBLISHING', 'RUNNING')
            review = await phase(turn, 'BUSINESS_REVIEW', 'RUNNING', attempt=2)
            await proceed.wait()
            await phase(turn, 'BUSINESS_REVIEW', 'COMPLETE', operation_id=review)
            await phase(turn, 'PUBLISHING', 'COMPLETE', operation_id=outer)
            yield 'done'
        iterator = stream_progress(service, agent, source())
        first = await anext(iterator)
        second = await asyncio.wait_for(anext(iterator), 1)
        self.assertIsInstance(second, CustomEvent)
        self.assertEqual(second.value['stage'], 'BUSINESS_REVIEW')
        self.assertEqual(second.value['attempt'], 2)
        message = AssistantMsg('agent', [], id='r')
        apply_progress(message, second)
        apply_progress(message, first)
        self.assertEqual(message.metadata['analysisExecutionProgress'], second.value)
        proceed.set()
        rest = [event async for event in iterator]
        self.assertEqual(rest[0].value['stage'], 'PUBLISHING')
        self.assertEqual(rest[-1], 'done')
        self.assertIsNone(service.progress_notifier)

    async def test_cancel_closes_source_without_background_work_or_late_events(self):
        service = SimpleNamespace()
        turn = SimpleNamespace(state={}, service=service)
        agent = SimpleNamespace(state=SimpleNamespace(reply_id='r'))
        stopped = asyncio.Event()
        async def source():
            try:
                await phase(turn, 'CALCULATING', 'RUNNING')
                await asyncio.Event().wait()
                yield 'unexpected'
            finally:
                stopped.set()
        iterator = stream_progress(service, agent, source())
        await anext(iterator)
        await iterator.aclose()
        self.assertTrue(stopped.is_set())
        self.assertIsNone(service.progress_notifier)
