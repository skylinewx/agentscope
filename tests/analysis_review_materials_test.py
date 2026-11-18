"""只缩减无关定义与请求副本；事实、空值、读集及错误根因保持真实。"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from analysis_extension.grid_files import structure_profile
from analysis_extension.review_materials import focus_query_metadata, failure_summary


class ReviewMaterialsTest(unittest.TestCase):
    def test_discovery_without_resolved_context_keeps_all_candidates(self):
        evidence = {'sources': [{'captureRef': 'c', 'entities': [{'id': 'a'}, {'id': 'b'}]}]}
        before = deepcopy(evidence)
        focus_query_metadata(evidence)
        self.assertEqual(evidence, before)

    def test_resolved_context_keeps_names_and_original_evidence(self):
        facts = [{'objectId': 'org-a', 'measureCode': 'money', 'dimensions': {'period': 'p1'}, 'value': None},
                 {'objectId': 'org-b', 'value': '0'}]
        data = [{'columns': ['dimensions', 'value'], 'rows': [[{'product': 'product-a'}, None], [{}, '0']]}]
        run = {'fileInputs': [{'context': {'target': {'taskId': 'task-a', 'viewId': 'form-a'},
                                         'effectiveDimensions': {'period': 'p1'}}}], 'sourceData': data}
        entities = [{'id': 'task-a', 'label': '已选任务'}, {'id': 'task-b', 'label': '另一任务'},
                    {'id': 'form-a', 'label': '已选表样'}, {'id': 'org-a', 'label': '主体'},
                    {'id': 'form-c', 'label': '其他表样', 'periodType': 'N'}, {'id': 'product-a', 'label': '读集中的产品'}]
        evidence = {'sources': [{'captureRef': 'c', 'facts': deepcopy(facts), 'entities': entities}],
                    'pythonRuns': [deepcopy(run)]}
        focus_query_metadata(evidence)
        source = evidence['sources'][0]
        self.assertEqual([item['id'] for item in source['entities']], ['task-a', 'form-a', 'org-a', 'product-a'])
        self.assertEqual(source['facts'], facts)
        self.assertEqual(evidence['pythonRuns'], [run])
        self.assertEqual(source['definitionProjection']['read']['captureRef'], 'c')
        self.assertEqual(evidence['materialSelection']['omittedDefinitionCount'], 2)

    def test_failure_retains_root_cause_without_repeating_code(self):
        failure = {'tool': 'AnalysisPython', 'state': 'FAILED', 'callId': 'call', 'error': {
            'code': 'PYTHON_FAILED', 'message': '执行失败', 'details': {'rootCause': '缺少字段',
            'path': '/columns', 'requiresSourceQuery': False, 'retryArguments': {'code': 'large code'}}}}
        result = failure_summary(failure)
        self.assertEqual(result['error']['details']['rootCause'], '缺少字段')
        self.assertIs(result['error']['details']['requiresSourceQuery'], False)
        self.assertNotIn('retryArguments', result['error']['details'])
        self.assertIn('retryArguments', failure['error']['details'])

    def test_structure_directory_uses_actual_codes_and_exposes_completeness(self):
        cells = [{'measureCode': 'm' + str(index), 'headers': ['共同表头', '标题' + str(index)],
                  'dimensions': {'period': 'p1'}, 'mapType': 'ZB', 'value': '1'} for index in range(34)]
        cells.append(deepcopy(cells[0]))
        with patch('analysis_extension.grid_files.records', return_value=iter(cells)):
            structure = structure_profile({})
        self.assertEqual(len(structure['measures']), 32)
        self.assertFalse(structure['measureDirectoryComplete'])
        self.assertEqual(structure['measures'][0], {'measureCode': 'm0', 'cellCount': 2,
                         'headerExamples': ['标题0'], 'where': {'measureCode': 'm0'}})
        self.assertEqual(structure['dimensionCodes'], ['period'])
        self.assertNotIn('value', structure['measures'][0])


if __name__ == '__main__':
    unittest.main()
