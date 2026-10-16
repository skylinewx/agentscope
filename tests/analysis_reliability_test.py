"""Regression coverage for receipt isolation, exact selectors and report scope."""
from copy import deepcopy
import json
from analysis_fixtures import fixture, OWNER, CONTEXT
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.relations import captured_relations
from analysis_extension.adapters.budget_v2 import receipt_from_capture
from analysis_extension.formulas import trace_formula
from analysis_depth_test import chain


def reply(capture, receipt):
    capture['response']['content'][0]['text']=json.dumps(receipt,ensure_ascii=False)
    return capture


class ReliabilityTests(PresentationCase):
    def source(self):
        capture, selections, scope=fixture(('10','4','5'),unit=None)
        receipt=receipt_from_capture(capture)
        for row in receipt['data']['stepResults'][0]['rows']: row['resourceIri']='iri:'+row['key']
        ref=self.store.capture(OWNER,reply(capture,receipt))
        for s in selections: s['captureRef']=ref
        scope['population']['captureRef']=ref
        return ref,selections,scope

    def traversal(self,cursor=None,next_cursor=None,keys=('object-1','object-2')):
        capture,_,_=fixture()
        capture['originalToolName']='traverse_relation'
        capture['request']={'contractVersion':'2.0','contextId':CONTEXT['contextId'],'querySessionId':'query-A',
                            'resourceIri':'iri:object-0','capabilityId':'child-cap','traversalScope':'DIRECT',
                            'traversalMaximumDepth':1,'traversalPageSize':20}
        if cursor: capture['request']['traversalCursor']=cursor
        receipt={'contractVersion':'2.0','status':'SUCCEEDED','context':deepcopy(CONTEXT),'querySessionId':'query-A',
                 'completeness':{'enumerationExhausted':not next_cursor},'data':{
                 'rootResourceIri':'iri:object-0','capabilityId':'child-cap','scope':'DIRECT','maximumDepth':1,
                 'rows':[{'key':k,'label':'名称'+k,'resourceIri':'iri:'+k,'rootResourceIri':'iri:object-0','depth':1} for k in keys],
                 'page':{'enumerationExhausted':not next_cursor,'nextCursor':next_cursor,'stopReasons':[]}}}
        return self.store.capture(OWNER,reply(capture,receipt))

    async def test_one_failed_receipt_does_not_hide_valid_inventory_or_relations(self):
        source,_,_=self.source(); relation=self.traversal()
        bad,_,_=fixture(); bad['captureState']='error'
        bad_ref=self.store.capture(OWNER,bad)
        mismatch,_,_=fixture(); mismatch['request']['contextId']='other-context'
        self.store.capture(OWNER,mismatch)
        result=await self.service.inspect(section='inventory',memberDimension='MD_ORG')
        self.assertEqual(result['total'],3); self.assertEqual(result['invalidCaptureCount'],2)
        self.assertNotIn(bad_ref,[r['captureRef'] for r in result['items']])
        issues=await self.service.inspect(section='inventoryIssues',limit=1)
        self.assertEqual(issues['total'],2); self.assertEqual(issues['nextOffset'],1)
        self.assertFalse(issues['items'][0]['usableAsInput'])
        relations=await self.service.inspect(section='relations')
        self.assertEqual(relations['items'][0]['childIds'],['object-1','object-2'])
        self.assertTrue(relations['items'][0]['complete'])
        # Ownership and corrupt storage remain hard errors, not skipped receipts.
        with self.assertRaises(AnalysisError): self.store.get_capture(Owner('other','agent','session'),source)

    async def test_exact_selectors_root_rows_and_invalid_steps(self):
        source,_,_=self.source(); ref=self.traversal()
        result=await self.service.inspect(section='selectors',captureRef=ref)
        self.assertEqual(result['items'][0]['populationSelector'],{'captureRef':ref,'memberPointer':'/key'})
        rows=await self.service.inspect(section='rows',captureRef=ref)
        self.assertEqual(len(rows['items']),2)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='rows',captureRef=ref,stepId='invented')
        self.assertEqual(caught.exception.details['nextSection'],'selectors')

    async def test_verified_search_identity_can_supply_name_but_not_population(self):
        capture,selections,scope=fixture(('10',),unit=None)
        receipt=receipt_from_capture(capture)
        receipt['data']['stepResults'][0]['rows']=[]
        for step in receipt['data']['stepResults']:
            for form in step.get('formResults',[]): form['dimensions']=[{'code':'MD_ORG','valueModelCode':'MODEL_A'}]
        source=self.store.capture(OWNER,reply(capture,receipt))
        for s in selections:s['captureRef']=source
        scope.pop('population',None)
        names=deepcopy(capture);names['originalToolName']='search_entities'
        candidate={'key':'object-0','label':'来源正式名称','resourceIri':'iri:object-0',
                   'modelCode':'MODEL_A','kind':'RESOURCE','matchedBy':['RESOURCE_RDF_VERIFIED']}
        names_ref=self.store.capture(OWNER,reply(names,{**receipt,'data':{'candidates':[candidate]}}))
        found=await self.service.inspect(section='inventory',memberDimension='MD_ORG')
        self.assertEqual(found['items'][0]['objectLabel'],'来源正式名称')
        self.assertEqual(found['items'][0]['objectLabelCaptureRefs'],[names_ref])
        entities=await self.service.inspect(section='entities',objectId='object-0')
        self.assertEqual(entities['items'][0]['label'],'来源正式名称')
        self.assertEqual((await self.service.inspect(section='selectors',captureRef=names_ref))['items'],[])
        built=await self.service.build([source,names_ref],selections,scope,[],'names')
        facts=(await self.service.inspect(section='facts',analysisRef=built['analysisRef']))['items']
        self.assertEqual(facts[0]['objectLabel'],'来源正式名称')
        # A fuzzy suggestion without an actual RDF resource is not a source name.
        candidate['matchedBy']=['SEARCH_VECTOR_ONLY'];candidate['label']='未验证的相似名称'
        other=self.store.capture(OWNER,reply(names,{**receipt,'data':{'candidates':[candidate]}}))
        self.assertEqual((await self.service.inspect(section='entities',captureRef=other))['items'],[])

    async def test_scope_recovery_returns_actual_selector_and_dimension(self):
        ref,selections,scope=self.source()
        scope['expectedDimensions']={'MD_SCENE':'invented'}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.build([ref],selections,scope,[],'bad-scope')
        details=caught.exception.details
        self.assertEqual(details['field'],'scope.expectedDimensions')
        self.assertEqual(details['dimension'],'MD_SCENE')
        self.assertEqual(details['captureRef'],ref)
        self.assertIn('actual',details)

    async def test_target_lookup_does_not_invalidate_full_page_of_same_observation(self):
        source,selections,scope=self.source()
        targeted=deepcopy(self.store.get_capture(OWNER,source));receipt=receipt_from_capture(targeted)
        for step in receipt['data']['stepResults']:
            for form in step.get('formResults',[]):
                form['cells']=form['page']['cells'];form['page']=None
        other=self.store.capture(OWNER,reply(targeted,receipt))
        result=await self.service.build([source,other],selections,scope,[],'combined')
        self.assertEqual(result['scope']['coverage'],'COMPLETE')

    def test_model_caption_binds_formula_node_numbers_and_emphasis(self):
        from analysis_extension.engine import calculate,RuleRegistry
        from analysis_extension.validation import render_parts
        scope={'scopeId':'scope','coverage':'UNKNOWN'}
        facts=chain();value=calculate(facts,scope,[{'id':'formula','operator':'FORMULA_CHECK','inputRefs':['root']}],RuleRegistry())[0]
        text,refs=render_parts({'derived_formula':value},[
            {'text':'复算后高于来源 **'},
            {'valueRef':'derived_formula','nodeRef':'subtotal','field':'localAbsoluteDifference','format':{'decimalPlaces':2}},
            {'text':'**，差异继续传入结果。'}])
        self.assertEqual(text,'复算后高于来源 **7.00**，差异继续传入结果。')
        self.assertEqual(refs,['derived_formula'])
        with self.assertRaises(AnalysisError):
            render_parts({'derived_formula':value},[{'valueRef':'derived_formula','nodeRef':'invented','field':'unit'}])

    def test_semantic_caption_preserves_punctuation_and_amounts(self):
        from analysis_extension.diagrams import relationship_scene
        from analysis_extension.report_scenes import chart_content
        text='经营结果（按预算口径）为 -7,021.86；调整后仍需说明条件，不能把平衡写成转正。'
        item={'key':'k','label':'项目','display':'100','note':''}
        scene=relationship_scene('FORMULA','计算关系','',item,[item],status=text)
        self.assertIn('<p class="relation-caption">'+text+'</p>',chart_content(scene))

    async def test_paged_relation_requires_initial_page_and_all_linked_pages(self):
        source,_,_=self.source()
        tail=self.traversal(cursor='page-2',keys=('object-2',))
        def relations(refs):return captured_relations({r:self.store.get_capture(OWNER,r) for r in refs})
        self.assertEqual(relations([source,tail]),[])
        first=self.traversal(next_cursor='page-2',keys=('object-1',))
        self.assertFalse(relations([source,first])[0]['complete'])
        self.assertTrue(relations([source,first,tail])[0]['complete'])
        self.assertEqual(relations([source,first,tail])[0]['childIds'],['object-1','object-2'])

    async def test_reconcile_uses_source_relation_and_cannot_omit_a_child(self):
        source,selections,scope=self.source(); ref=self.traversal()
        relation=(await self.service.inspect(section='relations'))['items'][0]['relationRef']
        draft=await self.service.build([source,ref],selections,scope,[],'base')
        facts=(await self.service.inspect(section='facts',analysisRef=draft['analysisRef']))['items']
        op={'id':'check','operator':'RECONCILE','aggregation':'SUM','relationRef':relation,'inputRefs':[f['factId'] for f in facts]}
        result=await self.service.build([source,ref],selections,scope,[op],'check')
        value=result['derivedValues'][0]
        self.assertEqual(value['value'],'1'); self.assertEqual(value['reconciliation']['childSum'],'9')
        op['inputRefs']=op['inputRefs'][:-1]
        blocked=await self.service.build([source,ref],selections,scope,[op],'partial')
        self.assertEqual(blocked['derivedValues'][0]['calculationValidation'],'BLOCKED')

    async def test_report_declared_topics_must_be_selected_and_presented(self):
        ref,selections,scope=self.source()
        draft=await self.service.build([ref],selections,scope,[],'base')
        facts=(await self.service.inspect(section='facts',analysisRef=draft['analysisRef']))['items']
        scope['reportTopics']=[{'title':'总体','question':'总体数值','factRefs':[facts[0]['factId']]},
                               {'title':'成员','question':'成员情况','factRefs':[f['factId'] for f in facts[1:]]}]
        result=await self.service.build([ref],selections,scope,[],'report')
        view={'viewId':'members','scopeId':result['scope']['scopeId'],'type':'TABLE','valueRefs':[f['factId'] for f in facts[1:]]}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(result['analysisRef'],[],[view],'incomplete',0)
        self.assertEqual(caught.exception.code,'REPORT_TOPIC_UNREPRESENTED')
        view['valueRefs']=[f['factId'] for f in facts]
        await self.service.publish(result['analysisRef'],[],[view],'complete',0)
        results=await self.service.inspect(section='results')
        self.assertEqual(results['total'],2)
        self.assertEqual(results['items'][-1]['reportTopics'][0]['title'],'总体')

    def test_negative_difference_does_not_reverse_recalculation_direction(self):
        result=trace_formula(chain(),'root')
        comparison=result['comparisons']['recursive']
        self.assertEqual(comparison['leftValue'],'-8'); self.assertEqual(comparison['rightValue'],'-1')
        self.assertEqual(comparison['leftComparedToRight'],'LESS')
        self.assertEqual(comparison['absoluteDifference'],'7')

    def test_absolute_difference_keeps_all_significant_digits(self):
        from analysis_extension.comparisons import comparison
        amount='123456789012345678901234567890.123456789'
        result=comparison('0',amount,'source','calculation')
        self.assertEqual(result['absoluteDifference'],amount)
        self.assertEqual(result['difference'],'-'+amount)
        self.assertEqual(result['leftComparedToRight'],'LESS')
