"""代码分析直接登记工作，保留有效输入，真实粒度变化仍使旧计算失效。"""
from copy import deepcopy
from jsonschema import Draft202012Validator

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_budget_batch_test import batch_fixture, update
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisPython
from analysis_extension.python_program import validate_code


CODE = '''result=[{"name":"total","label":"所选数据合计","kind":"SCALAR",
"value":sum(D(item["value"]) for item in inputs if item["value"] is not None),
"unit":"元","inputRefs":[item["ref"] for item in inputs]}]'''
WORK = [{'taskId': 'total', 'title': '所选数据合计', 'kind': 'COMPUTATION',
         'operator': 'PYTHON', 'refs': ['total']}]


class PythonContinuationTests(PresentationCase):
    async def test_retry_freezes_workspace_file_even_after_it_is_deleted(self):
        path = self.service.delivery.root / 'frozen.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"amount":"1.005"}', encoding='utf-8')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(fileInputs=[{'path':'frozen.json'}], code='raise ValueError("fix")',
                purpose='冻结文件', requestKey='freeze-failed')
        retry = caught.exception.details['retryInput']
        failed = self.store.get_python_run(OWNER, retry['retryOfRunRef'])
        self.assertTrue(failed['inputDeliveryManifest']['inlineComplete'])
        self.assertFalse(list((self.store.root / 'python-input-journals').rglob('*.jsonl')))
        path.unlink()
        result = await self.service.python(**retry, code="result=[{'name':'amount','label':'金额','value':D(inputs[0]['data']['amount']),'decimalPlaces':2,'unit':'元','inputRefs':[inputs[0]['ref']]}]",
            purpose='修正代码复用原文件', requestKey='freeze-fixed')
        self.assertEqual(result['derivedValues'][0]['value'], '1.01')
        record = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertEqual(record['parentAttemptRef'], retry['retryOfRunRef'])
        self.assertEqual(record['fileInputs'][0]['data'], {'amount':'1.005'})

    def test_expired_attempt_is_failed_and_preserves_frozen_inputs(self):
        record = {'runRef':'python_interrupted', 'status':'RUNNING', 'executionDeadline':1,
                  'inputRecords':[{'ref':'a','value':'1.005'}], 'fileInputs':[], 'code':'result=[]'}
        self.store.save_python_run(OWNER, record['runRef'], record)
        self.assertEqual(self.store.recover_expired_python(2), 1)
        recovered = self.store.get_python_run(OWNER, record['runRef'])
        self.assertEqual(recovered['status'], 'FAILED')
        self.assertEqual(recovered['error']['code'], 'PYTHON_HOST_INTERRUPTED')
        self.assertEqual(recovered['inputRecords'], record['inputRecords'])
        self.assertEqual(self.store.recover_expired_python(3), 0)

    def test_normal_names_are_language_features_not_security_boundaries(self):
        validate_code('result = [value for _, value in enumerate(inputs)]')
        for code in ('result = __builtins__', 'result = _private', 'result = inputs.__class__'):
            validate_code(code)

    async def test_code_and_work_plan_register_together_without_rebuild(self):
        capture, selections, scope = saved_fixture(self.store, ('0.1', '0.2'))
        result = await self.service.python(captureRefs=[capture], selections=selections, scope=scope,
            code=CODE, purpose='通用合计', requestKey='direct-work', workPlan=WORK)
        self.assertEqual(result['completion']['state'], 'READY')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(artifact['derivedValues'][0]['value'], '0.3')
        self.assertEqual(artifact['plan']['workPlan'][0]['refs'], [artifact['derivedValues'][0]['derivedId']])
        self.assertEqual(WORK[0]['refs'], ['total'])
        # 继续执行可增补工作，省略旧目标不会删除已经登记的工作。
        continued = await self.service.python(analysisRef=result['analysisRef'], code=CODE,
            purpose='保留原计划的再次计算', requestKey='continue-work', workPlan=[])
        self.assertEqual(continued['completion']['state'], 'READY')
        self.assertEqual(len(continued['completion']['items']), 1)

    async def test_output_error_returns_real_reusable_inputs(self):
        capture, selections, scope = saved_fixture(self.store, ('0.1',))
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(captureRefs=[capture], selections=selections, scope=scope,
                code='result=[{"name":"total","label":"合计","value":"0.1","unit":"元"}]',
                purpose='校验输出契约', requestKey='missing-dependency')
        error = caught.exception
        self.assertEqual(error.code, 'PYTHON_RESULT_INVALID')
        self.assertEqual(error.details['path'], '/result/0')
        self.assertFalse(error.details['requiresSourceQuery'])
        retry = error.details['retryInput']
        run = self.store.get_python_run(OWNER, retry['retryOfRunRef'])
        artifact = self.store.get_analysis(OWNER, run['sourceAnalysisRef'])
        self.assertEqual(run['inputRefs'], [fact['factId'] for fact in artifact['facts']])
        fixed = await self.service.python(**retry, code=CODE, purpose='修正真实依赖', requestKey='fixed-dependency')
        self.assertEqual(fixed['derivedValues'][0]['value'], '0.1')

    def test_execution_schema_uses_one_frozen_reference(self):
        request = {'code': CODE, 'purpose': '分析', 'fallbackReason': '自由处理', 'requestKey': 'schema',
                   'inputSetRef': 'inputs_actual'}
        validator = Draft202012Validator(AnalysisPython.input_schema)
        self.assertFalse(list(validator.iter_errors(request)))
        self.assertTrue(list(validator.iter_errors({**request, 'inputRefs': ['fact_a']})))
        self.assertTrue(list(validator.iter_errors({**request, 'retryOfRunRef': 'python_old'})))
        self.assertTrue(list(validator.iter_errors({key: value for key, value in request.items() if key != 'inputSetRef'})))

    async def test_registration_failure_preserves_successful_execution_evidence(self):
        capture, selections, scope = saved_fixture(self.store, ('0.1',))
        work = deepcopy(WORK)
        work[0]['refs'] = ['missing-output']
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(captureRefs=[capture], selections=selections, scope=scope,
                code=CODE, purpose='登记错误留痕', requestKey='invalid-work', workPlan=work)
        self.assertEqual(caught.exception.code, 'INVALID_WORK_REF')
        run = self.store.get_python_run(OWNER, caught.exception.details['pythonRunRef'])
        self.assertEqual(run['status'], 'COMPLETED')
        self.assertEqual(run['outputs'][0]['value'], '0.1')
        self.assertEqual(run['registrationError']['code'], 'INVALID_WORK_REF')

    async def test_changed_grain_reports_why_previous_code_output_cannot_be_reused(self):
        capture, selection, _, receipt = batch_fixture(aggregate=False)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['columns'].insert(0, {'name': 'DATATIME', 'label': '时期', 'type': 'STRING'})
        table['columnRoles']['DATATIME'] = {'kind': 'DIMENSION', 'dimCode': 'DATATIME', 'valueColumn': 'DATATIME'}
        table['data']['fixedDimValMap'].pop('DATATIME')
        table['data']['fixedDimNameMap'].pop('DATATIME')
        table['data']['rows'] = [['2026N0001'] + row for row in table['data']['rows']]
        update(capture, receipt)
        capture_ref = self.store.capture(OWNER, capture)
        selection = {**selection, 'captureRef': capture_ref, 'role': 'PRIMARY'}
        scope = {'label': '测试范围', 'selectionReason': '按真实回执分析'}
        result = await self.service.python(captureRefs=[capture_ref], selections=[selection], scope=scope,
            code=CODE, purpose='测试粒度连续性', requestKey='grain', workPlan=WORK)
        original = self.store.get_analysis(OWNER, result['analysisRef'])
        modified = {**selection, 'grain': ['MD_ORG']}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.build([capture_ref], [modified], scope, [], 'changed-grain',
                                     baseAnalysisRef=result['analysisRef'])
        self.assertEqual(caught.exception.code, 'INVALID_WORK_REF')
        self.assertEqual(caught.exception.details['reason'], 'PYTHON_INPUT_CHANGED')
        self.assertTrue(any('grain' in item.get('changedFields', []) for item in caught.exception.details['changedInputs']))
        self.assertEqual(self.store.get_analysis(OWNER, result['analysisRef']), original)
