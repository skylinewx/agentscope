"""Accuracy and presentation boundaries for model-led synthesis."""
from copy import deepcopy
import json
import unittest

from analysis_fixtures import OWNER
import analysis_visuals_test as visuals
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.formulas import Expression, trace_formula
from analysis_extension.models import AnalysisError, Owner, canonical
from analysis_extension.presentation import project
from analysis_extension.validation import validate_claims, validate_views, summary
from analysis_extension.formatting import number_text, formula_text
from analysis_extension.tools import AnalysisBuild, AnalysisInspect, AnalysisPublish
from jsonschema import Draft202012Validator


def chain():
    shared={'observationId':'observation', 'bindingRef':'binding', 'objectId':'member', 'objectLabel':'业务单位',
            'sourceOrigins':['source'], 'context':{'snapshot':'same'}, 'unit':None, 'scale':None,
            'valueState':'PRESENT', 'precision':'raw_json_number'}
    return [{**shared, 'factId':fid, 'cellAddress':address, 'cellKey':address, 'headers':[label], 'value':value, 'formula':formula}
            for fid,address,label,value,formula in [('root','A1','净结果','-8','B1-C1'),
                ('subtotal','B1','业务结果','100','D1-E1'), ('expense','C1','其他支出','108',None),
                ('income','D1','收入','150',None), ('cost','E1','成本','43','OTHER!A1')]]


