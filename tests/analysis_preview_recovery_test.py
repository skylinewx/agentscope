import json
from copy import deepcopy
from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.tools import AnalysisPublish
from analysis_extension.models import AnalysisError
from analysis_extension.inspect_pages import character_page, INSPECT_CHAR_BUDGET


class PreviewRecoveryTests(PresentationCase):
    async def request(self):
        cap, selections, scope = saved_fixture(self.store, ('7', '-1', '20'))
        built = await self.service.build([cap], selections, scope, [{'id':'order', 'operator':'SORT', 'direction':'desc'}], 'build')
        self.ref = built['analysisRef']
        self.original = self.store.get_analysis(OWNER, self.ref)
        refs = [f['factId'] for f in self.original['facts']]
        base = {'scopeId': built['scope']['scopeId'], 'valueRefs': refs}
        return {'analysisRef': self.ref, 'expectedBaseRevision': 0, 'requestKey': 'preview', 'claims': [],
                'presentation': {'labels': [{'valueRef': refs[0], 'role': 'measure', 'label': '指标简称'}]},
                'viewPlan': [{**base, 'viewId':'bar','type':'BAR_VALUES','orderRef':'derived_order'},
                             {**base, 'viewId':'table','type':'TABLE'}]}

    async def test_preview_is_not_publication_and_saved_preview_can_be_promoted(self):
        req = await self.request()
        result = await self.service.publish(**req, previewOnly=True)
        self.assertFalse(result['diagnostics']['visualInspectionPerformed'])
        self.assertIn('PAIRED_ORDER_DIFFERS', [w['code'] for w in result['diagnostics']['warnings']])
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), self.original)
        self.assertIsNone(self.store.get_presentation(OWNER, 'delivery', self.ref))
        self.assertNotIn('document', result)
        preview = await self.service.publish(self.ref, requestKey='preview2', expectedBaseRevision=0, previewOnly=True,
            repairRef=result['repairRef'], repairs=[{'op':'add','path':'/viewPlan/1/orderRef','value':'derived_order'}])
        self.assertNotIn('PAIRED_ORDER_DIFFERS', [w['code'] for w in preview['diagnostics']['warnings']])
        published = await self.service.publish(self.ref, requestKey='publish', expectedBaseRevision=0, repairRef=preview['repairRef'], repairs=[])
        saved = self.store.get_analysis(OWNER, self.ref)
        self.assertEqual(saved['facts'], self.original['facts'])
        self.assertEqual(saved['presentation'], req['presentation'])
        self.assertTrue(published['delivery']['registered'])

    async def test_tool_schema_errors_are_aggregated_before_business_execution(self):
        req = await self.request()
        req['presentation']['showUnit'] = False
        del req['viewPlan'][1]['viewId']
        del req['viewPlan'][1]['scopeId']
        tool = AnalysisPublish(self.service)
        response = await tool.call(**req)
        error = json.loads(response.content[0].text)['error']
        self.assertEqual(len(error['details']['issues']), 3)
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), self.original)
        repaired = await tool.call(analysisRef=self.ref, expectedBaseRevision=0, requestKey='fixed', previewOnly=True,
            repairRef=error['details']['repairRef'], repairs=[
                {'op':'remove','path':'/presentation/showUnit'},
                {'op':'add','path':'/viewPlan/1/viewId','value':'table'},
                {'op':'add','path':'/viewPlan/1/scopeId','value':self.original['scope']['scopeId']}])
        self.assertTrue(json.loads(repaired.content[0].text)['previewOnly'])

    async def test_invalid_repair_and_unauthorized_preview_do_not_publish(self):
        req = await self.request()
        result = await self.service.publish(**req, previewOnly=True)
        with self.assertRaises(AnalysisError):
            await self.service.publish(self.ref, requestKey='bad', expectedBaseRevision=0, repairRef=result['repairRef'],
                repairs=[{'op':'replace','path':'/facts/0/value','value':'999'}])
        async def denied(): raise AnalysisError('ACCESS_DENIED', 'denied')
        self.service.authorize = denied
        with self.assertRaises(AnalysisError): await self.service.publish(**req, previewOnly=True)
        self.assertEqual(self.store.get_analysis(OWNER, self.ref), self.original)

    async def test_budget_pages_keep_whole_items_and_complete_cursor(self):
        rows = [{'factId': str(i), 'value': '123.456', 'note': '长' * 3000} for i in range(31)]
        offset, returned = 0, []
        while offset is not None:
            page = character_page(rows, offset, 100, 100)
            self.assertLessEqual(len(json.dumps(page,ensure_ascii=False)), INSPECT_CHAR_BUDGET)
            returned.extend(page['items'])
            offset = page['nextOffset']
        self.assertEqual(returned, rows)

    async def test_host_preserves_structured_schema_errors_and_checks_permission(self):
        from unittest.mock import AsyncMock
        from agentscope.agent import Agent, InjectionConfig
        from agentscope.tool import Toolkit
        from agentscope.message import ToolCallBlock, UserMsg, TextBlock
        from agentscope.model import ChatResponse
        from agentscope.permission import PermissionDecision, PermissionBehavior
        from utils import MockModel
        req=await self.request()
        del req['viewPlan'][1]['viewId']
        tool=AnalysisPublish(self.service)
        for permitted in (True,False):
            with self.subTest(permitted=permitted):
                permission=AsyncMock(return_value=PermissionDecision(behavior=PermissionBehavior.ALLOW if permitted else PermissionBehavior.DENY, message='test decision'))
                tool.check_permissions=permission
                model=MockModel(context_size=1000000)
                model.set_responses([ChatResponse(content=[ToolCallBlock(id='pub',name='AnalysisPublish',input=json.dumps(req))],is_last=True),ChatResponse(content=[TextBlock(text='Done')],is_last=True)])
                agent=Agent('test',system_prompt='test',model=model,toolkit=Toolkit(tools=[tool]),injection_config=InjectionConfig(inject_runtime_state=False))
                await agent.reply(UserMsg('user','preview'))
                permission.assert_awaited_once()
                result=next(b for m in agent.state.context for b in m.get_content_blocks('tool_result'))
                output=json.dumps(result.model_dump(),ensure_ascii=False)
                if permitted:
                    self.assertIn('INVALID_PUBLICATION',output)
                    self.assertIn('repairRef',output)
                else:
                    self.assertNotIn('repairRef',output)
                self.assertEqual(self.store.get_analysis(OWNER,self.ref),self.original)

    async def test_compact_facts_can_be_restored_by_id_and_full_detail(self):
        await self.request()
        compact = await self.service.inspect(section='facts', analysisRef=self.ref)
        first = compact['items'][0]
        self.assertIn(first['contextRef'], compact['contexts'])
        full = await self.service.inspect(section='facts', analysisRef=self.ref, factRef=first['factId'], detail='full')
        self.assertEqual(full['items'], [self.original['facts'][0]])
        field = await self.service.inspect(section='facts', analysisRef=self.ref, factRef=first['factId'], fields=['value','formula'])
        self.assertEqual(set(field['items'][0]), {'factId','value','formula'})
