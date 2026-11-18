"""Real restricted worker, immutable lineage and failure/cancellation regression."""
import asyncio
from unittest.mock import patch

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.python_analysis import execute_python
from analysis_extension.python_worker import validate_code


class PythonAnalysisTests(PresentationCase):
    async def test_formatted_strings_keep_existing_expression_security(self):
        ref, selectors, scope = saved_fixture(self.store, ('1.20',))
        output = await self.service.python(captureRefs=[ref], selections=selectors, scope=scope,
            code='result=[{"name":"label","label":"金额说明","kind":"DIAGNOSTIC",'
                 '"data":{"text":f"金额 {D(inputs[0][\'value\']):.2f}"},"inputRefs":[inputs[0]["ref"]]}]',
            purpose='使用标准格式化语法整理说明', requestKey='formatted-string')
        self.assertEqual(output['derivedValues'][0]['data']['text'], '金额 1.20')
        for code in ('result = f"{inputs.__class__}"', 'result = f"{open(\'hidden\')}"'):
            with self.assertRaises((ValueError, AnalysisError)):
                validate_code(code)

    async def test_generic_products_chain_and_survive_rebuild_with_more_sources(self):
        ref, selectors, scope = saved_fixture(self.store, ('0.1', '0.2'))
        code = '''
def row(item):
    return [item['objectLabel'], D(item['value']) * 2]
refs = [item['ref'] for item in inputs]
result = [
 {'kind':'TABLE','name':'doubled','label':'条件测算','inputRefs':refs,
  'columns':[{'name':'unit','label':'对象','type':'STRING'},{'name':'amount','label':'金额','type':'DECIMAL_STRING','unit':'元'}],
  'rows':[row(item) for item in inputs]},
 {'kind':'DIAGNOSTIC','name':'coverage','label':'缺值检查','inputRefs':refs,
  'data':{'missing':sum(1 for item in inputs if item['value'] is None)}},
 {'kind':'FILE','name':'data','label':'测算明细','inputRefs':refs,'filename':'result.csv',
  'text':csv_write([row(item) for item in inputs])}]
'''
        output = await self.service.python(captureRefs=[ref], selections=selectors, scope=scope,
                                          code=code, purpose='通用数据处理', requestKey='table')
        stored = self.store.get_analysis(OWNER, output['analysisRef'])
        table = stored['derivedValues'][0]
        self.assertEqual(table['rows'][0][1], '0.2')
        self.assertTrue((self.delivery.root / stored['derivedValues'][2]['path']).is_file())
        result = await self.service.python(analysisRef=output['analysisRef'], inputRefs=[table['derivedId']],
            code='result=[{"name":"total","label":"测算合计","value":sum(D(row[1]) for row in inputs[0]["rows"]),"unit":"元","inputRefs":[inputs[0]["ref"]]}]',
            purpose='复用代码产物', requestKey='chain')
        self.assertEqual(result['derivedValues'][-1]['value'], '0.6')
        from analysis_extension.document_data import compile_datasets
        datasets, _ = compile_datasets(stored, [{'key':'sample','valueRef':table['derivedId']}])
        self.assertEqual(datasets['sample']['kind'], 'TABULAR')
        rebuilt = await self.service.build([ref], selectors, scope, [], 'rebuilt', baseAnalysisRef=result['analysisRef'])
        self.assertEqual(len(rebuilt['derivedValues']), 4)
        more_ref, more_selectors, more_scope = saved_fixture(self.store, ('0.1', '0.2', '0.3'))
        extended = await self.service.build([more_ref], more_selectors, more_scope, [], 'extended', baseAnalysisRef=result['analysisRef'])
        self.assertEqual(extended['factCount'], 3)
        self.assertEqual(len(extended['derivedValues']), 4)
        self.assertEqual(extended['derivedValues'][-1]['value'], '0.6')
        page = await self.service.inspect(section='products', analysisRef=output['analysisRef'], derivedId=table['derivedId'], limit=1)
        self.assertEqual(len(page['rows']['items']), 1)

    async def test_file_input_freezes_exact_data_and_cannot_escape_workspace(self):
        self.delivery.root.mkdir(parents=True)
        (self.delivery.root / 'input.json').write_text('[{"amount":0.1},{"amount":0.2}]', encoding='utf-8')
        output = await self.service.python(fileInputs=[{'path':'input.json'}],
            code='result=[{"name":"total","label":"文件合计","value":sum(D(row["amount"]) for row in inputs[0]["data"]),"unit":None,"inputRefs":[inputs[0]["ref"]]}, {"kind":"DIAGNOSTIC","name":"source","label":"输入概况","data":{"rows":len(inputs[0]["data"])},"inputRefs":[inputs[0]["ref"]]}]',
            purpose='授权数据文件计算', requestKey='file')
        self.assertEqual(output['derivedValues'][0]['value'], '0.3')
        run = self.store.get_python_run(OWNER, output['pythonRunRef'])
        self.assertEqual(run['fileInputs'][0]['data'][0]['amount'], '0.1')
        inspected = await self.service.inspect(**output['derivedValues'][1]['nextRead'])
        self.assertEqual(inspected['outputs'][1]['data'], {'rows': 2})
        with self.assertRaises(AnalysisError) as error:
            await self.service.python(fileInputs=[{'path':'../secret.json'}], code='result=[]', purpose='越界校验', requestKey='escape')
        self.assertEqual(error.exception.code, 'PYTHON_FILE_PATH')

    async def test_exact_secondary_values_register_and_publish_without_changing_sources(self):
        ref, selectors, scope = saved_fixture(self.store, ('0.1', '0.2'))
        built = await self.service.build([ref], selectors, scope, [], 'base')
        base = self.store.get_analysis(OWNER, built['analysisRef'])
        refs = [fact['factId'] for fact in base['facts']]
        code = '''result = [{"name": "average", "label": "均值", "value": sum(D(row["value"]) for row in inputs) / len(inputs), "unit": inputs[0]["unit"], "inputRefs": [row["ref"] for row in inputs]}]'''
        output = await self.service.python(analysisRef=built['analysisRef'], inputRefs=refs,
                                          code=code, purpose='已确认同口径数值的均值', requestKey='average')
        value = output['derivedValues'][-1]
        self.assertEqual(value['value'], '0.15')
        self.assertEqual(value['inputRefs'], refs)
        self.assertEqual(value['businessValidation'], 'NOT_REVIEWED')
        updated = self.store.get_analysis(OWNER, output['analysisRef'])
        self.assertEqual(updated['facts'], base['facts'])
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef']), base)
        record = await self.service.inspect(section='python', pythonRunRef=output['pythonRunRef'])
        self.assertEqual(record['code'], code)
        self.assertEqual(record['status'], 'COMPLETED')
        with self.assertRaises(AnalysisError):
            self.store.get_python_run(Owner('another-user', OWNER.agent_id, OWNER.session_id), output['pythonRunRef'])
        with patch('analysis_extension.python_analysis.execute_python', side_effect=AssertionError('must reuse')):
            repeated = await self.service.python(analysisRef=built['analysisRef'], inputRefs=refs,
                                                code=code, purpose='已确认同口径数值的均值', requestKey='average')
        self.assertEqual(repeated['analysisRef'], output['analysisRef'])
        self.assertEqual(repeated['pythonRunRef'], output['pythonRunRef'])
        new_run = await self.service.python(analysisRef=built['analysisRef'], inputRefs=refs,
                                            code=code, purpose='已确认同口径数值的均值', requestKey='new-average')
        self.assertNotEqual(new_run['pythonRunRef'], output['pythonRunRef'])
        self.assertEqual(self.store.get_python_run(OWNER, output['pythonRunRef']), record)
        published = await self.service.publish(output['analysisRef'], [], [
            {'viewId': 'average', 'scopeId': output['scope']['scopeId'], 'type': 'METRIC', 'valueRefs': [value['derivedId']]}],
            'published')
        self.assertEqual(published['publicationState'], 'PUBLISHED')

    async def test_nulls_are_not_implicitly_zero_and_bad_output_keeps_run_record(self):
        rows = [{'ref': 'x', 'value': None, 'unit': '元'}]
        code = 'result=[{"name":"empty","label":"空值","value":inputs[0]["value"],"unit":"元","inputRefs":["x"]}]'
        self.assertIsNone((await execute_python(code, rows, 5))[0]['value'])
        ref, selections, scope = saved_fixture(self.store, ('3',))
        base = await self.service.build([ref], selections, scope, [], 'base')
        fact = self.store.get_analysis(OWNER, base['analysisRef'])['facts'][0]['factId']
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(analysisRef=base['analysisRef'], inputRefs=[fact], purpose='错误输出',
                                      code='result=[{"value":"5"}]', requestKey='bad')
        self.assertEqual(caught.exception.code, 'PYTHON_RESULT_INVALID')
        record = self.store.get_python_run(OWNER, caught.exception.details['pythonRunRef'])
        self.assertEqual(record['status'], 'FAILED')
        self.assertEqual(self.store.list_results(OWNER, 0, 100)['total'], 1)

    async def test_timeout_and_cancel_terminate_the_child(self):
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('while True:\n    pass', [], 1)
        self.assertEqual(caught.exception.code, 'PYTHON_TIMEOUT')
        task = asyncio.create_task(execute_python('while True:\n    pass', [], 10))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_import_private_access_float_and_indirect_calls_are_rejected(self):
        for code in ('import os', 'result=open("secret")', 'result=inputs.__class__',
                     'result=[x for x in ().__class__.__bases__]', 'result=0.1',
                     'result=eval("1")', 'result=__builtins__', 'result="{0.__class__}".format(inputs)'):
            with self.subTest(code=code), self.assertRaises((ValueError, SyntaxError)):
                validate_code(code)

    async def test_large_allocation_hits_worker_resource_limit(self):
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('result=[0] * 1000000000', [], 5)
        self.assertEqual(caught.exception.code, 'PYTHON_EXECUTION_FAILED')
