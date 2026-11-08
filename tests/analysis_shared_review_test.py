from copy import deepcopy
import json
from unittest import TestCase
from analysis_extension.review_encoding import pack_shared_review_values, pack_review_records, unpack_review_records


class SharedReviewTest(TestCase):
    def test_dimension_dictionary_is_lossless_for_large_multi_task_forms(self):
        material = {'facts': [{'factId': f'fact-{index}', 'value': None if index % 7 == 0 else str(index),
                              'valueState': 'NULL' if index % 7 == 0 else 'PRESENT',
                              'dimensions': {'DATATIME': f'{2024 + index % 3}N0001', 'MD_ORG': 'GT00',
                                             'MD_BUDACCOUNT': f'{index % 38:04}', 'MD_STAGE': f'S{index % 4}',
                                             **({'optional': None if index % 2 else False} if index % 3 else {})}}
                             for index in range(1520)]}
        before = deepcopy(material)
        packed = pack_review_records(material)
        self.assertIsInstance(packed['facts']['_recordTable']['valueSets']['dimensions'], dict)
        self.assertIn('_recordTable', packed['facts']['_recordTable']['valueSets']['dimensions'])
        self.assertEqual(unpack_review_records(packed), before)
        self.assertEqual(material, before)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(material)) // 3)

    def test_large_repeated_contexts_roundtrip_without_changing_values_scope_or_failures(self):
        context = {'task': '年度预算', 'period': '2026N0001', 'dimensions': {'ORG': 'GT00'},
                   'binding': 'source/' + 'opaque-binding/' * 120}
        material = {'sources': [{'context': deepcopy(context), 'value': value, 'missing': None,
                                 'failed': index % 3 == 0, 'scope': ['A', 'B']}
                                for index, value in enumerate(['9007199254740993.01', None, '0', '-5.50'] * 40)]}
        before = deepcopy(material)
        packed = pack_shared_review_values(material)
        self.assertEqual(packed['sharedValueEncoding'], 1)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(material)) // 4)
        values = packed['reviewSharedValues']

        def restore(value):
            if isinstance(value, dict):
                if set(value) == {'_sharedValueRef'}:
                    return deepcopy(values[value['_sharedValueRef']])
                return {key: restore(child) for key, child in value.items()
                        if key not in ('sharedValueEncoding', 'reviewSharedValues')}
            if isinstance(value, list):
                return [restore(child) for child in value]
            return value
        self.assertEqual(restore(packed), before)
        self.assertEqual(material, before)

    def test_source_marker_collision_does_not_reinterpret_business_data(self):
        material = {'records': [{'payload': 'x' * 300, '_sharedValueRef': 3} for _ in range(200)]}
        self.assertEqual(pack_shared_review_values(material), material)
