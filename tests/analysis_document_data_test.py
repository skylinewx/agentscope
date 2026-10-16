"""Independent regressions for exact ordered sets and complete formula inputs."""
from copy import deepcopy

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.document_data import compile_datasets, formula_dataset
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.publication import schema_issues


class DocumentDataTests(PresentationCase):
    async def test_sorted_rows_preserve_negatives_ties_missing_and_exact_decimals(self):
        cap, selectors, scope = saved_fixture(self.store, ('-8300', '-1033.14', '123456789012345678.123456789', '-1033.14', None), unit='')
        built = await self.service.build([cap], selectors, scope, [{'id':'order','operator':'SORT'}], 'set-build')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        doc = {'title':'排序集合', 'html':'<html><head></head><body><main></main></body></html>',
               'datasets':[{'key':'ranking','valueRef':'derived_order'}]}
        compiled = compile_document(artifact, doc)
        dataset = compiled['datasets']['ranking']
        self.assertEqual([row['value'] for row in dataset['rows']], ['123456789012345678.123456789','-1033.14','-1033.14','-8300'])
        self.assertEqual([row['objectId'] for row in dataset['rows']], ['object-2','object-1','object-3','object-0'])
        self.assertEqual(dataset['unavailable'][0]['value'], None)
        self.assertTrue(all(row['unit'] == '' for row in dataset['rows']))
        self.assertEqual(compiled['bindingCount'], 0)
        self.assertEqual(compiled['datasetCount'], 1)
        self.assertEqual(schema_issues({'analysisRef':built['analysisRef'],'document':doc,'requestKey':'p','expectedBaseRevision':0}), [])
        preview = await self.service.publish(analysisRef=built['analysisRef'],document=doc,requestKey='preview',expectedBaseRevision=0,previewOnly=True,
                                             renderCheckUnavailableReason='离线数据集合契约测试未启动浏览器，实际视觉效果未验证。')
        self.assertEqual(schema_issues(preview['confirmPublish']), [])
        bad = {**preview['confirmPublish'], 'workRefs': []}
        self.assertEqual(schema_issues(bad)[0]['path'], '/workRefs')
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'], published['delivery'])
        self.assertEqual(self.delivery.view(published['delivery']['path'])['freeDocument']['html'], compiled['html'])

    def formula(self, expression='A1-B1+2'):
        facts = {ref: {'factId':ref,'objectId':'a','objectLabel':'甲','headers':[name],
                       'unit':'','valueState':'PRESENT','value':raw}
                 for ref, name, raw in [('root','结果','5'),('a','输入甲','10'),('b','输入乙','3')]}
        node = {'factRef':'root','expression':expression,'dependencies':[{'address':'A1','factRef':'a'},{'address':'B1','factRef':'b'}],
                'observed':'5','recalculated':'8','difference':'-3','localCheck':{'value':'9','difference':'-4'},
                'propagatedDifference':'1','differenceOriginRefs':['a']}
        value = {'derivedId':'derived_formula','operator':'FORMULA_CHECK','calculationValidation':'PASS',
                 'formulaTrace':{'targetFactRef':'root','nodes':[node]}}
        return value, facts

    def test_formula_graph_preserves_operands_signs_constant_and_difference_baselines(self):
        value, facts = self.formula()
        node = formula_dataset(value, facts, ['root'], None)['nodes'][0]
        self.assertEqual([o['factRef'] for o in node['operands']], ['a','b'])
        self.assertEqual([c['value'] for c in node['contributions']], ['10','-3'])
        self.assertEqual(node['constant']['value'], '2')
        self.assertEqual(node['values']['localDifference']['value'], '-4')
        self.assertEqual(node['values']['recursiveDifference']['value'], '-3')
        self.assertNotIn('A1', str(node['expressionTree']))
        self.assertEqual(node['values']['propagatedDifference']['value'], '1')
        nonlinear, facts = self.formula('A1*B1')
        node = formula_dataset(nonlinear, facts, None, None)['nodes'][0]
        self.assertFalse(node['additive'])
        self.assertNotIn('contributions', node)
        for refs in ([], ['missing'], ['root','root']):
            with self.assertRaises(AnalysisError): formula_dataset(value, facts, refs, None)

    def test_small_linear_coefficients_and_terminal_hierarchy_keep_their_meaning(self):
        value, facts = self.formula('A1*0.00000001-B1')
        node = formula_dataset(value, facts, None, None)['nodes'][0]
        self.assertTrue(node['additive'])
        self.assertEqual(node['contributions'][0]['coefficient'],'0.00000001')
        self.assertEqual(node['contributions'][0]['value'],'0.00000010')
        reconciliation={'derivedId':'derived_h','operator':'RECONCILE','calculationValidation':'PASS','inputRefs':['root'],
            'reconciliation':{'parentValue':'5','childSum':None,'difference':None,'status':'NOT_APPLICABLE',
                              'expectation':'PARENT_EQUALS_DIRECT_CHILDREN_SUM','relationRef':'rel_a'}}
        data,_=compile_datasets({'facts':list(facts.values()),'derivedValues':[reconciliation]},[{'key':'h','valueRef':'derived_h'}])
        self.assertEqual(data['h']['status'],'NOT_APPLICABLE')
        self.assertEqual(data['h']['children'],[])
        self.assertIsNone(data['h']['values']['childSum']['value'])

    def test_dataset_rejects_failed_wrong_kind_and_invented_refs(self):
        value, facts = self.formula()
        artifact = {'facts': list(facts.values()), 'derivedValues': [value]}
        for spec in ({'key':'f','valueRef':'missing'}, {'key':'f','valueRef':'derived_formula','nodeRefs':['missing']},
                     {'key':'f','valueRef':'derived_formula','value':'99'}):
            with self.assertRaises(AnalysisError): compile_datasets(artifact, [spec])
        value['calculationValidation']='FAIL'
        with self.assertRaises(AnalysisError): compile_datasets(artifact, [{'key':'f','valueRef':'derived_formula'}])