class FormulaDepthTests(unittest.TestCase):
    def test_local_match_and_propagated_difference_with_included_boundary(self):
        trace=trace_formula(chain(),'root')
        nodes={n['factRef']:n for n in trace['nodes']}
        self.assertEqual(trace['executionStatus'],'COMPLETED')
        self.assertEqual(trace['sourceConsistency'],'MISMATCH')
        self.assertEqual(trace['targetLocalCheck'],{'status':'MATCH','value':'-8','difference':'0'})
        self.assertEqual(trace['value'],'-1')
        self.assertEqual(nodes['root']['difference'],'-7')
        self.assertEqual(nodes['root']['propagatedDifference'],'-7')
        self.assertEqual(trace['differenceOriginRefs'],['subtotal'])
        self.assertEqual(nodes['subtotal']['localCheck']['difference'],'-7')
        self.assertEqual(nodes['cost']['numericRole'],'INCLUDED_SOURCE_VALUE')
        self.assertEqual(trace['externalBoundaries'],['cost'])
        self.assertEqual(trace['pendingDependencies'],[])

    def test_local_check_survives_unsupported_child_formula(self):
        facts=chain(); facts[1]['formula']='ABS(D1)'
        trace=trace_formula(facts,'root')
        self.assertEqual(trace['status'],'BLOCKED')
        self.assertEqual(trace['targetLocalCheck']['status'],'MATCH')
        self.assertIsNone(trace['value'])

    def test_cancelling_child_differences_do_not_mean_entire_chain_matches(self):
        facts=chain(); facts[2]['formula']='D1-E1+8'; facts[2]['value']='108'
        trace=trace_formula(facts,'root')
        self.assertEqual(trace['value'],'-8')
        self.assertEqual(trace['nodes'][0]['difference'],'0')
        self.assertEqual(trace['status'],'MISMATCH')
        self.assertEqual(set(trace['differenceOriginRefs']),{'subtotal','expense'})

    def test_missing_and_unit_conflict_cannot_form_local_check(self):
        for change in ({'value':None,'valueState':'NO_VALUE'}, {'unit':'another'}):
            facts=chain(); facts[1].update(change)
            trace=trace_formula(facts,'root')
            self.assertEqual(trace['status'],'BLOCKED')
            self.assertEqual(trace['targetLocalCheck']['status'],'BLOCKED')

    def test_formatting_does_not_round_calculations(self):
        self.assertEqual(number_text('12345678901234567890.125',{'grouping':True,'decimalPlaces':2}),'12,345,678,901,234,567,890.13')
        self.assertEqual(number_text('-0.001',{'decimalPlaces':2}),'0.00')
        for bad in ({'decimalPlaces':True},{'decimalPlaces':17},{'grouping':'true'},{'unit':'万元'},None):
            if bad is None: continue
            with self.assertRaises(AnalysisError): number_text('12',bad)

    def test_business_formula_preserves_operator_precedence_without_nested_chain(self):
        names={'A1':'收入','B1':'成本','C1':'费用','D1':'数量','E1':'基数'}
        self.assertEqual(formula_text(Expression('A1-B1-C1+D1').tree,names),'收入 − 成本 − 费用 + 数量')
        self.assertEqual(formula_text(Expression('A1-(B1-C1)').tree,names),'收入 − (成本 − 费用)')
        self.assertEqual(formula_text(Expression('-(A1+B1)*C1/(D1/E1)').tree,names),'-(收入 + 成本) × 费用 ÷ (数量 ÷ 基数)')

    def test_diagram_notes_remain_whole_semantic_text(self):
        from analysis_extension.diagrams import relationship_scene
        note='直属子级合计 103,655,266.86；父级减子级合计差额 33,165,819.20'
        item={'key':'k','label':'项目','display':'100','note':'按直接项目计算 1,544,521.86；本层差额 -7,021.86'}
        graph=relationship_scene('FORMULA','计算关系','',item,[item],status=note)
        self.assertEqual(graph['relationship']['note'],note)
        self.assertEqual(graph['relationship']['inputs'][0]['note'],item['note'])
        self.assertEqual(graph['primitives'],[])

    def test_formula_nodes_follow_model_order_in_graph_waterfall_and_details(self):
        from analysis_extension.diagrams import relation_scenes
        from analysis_extension.comparison_scenes import comparison_scenes
        from analysis_extension.tables import business_tables
        from analysis_extension.formula_selection import selected_nodes
        facts=chain(); trace=trace_formula(facts,'root')
        value={'derivedId':'derived_formula','formulaTrace':trace}
        plan={'viewId':'focus','type':'FORMULA','valueRefs':['derived_formula'],
              'nodeRefs':['subtotal','root'],'includeDetails':True,'showUnit':False}
        art={'facts':facts,'derivedValues':[value],'viewPlan':[plan],'technicalDetails':False}
        before=canonical(art)
        graphs=relation_scenes(art)
        self.assertEqual([g['relationship']['result']['label'] for g in graphs],['业务结果','净结果'])
        self.assertEqual(graphs[0]['relationship']['equation'],'收入 − 成本 = 业务结果')
        self.assertEqual(graphs[1]['relationship']['equation'],'业务结果 − 其他支出 = 净结果')
        table=business_tables(art,facts,[])[0]
        self.assertEqual([r['label'] for r in table['rows']],['业务结果','净结果'])
        waterfall={**art,'viewPlan':[{**plan,'type':'WATERFALL'}]}
        self.assertEqual([g['title'].split(' · ')[-1] for g in comparison_scenes(waterfall)],['业务结果','净结果'])
        self.assertEqual(canonical(art),before)
        self.assertEqual(selected_nodes(trace,{}),trace['nodes'])
        # Full calculation remains available despite focused display.
        self.assertEqual(trace['value'],'-1')
        self.assertEqual(len(trace['nodes']),5)

    def test_source_bound_arithmetic_uses_shared_exact_calculator(self):
        facts=chain()
        result=calculate(facts,{'scopeId':'s'},[{'id':'ratio','operator':'ARITHMETIC',
            'inputRefs':['root','subtotal'],'expression':'abs(v0)/v1*100','decimalPlaces':2}],RuleRegistry())[0]
        self.assertEqual(result['value'],'8.00')
        self.assertEqual(result['sourceFactIds'],['root','subtotal'])
        self.assertEqual(result['executionStatus'],'COMPLETED')
        self.assertIsNone(result['unit'])
        for expression in ('v0/0','__import__("os")','v8+1','999'):
            value=calculate(facts,{'scopeId':'s'},[{'id':'bad','operator':'ARITHMETIC','inputRefs':['root'],'expression':expression}],RuleRegistry())[0]
            self.assertEqual(value['executionStatus'],'BLOCKED')
        unused=calculate(facts,{'scopeId':'s'},[{'id':'unused','operator':'ARITHMETIC',
            'inputRefs':['root','subtotal'],'expression':'abs(v0)'}],RuleRegistry())[0]
        self.assertEqual(unused['diagnostics'][0]['code'],'UNUSED_ARITHMETIC_INPUT')


class SynthesisServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=visuals.VisualTests(); self.case.setUp(); self.addCleanup(self.case.doCleanups)
        await self.case.draft()
        self.artifact=self.case.artifact; self.service=self.case.service; self.store=self.case.store

    def reconciliation(self, artifact=None, **overrides):
        art=artifact or self.artifact
        operation={'id':'total','operator':'RECONCILE','aggregation':'SUM',
                   'relationRef':art['relations'][0]['relationRef'],'inputRefs':self.case.results,**overrides}
        return calculate(art['facts'],art['scope'],[operation],self.case.case.rules,art['relations'])[0]

    async def test_reconciliation_unknown_units_and_explicit_parent_order(self):
        result=self.reconciliation()
        self.assertEqual(result['calculationValidation'],'PASS')
        self.assertEqual(result['executionStatus'],'COMPLETED')
        self.assertEqual(result['value'],'-20') # parent -5 minus children 20 and -5
        self.assertEqual(result['reconciliation']['childSum'],'15')
        self.assertEqual(result['sourceConsistency'],'MISMATCH')
        self.assertIsNone(result['unit'])
        for overrides in ({'aggregation':None}, {'inputRefs':self.case.results[::-1]},
                          {'relationRef':'invented'}, {'inputRefs':self.case.results[:2]}):
            self.assertEqual(self.reconciliation(**overrides)['calculationValidation'],'BLOCKED')

    async def test_reconciliation_rejects_missing_data_and_conflicting_context(self):
        for change in ({'valueState':'NO_VALUE','value':None},{'unit':'万元'}, {'period':'another'},
                       {'precision':'display_only'}, {'bindingRef':'other'}, {'sourceOrigins':['foreign']}):
            art=deepcopy(self.artifact)
            next(f for f in art['facts'] if f['factId']==self.case.results[-1]).update(change)
            self.assertEqual(self.reconciliation(art)['calculationValidation'],'BLOCKED')

    async def test_empty_and_incomplete_child_enumerations(self):
        art=deepcopy(self.artifact)
        art['relations'][0]['childIds']=[]
        leaf=self.reconciliation(art,inputRefs=self.case.results[:1])
        self.assertEqual(leaf['reconciliation']['status'],'NOT_APPLICABLE')
        self.assertIsNone(leaf['value'])
        self.assertIsNone(leaf['reconciliation']['childSum'])
        art['relations'][0]['complete']=False
        self.assertEqual(self.reconciliation(art,inputRefs=self.case.results[:1])['calculationValidation'],'BLOCKED')

    async def test_hierarchy_render_uses_only_matching_check(self):
        art=deepcopy(self.artifact); check=self.reconciliation()
        art['derivedValues'].append(check); art['publicationState']='PUBLISHED'
        plan={**self.case.plans()[3],'comparisonRef':check['derivedId'],'numberFormat':{'decimalPlaces':2,'grouping':True}}
        plan.pop('description',None)
        plan['descriptionParts']=[{'text':'直属子级合计 '},{'valueRef':check['derivedId'],'field':'childSum','format':{'decimalPlaces':2}},
                                  {'text':'；差额 **'},{'valueRef':check['derivedId'],'field':'difference','format':{'decimalPlaces':2}},{'text':'**'}]
        art['viewPlan']=[plan]
        validate_views(art,art['viewPlan'])
        scenes=project(art,self.case.case.rules)['scenes']
        texts=project(art,self.case.case.rules)['sections'][0]['blocks'][0]['description']
        self.assertIn('直属子级合计 15.00',texts); self.assertIn('差额 **-20.00**',texts)
        self.assertNotIn('父级减子级合计差额',''.join(p.get('text','') for s in scenes for p in s['primitives']))
        with self.assertRaises(AnalysisError): validate_views(art,[{**plan,'valueRefs':self.case.results[:2]}])

    async def test_claim_formats_and_reconciliation_fields_keep_exact_values(self):
        art=deepcopy(self.artifact); art['derivedValues'].append(self.reconciliation())
        before=canonical(art)
        claim={'claimId':'c','scopeId':art['scope']['scopeId'],'kind':'computed','parts':[
            {'text':'子级合计 **'},{'valueRef':'derived_total','field':'childSum','format':{'decimalPlaces':2,'grouping':True}},{'text':'**'}]}
        self.assertEqual(validate_claims(art,[claim])[0]['rendered'],'子级合计 **15.00**')
        self.assertEqual(canonical(art),before)
        for part in ({'valueRef':self.case.results[0],'field':'childSum'},
                     {'valueRef':self.case.results[0],'field':'unit','format':{'grouping':True}},
                     {'valueRef':'derived_total','format':{'decimalPlaces':18}}):
            with self.assertRaises(AnalysisError): validate_claims(art,[{**claim,'parts':[part]}])

    async def test_formula_claims_can_bind_local_and_recursive_difference(self):
        art=deepcopy(self.artifact)
        for field in ('sourceValue','localValue','localDifference','difference'):
            claim={'claimId':'c','scopeId':art['scope']['scopeId'],'kind':'computed',
                   'parts':[{'valueRef':'derived_formula','field':field,'format':{'grouping':True}}]}
            self.assertTrue(validate_claims(art,[claim])[0]['rendered'])

    async def test_model_labels_are_bound_safe_and_column_consistent(self):
        art=deepcopy(self.artifact); art['publicationState']='PUBLISHED'
        plan={**self.case.plans()[2],'labels':[{'valueRef':self.case.results[0],'label':'净额'}]}
        art['viewPlan']=[plan]; validate_views(art,[plan])
        texts=''.join(s['relationship']['equation'] for s in project(art,self.case.case.rules)['scenes'])
        self.assertIn('净额',texts)
        with self.assertRaises(AnalysisError): validate_views(art,[{**plan,'labels':[{'valueRef':'invented','label':'收入'}]}])
        art['viewPlan']=[{'viewId':'t','scopeId':art['scope']['scopeId'],'type':'TABLE','valueRefs':self.case.results,
                         'labels':[{'valueRef':self.case.results[0],'label':'不同列名'}]}]
        table = project(art,self.case.case.rules)['tables'][0]
        self.assertEqual(table['columns'][0]['label'], '不同列名')
        art['viewPlan'][0]['labels'].append({'valueRef':self.case.results[1], 'label':'明确冲突名'})
        with self.assertRaises(AnalysisError): project(art,self.case.case.rules)

    async def test_inventory_reuses_names_preserves_observations_and_paginates(self):
        dimension=self.artifact['scope']['memberDimension']
        result=await self.service.inspect(section='inventory',memberDimension=dimension,limit=2)
        self.assertEqual(len(result['items']),2); self.assertEqual(result['nextOffset'],2)
        self.assertEqual(result['scope'],'current_session')
        result=await self.service.inspect(section='inventory',memberDimension=dimension,objectId='object-0',search='收入')
        self.assertGreater(len(result['items']),0)
        self.assertTrue(all(i['objectId']=='object-0' and result['contexts'][i['contextRef']]['observationId'] and i['captureRef'] for i in result['items']))
        # The fixture captures the original names and then changed names in the
        # same context: the index must expose the conflict rather than pick one.
        self.assertTrue(all(i['objectLabelState']=='CONFLICT' and i['objectLabel']=='' for i in result['items']))
        import tempfile
        from analysis_extension.store import AnalysisStore
        from analysis_extension.inventory import inventory
        with tempfile.TemporaryDirectory() as directory:
            clean=AnalysisStore(directory)
            clean.capture(OWNER,self.store.get_capture(OWNER,self.artifact['captureRefs'][0]))
            resolved=inventory(clean,OWNER,0,50,dimension,objectId='object-0')
            self.assertTrue(all(i['objectLabel']=='集团预算中心（合成）' for i in resolved['items']))
        with self.assertRaises(AnalysisError): await self.service.inspect(section='inventory',objectId='object-0')
        other=inventory(self.store,Owner('another-user',OWNER.agent_id,OWNER.session_id),0,50)
        self.assertEqual(other['total'],0)

    async def test_fact_filter_and_summary_execution_meaning(self):
        facts=await self.service.inspect(section='facts',analysisRef=self.artifact['analysisRef'],objectId='object-0',search='收入')
        self.assertTrue(facts['items'])
        self.assertTrue(all(f['objectId']=='object-0' for f in facts['items']))
        checks=summary(self.artifact)['checks']
        self.assertEqual(checks[0]['executionStatus'],'COMPLETED')
        self.assertEqual(checks[0]['sourceConsistency'],'MATCH')

    async def test_current_tool_schemas_accept_new_capabilities(self):
        protocols=await self.service.inspect(section='protocol')
        self.assertIn('RECONCILE',protocols['operators']); self.assertIn('ARITHMETIC',protocols['operators'])
        Draft202012Validator(AnalysisInspect.input_schema).validate({'section':'inventory','memberDimension':'dimension','search':'收入'})
        validator=Draft202012Validator(AnalysisPublish.input_schema)
        validator.validate({'analysisRef':'a','requestKey':'p','expectedBaseRevision':0,'claims':[
            {'claimId':'c','scopeId':'s','kind':'computed','parts':[{'valueRef':'v','field':'childSum','format':{'grouping':True,'decimalPlaces':2}}]}],
            'viewPlan':[{'viewId':'h','scopeId':'s','type':'HIERARCHY','valueRefs':['p','c'],'relationRef':'r','comparisonRef':'d',
                        'labels':[{'valueRef':'p','label':'父级'}],'numberFormat':{'grouping':True}}]})
