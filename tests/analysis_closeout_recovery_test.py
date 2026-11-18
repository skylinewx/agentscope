"""原生后台回执、版本顺序及跨实例交付恢复。"""
from copy import deepcopy
import json
from unittest.mock import patch

from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.state import AgentState
from agentscope.tool import ToolResponse
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import Owner, AnalysisError
from analysis_extension.store import AnalysisStore
from analysis_extension.turn_completion import TurnCompletion, current_turn


class CloseoutRecoveryTests(PresentationCase):
    def turn(self):
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '解释当前数据')))
        turn.task(mode='ANALYSIS', goal='解释数据', delivery='DOCUMENT')
        self.service.turn = turn
        return turn

    def observe(self, turn, name, value):
        sequence = turn.start_tool()
        call = ToolCallBlock(id='native-' + str(sequence), name=name, input='{}')
        response = ToolResponse(id=call.id, content=[TextBlock(text=json.dumps(value))])
        turn.record_tool_result(call, response, sequence, 12.5)
        turn.observe(call, response)
        return call, response

    async def published(self, turn):
        from analysis_turn_completion_test import TurnCompletionTests
        await TurnCompletionTests.generic_result(self, turn)
        # 重复调用可能创建展示修订，应取宿主记录的实际发布版本。
        return self.store.get_analysis(OWNER, turn.state['analysisRef'])

    async def test_publication_survives_stale_state_and_missing_notification(self):
        turn = self.turn()
        stale = deepcopy(turn.state)
        artifact = await self.published(turn)
        self.service.store = AnalysisStore(self.store.root)
        resumed = TurnCompletion(self.service, json.loads(json.dumps(stale)))
        resumed.recover_background_evidence()
        self.assertEqual(resumed.state['analysisRef'], artifact['analysisRef'])
        self.assertTrue(resumed.state['delivery']['registered'])
        self.assertEqual(resumed.check()['status'], 'READY')
        self.assertEqual(resumed.state['toolPerformance']['AnalysisPublish']['calls'], 2)
        before = deepcopy(resumed.state)
        resumed.recover_background_evidence()
        self.assertEqual(resumed.state, before)

    async def test_notification_is_owner_turn_and_call_scoped(self):
        turn = self.turn()
        await self.published(turn)
        record = self.store.native_results(OWNER, turn.state['turnKey'])[-1]
        source = {'backgroundToolCallId': record['toolCallId'], 'backgroundState': 'success',
                  'toolResponseMetadata': {'analysisTurnKey': turn.state['turnKey'],
                                           'analysisToolReceiptRef': record['receiptRef']}}
        resumed = TurnCompletion(self.service, {**deepcopy(turn.state), 'delivery': {}})
        resumed.observe_background(source)
        self.assertTrue(resumed.state['delivery']['registered'])
        other = self.turn()
        other.observe_background(source)
        self.assertNotIn('delivery', other.state)
        self.assertEqual(self.store.native_results(Owner(OWNER.user_id, OWNER.agent_id, 'other'),
                                                   turn.state['turnKey'], record['receiptRef']), [])
        bad = deepcopy(source)
        bad['backgroundToolCallId'] = 'different-call'
        resumed.observe_background(bad)
        self.assertIn('different-call', resumed.state['backgroundEvidenceErrors'])

    async def test_late_build_and_preview_do_not_erase_published_delivery(self):
        turn = self.turn()
        await self.published(turn)
        expected = deepcopy(turn.state['delivery'])
        old = self.store.native_results(OWNER, turn.state['turnKey'])[0]
        response = ToolResponse.model_validate(old['response'])
        call = ToolCallBlock(id=old['toolCallId'], name=old['toolName'], input=old['input'])
        turn.observe(call, response)
        self.assertEqual(turn.state['delivery'], expected)
        # 同一已发布版本的重复 build 即使后发，也不应该清掉交付记录。
        self.observe(turn, 'AnalysisBuild', {'analysisRef': turn.state['analysisRef']})
        self.assertEqual(turn.state['delivery'], expected)

    async def test_stats_and_sequence_survive_service_reconstruction(self):
        turn = self.turn()
        await self.published(turn)
        records = self.store.native_results(OWNER, turn.state['turnKey'])
        last = records[-1]
        response = ToolResponse.model_validate(last['response'])
        call = ToolCallBlock(id=last['toolCallId'], name=last['toolName'], input=last['input'])
        turn.record_tool_result(call, response, last['sequence'], 99)
        self.assertEqual(turn.state['toolPerformance']['AnalysisPublish']['calls'], 2)
        self.service.store = AnalysisStore(self.store.root)
        restored = TurnCompletion(self.service, deepcopy(turn.state))
        self.assertEqual(restored.start_tool(), last['sequence'] + 1)
        persisted = self.store.turn_progress(OWNER, turn.state['turnKey'])
        publication = persisted['stages']['publication']
        self.assertEqual(publication['calls'], 2)
        self.assertEqual(publication['elapsedMs'], 25)

    async def test_optional_new_draft_can_restore_current_question_delivery(self):
        turn = self.turn()
        published = await self.published(turn)
        original_delivery = deepcopy(turn.state['delivery'])
        draft = self.store.build(OWNER, {'kind': 'optional-style'}, deepcopy(published), 'optional-style', published['analysisRef'])
        self.observe(turn, 'AnalysisBuild', {'analysisRef': draft['analysisRef']})
        with patch('analysis_extension.turn_completion.document_review', return_value=('saved', {'issues': []})):
            self.assertTrue(turn.restore_deliverable())
        self.assertEqual(turn.state['analysisRef'], published['analysisRef'])
        self.assertEqual(turn.state['retainedDraftRef'], draft['analysisRef'])
        self.assertEqual(turn.state['delivery'], original_delivery)
        self.assertEqual(turn.check()['status'], 'READY')

    async def test_new_required_work_or_changed_fact_cannot_restore_old_delivery(self):
        for change in ('work', 'fact'):
            with self.subTest(change=change):
                turn = self.turn()
                published = await self.published(turn)
                draft = deepcopy(published)
                if change == 'work':
                    draft['plan']['workPlan'].append({'taskId': 'required', 'title': '新增必要核对', 'kind': 'VALUES', 'refs': []})
                else:
                    draft['facts'][0]['value'] = '999999'
                draft = self.store.build(OWNER, {'kind': change}, draft, change, published['analysisRef'])
                self.observe(turn, 'AnalysisBuild', {'analysisRef': draft['analysisRef']})
                with patch('analysis_extension.turn_completion.document_review', return_value=('saved', {'issues': []})):
                    self.assertFalse(turn.restore_deliverable())
                self.assertEqual(turn.state['analysisRef'], draft['analysisRef'])
