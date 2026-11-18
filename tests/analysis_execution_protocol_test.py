"""Synthetic host authority tests; these do not claim container isolation."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import analysis_fixtures
from analysis_extension.execution_inputs import InputBroker, InputRequestError, delivery_summary
from analysis_extension.execution_protocol import read_message, write_message
from analysis_extension.execution_resources import ExecutionPolicy, Admission
from analysis_extension.python_program import evaluate, validate_code


class HostInputTests(unittest.TestCase):
    def test_image_worker_dependency_closure_excludes_host_services(self):
        import ast
        import sys
        from analysis_extension.execution_environment import ROOT, WORKER_FILES
        modules = {Path(name).stem for name in WORKER_FILES}
        for name in WORKER_FILES:
            for node in ast.walk(ast.parse((ROOT / name).read_text('utf-8'))):
                if isinstance(node, ast.Import):
                    imports = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [(node.module or '').split('.')[0]]
                else:
                    continue
                self.assertTrue(all(item in modules or item in sys.stdlib_module_names for item in imports), (name, imports))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cells = [{'key': str(i), 'row': i, 'column': 1, 'measureCode': 'amount',
                  'rawValueJson': json.dumps(str(i)), 'dimensions': {}, 'headers': []} for i in range(7)]
        raw = b''.join(json.dumps(cell).encode() + b'\n' for cell in cells)
        path = self.root / 'grid.jsonl'
        path.write_bytes(raw)
        self.grid = {'ref': 'grid-A', 'kind': 'GRID_FILE', '_path': str(path),
                     'sha256': hashlib.sha256(raw).hexdigest(), 'byteSize': len(raw), 'cellCount': 7}
        self.broker = InputBroker([self.grid], self.root / 'journal.jsonl', block_rows=2)
        self.addCleanup(lambda: self.broker.finish() if not self.broker.journal.closed else None)
        self.broker.inline_pending()
        self.broker.inline_sent()

    def test_full_delivery_replay_is_idempotent_and_preserves_every_record(self):
        cursor = self.broker.open('grid-A', {})['cursor']
        keys = []
        for sequence in range(4):
            response, pending = self.broker.next(cursor, sequence)
            self.broker.sent(pending)
            keys.extend(row['key'] for row in response['rows'])
            repeated, pending = self.broker.next(cursor, sequence)
            self.assertEqual(repeated, response)
            self.assertIsNone(pending)
        read = self.broker.manifest['reads'][0]
        self.assertEqual(keys, [str(i) for i in range(7)])
        self.assertEqual(read['sentCount'], 7)
        self.assertTrue(read['sendComplete'])
        self.assertTrue(self.broker.complete)
        self.assertNotIn('_path', self.broker.public_inputs[0])

    def test_early_stop_and_pending_send_cannot_claim_full_input(self):
        cursor = self.broker.open('grid-A', {})['cursor']
        response, pending = self.broker.next(cursor, 0)
        self.assertFalse(self.broker.complete)
        self.assertEqual(self.broker.manifest['reads'][0]['sentCount'], 0)
        self.broker.sent(pending)
        self.broker.close_cursor(cursor)
        self.assertTrue(self.broker.complete)
        self.assertFalse(self.broker.manifest['reads'][0]['sendComplete'])
        self.assertEqual(delivery_summary(self.broker.manifest)['sources'][0]['sentCount'], 2)

    def test_parallel_cursors_cannot_accumulate_unbounded_host_buffers(self):
        first = self.broker.open('grid-A', {})['cursor']
        _, pending = self.broker.next(first, 0)
        self.broker.sent(pending)
        self.broker.cache_bytes = self.broker.cursors[first]['lastBytes'] + 1
        second = self.broker.open('grid-A', {})['cursor']
        with self.assertRaisesRegex(ValueError, '读取缓存超过容量'):
            self.broker.next(second, 0)
        self.assertFalse(self.broker.complete)
        self.assertIn('INPUT_BUFFER_LIMIT', self.broker.manifest['violations'])

    def test_closing_cursor_releases_cached_blocks_for_next_source(self):
        first = self.broker.open('grid-A', {})['cursor']
        _, pending = self.broker.next(first, 0)
        self.broker.sent(pending)
        self.broker.cache_bytes = self.broker.cursors[first]['lastBytes'] + 1
        self.broker.close_cursor(first)
        second = self.broker.open('grid-A', {})['cursor']
        response, pending = self.broker.next(second, 0)
        self.broker.sent(pending)
        self.assertEqual(len(response['rows']), 2)
        self.assertTrue(self.broker.complete)

    def test_filtered_delivery_does_not_confuse_scan_count_with_sent_count(self):
        cursor = self.broker.open('grid-A', {'startRow': 5})['cursor']
        for sequence in range(2):
            response, pending = self.broker.next(cursor, sequence)
            self.broker.sent(pending)
        read = self.broker.manifest['reads'][0]
        self.assertEqual((read['scannedCount'], read['sentCount']), (7, 2))
        self.assertTrue(read['sendComplete'])

    def test_model_summary_does_not_repeat_the_complete_inline_reference_list(self):
        manifest = {'inlineRefs': ['input-' + str(i) for i in range(1000)], 'inlineComplete': True, 'reads': []}
        summary = delivery_summary(manifest)
        self.assertEqual(summary['inlineCount'], 1000)
        self.assertEqual(summary['inlineSelection'], 'ALL_FROZEN_NON_GRID_INPUTS')
        self.assertNotIn('inlineRefs', summary)
        self.assertEqual(len(manifest['inlineRefs']), 1000)

    def test_cross_run_cursor_and_unauthorized_reference_poison_success(self):
        with self.assertRaises(ValueError):
            self.broker.next('another-run', 0)
        with self.assertRaises(ValueError):
            self.broker.open('../secret', {})
        self.assertFalse(self.broker.complete)

    def test_invalid_filter_can_be_corrected_without_losing_authority(self):
        with self.assertRaises(InputRequestError):
            self.broker.open('grid-A', {'mapType': 'INPUT'})
        self.assertTrue(self.broker.complete)

    def test_protocol_roundtrip_and_size_limit(self):
        from analysis_extension.execution_protocol import decode
        for payload in (b'{"type":"RESULT","type":"ERROR"}', b'{"type":"RESULT","value":NaN}'):
            with self.assertRaises(ValueError):
                decode(payload)
        channel = io.BytesIO()
        write_message(channel, {'type': 'READ_OPEN', 'sourceRef': 'A'})
        channel.seek(0)
        self.assertEqual(read_message(channel)['sourceRef'], 'A')
        with self.assertRaises(ValueError):
            write_message(io.BytesIO(), {'type': 'RESULT', 'text': 'x' * 100}, limit=20)

    def test_free_language_decimal_type_and_half_up_are_separate_from_authority(self):
        result = evaluate('''
from decimal import Decimal as Money
class Computation:
    def apply(self, x):
        try:
            assert isinstance(x, Money)
            return getattr(x, 'copy_abs')().normalize().scaleb(0).quantize(Money('.01'))
        except ValueError:
            raise
result = [{'value': Computation().apply(D('-1.005')), 'ratio': 1 / 8}]
''', [], lambda *args: iter(()))
        self.assertEqual(result, [{'value': '1.01', 'ratio': '0.125'}])
        validate_code('import os\nclass Normal: pass\nresult=[]')
        self.assertEqual(evaluate("from pathlib import PurePosixPath\nresult=[{'path':str(PurePosixPath('/tmp') / 'data.csv')}]",
                                  [], lambda *args: iter(())), [{'path':'/tmp/data.csv'}])


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_queued_task_releases_its_lease(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            gate = Admission(ExecutionPolicy(max_parallel=1), {'NCPU':8,'MemTotal':16*1024**3}, directory)
            await gate.acquire('a', 'a', 'container-a', 10)
            queued = asyncio.create_task(gate.acquire('b', 'b', 'container-b', 10))
            await asyncio.sleep(.1)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            gate.release('a', True)
            await gate.acquire('c', 'c', 'container-c', 10)
            gate.release('c', True)

    def test_half_up_matches_calculate_products_and_rendering(self):
        from decimal import Decimal
        from numeric_tools import calculate
        from analysis_extension.python_products import product_number
        from analysis_extension.formatting import number_text
        for raw, expected in [('1.005','1.01'), ('-1.005','-1.01'), ('2.345','2.35')]:
            with self.subTest(raw=raw):
                self.assertEqual(calculate(raw, decimalPlaces=2)['value'], expected)
                self.assertEqual(product_number(raw, 2), expected)
                self.assertEqual(number_text(raw, {'decimalPlaces':2}), expected)
                self.assertEqual(evaluate('result=[{"v":D(inputs[0]["value"]).quantize(D(".01"))}]',
                    [{'value':raw}], lambda *args: iter(()))[0]['v'], expected)

    async def test_removed_container_and_auto_remove_race_are_cleanup_success(self):
        from analysis_extension.execution_environment import DockerBackend
        plan = {'docker':'docker','container':'analysis-owned','scope':'scope','policy':ExecutionPolicy()}
        for responses in ([(1, 'error: no such object: analysis-owned')],
                          [(0, '{"agentscope.analysis.runtime":"scope"}'), (1, 'removal race'),
                           (1, 'Error: No such container: analysis-owned')]):
            with patch('analysis_extension.execution_environment.command_output', AsyncMock(side_effect=responses)):
                self.assertTrue(await DockerBackend()._remove(plan))
        with patch('analysis_extension.execution_environment.command_output', AsyncMock(return_value=(1, 'permission denied'))):
            self.assertFalse(await DockerBackend()._remove(plan))

    async def test_separate_instances_share_queue_and_resource_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ExecutionPolicy(max_parallel=1, queue_seconds=0.05)
            first = Admission(policy, {'NCPU': 8, 'MemTotal': 16 * 1024**3}, directory)
            second = Admission(policy, {'NCPU': 8, 'MemTotal': 16 * 1024**3}, directory)
            await first.acquire('run-a', 'session-a', 'container-a', 10)
            with self.assertRaisesRegex(Exception, '等待分析执行额度'):
                await second.acquire('run-b', 'session-b', 'container-b', 10)
            first.release('run-a', True)
            await second.acquire('run-c', 'session-c', 'container-c', 10)
            second.release('run-c', True)
