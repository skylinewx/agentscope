"""Real host adapter over shared native facts; the surrounding receipt is synthetic.

This is deliberately not labelled an ontology end-to-end or live-model test.
"""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
from unittest import TestCase
from analysis_budget_batch_test import batch_fixture, update
from analysis_extension.adapters.budget_v2 import adapt


class SharedBudgetContractHostTest(TestCase):
    def setUp(self):
        root = Path(os.environ.get('BUDGET_CONTRACT_ROOT', Path(__file__).resolve().parents[2]/'budget/test-contracts/budget-api'))
        spec = importlib.util.spec_from_file_location('shared_budget_contract_pack', root/'tools/contract_pack.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.pack = module.ContractPack(root)
        lock = json.loads((Path(__file__).parent/'fixtures/budget-regression/contract.lock.json').read_text(encoding='utf8'))
        self.assertEqual(lock['checksumsSha256'], self.pack.digest)

    def capture(self):
        case = self.pack.cases['QUERY-HOST-SORTED']
        request = self.pack.body(case, 'request')
        data = self.pack.body(case, 'response')['data']
        capture, selection, scope, receipt = batch_fixture(False)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['request'], table['data'] = request, data
        for origin, metric, account in zip(table['metrics'], request['metrics'], ('PROFIT','COST')):
            origin.update(name=metric['name'], measureCode='Z', cellExp=metric['cellExp'],
                          selectedDimensions={'ACCOUNT':account,'MD_SCENE':'BUDGET'},
                          headers=[account],dimensionModels={'MD_ORG':'ORG'})
            origin['context']['effectiveDimensions'] = deepcopy(metric['dimValMap'])
            origin['context']['target'].update(mainTaskId='task',viewId='view',organizationCategory='ORG')
        table['columnRoles'] = {
            'DATATIME':{'kind':'DIMENSION','dimCode':'DATATIME','valueColumn':'DATATIME'},
            'DATATIME_NAME':{'kind':'NAME','dimCode':'DATATIME','valueColumn':'DATATIME'},
            'MD_ORG':{'kind':'DIMENSION','dimCode':'MD_ORG','valueColumn':'MD_ORG'},
            'MD_ORG_NAME':{'kind':'NAME','dimCode':'MD_ORG','valueColumn':'MD_ORG'},
            'metric1':{'kind':'METRIC'},'metric2':{'kind':'METRIC'}}
        public = deepcopy(request)
        public.pop('budTaskId')
        public['metrics'] = [{'selection':{'periodCode':'2026N0001','measureCode':'Z','targetDimensions':{'ACCOUNT':account}}}
                             for account in ('PROFIT','COST')]
        capture['request']['plan']['steps'][1]['batchQuery'] = public
        scope.update(label='合成单位全范围',members=['A','B','C','E','D'])
        update(capture, receipt)
        return capture,selection,scope

    def test_full_order_accounts_and_null_survive_real_host_adaptation(self):
        capture, selection, scope = self.capture()
        result = adapt({'batch-ref':capture},[selection],scope,100)
        facts = result['facts']
        self.assertEqual(10,len(facts))
        profit = [f for f in facts if f['measureCode']=='metric1']
        self.assertEqual(['A','B','C','E','D'],[f['objectId'] for f in profit])
        self.assertEqual(['30000.00','20000.00','10000.00','0',None],[f['value'] for f in profit])
        self.assertEqual(['PRESENT']*4+['NO_VALUE'],[f['valueState'] for f in profit])
        self.assertEqual({'PROFIT','COST'},{f['dimensions']['ACCOUNT'] for f in facts})
        self.assertEqual('COMPLETE',result['scope']['coverage'])

    def test_mismatched_source_request_is_not_accepted_as_real_facts(self):
        from analysis_extension.models import AnalysisError
        capture, selection, scope = self.capture()
        receipt=json.loads(capture['response']['content'][0]['text'])
        receipt['data']['stepResults'][0]['batchResults'][0]['request']['metrics'][0]['cellExp']='M_N[Z,ACCOUNT="OTHER"]'
        update(capture,receipt)
        with self.assertRaises(AnalysisError): adapt({'batch-ref':capture},[selection],scope,100)
