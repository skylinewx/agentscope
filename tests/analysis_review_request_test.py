import unittest
from copy import deepcopy
from dataclasses import replace

from analysis_extension.review_request import make_review_request, review_material
from analysis_extension.review_budget import ReviewBudgetConfig
from analysis_extension.turn_completion import semantic_review_key


class ReviewRequestTests(unittest.TestCase):
    def test_complete_inputs_roundtrip_and_actual_code_is_readable(self):
        material = {'reviewStage': 'CALCULATION', 'userRequest': '比较渠道',
                    'facts': [{'factId': 'f', 'value': None, 'period': '2026J0002'}],
                    'pythonRuns': [{'runRef': 'r', 'code': 'result = inputs\n# ``` stays literal',
                                    'inputRecords': [{'ref':'f','value':None,'extra':{'x':[1,'1',None]}}]}]}
        before = deepcopy(material)
        request = make_review_request(material)
        self.assertEqual(review_material(make_review_request(request.stored())), before)
        self.assertEqual(material, before)
        self.assertTrue(request.markdown.startswith('# 计算专项审查材料'))
        self.assertIn('## 二、查询结果与实际输入', request.markdown)
        self.assertIn('````python\nresult = inputs', request.markdown)
        self.assertIn('2026年第2季度', request.markdown)
        self.assertEqual(len(request), len(request.markdown))

    def test_timeout_does_not_invalidate_semantics_but_data_and_model_do(self):
        budget = ReviewBudgetConfig()
        request = make_review_request({'facts':[{'factId':'f','value':'1'}]})
        key = semantic_review_key(request, 'policy', budget)
        self.assertEqual(key, semantic_review_key(request, 'policy', replace(budget, call_timeout_seconds=240)))
        self.assertNotEqual(key, semantic_review_key(request, 'policy', replace(budget, reviewer_identity='new')))
        self.assertNotEqual(key, semantic_review_key({'facts':[{'factId':'f','value':'2'}]}, 'policy', budget))

    def test_frozen_wire_cannot_be_changed_on_resume(self):
        request = make_review_request({'userRequest':'查询', 'facts':[]})
        saved = request.stored()
        saved['markdown'] += 'changed'
        with self.assertRaisesRegex(Exception, '摘要不一致'): make_review_request(saved)

    def test_table_rows_restore_review_evidence_after_freezing_without_rewriting_prose(self):
        first='fact_'+'a'*40
        second='fact_'+'b'*40
        material={'facts':[{'factId':first,'value':'10','recordTable':'A'},
                           {'factId':second,'value':'10','recordTable':'B'}]}
        request=make_review_request(material)
        self.assertEqual(request.wire_references,{'表1第1行':first,'表2第1行':second})
        self.assertNotIn('@e',request.markdown)
        frozen=make_review_request(request.stored())
        verdict={'issues':[{'message':'表2第1行','evidenceRefs':['表2第1行']}],
                 'coverage':[{'location':'表1第1行','evidenceRefs':['表1第1行']}]}
        restored=frozen.restore_verdict(verdict)
        self.assertEqual(restored['issues'][0]['evidenceRefs'],[second])
        self.assertEqual(restored['coverage'][0]['evidenceRefs'],[first])
        self.assertEqual(restored['issues'][0]['message'],'表2第1行')
        self.assertEqual(restored['coverage'][0]['location'],'表1第1行')
        self.assertEqual(frozen.material,material)

    def test_computation_locations_restore_and_unknown_locations_still_fail(self):
        from analysis_extension.review_coverage import validate_coverage
        material={'facts':[{'factId':'fact1','value':'5'}],
                  'pythonRuns':[{'runRef':'run1','code':'result = inputs'}],
                  'derived':[{'derivedId':'derived1','label':'渠道汇总','value':'5'}]}
        request=make_review_request(material)
        self.assertIn('结果 1：渠道汇总',request.markdown)
        verdict={'issues':[],'coverage':[{'requirement':'汇总','status':'SATISFIED','location':'结果1',
                   'evidenceRefs':['业务数据表 １ 第 １ 行','Python 脚本 1','结果 1']}]}
        restored=make_review_request(request.stored()).restore_verdict(verdict)
        self.assertEqual(restored['coverage'][0]['evidenceRefs'],['fact1','run1','derived1'])
        validate_coverage(restored,material)
        verdict['coverage'][0]['evidenceRefs']=['结果99']
        with self.assertRaisesRegex(Exception,'数据定位无法对应'):
            validate_coverage(request.restore_verdict(verdict),material)
