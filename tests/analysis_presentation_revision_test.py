"""Behavioral regression tests for semantic diagrams and model-authored presentation."""
from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
import json
from html.parser import HTMLParser

from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER, fixture, saved_fixture
import analysis_model_presentation_test as helpers
from analysis_extension.models import AnalysisError
from analysis_extension.adapters.budget_v2 import receipt_from_capture
from analysis_extension.validation import validate_views
from analysis_extension.presentation_options import validate_presentation
from analysis_extension.presentation import project
from analysis_extension.report import render
from analysis_extension.engine import calculate
from analysis_depth_test import chain


class Html(HTMLParser):
    def __init__(self, source):
        super().__init__(); self.stack=[]; self.formulas=[]; self.folded=0; self.feed(source)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='p' and attrs.get('class')=='relation-equation': self.formulas.append(''); self.stack.append('formula')
        elif self.stack: self.stack.append(tag)
        if tag=='details' and attrs.get('class')=='diagram-fold': self.folded+=1
    def handle_endtag(self, tag):
        if self.stack: self.stack.pop()
    def handle_data(self, text):
        if self.stack: self.formulas[-1]+=text


class PresentationRevisionTests(PresentationCase):
    build = helpers.ModelPresentationTests.build
    plan = helpers.ModelPresentationTests.plan

    async def publish_plan(self, plans, presentation=None):
        result=await self.service.publish(self.ref,[],plans,'publish-revision',0,presentation=presentation)
        self.assertTrue(result['delivery']['registered'])
        document=self.delivery.view(result['delivery']['path'])
        return document,render(document),result

    async def test_shared_roles_preserve_asymmetric_groups_and_cross_type_order(self):
        await self.build(formula=True)
        first,second=self.refs[:2]
        plans=[self.plan('METRIC',viewId='a',valueRefs=[first,second],groupId='topic-a',title='对象甲'),
               self.plan('FORMULA',valueRefs=['derived_formula'],groupId='topic-a',description='这一段属于整组计算。'),
               self.plan('METRIC',viewId='b',valueRefs=[self.refs[-1]],groupId='topic-b',title='对象乙')]
        options={'groups':[{'groupId':'topic-a','title':'甲的结果与来源'},{'groupId':'topic-b','title':'乙的结果'}],
                 'labels':[{'valueRef':first,'role':'measure','label':'结果简称'}, {'valueRef':first,'role':'object','label':'对象简称'}]}
        view,html,_=await self.publish_plan(plans,options)
        self.assertEqual([b['viewId'] for s in view['sections'] for b in s['blocks']],['a','FORMULA','b'])
        self.assertEqual([len(b['metricIndexes']) for s in view['sections'] for b in s['blocks']],[2,0,1])
        self.assertEqual(view['metrics'][0]['label'],'结果简称')
        self.assertIn('结果简称',diagrams(view)[0]['relationship']['equation'])
        self.assertEqual(html.count('这一段属于整组计算。'),1)
        self.assertLess(html.index('甲的结果与来源'),html.index('乙的结果'))

    async def test_formula_is_one_flowing_paragraph_and_report_folding_is_effective(self):
        await self.build(formula=True)
        label='保留关键限定的长指标名称'*5
        plans=[self.plan('FORMULA',valueRefs=['derived_formula'],showChecks=True,collapsed=True,labels=[{'valueRef':self.refs[0],'label':label}])]
        view,html,_=await self.publish_plan(plans)
        parsed=Html(html)
        self.assertEqual(parsed.formulas,[diagrams(view)[0]['relationship']['equation']])
        self.assertIn(label,parsed.formulas[0]); self.assertEqual(parsed.folded,1)
        self.assertNotIn('80ch',html); self.assertIn('content-visibility: visible',html)
        self.assertEqual(diagrams(view)[0]['primitives'],[])
        self.assertEqual(diagrams(view)[0]['relationship']['checks'][1]['display'],'0')

    async def test_shared_names_have_separate_table_row_and_column_roles(self):
        await self.build(values=('1',))
        view,html,_=await self.publish_plan([self.plan()], {'labels':[
            {'valueRef':self.refs[0],'role':'measure','label':'经营指标'}, {'valueRef':self.refs[0],'role':'object','label':'业务单位'}]})
        self.assertEqual(view['tables'][0]['columns'][0]['label'],'经营指标')
        self.assertEqual(view['tables'][0]['rows'][0]['label'],'业务单位')

    async def test_same_sort_result_orders_negative_values_in_chart_table_and_summary(self):
        source,selectors,scope=saved_fixture(self.store,('100','-1033.14','-8300'),unit=None)
        built=await self.service.build([source],selectors,scope,[{'id':'sort','operator':'SORT','direction':'desc'}],'sorted')
        self.ref=built['analysisRef']; self.sid=built['scope']['scopeId']; self.artifact=self.store.get_analysis(OWNER,self.ref)
        self.refs=[f['factId'] for f in self.artifact['facts']]
        plans=[self.plan('BAR_VALUES',valueRefs=list(reversed(self.refs)),orderRef='derived_sort'),self.plan(orderRef='derived_sort')]
        view,html,result=await self.publish_plan(plans)
        self.assertEqual([row['cells'][view['tables'][0]['columns'][0]['key']]['exact'] for row in view['tables'][0]['rows']],['100','-1033.14','-8300'])
        self.assertEqual([p['text'] for p in comparison(view)['primitives'] if p['kind']=='text' and p['tone']=='value'],['100','-1,033.14','-8,300'])
        self.assertTrue(all(order['factRefs']==self.refs for order in result['summary']['displayOrders']))

    async def test_selected_order_applies_without_forcing_a_sort(self):
        await self.build()
        view,_,_=await self.publish_plan([self.plan(valueRefs=list(reversed(self.refs)))])
        self.assertEqual([r['label'] for r in view['tables'][0]['rows']],['合成成员 2','合成成员 1','合成成员 0'])

    async def test_explicit_group_name_cannot_collide_with_an_ungrouped_view(self):
        await self.build()
        view,_,_=await self.publish_plan([
            self.plan('METRIC',viewId='a',valueRefs=[self.refs[0]]),
            self.plan('METRIC',viewId='b',valueRefs=[self.refs[1]],groupId='view:a')],
            {'groups':[{'groupId':'view:a','title':'独立业务主题'}]})
        self.assertEqual([s['groupId'] for s in view['sections']],['view:a','group:view:a'])

    async def test_published_sort_applies_before_pagination_and_top_n(self):
        from analysis_extension.presentation import page
        source,selectors,scope=saved_fixture(self.store,tuple(str(n) for n in range(15)),unit=None)
        built=await self.service.build([source],selectors,scope,[{'id':'sort','operator':'SORT','direction':'desc'}],'paged-sort')
        self.ref=built['analysisRef']; self.sid=built['scope']['scopeId']; self.artifact=self.store.get_analysis(OWNER,self.ref)
        self.refs=[f['factId'] for f in self.artifact['facts']]
        await self.publish_plan([self.plan(orderRef='derived_sort')])
        artifact=self.store.get_analysis(OWNER,self.ref)
        document=project(artifact,self.rules)
        def amounts(view):
            table=view['tables'][0]; key=table['columns'][0]['key']
            return [row['cells'][key]['exact'] for row in table['rows']]
        self.assertEqual(amounts(page(document,0,12)),[str(n) for n in range(14,-1,-1)])
        self.assertEqual(amounts(page(document,12,12)),[str(n) for n in range(14,-1,-1)])
        self.assertEqual(amounts(project(artifact,self.rules,{'topN':2})),['14','13'])
        self.assertEqual(amounts(project(artifact,self.rules,{'sort':'asc','topN':2})),['0','1'])

    async def test_tables_have_independent_orders_without_a_global_conflict(self):
        await self.build()
        await self.publish_plan([self.plan(viewId='forward'),self.plan(viewId='reverse',valueRefs=list(reversed(self.refs)))])
        document = project(self.store.get_analysis(OWNER, self.ref), self.rules)
        self.assertEqual([r['memberKey'] for r in document['tables'][0]['rows']],
                         list(reversed([r['memberKey'] for r in document['tables'][1]['rows']])))

    async def test_relation_capacity_is_rejected_before_client_delivery(self):
        from analysis_extension.diagrams import relationship_scene
        item={'key':'k','label':'指标','display':'1','note':''}
        for equation,inputs in [('😀'*8001,[item]), ('值 = 结果',[item]*101)]:
            with self.assertRaises(AnalysisError) as error:
                relationship_scene('FORMULA','计算关系','',item,inputs,equation)
            self.assertEqual(error.exception.code,'DIAGRAM_LIMIT')

    async def test_one_scope_cannot_merge_same_key_from_different_models(self):
        from analysis_extension.adapters.budget_v2 import adapt
        first,selectors,scope=fixture(('12',)); second=deepcopy(first)
        for capture,model in ((first,'BUDGET'),(second,'FINANCE')):
            receipt=receipt_from_capture(capture)
            for step in receipt['data']['stepResults']:
                for form in step.get('formResults',[]):
                    form['dimensions']=[{'code':'MD_ORG','valueModelCode':model}]
            capture['response']['content'][0]['text']=json.dumps(receipt)
        selection=selectors[0]
        with self.assertRaises(AnalysisError) as error:
            adapt({'first':first,'second':second},[{**selection,'captureRef':'first'},{**selection,'captureRef':'second'}],scope,100)
        self.assertEqual(error.exception.code,'MEMBER_IDENTITY_CONFLICT')

    async def test_node_caption_cannot_take_another_nodes_check(self):
        await self.build(formula=True)
        plan=self.plan('FORMULA',valueRefs=['derived_formula'],nodeDescriptions=[{'nodeRef':self.refs[1],
            'parts':[{'valueRef':'derived_formula','field':'localValue'}]}])
        with self.assertRaises(AnalysisError) as error: validate_views(self.artifact,[plan])
        self.assertEqual(error.exception.code,'INVALID_NODE_DESCRIPTION')
        plan['nodeDescriptions'][0]['parts'][0]['nodeRef']=self.refs[1]
        # A non-formula leaf cannot lend a local calculation to its own caption either.
        with self.assertRaises(AnalysisError): validate_views(self.artifact,[plan])

    async def test_group_caption_is_not_reused_on_mismatching_formula_node(self):
        await self.build(formula=True)
        facts=chain(); self.artifact['facts']=facts; self.artifact['scope']['members']=['member']
        self.artifact['derivedValues']=calculate(facts,self.artifact['scope'],[{'id':'f','operator':'FORMULA_CHECK','inputRefs':['root']}],self.rules)
        self.artifact['viewPlan']=[self.plan('FORMULA',showChecks=True,valueRefs=['derived_f'],description='目标本层一致，差异来自下层。',nodeDescriptions=[{'nodeRef':'subtotal','parts':[
            {'text':'本项目本层差额 '},{'valueRef':'derived_f','nodeRef':'subtotal','field':'localDifference'}]}])]
        self.artifact['publicationState']='PUBLISHED'
        from analysis_extension.diagrams import relation_scenes
        graphs=relation_scenes(self.artifact)
        self.assertEqual([g['description'] for g in graphs],['','本项目本层差额 -7'])
        self.assertEqual(graphs[1]['relationship']['checks'][1]['display'],'-7')

    async def test_formula_waterfall_shows_residual_and_rejects_nonlinear_attribution(self):
        await self.build(formula=True)
        fact=self.artifact['facts'][0]; fact['value']='-4'
        self.artifact['derivedValues']=calculate(self.artifact['facts'],self.artifact['scope'],self.artifact['plan']['operations'],self.rules)
        self.artifact['viewPlan']=[self.plan('WATERFALL',showChecks=True,orientation='horizontal',valueRefs=['derived_formula'],nodeRefs=[fact['factId']])]
        self.artifact['publicationState']='PUBLISHED'
        view=project(self.artifact,self.rules)
        values=[p['text'] for p in diagrams(view)[0]['primitives'] if p['kind']=='text']
        self.assertIn('差额（来源 − 计算）',values); self.assertIn('1',values); self.assertIn('-4',values)
        self.assertEqual(diagrams(view)[0]['type'],'WATERFALL')
        fact['formula']='B1*C1'
        self.artifact['derivedValues']=calculate(self.artifact['facts'],self.artifact['scope'],self.artifact['plan']['operations'],self.rules)
        with self.assertRaises(AnalysisError) as error: project(self.artifact,self.rules)
        self.assertEqual(error.exception.code,'NON_ADDITIVE_CHART')

    async def test_compact_inventory_and_model_identity_do_not_merge_homonymous_keys(self):
        capture,selectors,scope=fixture(('12',))
        receipt=receipt_from_capture(capture)
        rows=receipt['data']['stepResults'][0]['rows']
        rows[0].update(modelCode='MODEL_BUDGET',label='预算组织名称')
        rows.append({**rows[0],'modelCode':'MODEL_FINANCE','label':'财务组织名称'})
        for step in receipt['data']['stepResults']:
            for form in step.get('formResults',[]): form['dimensions']=[{'code':'MD_ORG','valueModelCode':'MODEL_BUDGET'}]
        capture['response']['content'][0]['text']=json.dumps(receipt)
        source=self.store.capture(OWNER,capture)
        for selector in selectors: selector['captureRef']=source
        scope.pop('population')
        built=await self.service.build([source],selectors,scope,[],'identity')
        facts=(await self.service.inspect(section='facts',analysisRef=built['analysisRef']))['items']
        self.assertEqual(facts[0]['objectLabel'],'预算组织名称'); self.assertEqual(facts[0]['objectModelCode'],'MODEL_BUDGET')
        compact=await self.service.inspect(section='inventory',memberDimension='MD_ORG',cellKey='2:1',captureRef=source)
        full=await self.service.inspect(section='inventory',memberDimension='MD_ORG',detail='full')
        row=compact['items'][0]
        self.assertEqual(row['objectLabel'],'预算组织名称')
        self.assertEqual(compact['contexts'][row['contextRef']]['observationId'],full['items'][0]['observationId'])
        self.assertEqual((await self.service.inspect(section='inventory',cellKey='not-a-cell'))['items'],[])

    async def test_invalid_groups_names_and_incomplete_sort_fail_before_publish(self):
        await self.build()
        for options in ({'labels':[{'valueRef':self.refs[0],'role':'unknown','label':'x'}]}, {'groups':[{'groupId':'g','title':'x'},{'groupId':'g','title':'y'}]}):
            with self.assertRaises(AnalysisError): validate_presentation(self.artifact,options,[self.plan()])
        with self.assertRaises(AnalysisError): validate_views(self.artifact,[self.plan(orderRef='derived_sum')])
