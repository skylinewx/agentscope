"""覆盖性能优化的语义边界，不以放松数据校验换取速度。"""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from analysis_extension.review_encoding import pack_review_records, unpack_review_records
from analysis_extension.store import AnalysisStore
from analysis_extension.timing import timing_event, tool_timing_context, append_timing, record_timing
from analysis_fixtures import OWNER


class ReviewEncodingTests(TestCase):
    def test_mixed_values_missing_keys_and_nested_inputs_restore_exactly(self):
        records = [{'factId': 'f'+str(index), 'value': value,
                    'dimensions': {'year': 'p1', 'unit': 'u1'},
                    'inputs': [{'ref': 'r'+str(number), 'value': str(number), 'unit': '元'} for number in range(8)]}
                   for index, value in enumerate([None, '0', 0, False, '100.01', '-9.02'])]
        records[1]['optional'] = None
        records[2]['optional'] = False
        records[3]['optional'] = 0
        material = {'facts': records, 'documentText': '原始解释', 'derived': [{'inputRefs': ['f0', 'f1']}]}
        original = deepcopy(material)
        packed = pack_review_records(material)
        self.assertIn('_recordTable', packed['facts'])
        self.assertEqual(unpack_review_records(packed), original)
        self.assertEqual(material, original)
        self.assertIs(unpack_review_records(packed)['facts'][3]['value'], False)

    def test_source_reserved_field_is_not_reinterpreted(self):
        original = {'facts': [{'_recordTable': 'literal source'}] * 8}
        self.assertEqual(pack_review_records(original), original)

    def test_context_maps_and_repeated_dependencies_round_trip_without_type_loss(self):
        contexts = {f'context{index}': {'binding': ('binding-' + str(index % 3)) * 30,
                    'condition': {'org': index % 2, 'period': 'year'},
                    'inputRefs': ['fact-' + str(number) for number in range(20)],
                    'optional': [None, False, 0, '0'][index % 4]}
                    for index in range(30)}
        contexts['context2'].pop('optional')
        original = {'contexts': contexts}
        packed = pack_review_records(original)
        table = packed['contexts']['_recordTable']
        self.assertEqual(table['recordKeys'], list(contexts))
        self.assertIn('binding', table['valueSets'])
        self.assertEqual(unpack_review_records(packed), original)
        self.assertIs(unpack_review_records(packed)['contexts']['context1']['optional'], False)
        self.assertEqual(pack_review_records(packed), packed)


class MaintenanceAndTimingTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AnalysisStore(Path(self.temp.name) / 'data')

    def test_maintenance_interval_persists_across_instances(self):
        first = self.store.collect_expired_if_due(timestamp=1000)
        self.assertTrue(first['performed'])
        reopened = AnalysisStore(self.store.root)
        with patch.object(reopened, '_collect_expired', wraps=reopened._collect_expired) as collect:
            self.assertFalse(reopened.collect_expired_if_due(timestamp=1299)['performed'])
            collect.assert_not_called()
            self.assertTrue(reopened.collect_expired_if_due(timestamp=1300)['performed'])
            collect.assert_called_once()

    def test_failed_maintenance_does_not_delay_retry(self):
        with patch.object(self.store, '_collect_expired', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.store.collect_expired_if_due(timestamp=1000)
        self.assertTrue(self.store.collect_expired_if_due(timestamp=1001)['performed'])

    def test_timing_context_and_bounded_records(self):
        turn = SimpleNamespace(service=SimpleNamespace(owner=OWNER), state={'turnKey': 'turn'})
        with tool_timing_context('publication-call', 'AnalysisPublish'):
            event = timing_event(turn, 'reviewModel', 12, includedIn='publication')
            nested = timing_event(turn, 'tool', 2, callId='verify-call')
        self.assertEqual(event['callId'], 'publication-call')
        self.assertEqual(nested['parentCallId'], 'publication-call')
        self.assertNotIn('callId', timing_event(turn, 'mainModel', 3))
        saved = {}
        for _ in range(513):
            append_timing(saved, event)
        self.assertEqual(len(saved['timingEvents']), 512)
        self.assertEqual(saved['timingEventsOmitted'], 1)

    def test_observability_storage_failure_does_not_replace_business_result(self):
        turn = SimpleNamespace(service=SimpleNamespace(owner=OWNER), state={'turnKey': 'turn'})
        with patch('analysis_extension.review_control.progress', side_effect=OSError('unavailable')):
            with patch('analysis_extension.timing.logger.warning') as warning:
                record_timing(turn, 'pythonExecution', 12, status='COMPLETED')
        warning.assert_called_once()

    def test_independent_async_calls_keep_their_own_timing_context(self):
        import asyncio
        turn = SimpleNamespace(service=SimpleNamespace(owner=OWNER), state={'turnKey': 'turn'})

        async def run(call_id):
            with tool_timing_context(call_id, 'AnalysisInspect'):
                await asyncio.sleep(0)
                return timing_event(turn, 'read', 1)

        async def concurrent_calls():
            return await asyncio.gather(run('first'), run('second'))

        events = asyncio.run(concurrent_calls())
        self.assertEqual([event['callId'] for event in events], ['first', 'second'])
        self.assertNotIn('callId', timing_event(turn, 'mainModel', 1))
