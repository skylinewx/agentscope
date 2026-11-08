"""后台维护不会挡住新提问，并发写入、回滚及重启仍保留真实依据。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from analysis_fixtures import OWNER
from analysis_extension.bootstrap import create_analysis_middlewares
from analysis_extension.maintenance import run_maintenance
from analysis_extension.models import digest
from analysis_extension.router import create_analysis_router
from analysis_extension.store import AnalysisStore
from analysis_extension import store_cleanup


class MaintenanceStorageTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AnalysisStore(Path(self.temp.name) / 'analysis-data')
        self.old = time.time() - self.store.limits.retention_seconds - 60

    def orphan(self, value):
        with self.store._transaction() as db:
            blob = self.store._put(db, {'value': value})
        path = self.store._blob_path(blob)
        os.utime(path, (self.old, self.old))
        return path

    def grid(self, name='a'):
        root = self.store.root / 'grid-files' / digest(OWNER.key)
        root.mkdir(parents=True, exist_ok=True)
        path = root / (name * 64 + '.jsonl')
        path.write_bytes(b'{}\n')
        os.utime(path, (self.old, self.old))
        return path

    def assert_usage(self):
        with self.store._transaction() as db:
            actual = db.execute('SELECT bytes FROM blob_usage WHERE id=1').fetchone()[0]
        self.assertEqual(actual, sum(path.stat().st_size for path in self.store.blobs.iterdir()))

    def test_orphans_removed_in_batches_and_quota_includes_unregistered_files(self):
        keep = self.store.capture(OWNER, {'value': 'retained'})
        paths = [self.orphan(index) for index in range(70)]
        unknown = self.store.blobs / 'external-file'
        unknown.write_bytes(b'not owned by the collector')
        abandoned = self.store.blobs / 'pending-abandoned'
        abandoned.write_bytes(b'crashed write')
        os.utime(abandoned, (self.old, self.old))
        grid = self.grid()
        result = self.store.collect_expired_if_due()
        self.assertTrue(result['performed'])
        self.assertEqual(result['blobsRemoved'], 71)
        self.assertEqual(result['gridFilesRemoved'], 1)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertFalse(grid.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(self.store.get_capture(OWNER, keep)['value'], 'retained')
        self.assert_usage()

    def test_slow_scan_releases_database_and_new_references_defer_deletion(self):
        data = {'value': 'reused'}
        ref = self.store.capture(OWNER, data)
        with self.store._transaction() as db:
            blob = db.execute('SELECT digest FROM captures WHERE ref=?', (ref,)).fetchone()[0]
            db.execute('DELETE FROM captures WHERE ref=?', (ref,))
        path = self.store._blob_path(blob)
        os.utime(path, (self.old, self.old))
        grid = self.grid()
        scanned, resume = Event(), Event()
        original = store_cleanup.scan_grids

        def slow_scan(*args):
            result = original(*args)
            scanned.set()
            if not resume.wait(5):
                raise TimeoutError('test did not release the scan')
            return result

        with patch.object(store_cleanup, 'scan_grids', slow_scan), ThreadPoolExecutor(2) as pool:
            cleanup = pool.submit(self.store.collect_expired_if_due)
            try:
                self.assertTrue(scanned.wait(3))
                # A different connection must commit while the filesystem scan is still paused.
                write = pool.submit(self.store.capture, OWNER, data)
                self.assertEqual(write.result(timeout=2), ref)
                grid_ref = pool.submit(self.store.capture, OWNER, {'gridFiles': [{'sha256': grid.stem}]}).result(timeout=2)
                self.assertFalse(cleanup.done())
            finally:
                resume.set()
            result = cleanup.result(timeout=3)
        self.assertTrue(result['deferred'])
        self.assertNotIn('completedAt', result)
        self.assertTrue(path.exists())
        self.assertTrue(grid.exists())
        self.assertIn('completedAt', self.store.collect_expired_if_due())
        self.assertTrue(self.store.get_capture(OWNER, grid_ref))
        self.assertTrue(path.exists())
        self.assertTrue(grid.exists())
        self.assert_usage()

    def test_rollback_during_scan_invalidates_quota_snapshot(self):
        self.orphan('old')
        original = store_cleanup.scan_grids

        def rolled_back_write(*args):
            result = original(*args)
            with self.assertRaises(RuntimeError):
                with self.store._transaction() as db:
                    self.store._put(db, {'uncommitted': 'x' * 1000})
                    raise RuntimeError('rollback after filesystem write')
            return result

        with patch.object(store_cleanup, 'scan_grids', rolled_back_write):
            self.assertTrue(self.store.collect_expired_if_due()['deferred'])
        self.assertIn('completedAt', self.store.collect_expired_if_due())
        self.assert_usage()
        self.assertFalse(list(self.store.root.glob('blob-write-*.pending')))

    def test_new_reference_between_delete_batches_is_preserved(self):
        first, second = self.orphan('first'), self.orphan('second')
        original = self.store._transaction
        inserted = False

        @contextmanager
        def interleaved_transaction(**kwargs):
            nonlocal inserted
            with original(**kwargs) as db:
                yield db
            if not inserted and (not first.exists() or not second.exists()):
                inserted = True
                survivor = first if first.exists() else second
                with original() as writer:
                    writer.execute('INSERT INTO semantic_reviews VALUES(?,?,?,?)',
                                   (OWNER.key, 'concurrent-reference', survivor.stem, time.time()))

        with patch.object(store_cleanup, 'BATCH_SIZE', 1), patch.object(self.store, '_transaction', interleaved_transaction):
            result = self.store.collect_expired_if_due()
        self.assertTrue(result['deferred'])
        self.assertEqual(sum(path.exists() for path in (first, second)), 1)
        self.assert_usage()

    def test_another_process_skips_active_maintenance_and_crash_lock_is_reusable(self):
        script = ('import sys; from pathlib import Path; '
                  'from analysis_extension.store_cleanup import maintenance_lock; '
                  'lock=maintenance_lock(Path(sys.argv[1])); print(lock.__enter__()); lock.__exit__(None,None,None)')
        with store_cleanup.maintenance_lock(self.store.root) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.store.collect_expired_if_due()['reason'], 'IN_PROGRESS')
            child = subprocess.run([sys.executable, '-c', script, str(self.store.root)],
                                   capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(child.stdout.strip(), 'False')
        # A process exiting without __exit__ also releases the OS-backed SQLite lock.
        crash_script = script.replace('print(lock.__enter__()); lock.__exit__(None,None,None)', 'print(lock.__enter__())')
        child = subprocess.run([sys.executable, '-c', crash_script, str(self.store.root)],
                               capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(child.stdout.strip(), 'True')
        self.assertTrue(self.store.collect_expired_if_due()['performed'])

    def test_stopping_during_scan_does_not_mark_completed_and_can_retry(self):
        path = self.orphan('keep until retry')
        stop = Event()
        original = store_cleanup.scan_blobs

        def stop_scan(*args):
            result = original(*args)
            stop.set()
            return result

        with patch.object(store_cleanup, 'scan_blobs', stop_scan):
            self.assertEqual(self.store.collect_expired_if_due(stop=stop)['reason'], 'STOPPING')
        self.assertTrue(path.exists())
        self.assertTrue(self.store.collect_expired_if_due()['performed'])
        self.assertFalse(path.exists())

    def test_no_old_grid_files_skips_reading_historical_captures(self):
        self.grid().touch()
        self.store.capture(OWNER, {'value': 'history'})
        with patch.object(self.store, '_read', side_effect=AssertionError('unnecessary history read')):
            self.assertTrue(self.store.collect_expired_if_due()['performed'])

    def test_refreshed_grid_file_is_not_deleted_using_old_scan(self):
        grid = self.grid()
        original = store_cleanup.scan_grids

        def reused_file(*args):
            candidates = original(*args)
            with self.store._transaction():
                os.utime(grid, None)
            return candidates

        with patch.object(store_cleanup, 'scan_grids', reused_file):
            self.assertEqual(self.store.collect_expired_if_due()['gridFilesRemoved'], 0)
        self.assertTrue(grid.exists())


class MaintenanceLifecycleTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'analysis-config.local.json'
        self.store = AnalysisStore(self.root / 'analysis-data')

    async def test_service_and_chat_start_while_cleanup_runs_and_shutdown_joins_worker(self):
        entered, exited = Event(), Event()

        def slow_cleanup(store, *args, stop=None, **kwargs):
            entered.set()
            if not stop.wait(5):
                raise TimeoutError('shutdown did not signal the worker')
            exited.set()
            return {'performed': False, 'reason': 'STOPPING'}

        app = FastAPI()
        app.state.resource_access_service = SimpleNamespace(get_resource=AsyncMock())
        app.state.storage = SimpleNamespace(get_session=AsyncMock(
            return_value=SimpleNamespace(user_id=OWNER.user_id, agent_id=OWNER.agent_id)))
        app.include_router(create_analysis_router(self.path))

        @app.get('/ping')
        async def ping():
            return {'ok': True}

        with patch.object(AnalysisStore, 'collect_expired_if_due', slow_cleanup):
            async with app.router.lifespan_context(app):
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
                    self.assertEqual((await asyncio.wait_for(client.get('/ping'), 2)).status_code, 200)
                middlewares = await asyncio.wait_for(create_analysis_middlewares(
                    app, OWNER.user_id, OWNER.agent_id, OWNER.session_id,
                    SimpleNamespace(workdir=str(self.root / 'workspace')), self.path), 2)
                self.assertEqual(len(middlewares), 1)
                self.assertFalse(exited.is_set())
        self.assertTrue(exited.is_set())

    async def test_failed_maintenance_retries_without_a_new_user_request(self):
        stop, calls = Event(), []

        def cleanup(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise OSError('temporary filesystem failure')
            stop.set()
            return {'performed': True}

        with patch.object(AnalysisStore, 'collect_expired_if_due', cleanup), patch('analysis_extension.maintenance.logger.exception') as log:
            await asyncio.wait_for(asyncio.to_thread(run_maintenance, self.path, stop, 0.001), 3)
        self.assertEqual(len(calls), 2)
        log.assert_called_once()

    async def test_disabled_capabilities_are_respected_and_configuration_is_reloaded(self):
        stop = Event()
        self.path.write_text(json.dumps({'schemaVersion': 1, 'activation': 'disabled'}), 'utf-8')
        original_wait = stop.wait

        def enable_on_poll(seconds):
            self.path.write_text(json.dumps({'schemaVersion': 1}), 'utf-8')
            return original_wait(0.001)

        def cleanup(*args, **kwargs):
            stop.set()
            return {'performed': True}

        with patch.object(stop, 'wait', side_effect=enable_on_poll) as poll, patch.object(AnalysisStore, 'collect_expired_if_due', cleanup):
            await asyncio.wait_for(asyncio.to_thread(run_maintenance, self.path, stop), 3)
        self.assertGreaterEqual(poll.call_count, 2)
