from analysis_extension.review_request import review_material
from copy import deepcopy
from unittest import TestCase

from analysis_extension.computation_review import computation_key, use_reviewed_computation
from analysis_presentation_fixtures import PresentationCase
import analysis_report_review_delivery_test as review_fixtures


class ComputationReviewTests(TestCase):
    def test_review_followup_uses_frozen_grouping_while_current_request_stays_visible(self):
        from analysis_extension.computation_review import calculation_material, business_material
        source = {'userRequest': '分析渠道',
                  'computationIntent': {'userRequest': '分析渠道', 'queryIntent': {'period': '2026年', 'grouping': '渠道'}},
                  'computationGroupingRequirements': [{'keyword': '渠道', 'required': True}],
                  'queryIntent': {'period': '2026年', 'grouping': '渠道'},
                  'groupingRequirements': [{'keyword': '渠道', 'required': False}],
                  'facts': [{'period': '2026J0002', 'value': '1'}],
                  'pythonRuns': [{'runRef': 'r', 'code': 'result=inputs', 'inputRecords': [{'period':'2026J0002','value':'1'}]}]}
        key = computation_key(source, 'policy')
        followup = {**source, 'userRequest': '继续审查，不取数不计算',
                    'queryIntent': {'period': '2026年'}, 'groupingRequirements': []}
        self.assertEqual(computation_key(followup, 'policy'), key)
        self.assertEqual(calculation_material(followup, key)['queryIntent']['grouping'], '渠道')
        final = business_material(followup, {'status': 'PASS'}, key)
        self.assertEqual(final['userRequest'], followup['userRequest'])
        self.assertEqual(final['queryIntent'], {'period': '2026年'})
        for field, value in [('computationGroupingRequirements', [{'keyword': '区域', 'required': True}]),
                             ('facts', [{'period': '2026J0003', 'value': '1'}]),
                             ('scope', {'members': ['different']})]:
            self.assertNotEqual(computation_key({**followup, field:value}, 'policy'), key)

    def test_text_and_css_do_not_invalidate_computations_but_input_scope_role_and_code_do(self):
        source = {'userRequest': '对比两组', 'facts': [{'value': '1', 'inputRole': 'PRIMARY'}],
                  'scope': {'members': ['a'], 'referenceMembers': ['a', 'b']},
                  'pythonRuns': [{'runRef': 'r', 'code': 'result=inputs', 'inputRecords': [{'ref': 'f', 'value': '1'}]}],
                  'derived': [{'value': '1'}], 'documentText': '报告第一版'}
        key = computation_key(source, 'prompt-v1')
        edited = {**source, 'documentText': '修正说明', 'documentSourceText': '<style>p{color:red}</style>'}
        self.assertEqual(computation_key(edited, 'prompt-v1'), key)
        for field, value in [('facts', [{'value': '1', 'inputRole': 'REFERENCE'}]),
                             ('scope', {'members': ['a'], 'referenceMembers': ['b']}),
                             ('pythonRuns', [{'runRef': 'r', 'code': 'changed'}]), ('derived', [{'value': '2'}])]:
            self.assertNotEqual(computation_key({**source, field: value}, 'prompt-v1'), key)
        self.assertNotEqual(computation_key(source, 'prompt-v2'), key)
        before = deepcopy(source)
        projected = use_reviewed_computation(source, {'materialKey': key, 'issues': []})
        self.assertEqual(source, before)
        self.assertEqual(projected['pythonRuns'], [])
        self.assertEqual(projected['derived'], source['derived'])
        self.assertEqual(projected['documentText'], source['documentText'])


