"""合并分析计算后，核对材料共享输入而不改变执行证据。"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from analysis_extension.analysis_materials import share_item_fields
from analysis_extension.turn_completion import compact_analysis_evidence, analysis_review_payloads


def restore_inputs(payload, run):
    if 'inputRecords' in run:
        return deepcopy(run['inputRecords'])
    saved = payload['pythonInputSets'][run['inputSetRef']]
    restored = []
    for index, record in enumerate(saved['inputRecords']):
        context = saved['inputContexts'].get(record.get('inputContextRef'), {})
        original = {**context, **record}
        if record.get('inputContextRef') in saved['inputContexts']:
            original.pop('inputContextRef')
        for field in ('dimensions', 'dimensionNames'):
            if isinstance(context.get(field), dict):
                original[field] = {**context[field], **record.get(field, {})}
        link = saved.get('factInputLinks', {}).get(str(index))
        if link:
            source = next(deepcopy(fact) for fact in payload['facts'] if fact['factId'] == link[0])
            fact_context = payload.get('factContexts', {}).get(source.pop('factContextRef', None), {})
            source = {**payload.get('commonFactContext', {}), **fact_context, **source}
            for field in ('dimensions', 'dimensionNames'):
                common = payload.get('commonFactDimensions', {}).get(field, {})
                if common:
                    source[field] = {**common, **source.get(field, {})}
            original = {**{key: source[key] for key in saved['factInputFieldSets'][link[1]]}, **original}
        restored.append(original)
    return restored


def records():
    return [{'ref': 'f' + str(index), 'value': None if index % 3 == 0 else str(index),
             'valueState': 'NO_VALUE' if index % 3 == 0 else 'PRESENT',
             'sourceContextId': 'source-A', 'captureRef': 'capture-A',
             'headers': ['预算金额'], 'unit': '元', 'scale': '1',
             'dimensions': {'YEAR': '2026', 'UNIT': str(index), 'MODE': 'BUDGET'},
             'dimensionNames': {'YEAR': '2026年', 'UNIT': '单位' + str(index), 'MODE': '预算'},
             'description': '来源完整背景' * 50} for index in range(60)]


class ReviewInputTests(unittest.TestCase):
    def payload(self):
        inputs = records()
        inputs += [{'ref': 'native', 'operator': 'FORMULA_CHECK', 'value': None,
                    'formulaTrace': {'nodes': [{'observed': '3', 'recalculated': None}],
                                     'pendingDependencies': ['missing'], 'status': 'BLOCKED'},
                    'localValue': '3', 'localDifference': '-1'}]
        return {'facts': [], 'derived': [], 'relations': [], 'pythonRuns': [
            {'runRef': 'run' + str(index), 'code': 'emit(inputs)', 'inputRecords': deepcopy(inputs),
             'executionStatus': 'COMPLETED'} for index in range(3)],
            'toolFailures': [{'error': {'code': 'PYTHON_SYNTAX', 'message': '保留失败记录'}}]}

    def test_repeated_inputs_share_one_snapshot_and_reconstruct_exactly(self):
        original = self.payload()
        before = deepcopy(original)
        compact = compact_analysis_evidence(original)
        self.assertEqual(original, before)
        self.assertEqual(len(compact['pythonInputSets']), 1)
        for actual, expected in zip(compact['pythonRuns'], original['pythonRuns']):
            self.assertEqual(restore_inputs(compact, actual), expected['inputRecords'])
            self.assertEqual({key: value for key, value in actual.items() if key != 'inputSetRef'},
                             {key: value for key, value in expected.items() if key != 'inputRecords'})
        self.assertEqual(compact['toolFailures'], original['toolFailures'])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(original)) // 2)
        self.assertEqual(compact_analysis_evidence(compact), compact)

    def test_same_reference_different_values_and_order_stay_separate(self):
        payload = self.payload()
        payload['pythonRuns'][1]['inputRecords'][1]['value'] = '99'
        payload['pythonRuns'][2]['inputRecords'].reverse()
        compact = compact_analysis_evidence(payload)
        self.assertEqual(len(compact['pythonInputSets']), 3)
        for expected, actual in zip(payload['pythonRuns'], compact['pythonRuns']):
            self.assertEqual(restore_inputs(compact, actual), expected['inputRecords'])

    def test_null_missing_empty_maps_and_reserved_source_field_remain_distinct(self):
        payload = self.payload()
        payload['pythonRuns'][0]['inputRecords'] += [
            {'ref': 'x', 'value': None, 'dimensions': {}},
            {'ref': 'x', 'dimensions': {}},
            {'ref': 'x', 'value': None},
            {'ref': 'null-map-1', 'value': None, 'dimensions': None},
            {'ref': 'null-map-2', 'value': None, 'dimensions': None},
            {'ref': 'y', 'inputContextRef': 'source-owned', 'value': None}]
        compact = compact_analysis_evidence(payload)
        for expected, actual in zip(payload['pythonRuns'], compact['pythonRuns']):
            self.assertEqual(restore_inputs(compact, actual), expected['inputRecords'])

    def test_json_number_and_boolean_are_not_shared_as_equal(self):
        rows = [{'value': '1', 'flag': True, 'dimensions': {'X': True}},
                {'value': '2', 'flag': 1, 'dimensions': {'X': 1}}]
        common, compact = share_item_fields(rows)
        self.assertEqual(common, {})
        self.assertEqual(compact, rows)

    def test_small_and_empty_inputs_do_not_add_projection_overhead(self):
        for inputs in ([], [{'ref': 'f', 'value': '1'}]):
            payload = {'pythonRuns': [{'inputRecords': inputs}], 'facts': []}
            self.assertEqual(compact_analysis_evidence(payload), payload)

    def test_combined_calculations_fit_without_dropping_dependencies(self):
        payload = self.payload()
        payload['facts'] = [{'factId': record['ref'], 'value': record['value']} for record in records()]
        payload['derived'] = [{'derivedId': 'result', 'inputRefs': [fact['factId'] for fact in payload['facts']],
                               'pythonRunRef': 'run2', 'value': '42'}]
        before_size = len(json.dumps(payload, ensure_ascii=False))
        with patch('analysis_extension.turn_completion.QUERY_REVIEW_LIMIT', 30000):
            parts = analysis_review_payloads(payload)
        self.assertGreater(before_size, 60000)
        self.assertEqual(len(parts), 1)
        self.assertLess(len(parts[0]), 30000)
        from analysis_extension.review_encoding import unpack_review_records
        actual = unpack_review_records(json.loads(parts[0]))
        self.assertEqual(actual['derived'], payload['derived'])
        self.assertEqual({fact['factId'] for fact in actual['facts']},
                         set(payload['derived'][0]['inputRefs']))
        for expected, run in zip(payload['pythonRuns'], actual['pythonRuns']):
            self.assertEqual(restore_inputs(actual, run), expected['inputRecords'])

    def test_fact_snapshots_are_shared_field_by_field_without_changing_inputs(self):
        inputs = records()
        facts = [{'factId': row['ref'], **{key: value for key, value in row.items() if key != 'ref'}} for row in inputs]
        inputs[1]['value'] = '999'
        inputs[2].pop('unit')
        inputs[3]['dimensions']['YEAR'] = '2025'
        # 浅复制会共享维度，先把事实中不同快照恢复为明确的原年度。
        facts = deepcopy(facts)
        facts[3]['dimensions']['YEAR'] = '2026'
        inputs[4]['flag'] = True
        facts[4]['flag'] = 1
        inputs[5]['factInputRef'] = 'source-owned'
        payload = {'facts': facts, 'derived': [], 'relations': [], 'pythonRuns': [{'runRef': 'run', 'inputRecords': inputs}]}
        original = deepcopy(payload)
        compact = compact_analysis_evidence(payload)
        self.assertEqual(payload, original)
        self.assertEqual(restore_inputs(compact, compact['pythonRuns'][0]), inputs)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(payload)))
        self.assertEqual(compact_analysis_evidence(compact), compact)
