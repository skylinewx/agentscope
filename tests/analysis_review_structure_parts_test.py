from copy import deepcopy
import json
from unittest import TestCase

from analysis_extension.review_structure_parts import structure_review_parts


class StructureReviewPartsTests(TestCase):
    def test_all_structure_cells_are_reviewed_and_every_numeric_input_is_kept(self):
        cells = [{'key': str(index), 'formula': 'x' * 300, 'references': [str(index + 1)]} for index in range(50)]
        payload = {'facts': [{'factId': 'zero', 'value': '0'}, {'factId': 'null', 'value': None}],
                   'derived': [{'derivedId': 'sum', 'inputRefs': ['zero', 'null']}],
                   'pythonRuns': [{'inputRecords': [{'value': '0'}, {'value': None}]}],
                   'structureEvidence': [{'captureRef': 'original', 'coverage': 'PARTIAL', 'cells': cells}],
                   'toolFailures': [{'code': 'DEPENDENCY_UNAVAILABLE'}]}
        before = deepcopy(payload)
        encode = lambda item: json.dumps(item, ensure_ascii=False)
        parts = structure_review_parts(payload, encode, 6000, 20)
        self.assertGreater(len(parts), 1)
        decoded = [json.loads(part) for part in parts]
        self.assertEqual([cell for part in decoded for cell in part['structureEvidence'][0]['cells']], cells)
        for part in decoded:
            for key in ('facts', 'derived', 'pythonRuns', 'toolFailures'):
                self.assertEqual(part[key], payload[key])
            self.assertEqual(part['structureEvidence'][0]['coverage'], 'PARTIAL')
            self.assertEqual(part['structureEvidence'][0]['reviewCellCount'], len(cells))
            self.assertEqual(part['evidencePartition']['count'], len(parts))
            self.assertLessEqual(len(encode(part)), 6000)
        self.assertEqual(payload, before)

    def test_large_calculation_is_not_silently_sampled(self):
        self.assertIsNone(structure_review_parts({'facts': ['x' * 10000],
            'structureEvidence': [{'cells': [{'value': '1'}]}]}, json.dumps, 5000, 20))
