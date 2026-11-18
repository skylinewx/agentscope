"""Redis 摘要与完整会话分离，故障只重试读取，认证不误报失效。"""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from storage_redis_test import make_storage, make_session_config
from agentscope.state import AgentState
from agentscope.message import UserMsg, AssistantMsg, ToolCallBlock, ToolCallState
from agentscope.app.storage._errors import StorageTemporarilyUnavailable


class SessionSummaryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = make_storage()
        self.addAsyncCleanup(self.storage._client.aclose)
        self.user, self.agent = 'summary-user', 'summary-agent'

    async def create(self):
        state = AgentState(context=[UserMsg('user', 'large-evidence-' * 20000)])
        return await self.storage.upsert_session(self.user, self.agent, make_session_config(), state)

    async def test_summary_is_small_full_api_remains_complete(self):
        record = await self.create()
        full_key = self.storage._key(self.storage.key_config.session, user_id=self.user, session_id=record.id)
        full_size = len(await self.storage._client.get(full_key))
        summary_size = len(await self.storage._client.get(self.storage._summary_key(self.user, record.id)))
        self.assertLess(summary_size, full_size / 10)
        summaries = await self.storage.list_session_summaries(self.user, self.agent)
        self.assertEqual(summaries[0].session.state.context, [])
        full = await self.storage.list_sessions(self.user, self.agent)
        self.assertEqual(full[0].state.context, record.state.context)

    async def test_mutation_updates_name_parked_status_and_ttl_together(self):
        self.storage.key_ttl = 30
        record = await self.create()
        def update(value):
            value.config.name = 'updated'
            value.state.context = [AssistantMsg('assistant', [ToolCallBlock(id='pending-call', name='pending', input='{}', state=ToolCallState.SUBMITTED)])]
        await self.storage.mutate_session(self.user, self.agent, record.id, update)
        summaries = await self.storage.list_session_summaries(self.user, self.agent)
        self.assertEqual(summaries[0].session.config.name, 'updated')
        self.assertEqual(summaries[0].parked_status, 'awaiting_external_result')
        ttl = await self.storage._client.ttl(self.storage._summary_key(self.user, record.id))
        self.assertTrue(28 <= ttl <= 30)

    async def test_backfill_expiry_and_delete_never_return_orphans(self):
        record = await self.create()
        summary_key = self.storage._summary_key(self.user, record.id)
        await self.storage._client.delete(summary_key)
        summaries = await self.storage.list_session_summaries(self.user, self.agent)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(await self.storage._client.exists(summary_key))
        await self.storage.delete_session(self.user, self.agent, record.id)
        self.assertFalse(await self.storage._client.exists(summary_key))
        record = await self.create()
        full_key = self.storage._key(self.storage.key_config.session, user_id=self.user, session_id=record.id)
        await self.storage._client.delete(full_key)
        self.assertEqual(await self.storage.list_session_summaries(self.user, self.agent), [])

    async def test_read_timeout_retries_once_and_then_reports_unavailable(self):
        from redis.exceptions import TimeoutError
        read = AsyncMock(side_effect=[TimeoutError(), 'ok'])
        self.assertEqual(await self.storage._read('fixture', read), 'ok')
        self.assertEqual(read.await_count, 2)
        read = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(StorageTemporarilyUnavailable):
            await self.storage._read('fixture', read)
        self.assertEqual(read.await_count, 2)

    async def test_backfill_cannot_overwrite_a_concurrent_newer_record(self):
        record = await self.create()
        summary_key = self.storage._summary_key(self.user, record.id)
        full_key = self.storage._key(self.storage.key_config.session, user_id=self.user, session_id=record.id)
        await self.storage._client.delete(summary_key)
        original_pipeline = self.storage._client.pipeline
        changed = False

        def interleaved_pipeline(*args, **kwargs):
            pipeline = original_pipeline(*args, **kwargs)
            original_get = pipeline.get

            async def interleaved_get(key):
                nonlocal changed
                raw = await original_get(key)
                if key == full_key and not changed:
                    changed = True
                    await self.storage.mutate_session(self.user, self.agent, record.id,
                        lambda value: setattr(value.config, 'name', 'concurrent-update'))
                return raw

            pipeline.get = interleaved_get
            return pipeline

        with patch.object(self.storage._client, 'pipeline', side_effect=interleaved_pipeline):
            summary = await self.storage._backfill_session_summary(self.user, record.id)
        self.assertEqual(summary.session.config.name, 'concurrent-update')
        summaries = await self.storage.list_session_summaries(self.user, self.agent)
        self.assertEqual(summaries[0].session.config.name, 'concurrent-update')

    async def test_unknown_mutation_outcome_is_not_retried(self):
        from redis.exceptions import TimeoutError
        record = await self.create()
        original = self.storage._client.pipeline
        execute = AsyncMock(side_effect=TimeoutError())
        def failing_pipeline(*args, **kwargs):
            pipeline = original(*args, **kwargs)
            pipeline.execute = execute
            return pipeline
        with patch.object(self.storage._client, 'pipeline', side_effect=failing_pipeline):
            with self.assertRaises(TimeoutError):
                await self.storage._write_session('fixture-key', record)
        self.assertEqual(execute.await_count, 1)
