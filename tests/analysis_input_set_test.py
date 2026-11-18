from copy import deepcopy
from unittest.mock import patch
from jsonschema import Draft202012Validator

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.tools import AnalysisTask, AnalysisPython


class InputSetTests(PresentationCase):
    async def test_build_prepares_complete_inputs_without_model_reference_list(self):
        capture, selections, scope = saved_fixture(self.store, ('1.005', '2', None))
        built = await self.service.build([capture], selections, scope, [], 'input-set')
        frozen = self.store.get_input_set(OWNER, built['inputSetRef'])
        self.assertEqual(len(frozen['records']), 3)
        self.assertEqual([item['value'] for item in frozen['records']], ['1.005', '2', None])
        result = await self.service.python(inputSetRef=built['inputSetRef'], code='''
chosen = [item for item in inputs if item.get('value') is not None]
result = [{'name':'total','label':'非空合计','value':sum(D(item['value']) for item in chosen),
           'decimalPlaces':2,'unit':'元','inputRefs':[item['ref'] for item in chosen]}]
''', purpose='合法子集运算', requestKey='execute-set')
        self.assertEqual(result['derivedValues'][-1]['value'], '3.01')
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertEqual(len(run['inputRecords']), 3)
        self.assertEqual(self.store.get_input_set(OWNER, built['inputSetRef']), frozen)
        self.assertEqual(run['inputSetRef'], built['inputSetRef'])

    async def test_file_snapshot_survives_edit_and_failed_retry(self):
        path = self.delivery.root / 'values.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"amount":"1.005"}', encoding='utf-8')
        prepared = await self.service.task(action='prepare_inputs', fileInputs=[{'path':'values.json'}])
        path.write_text('{"amount":"999"}', encoding='utf-8')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(inputSetRef=prepared['inputSetRef'], code='raise ValueError("fix")',
                                      purpose='修复前', requestKey='failed')
        path.unlink()
        output = await self.service.python(**caught.exception.details['retryInput'], purpose='修正后', requestKey='retry',
            supersedesRunRefs=[caught.exception.details['retryInput']['retryOfRunRef']],
            code="result=[{'name':'value','label':'金额','value':D(inputs[0]['data']['amount']), 'decimalPlaces':2,'unit':'元','inputRefs':[inputs[0]['ref']]}]")
        self.assertEqual(output['derivedValues'][0]['value'], '1.01')
        retried = self.store.get_python_run(OWNER, output['pythonRunRef'])
        self.assertEqual(retried['supersedesRunRefs'], [caught.exception.details['retryInput']['retryOfRunRef']])

    async def test_changed_intent_requires_new_set_and_other_owner_cannot_read(self):
        from types import SimpleNamespace
        self.service.turn = SimpleNamespace(state={'turnKey':'turn', 'request':'原问题', 'queryIntent': {'period':'2026'}})
        path = self.delivery.root / 'values.json'; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[1]', encoding='utf-8')
        prepared = await self.service.task(action='prepare_inputs', fileInputs=[{'path':'values.json'}])
        old = deepcopy(self.store.get_input_set(OWNER, prepared['inputSetRef']))
        from analysis_extension.prepared_inputs import task_basis
        self.service.turn.state['queryIntent'].update(grouping='渠道', order='降序')
        self.assertEqual(old['taskBasis'], task_basis(self.service))
        self.service.turn.state['queryIntent']['period'] = '2027'
        with self.assertRaises(AnalysisError) as changed:
            await self.service.python(inputSetRef=prepared['inputSetRef'], code='result=[]', purpose='变化', requestKey='changed')
        self.assertEqual(changed.exception.code, 'STALE_INPUT_SET')
        renewed = await self.service.task(action='prepare_inputs', fileInputs=[{'path':'values.json'}])
        self.assertNotEqual(renewed['inputSetRef'], prepared['inputSetRef'])
        self.assertEqual(self.store.get_input_set(OWNER, prepared['inputSetRef']), old)
        with self.assertRaises(AnalysisError):
            self.store.get_input_set(Owner('other', OWNER.agent_id, OWNER.session_id), prepared['inputSetRef'])

    async def test_native_prerequisites_and_multiple_outputs_remain_available(self):
        capture, selections, scope = saved_fixture(self.store, ('1', '2'))
        built = await self.service.build([capture], selections, scope, [], 'base')
        result = await self.service.python(inputSetRef=built['inputSetRef'], purpose='排序后生成两个产物', requestKey='multi',
            operations=[{'id':'order','operator':'SORT'}], code='''
order = next(item for item in inputs if item.get('operator') == 'SORT')
result=[{'name':'first','label':'首项','value':order['rows'][0]['values']['value'],'unit':'元','inputRefs':[order['ref']]},
        {'name':'count','label':'数量','value':str(len(order['rows'])),'unit':None,'inputRefs':[order['ref']]}]
''')
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertIn('SORT', [item.get('operator') for item in run['inputRecords']])
        self.assertEqual(len(run['outputs']), 2)

    async def test_successful_code_can_be_corrected_on_same_frozen_input(self):
        capture, selections, scope = saved_fixture(self.store, ('1', '2'))
        built = await self.service.build([capture], selections, scope, [], 'correction-base')
        code = "result=[{'name':'value','label':'值','value':D(inputs[0]['value'])*{factor},'unit':'元','inputRefs':[inputs[0]['ref']]}]"
        first = await self.service.python(inputSetRef=built['inputSetRef'], code=code.replace('{factor}', '2'),
                                          purpose='首次', requestKey='first')
        second = await self.service.python(inputSetRef=built['inputSetRef'], code=code.replace('{factor}', '3'),
                                           purpose='有依据修正', requestKey='second', supersedesRunRefs=[first['pythonRunRef']])
        a = self.store.get_python_run(OWNER, first['pythonRunRef'])
        b = self.store.get_python_run(OWNER, second['pythonRunRef'])
        self.assertNotEqual(a['runRef'], b['runRef'])
        self.assertEqual(a['inputDigest'], b['inputDigest'])
        self.assertEqual(a['outputs'][0]['value'], '2')
        self.assertEqual(b['outputs'][0]['value'], '3')
        self.assertEqual(b['supersedesRunRefs'], [a['runRef']])
        corrected = self.store.get_analysis(OWNER, second['analysisRef'])
        self.assertEqual({run['runRef'] for run in corrected['pythonRuns']}, {a['runRef'], b['runRef']})

    async def test_file_reuse_and_inspect_use_the_same_frozen_contract(self):
        path = self.delivery.root / 'values.json'; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[1,2]', encoding='utf-8')
        prepared = await self.service.task(action='prepare_inputs', fileInputs=[{'path':'values.json'}])
        read = await self.service.inspect(section='inputFields', inputSetRef=prepared['inputSetRef'])
        self.assertEqual(read['recordCount'], 1)
        request = dict(inputSetRef=prepared['inputSetRef'], code="result=[{'name':'n','label':'个数','value':len(inputs[0]['data']),'unit':None,'inputRefs':[inputs[0]['ref']]}]",
                       purpose='个数', requestKey='file-reuse')
        first = await self.service.python(**request)
        path.unlink()
        second = await self.service.python(**request)
        self.assertEqual(first['pythonRunRef'], second['pythonRunRef'])
        self.assertEqual(second['continuation'], {'input': {'inputSetRef': prepared['inputSetRef']}})

    async def test_more_than_old_reference_limit_is_frozen_and_retry_keeps_all_inputs(self):
        import json
        from analysis_budget_batch_test import batch_fixture
        capture, _, _, receipt = batch_fixture(aggregate=False)
        data = receipt['data']['stepResults'][0]['batchResults'][0]
        data['data']['columns'] = data['data']['columns'][:3]
        data['metrics'] = data['metrics'][:1]
        data['request']['metrics'] = data['request']['metrics'][:1]
        data['columnRoles'].pop('metric2')
        capture['request']['plan']['steps'][1]['batchQuery']['metrics'] = data['request']['metrics']
        data['data']['rows'] = [[str(i), '成员' + str(i), str(i)] for i in range(5001)]
        data['data']['rowCount'] = 5001
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        ref = self.store.capture(OWNER, capture)
        built = await self.service.build([ref], [{'captureRef':ref, 'stepId':'batch','batchId':'batch-A','columnNames':['metric1']}],
            {'label':'完整批量来源','selectionReason':'已确认的全部成员'}, [], 'large-inputs')
        with self.assertRaises(AnalysisError) as error:
            await self.service.python(inputSetRef=built['inputSetRef'], code='raise ValueError("retry")', purpose='完整输入', requestKey='large-failed')
        result = await self.service.python(**error.exception.details['retryInput'],
            code="result=[{'name':'n','label':'个数','value':len(inputs),'unit':None,'inputRefs':[i['ref'] for i in inputs]}]",
            purpose='同一完整输入', requestKey='large-retry')
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertEqual(len(run['inputRecords']), 5001)
        self.assertEqual(run['outputs'][0]['value'], '5001')

    async def test_preparation_never_selects_latest_source_implicitly(self):
        capture, selections, scope = saved_fixture(self.store, ('1',))
        await self.service.build([capture], selections, scope, [], 'existing')
        with self.assertRaises(AnalysisError) as error:
            await self.service.task(action='prepare_inputs')
        self.assertEqual(error.exception.code, 'INPUT_SET_SELECTION')

    def test_active_tool_schemas_are_coordinated(self):
        prepare = {'action':'prepare_inputs','fileInputs':[{'path':'values.json'}]}
        self.assertFalse(list(Draft202012Validator(AnalysisTask.input_schema).iter_errors(prepare)))
        self.assertTrue(list(Draft202012Validator(AnalysisTask.input_schema).iter_errors({**prepare,'intent':{'period':'2026'}})))
        execute = {'inputSetRef':'inputs_actual','code':'result=[]','purpose':'x','fallbackReason':'x','requestKey':'x'}
        self.assertFalse(list(Draft202012Validator(AnalysisPython.input_schema).iter_errors(execute)))
        self.assertTrue(list(Draft202012Validator(AnalysisPython.input_schema).iter_errors({**execute,'analysisRef':'old'})))