class ComputationReviewIntegrationTests(PresentationCase):
    async def test_large_material_preparation_does_not_block_session_checkpoint_loop(self):
        import asyncio
        import json
        import threading
        from unittest.mock import patch
        from analysis_extension.turn_completion import _review_answer, prepare_review_parts
        await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        material = {'userRequest':'分析', 'facts':[], 'derived':[], 'documentText':'完整报告', 'deliveryEvidence':{}}
        preparing, checkpoint = threading.Event(), threading.Event()
        def slow_prepare(*args):
            preparing.set()
            if not checkpoint.wait(2):
                raise AssertionError('Preparation blocked the event loop')
            return prepare_review_parts(*args)
        async def heartbeat():
            while not preparing.is_set():
                await asyncio.sleep(.001)
            checkpoint.set()
        async def approved(*args):
            return {'issues': [], 'coverage': [{'requirement':'当前结果','status':'COVERED','location':'全文','evidenceRefs':[]}]}
        with patch('analysis_extension.turn_completion.review_payload', return_value=json.dumps(material)), \
             patch('analysis_extension.turn_completion.prepare_review_parts', side_effect=slow_prepare), \
             patch('analysis_extension.turn_completion._model_review', side_effect=approved):
            await asyncio.wait_for(asyncio.gather(_review_answer(None, self.turn, {'analysisRef':self.ref}, ''), heartbeat()), 5)
        self.assertTrue(checkpoint.is_set())

    async def test_calculation_unavailable_still_confirms_current_content_without_claiming_pass(self):
        import json
        from unittest.mock import patch
        from analysis_extension.models import AnalysisError
        from analysis_extension.turn_completion import _review_answer
        from analysis_fixtures import OWNER
        await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        material = {'userRequest':'分析', 'facts':[], 'derived':[], 'documentText':'当前成果',
                    'pythonRuns':[{'runRef':'run','code':'result=[]','inputRecords':[]}], 'deliveryEvidence':{}}
        calls = []
        async def reviewer(agent, prompt, payload):
            data = review_material(payload); calls.append(data)
            if data['reviewStage'] == 'CALCULATION':
                raise AnalysisError('REVIEW_UNAVAILABLE', '模拟专项审核超时')
            return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        with patch('analysis_extension.turn_completion.review_payload', return_value=json.dumps(material)), \
             patch('analysis_extension.turn_completion._model_review', side_effect=reviewer):
            with self.assertRaises(AnalysisError):
                await _review_answer(None, self.turn, {'analysisRef':self.ref}, '')
        self.assertEqual([item['reviewStage'] for item in calls], ['CALCULATION','FINAL_BUSINESS'])
        self.assertEqual(calls[-1]['calculationReview']['status'], 'UNAVAILABLE')
        record = self.store.get_report_review(OWNER, self.turn.state['currentReportReviewRef'])
        self.assertEqual(record['status'], 'PARTIAL')
        self.assertEqual([part['status'] for part in record['parts']], ['UNAVAILABLE', 'PASS'])

    async def test_resuming_with_changed_review_profile_reaudits_frozen_calculation_and_content(self):
        import json
        from dataclasses import replace
        from unittest.mock import patch
        from analysis_extension.turn_completion import _review_answer
        from analysis_fixtures import OWNER
        request = await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        material = {'userRequest':'原目标', 'facts':[], 'derived':[], 'documentText':'完整内容',
                    'revisionFocus': {'sameDataAndScope': True, 'meaning': '旧审核策略下的局部修改'},
                    'pythonRuns':[{'runRef':'run','code':'result=[]','inputRecords':[]}], 'deliveryEvidence':{}}
        async def review(artifact):
            return await _review_answer(None, self.turn, artifact, '')
        self.service.preview_reviewer = review
        async def approved(*args): return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        with patch('analysis_extension.turn_completion.review_payload', return_value=json.dumps(material)), \
             patch('analysis_extension.turn_completion._model_review', side_effect=approved):
            published = await self.service.publish(**request, finalize=True)
        record = self.store.get_report_review(OWNER, published['reportDelivery']['reviewRef'])
        old_key = record['calculationReview']['materialKey']
        self.service.review_budget = replace(self.service.review_budget, reviewer_identity='new-model-profile')
        calls = []
        async def renewed(prompt, payload):
            calls.append(review_material(payload)); return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        self.service.material_reviewer = renewed
        resumed = await self.service.publish(analysisRef=self.ref, requestKey='resume-new-profile', resumeReview=True)
        self.assertEqual([item['reviewStage'] for item in calls], ['CALCULATION','FINAL_BUSINESS'])
        self.assertEqual(calls[0]['userRequest'], '原目标')
        self.assertNotEqual(calls[0]['computationKey'], old_key)
        self.assertEqual(calls[1]['calculationReview']['materialKey'], calls[0]['computationKey'])
        self.assertNotIn('revisionFocus', calls[1])
        self.assertEqual(calls[1]['documentText'], '完整内容')
        self.assertEqual(resumed['reportDelivery']['semanticReview'], 'PASS')

    async def _resume_after_calculation_timeout(self, completed_parts_already_saved=False):
        import json
        from unittest.mock import patch
        from agentscope.message import UserMsg
        from agentscope.state import AgentState
        from analysis_extension.models import AnalysisError
        from analysis_extension.turn_completion import _review_answer, TurnCompletion, current_turn
        from analysis_extension.report_review import save_review
        from analysis_fixtures import OWNER
        request = await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        material = {'userRequest': '原目标', 'facts': [], 'derived': [], 'documentText': '完整内容',
                    'pythonRuns': [{'runRef': 'run', 'code': 'result=[]', 'inputRecords': []}],
                    'deliveryEvidence': {}}
        async def review(artifact):
            return await _review_answer(None, self.turn, artifact, '')
        async def unavailable(agent, prompt, payload):
            if review_material(payload)['reviewStage'] == 'CALCULATION':
                raise AnalysisError('REVIEW_UNAVAILABLE', '专项审核超时')
            return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        self.service.preview_reviewer = review
        with patch('analysis_extension.turn_completion.review_payload', return_value=json.dumps(material)), \
             patch('analysis_extension.turn_completion._model_review', side_effect=unavailable):
            await self.service.publish(**request, previewOnly=True)
        published = await self.service.publish_available_report(self.ref, 'REVIEW_LIMIT')
        frozen = deepcopy(self.store.get_analysis(OWNER, self.ref))
        record = self.store.get_report_review(OWNER, published['reportDelivery']['reviewRef'])
        self.assertEqual(record['calculationReview']['status'], 'UNAVAILABLE')
        if completed_parts_already_saved:
            # The former immutable cache bug could leave two PASS parts with an
            # UNAVAILABLE aggregate receipt. Recover without another code audit.
            part = record['parts'][0]
            self.store.save_semantic_review(OWNER, part['key'], {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]})
            part.update(status='PASS', issues=[])
            part.pop('failure', None)
            save_review(self.turn, record)
        calls = []
        async def renewed(prompt, payload):
            calls.append(review_material(payload))
            return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        self.service.material_reviewer = renewed
        self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '继续审核原报告')))
        resumed = await self.service.publish(analysisRef=self.ref, requestKey='resume', resumeReview=True)
        expected = ['FINAL_BUSINESS'] if completed_parts_already_saved else ['CALCULATION', 'FINAL_BUSINESS']
        self.assertEqual([item['reviewStage'] for item in calls], expected)
        self.assertEqual(calls[-1]['calculationReview']['status'], 'PASS')
        self.assertEqual(calls[-1]['documentText'], '完整内容')
        self.assertEqual(resumed['reportDelivery']['semanticReview'], 'PASS')
        self.assertEqual(resumed['reportDelivery']['mode'], 'REVIEWED_AFTER_RESUME')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), frozen)
        await self.service.publish(analysisRef=self.ref, requestKey='resume-again', resumeReview=True)
        self.assertEqual(len(calls), len(expected))

    async def test_unavailable_calculation_receipt_is_replaced_only_by_completed_review(self):
        await self._resume_after_calculation_timeout()

    async def test_completed_parts_repair_stale_unavailable_receipt_without_recalculating(self):
        await self._resume_after_calculation_timeout(completed_parts_already_saved=True)

    async def test_changed_report_reuses_actual_calculation_proof_and_changed_input_invalidates(self):
        import json
        from unittest.mock import patch
        from analysis_extension.turn_completion import _review_answer
        await review_fixtures.ReportReviewDeliveryTests.prepare(self)
        material = {'userRequest': '比较已有数据', 'facts': [{'factId': 'f', 'value': '1', 'unit': '元'}],
            'pythonRuns': [{'runRef': 'r', 'code': 'result=inputs', 'inputRecords': [{'ref': 'f', 'value': '1'}], 'status': 'COMPLETED'}],
            'derived': [{'derivedId': 'd', 'pythonRunRef': 'r', 'value': '1'}], 'documentText': '第一版',
            'deliveryEvidence': {}, 'reviewHistory': []}
        calls = []
        async def reviewer(agent, prompt, payload):
            calls.append(review_material(payload))
            return {'issues': [], 'coverage': [{'requirement': '核对当前材料', 'status': 'COVERED', 'location': '本阶段完整材料', 'evidenceRefs': []}]}
        with patch('analysis_extension.turn_completion.review_payload', side_effect=lambda *args: json.dumps(material)), \
             patch('analysis_extension.turn_completion._model_review', side_effect=reviewer):
            await _review_answer(None, self.turn, {'analysisRef': self.ref}, '')
            material['documentText'] = '不同分析说明'
            await _review_answer(None, self.turn, {'analysisRef': self.ref}, '')
            self.assertNotIn('pythonRuns', calls[-1])
            self.assertEqual(calls[0]['reviewStage'], 'CALCULATION')
            self.assertNotIn('documentText', calls[0])
            self.assertEqual(calls[1]['reviewStage'], 'FINAL_BUSINESS')
            self.assertEqual(calls[-1]['calculationReview']['coveredRunRefs'], ['r'])
            self.assertEqual(calls[-1]['documentText'], '不同分析说明')
            material['facts'][0]['value'] = '2'
            material['pythonRuns'][0]['inputRecords'][0]['value'] = '2'
            await _review_answer(None, self.turn, {'analysisRef': self.ref}, '')
            self.assertEqual(calls[-2]['pythonRuns'][0]['inputRecords'][0]['value'], '2')
            self.assertEqual(calls[-1]['calculationReview']['status'], 'PASS')
        self.assertEqual(len(calls), 5)
