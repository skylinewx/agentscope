"""Capacity must not remove source rows or split the inputs of a global Python calculation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from analysis_extension.models import AnalysisError
from analysis_extension.review_calculation_parts import calculation_review_parts
from analysis_extension.review_encoding import pack_review_records, pack_shared_review_values, unpack_review_records
from analysis_extension.turn_completion import analysis_review_payloads, compact_analysis_evidence
from analysis_review_inputs_test import restore_inputs


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def payload():
    rows = [{'factId': f'fact-{i}', 'value': None if i == 1 else str(i), 'unit': '元',
             'headers': ['实际观察' + str(i)], 'sourceContextId': 'source',
             'dimensions': {'YEAR': '2026', 'UNIT': str(i)}, 'provenance': str(i) * 400} for i in range(35)]
    inputs = [{'ref': item['factId'], **{k: v for k, v in item.items() if k != 'factId'}} for item in rows]
    return {'userRequest': '按原范围分析', 'documentText': '原始来源与计算结果的完整报告',
            'scope': {'members': list(range(35))}, 'sourceScopes': [{'rowCount': 35}],
            'facts': rows, 'relations': [], 'structureEvidence': [{'status': 'COMPLETE'}],
            'derived': [{'derivedId': 'result', 'operator': 'PYTHON', 'inputRefs': [r['ref'] for r in inputs],
                         'pythonRunRef': 'run1', 'value': '594', 'diagnostics': []}],
            'pythonRuns': [{'runRef': 'run1', 'inputRecords': inputs, 'code': 'emit(sum(values))', 'status': 'COMPLETED'}]}


class CalculationReviewPartsTests(unittest.TestCase):
    def test_source_scope_and_complete_execution_are_both_required_without_mutation(self):
        original = payload()
        before = deepcopy(original)
        limit = len(encode(original)) * 3 // 4
        parts = [json.loads(p) for p in calculation_review_parts(original, encode, limit, 32)]
        self.assertEqual(len(parts), 2)
        self.assertEqual(original, before)
        source, execution = parts
        for key in ('facts', 'sourceScopes', 'structureEvidence', 'derived'):
            self.assertEqual(source[key], original[key])
        self.assertEqual(execution['pythonRuns'], original['pythonRuns'])
        self.assertEqual(execution['derived'], original['derived'])
        for part in parts:
            for key in ('userRequest', 'scope', 'documentText'):
                self.assertEqual(part[key], original[key])
            self.assertTrue(part['evidencePartition']['allPartsRequired'])
            self.assertEqual(part['evidencePartition']['count'], 2)
            self.assertLess(len(encode(part)), limit)

    def test_large_executions_split_by_run_never_by_input_rows(self):
        original = payload()
        original['facts'] = original['facts'][:2]
        base = deepcopy(original['pythonRuns'][0])
        original['pythonRuns'] = [{**deepcopy(base), 'runRef': f'run{i}',
                                  'inputRecords': [{'ref': f'upstream-{i}', 'value': str(i)}] + base['inputRecords']}
                                 for i in range(3)]
        original['derived'] = [{'derivedId': f'out{i}', 'pythonRunRef': f'run{i}', 'value': str(i)} for i in range(3)]
        parts = [json.loads(p) for p in calculation_review_parts(original, encode, 50000, 32)]
        self.assertEqual(len(parts), 4)
        for index, part in enumerate(parts[1:]):
            self.assertEqual(part['pythonRuns'], [original['pythonRuns'][index]])
            self.assertEqual(part['derived'], [original['derived'][index]])
        self.assertEqual(parts[0]['derived'], original['derived'])

    def test_oversized_single_run_or_source_or_part_count_never_becomes_a_sample(self):
        original = payload()
        for limit, count in ((1000, 32), (len(encode(original)), 1)):
            self.assertIsNone(calculation_review_parts(original, encode, limit, count))
        original['facts'] = []
        self.assertIsNone(calculation_review_parts(original, encode, 1000, 32))

    def test_missing_snapshot_and_external_file_reads_keep_existing_capacity_checks(self):
        for change in ({'inputRecords': []}, {'inputRecords': None}, {'fileReads': [{'path': 'source.csv'}]}, {'runRef': None}):
            value = payload()
            value['pythonRuns'][0].update(change)
            self.assertIsNone(calculation_review_parts(value, encode, 100000, 32))

    def test_shared_projection_restores_code_inputs_nulls_and_facts_exactly(self):
        original = payload()
        original['pythonRuns'].append({**deepcopy(original['pythonRuns'][0]), 'runRef': 'run2'})
        compact = compact_analysis_evidence(original)
        shared = pack_shared_review_values(pack_review_records(compact))
        restored = unpack_review_records(shared)
        self.assertEqual(restored, compact)
        for actual, expected in zip(restored['pythonRuns'], original['pythonRuns']):
            self.assertEqual(restore_inputs(restored, actual), expected['inputRecords'])
            self.assertEqual(actual['code'], expected['code'])
        self.assertIsNone(restored['facts'][1]['value'])

    def test_normal_material_stays_single_and_truly_oversized_inputs_still_fail(self):
        value = payload()
        self.assertEqual(len(analysis_review_payloads(value)), 1)
        value['pythonRuns'][0]['code'] = 'x' * 50000
        with patch('analysis_extension.turn_completion.review_material_limit', return_value=30000):
            with self.assertRaises(AnalysisError) as caught:
                analysis_review_payloads(value)
        self.assertEqual(caught.exception.code, 'REVIEW_CAPACITY')
