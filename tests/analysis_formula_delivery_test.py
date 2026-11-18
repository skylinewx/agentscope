from copy import deepcopy
from unittest import TestCase
from analysis_extension.document_coverage import evaluate
from analysis_extension.document_checks import require_publication_checks
from analysis_extension.models import AnalysisError
from analysis_extension.document_formula_requirements import require_formula_datasets
from analysis_extension.document_data import compile_datasets
from analysis_budget_formula_arithmetic_test import budget_facts, computed


class FormulaDeliveryTests(TestCase):
    def declared(self):
        facts=budget_facts()
        value=computed(facts)
        artifact={'facts':facts,'derivedValues':[value], 'plan':{'workPlan':[
            {'taskId':'formation','kind':'COMPUTATION','operator':'FORMULA_CHECK','targetRefs':['root'],
             'refs':[value['derivedId']]}]}}
        return artifact,value

    def test_declared_formula_cannot_disappear_when_only_source_amount_is_bound(self):
        from analysis_extension.free_document import compile_document
        artifact,value=self.declared()
        document={'title':'形成分析','html':'<html><head></head><body>{{amount}}</body></html>',
                  'bindings':[{'key':'amount','valueRef':'root'}]}
        with self.assertRaises(AnalysisError) as caught: compile_document(artifact,document)
        self.assertEqual(caught.exception.code,'DOCUMENT_FORMULA_DATASET_REQUIRED')
        self.assertEqual(caught.exception.details['missingFormulas'][0]['missingNodeRefs'],['root'])
        document['datasets']=[{'key':'formation','valueRef':value['derivedId']}]
        compiled=compile_document(artifact,document)
        self.assertEqual(len(compiled['datasets']['formation']['nodes'][0]['waterfall']['steps']),3)

    def test_optional_or_unused_historical_formulas_do_not_add_obligations(self):
        artifact,value=self.declared()
        old=deepcopy(value);old['derivedId']='historical'
        artifact['derivedValues'].append(old)
        datasets,_=compile_datasets(artifact,[{'key':'formation','valueRef':value['derivedId']}])
        require_formula_datasets(artifact,datasets)
        artifact['plan']['workPlan']=[]
        require_formula_datasets(artifact,{})

    def test_partial_node_selection_is_not_complete_adopted_formula(self):
        artifact,value=self.declared()
        value['formulaTrace']['nodes'].append({'factRef':'expense','expression':'A1+B1','boundary':False})
        with self.assertRaises(AnalysisError) as caught:
            require_formula_datasets(artifact,{'formation':{'kind':'FORMULA','valueRef':value['derivedId'],
                                                          'nodes':[{'factRef':'root'}]}})
        self.assertEqual(caught.exception.details['missingFormulas'][0]['missingNodeRefs'],['expense'])

    def test_zero_contribution_is_required_and_legacy_measurement_cannot_bypass_it(self):
        artifact,value=self.declared()
        datasets,_=compile_datasets(artifact,[{'key':'formation','valueRef':value['derivedId']}])
        document={'bindings':[],'datasets':datasets}
        steps=datasets['formation']['nodes'][0]['waterfall']['steps']
        self.assertEqual(steps[1]['value'],'0')
        bindings=[{'setKey':'formation','nodeRef':'root','stepIndex':i} for i in (0,2)]
        partial=evaluate(document,{'charted':bindings,'presented':bindings,'complete':True})
        record={'viewport':{'width':900},'blocking':False,'status':'MEASURED','numericChecks':{},'orders':[]}
        for coverage in (None,partial,{'complete':True,'charted':[],'presented':[]}):
            record['valueCoverage']=coverage
            with self.assertRaises(AnalysisError) as caught:
                require_publication_checks(document,[record],unavailable_reason='测试只测一个宽度')
            self.assertEqual(caught.exception.code,'DOCUMENT_FORMULA_COVERAGE_MISSING')
        unchecked=require_publication_checks(document,[],unavailable_reason='浏览器未执行')
        self.assertEqual(unchecked['status'],'NOT_CHECKED')

    def test_multiterm_formula_cannot_be_replaced_by_only_a_table(self):
        document = {'bindings': [], 'datasets': {'profit': {'kind':'FORMULA', 'valueRef':'derived',
            'nodes':[{'factRef':'node', 'waterfall':{'steps':[{}, {}, {}]}}]}}}
        bindings = [{'setKey':'profit', 'nodeRef':'node', 'stepIndex':i} for i in range(3)]
        table_only = evaluate(document, {'charted':[], 'presented':bindings, 'complete':True})
        self.assertEqual(len(table_only['missingFormulaSteps']), 3)
        record = {'viewport':{'width':900}, 'blocking':False, 'status':'MEASURED',
                  'valueCoverage':table_only, 'numericChecks':{}, 'orders':[]}
        with self.assertRaises(AnalysisError) as caught:
            require_publication_checks(document, [record], unavailable_reason='只检测一个宽度')
        self.assertEqual(caught.exception.code, 'DOCUMENT_FORMULA_COVERAGE_MISSING')
        record['valueCoverage'] = evaluate(document, {'charted':bindings, 'presented':bindings, 'complete':True})
        require_publication_checks(document, [record], unavailable_reason='只检测一个宽度')
        changed = deepcopy(document)
        changed['datasets']['profit']['nodes'][0]['waterfall']['steps'].pop()
        self.assertEqual(evaluate(changed, {'charted':[], 'presented':[], 'complete':True})['requiredFormulaSteps'], [])
