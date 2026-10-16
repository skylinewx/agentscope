"""Visual capabilities use source relationships, including genuine gaps."""
from analysis_scene_assertions import comparison, diagrams
import json
from copy import deepcopy
from pathlib import Path
import unittest

import analysis_closeout_test as closeout
from analysis_fixtures import OWNER
from analysis_extension.adapters.budget_v2 import source_address
from analysis_extension.models import AnalysisError
from analysis_extension.presentation import project
from analysis_extension.validation import validate_claims, validate_views
from analysis_extension.relations import captured_relations
from analysis_extension.report import render


def visual_capture(store):
    ref, selectors, scope = closeout.mixed_fixture(store, 3)
    capture = store.get_capture(OWNER, ref)
    receipt = json.loads(capture['response']['content'][0]['text'])
    rows = receipt['data']['stepResults'][0]['rows']
    names = ['集团预算中心（合成）', '华东业务部（合成）', '华南业务部（合成）']
    for i, row in enumerate(rows):
        row.update(resourceIri=f'urn:fixture:member/{i}', label=names[i])
    capture['request']['plan']['steps'] += [
        {'id':'parent', 'operation':'RESOLVE_ENTITY', 'resourceIris':['urn:fixture:member/0']},
        {'id':'children', 'operation':'TRAVERSE_RELATION', 'inputs':['parent'],
         'traversalScope':'DIRECT', 'maximumDepth':1, 'capabilityId':'fixture:direct-children'}]
    receipt['data']['stepResults'].append({'stepId':'children', 'operation':'TRAVERSE_RELATION', 'exactness':'EXACT',
        'rows':[{**r,'rootResourceIri':'urn:fixture:member/0','depth':1} for r in rows[1:]]})
    capture['response']['content'][0]['text']=json.dumps(receipt,ensure_ascii=False)
    ref=store.capture(OWNER,capture)
    for selector in selectors: selector['captureRef']=ref
    scope['population']['captureRef']=ref
    scope['label']='2026 年预算 · 计算与一级层级（合成演示数据）'
    return ref, selectors, scope


class VisualTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case=closeout.CloseoutTests(); self.case.setUp(); self.addCleanup(self.case.tearDown)
        self.store=self.case.store; self.service=self.case.service

    async def draft(self):
        ref, selectors, scope=visual_capture(self.store)
        built=await self.service.build([ref],selectors,scope,[{'id':'formula','operator':'FORMULA_CHECK',
            'select':{'objectId':'object-0','cellKey':'2:1'}}],'visual')
        self.artifact=self.store.get_analysis(OWNER,built['analysisRef'])
        self.sid=built['scope']['scopeId']; self.built=built
        self.results=[f['factId'] for f in self.artifact['facts'] if f['cellKey']=='2:1']
        return built

    def plans(self):
        return [
            {'viewId':'metrics','scopeId':self.sid,'type':'METRIC','valueRefs':self.results},
            {'viewId':'comparison','scopeId':self.sid,'type':'BAR_VALUES','title':'各单位预算结果','valueRefs':self.results},
            {'viewId':'formula','scopeId':self.sid,'type':'FORMULA','title':'集团预算中心 · 结果如何计算','valueRefs':['derived_formula']},
            {'viewId':'hierarchy','scopeId':self.sid,'type':'HIERARCHY','title':'集团预算中心与直接下级','description':'一级上下级关系',
             'relationRef':self.artifact['relations'][0]['relationRef'],'valueRefs':self.results},
        ]

    async def publish(self):
        await self.draft()
        result=await self.service.publish(self.built['analysisRef'],[],self.plans(),'publish',0)
        self.artifact=self.store.get_analysis(OWNER,self.built['analysisRef'])
        self.path=result['delivery']['path']
        return self.case.delivery.view(self.path)

    def test_budget_grid_contract_and_explicit_index_override(self):
        cell={'key':'25:5','row':25,'column':5}
        self.assertEqual(source_address(cell,[]),'E25')
        self.assertEqual(source_address({'key':'3:27','row':3,'column':27},[]),'AA3')
        self.assertIsNone(source_address({'key':'0:5','row':0,'column':5},[]))
        self.assertIsNone(source_address({**cell,'key':'26:5'},[]))
        labels=[{'mapType':'COL_INDEX','column':5,'displayValue':'D'},{'mapType':'ROW_INDEX','row':25,'displayValue':'20'}]
        self.assertEqual(source_address(cell,labels),'D20')
        self.assertIsNone(source_address(cell,labels[:1]))

    async def test_calculation_and_hierarchy_are_rendered_alongside_comparison(self):
        view=await self.publish()
        self.assertEqual([s['type'] for s in diagrams(view)],['FORMULA','HIERARCHY'])
        self.assertEqual(comparison(view)['type'],'BAR_VALUES')
        texts=[s['relationship']['equation'] for s in diagrams(view)]+[n['label'] for s in diagrams(view) for n in s['relationship']['inputs']]
        self.assertTrue(any('收入 − 成本' in t for t in texts))
        self.assertTrue(any('华东业务部' in t for t in texts))
        self.assertFalse(any('本层计算一致' in t for t in texts))
        self.assertEqual(self.artifact['derivedValues'][0]['formulaTrace']['targetLocalCheck']['status'],'MATCH')
        self.assertEqual(len(set(m['label'] for m in view['metrics'])),3)
        html=render(project(self.artifact,self.case.rules))
        self.assertEqual(html.count('<svg '),1)
        self.assertIn('business-reference-1',html)
        self.assertIn('一级上下级关系',html)
        self.assertEqual(view['statuses']['receiptVerification'],'UNVERIFIED')
        self.assertEqual(self.built['verificationTaskCount'],1)
        self.assertEqual(len(self.built['verificationTasks'][0]['evidenceIds']),3)

    async def test_formula_gaps_and_mismatches_are_visible_without_fabricated_result(self):
        await self.draft()
        for mutation, expected in [('missing','计算未完成'),('different','计算有差异')]:
            artifact=deepcopy(self.artifact)
            from analysis_extension.engine import calculate
            if mutation=='missing': artifact['facts']=[f for f in artifact['facts'] if not(f['objectId']=='object-0' and f['cellKey']=='2:3')]
            else: next(f for f in artifact['facts'] if f['objectId']=='object-0' and f['cellKey']=='2:1')['value']='-4'
            artifact['derivedValues']=calculate(artifact['facts'],artifact['scope'],artifact['plan']['operations'],self.case.rules)
            artifact['viewPlan']=[self.plans()[2]]; artifact['publicationState']='PUBLISHED'
            validate_views(artifact,artifact['viewPlan'])
            view=project(artifact,self.case.rules)
            texts=json.dumps([s['relationship'] for s in diagrams(view)],ensure_ascii=False)
            if mutation=='missing': self.assertIn('未取得的项目',texts); self.assertNotIn('计算一致',texts)
            else:
                self.assertEqual(artifact['derivedValues'][0]['formulaTrace']['nodes'][0]['difference'],'1')
                self.assertIn('-4',texts)

    async def test_hierarchy_rejects_invented_edges_wrong_metrics_and_origins(self):
        await self.draft()
        view=self.plans()[3]
        for field,value in [('relationRef','invented'),('valueRefs',list(reversed(self.results)))]:
            with self.assertRaises(AnalysisError): validate_views(self.artifact,[{**view,field:value}])
        wrong=next(f['factId'] for f in self.artifact['facts'] if f['objectId']=='object-1' and f['cellKey']=='2:2')
        with self.assertRaises(AnalysisError): validate_views(self.artifact,[{**view,'valueRefs':[self.results[0],wrong]}])
        foreign=deepcopy(self.artifact); foreign['facts'][0]['sourceOrigins']=['another-origin']
        with self.assertRaises(AnalysisError): validate_views(foreign,[view])

    async def test_in_table_trace_stops_at_external_values_without_claiming_external_calculation(self):
        await self.draft()
        from analysis_extension.formulas import trace_formula
        facts=deepcopy(self.artifact['facts'])
        child=next(f for f in facts if f['objectId']=='object-0' and f['cellKey']=='2:2')
        child['formula']='OTHER_N[AMOUNT,PERIOD="2026"]'
        trace=trace_formula(facts,self.results[0])
        self.assertEqual(trace['status'],'MATCH')
        self.assertEqual(trace['externalBoundaries'],[child['factId']])
        node=next(n for n in trace['nodes'] if n['factRef']==child['factId'])
        self.assertEqual(node['status'],'SOURCE_VALUE')
        self.assertIsNone(node['recalculated'])
        self.assertEqual(trace_formula(facts,child['factId'])['status'],'BLOCKED')
        child['valueState']='NO_VALUE'; child['value']=None
        self.assertEqual(trace_formula(facts,self.results[0])['status'],'BLOCKED')
        child['valueState']='PRESENT'; child['value']='10'; child['formula']='ABS(B1)'
        self.assertEqual(trace_formula(facts,self.results[0])['status'],'BLOCKED')

    async def test_relation_extraction_rejects_recursive_or_unresolved_parent(self):
        await self.draft()
        ref=self.artifact['captureRefs'][0]; capture=self.store.get_capture(OWNER,ref)
        relation=next(s for s in capture['request']['plan']['steps'] if s['id']=='children')
        relation['maximumDepth']=2
        self.assertEqual(captured_relations({ref:capture}),[])
        relation['maximumDepth']=1
        receipt=json.loads(capture['response']['content'][0]['text'])
        receipt['data']['stepResults'][0]['rows'][0].pop('resourceIri')
        capture['response']['content'][0]['text']=json.dumps(receipt)
        self.assertEqual(captured_relations({ref:capture}),[])

    async def test_partial_hierarchy_and_blank_unit_remain_truthful(self):
        await self.draft()
        self.artifact['viewPlan']=[{**self.plans()[3],'valueRefs':self.results[:2]}]
        self.artifact['publicationState']='PUBLISHED'
        view=project(self.artifact,self.case.rules)
        text=json.dumps([s['relationship'] for s in diagrams(view)],ensure_ascii=False)
        self.assertIn('仅列示部分直接下级',text)
        self.assertNotIn('未注明',text); self.assertNotIn('单位未知',text)
        self.assertNotIn('合计',text)

    async def test_scalar_field_value_is_accepted_without_loosening_count_fields(self):
        await self.draft()
        claim={'claimId':'value','scopeId':self.sid,'kind':'fact','parts':[{'valueRef':self.results[0],'field':'value'}]}
        self.assertIn('-5',validate_claims(self.artifact,[claim])[0]['rendered'])
        claim['parts'][0]['field']='negative'
        with self.assertRaises(AnalysisError): validate_claims(self.artifact,[claim])

    async def test_approved_style_is_preserved_with_only_authorized_composition_slots(self):
        await self.publish()
        extension=Path(__file__).resolve().parents[1]/'examples/agent_service/analysis_extension'
        approved=Path(__file__).resolve().parents[1]/'before/examples/agent_service/analysis_extension'
        if approved.exists():
            base=(extension/'report.css').read_bytes()
            expected=(approved/'report.css').read_bytes()
            self.assertTrue(base.startswith(expected))
            added=base[len(expected):].decode('utf-8')
            self.assertIn('Model-selected composition',base.decode('utf-8'))
            for forbidden in ('background:', 'color:', 'font-family:', 'font-size:', 'max-width: 80ch'):
                self.assertNotIn(forbidden,added)
            old=(approved/'report.py').read_text('utf-8').split("html = f'''")[1]
            new=(extension/'report.py').read_text('utf-8').split("html = f'''")[1]
            # Content slots may be empty; the approved visual frame stays fixed.
            for marker in ('<!doctype html>', '<html lang="zh-CN">', '<header aria-labelledby="report-title">', '<footer class="note report-footer">', '</main></body></html>'):
                self.assertIn(marker, old); self.assertIn(marker, new)
            self.assertIn('<body><main class="wrap presentation-composition">',new)
            self.assertLess(new.index('{overview}{charts_html}{findings_html}'), new.index('{details_html}'))
            self.assertLess(new.index("document['summaryPosition'] == 'start'"),new.index('{overview}'))
            self.assertLess(new.index('{details_html}'),new.index("document['summaryPosition'] == 'end'"))
