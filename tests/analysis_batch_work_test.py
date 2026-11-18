"""即用资料、批量读取及一次相关计算的通用回归；不依赖真实业务编码。"""
from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

from jsonschema import Draft202012Validator

from analysis_fixtures import OWNER, fixture, saved_fixture
from analysis_budget_batch_test import batch_fixture
from analysis_closeout_test import mixed_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.analysis_materials import materials, share_item_fields
from analysis_extension.capture import ReceiptCapture
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.query_results import encode
from analysis_extension.service import compact_derived
from analysis_extension.tools import AnalysisInspect, AnalysisPython, QueryResultRead


class BatchWorkTests(PresentationCase):
    async def test_material_grid_selectors_build_without_inventing_blank_cells_or_members(self):
        reference, _, _ = mixed_fixture(self.store, 2)
        capture = self.store.get_capture(OWNER, reference)
        data = materials(capture, self.service.sources)
        selections = [{**item, 'role': 'PRIMARY', 'grain': ['MD_ORG']} for item in data['selections']]
        self.assertEqual([item['cellKeys'] for item in selections], [['2:1', '2:2', '2:3']] * 2)
        built = await self.service.python(
            captureRefs=[reference], selections=selections,
            scope={'label': '两个明确对象', 'selectionReason': '按实际网格选择器选择'},
            code='result=[{"name":"coordinates","label":"实际来源坐标","kind":"DIAGNOSTIC",'
                 '"inputRefs":[item["ref"] for item in inputs],'
                 '"data":[[item["stepId"],item["observationId"],item["cellKey"]] for item in inputs]}]',
            purpose='验证合并输入保留来源身份', requestKey='grid-coordinates')
        self.assertEqual(built['factCount'], 6)
        derived = self.store.get_analysis(OWNER, built['analysisRef'])['derivedValues'][0]
        self.assertEqual(derived['data'], [[f'form-{index}', f'observation-{index}', f'2:{column}']
                                           for index in range(2) for column in range(1, 4)])

    async def test_python_batch_inputs_keep_column_row_and_step_identity(self):
        capture, _, _, _ = batch_fixture()
        reference = self.store.capture(OWNER, capture)
        data = materials(self.store.get_capture(OWNER, reference), self.service.sources)
        built = await self.service.python(
            captureRefs=[reference], selections=data['selections'],
            scope={'label': '全部已取得行', 'selectionReason': '复用当前查询的完整结果'},
            code='result=[{"name":"coordinates","label":"批量来源坐标","kind":"DIAGNOSTIC",'
                 '"inputRefs":[item["ref"] for item in inputs],'
                 '"data":[[item["stepId"],item["batchId"],item["rowIndex"],item["columnName"]] for item in inputs]}]',
            purpose='区分相同物理指标的不同查询项', requestKey='table-coordinates')
        derived = self.store.get_analysis(OWNER, built['analysisRef'])['derivedValues'][0]
        expected = [[item['stepId'], item['batchId'], item['rowIndex'], item['columnName']] for item in data['items']]
        self.assertEqual(derived['data'], expected)

    async def test_missing_grid_selection_reports_exact_gap_without_dropping_it(self):
        reference, selections, scope = saved_fixture(self.store, ('1',))
        selections[0]['cellKeys'].append('2:99')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.build([reference], selections, scope, [], 'missing-cell')
        self.assertEqual(caught.exception.code, 'CELL_NOT_CAPTURED')
        self.assertEqual(caught.exception.details['missingCellKeys'], ['2:99'])
        self.assertEqual(caught.exception.details['capturedSelectionKeys'], ['2:1'])
        self.assertEqual(caught.exception.details['selectionRead']['captureRef'], reference)

    async def test_material_projection_keeps_fifty_one_rows_in_one_bounded_receipt(self):
        capture, _, _, receipt = batch_fixture(aggregate=False)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['columns'] = table['data']['columns'][:3]
        table['metrics'] = table['metrics'][:1]
        table['request']['metrics'] = table['request']['metrics'][:1]
        table['columnRoles'].pop('metric2')
        capture['request']['plan']['steps'][1]['batchQuery']['metrics'] = capture['request']['plan']['steps'][1]['batchQuery']['metrics'][:1]
        table['data']['rows'] = [[str(index), '成员' + str(index), None if index == 0 else str(index)] for index in range(51)]
        table['data']['rowCount'] = 51
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        reference = self.store.capture(OWNER, capture)
        saved = self.store.get_capture(OWNER, reference)
        result = materials(saved, self.service.sources)
        self.assertTrue(result['catalogComplete'])
        self.assertEqual(result['returnedCount'], 51)
        self.assertEqual([item['value'] for item in result['items']], [None, *map(str, range(1, 51))])
        self.assertEqual(result['fixedItemFields']['dimensions']['MD_BUDACCOUNT'], '0205')
        self.assertEqual({item['dimensions']['MD_ORG'] for item in result['items']}, set(map(str, range(51))))
        self.assertLessEqual(len(encode(result)), 14000)
        helper = ReceiptCapture(self.store, OWNER, frozenset(), AsyncMock(), sources=self.service.sources)
        projection = helper._model_projection(self.service.sources.for_capture(saved), receipt, saved, len(encode(receipt)))
        self.assertTrue(projection['analysisMaterials']['catalogComplete'])
        self.assertLessEqual(len(encode(projection)), 24000)

    def test_repeated_material_fields_round_trip_without_losing_null_or_identity(self):
        rows = [{'factId': 'fact-' + str(index), 'value': None if index == 0 else str(index),
                 'unit': '元', 'headers': ['相同的业务科目'],
                 'dimensions': {'PERIOD': '2026', 'SCENE': 'BUDGET', 'MEMBER': str(index)},
                 'dimensionNames': {'PERIOD': '2026年', 'SCENE': '预算', 'MEMBER': '成员' + str(index)}}
                for index in range(60)]
        rows[1]['optional'] = None
        original = deepcopy(rows)
        shared, compact = share_item_fields(rows)
        restored = []
        for item in compact:
            restored.append({**shared, **item, **{field: {**shared.get(field, {}), **item.get(field, {})}
                for field in ('dimensions', 'dimensionNames')}})
        self.assertEqual(restored, original)
        self.assertEqual(rows, original)
        self.assertIsNone(compact[0]['value'])
        self.assertNotIn('optional', shared)
        self.assertLess(len(encode({'fixedItemFields': shared, 'items': compact})), len(encode(rows)))

    async def test_query_materials_preserve_same_measure_different_accounts_and_null(self):
        capture, _, _, _ = batch_fixture(null_value=True)
        reference = self.store.capture(OWNER, capture)
        original = self.store.get_capture(OWNER, reference)
        result = materials(original, self.service.sources)
        self.assertEqual(result['selections'][0]['columnNames'], ['metric1', 'metric2'])
        self.assertEqual({item['columnName'] for item in result['items']}, {'metric1', 'metric2'})
        self.assertTrue(any(item['value'] is None for item in result['items']))
        self.assertTrue(result['catalogComplete'])
        self.assertEqual(self.store.get_capture(OWNER, reference), original)
        Draft202012Validator(AnalysisInspect.input_schema).validate(result['analysisRead'])
        request = {'captureRefs': [reference], 'selections': result['selections'],
                   'scope': {'label': '所选口径', 'selectionReason': '使用回执全部分组'},
                   'code': 'result=[]', 'purpose': '验证资料选择器',
                   'fallbackReason': '需要自定义的多指标资料诊断', 'requestKey': 'material'}
        Draft202012Validator(AnalysisPython.input_schema).validate(request)
        built = await self.service.build([reference], result['selections'], request['scope'], [], 'material')
        self.assertEqual(built['factCount'], len(result['items']))

    async def test_query_projection_gives_usable_formulas_without_touching_history(self):
        reference, _, _ = mixed_fixture(self.store, 2)
        capture = self.store.get_capture(OWNER, reference)
        adapter = self.service.sources.for_capture(capture)
        receipt = adapter.raw_receipt(capture['response'])
        helper = ReceiptCapture(self.store, OWNER, frozenset(), AsyncMock(), sources=self.service.sources)
        with patch.object(self.store, 'list_captures', side_effect=AssertionError('不扫描历史')):
            view = helper._model_projection(adapter, receipt, capture, len(encode(receipt)))
        self.assertLessEqual(len(encode(view)), 24000)
        material = view['analysisMaterials']
        targets = [item for item in material['items'] if item.get('formula')]
        self.assertEqual(len(targets), 2)
        self.assertEqual({item['formula'] for item in targets}, {'B1-C1'})
        self.assertEqual(len(material['selections']), 2)
        self.assertEqual(material['sourceState']['status'], receipt['status'])

    async def test_multiple_objects_and_pages_keep_filters_and_original_precision(self):
        reference, _, _ = saved_fixture(self.store, ('123456789012345678.00001', '0', None))
        requests = [{'captureRef': reference, 'pointer': '/data/stepResults', 'fields': ['stepId'], 'limit': 1},
                    {'captureRef': reference, 'pointer': '/data/stepResults/1/formResults/0/page/cells', 'limit': 1}]
        Draft202012Validator(QueryResultRead.input_schema).validate({'requests': requests, 'allPages': True})
        original = deepcopy(requests)
        with patch.object(self.store, 'list_captures', side_effect=AssertionError('不扫描历史')):
            result = await self.service.read_query_result(requests=requests, allPages=True)
        self.assertTrue(result['readComplete'])
        self.assertEqual(len(result['results'][0]['pages']), 4)
        self.assertEqual(result['results'][1]['pages'][0]['items'][0]['rawValueJson'], '"123456789012345678.00001"')
        self.assertEqual(requests, original)
        self.assertLessEqual(len(encode(result)), 24000)

    async def test_batch_failure_keeps_other_results_and_blocks_other_session(self):
        reference, _, _ = saved_fixture(self.store, ('1',))
        capture, _, _ = fixture(('900',))
        foreign = self.store.capture(Owner('other', OWNER.agent_id, OWNER.session_id), capture)
        result = await self.service.read_query_result(requests=[
            {'captureRef': reference, 'pointer': '/data/stepResults/0/rows', 'limit': 10},
            {'captureRef': reference, 'pointer': '/invented'},
            {'captureRef': foreign, 'pointer': ''}], allPages=True)
        self.assertEqual([item['status'] for item in result['results']], ['COMPLETE', 'ERROR', 'ERROR'])
        self.assertEqual(result['results'][2]['error']['code'], 'NOT_FOUND')
        self.assertFalse(result['readComplete'])
        self.assertEqual(result['results'][2]['pages'], [])
        self.assertNotIn('rawValueJson', encode(result['results'][2]))

    async def test_batch_inspect_distinct_targets_and_sections(self):
        reference, selections, scope = mixed_fixture(self.store, 2)
        built = await self.service.build([reference], selections, scope, [], 'base')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        targets = [fact['factId'] for fact in artifact['facts'] if fact['cellKey'] == '2:1']
        requests = [{'section': 'formulas', 'analysisRef': built['analysisRef'], 'factRef': target, 'limit': 1}
                    for target in targets]
        request = {'requests': requests, 'allPages': True}
        Draft202012Validator(AnalysisInspect.input_schema).validate(request)
        result = await self.service.inspect(**request)
        self.assertTrue(result['readComplete'])
        for item in result['results']:
            nodes = [node for page in item['pages'] for node in page['nodes']['items']]
            self.assertEqual(len(nodes), 3)
            self.assertEqual(len({node['factRef'] for node in nodes}), 3)

    async def test_large_batch_retains_exact_continuation_without_returning_false_complete(self):
        capture, _, _ = fixture(('1',))
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['records'] = [{'key': str(index), 'text': 'x' * 1800} for index in range(100)]
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        reference = self.store.capture(OWNER, capture)
        result = await self.service.read_query_result(requests=[{'captureRef': reference, 'pointer': '/records', 'limit': 100}], allPages=True)
        self.assertFalse(result['readComplete'])
        item = result['results'][0]
        keys = [row['key'] for page in item['pages'] for row in page['items']]
        self.assertEqual(keys, [str(index) for index in range(len(keys))])
        self.assertEqual(item['nextRead']['offset'], len(keys))
        self.assertEqual(item['nextRead']['captureRef'], reference)
        self.assertEqual(item['nextRead']['pointer'], '/records')
        self.assertLessEqual(len(encode(result)), 24000)

    def test_schema_does_not_allow_mixed_single_and_batch_inputs(self):
        for tool, request in [(AnalysisInspect, {'requests': [{'section': 'facts', 'analysisRef': 'a'}], 'section': 'facts'}),
                              (QueryResultRead, {'requests': [{'captureRef': 'c'}], 'captureRef': 'd'})]:
            self.assertTrue(list(Draft202012Validator(tool.input_schema).iter_errors(request)))
        self.assertTrue(list(Draft202012Validator(AnalysisInspect.input_schema).iter_errors(
            {'requests': [{'section': 'inventory'}]})))

    async def test_scoped_batch_cannot_hide_global_scan_behind_unused_reference(self):
        for request in ({'section': 'captures', 'captureRef': 'unused'},
                        {'section': 'inventory', 'analysisRef': 'unused'},
                        {'section': 'relations', 'captureRef': 'unused'}, {'captureRef': 'unused'}):
            self.assertTrue(list(Draft202012Validator(AnalysisInspect.input_schema).iter_errors({'requests': [request]})))
            with self.assertRaises(AnalysisError), patch.object(self.store, 'list_captures', side_effect=AssertionError('不扫描历史')):
                await self.service.inspect(requests=[request])

    async def test_material_filters_cannot_be_silently_ignored(self):
        reference, _, _ = saved_fixture(self.store, ('1', '2'))
        with self.assertRaises(AnalysisError) as error:
            await self.service.inspect(section='materials', captureRef=reference, objectId='object-0')
        self.assertEqual(error.exception.code, 'INVALID_MATERIAL_FILTER')

    async def test_native_formula_checks_and_python_outputs_execute_in_one_call(self):
        reference, selections, scope = mixed_fixture(self.store, 2)
        operations = [{'id': 'check' + str(index), 'operator': 'FORMULA_CHECK', 'checkScope': 'LOCAL',
                       'select': {'cellKey': '2:1', 'objectId': 'object-' + str(index)}} for index in range(2)]
        work = [{'taskId': 'formulas', 'title': '所选对象公式核对', 'kind': 'COMPUTATION',
                 'operator': 'FORMULA_CHECK', 'refs': ['check0', 'check1']},
                {'taskId': 'table', 'title': '比较结果', 'kind': 'COMPUTATION', 'operator': 'PYTHON', 'refs': ['comparison']}]
        code = '''checks = [item for item in inputs if item.get("operator") == "FORMULA_CHECK"]
refs = [item["ref"] for item in checks]
result = [{"name":"comparison","label":"本层核对","kind":"TABLE","inputRefs":refs,
"columns":[{"name":"source","label":"来源","type":"DECIMAL_STRING"},{"name":"computed","label":"本层","type":"DECIMAL_STRING"}],
"rows":[[item["sourceValue"],item["localValue"]] for item in checks]},
{"name":"coverage","label":"检查数","kind":"DIAGNOSTIC","data":{"count":len(checks)},"inputRefs":refs}]
'''
        output = await self.service.python(captureRefs=[reference], selections=selections, scope=scope,
                                          operations=operations, code=code, purpose='相关检查一次执行', requestKey='bundle', workPlan=work)
        values = output['derivedValues']
        self.assertEqual(output['completion']['state'], 'READY')
        self.assertEqual(values[-2]['rows'], [['-5', '-5'], ['20', '20']])
        self.assertTrue(values[-2]['payloadComplete'])
        self.assertEqual(values[-1]['data'], {'count': 2})
        run = self.store.get_python_run(OWNER, output['pythonRunRef'])
        self.assertTrue(any(item.get('formulaTrace') for item in run['inputRecords']))

    async def test_existing_analysis_can_supply_native_operation_to_same_script(self):
        reference, selections, scope = saved_fixture(self.store, ('0.1', '0.2'))
        built = await self.service.build([reference], selections, scope, [], 'base')
        result = await self.service.python(analysisRef=built['analysisRef'], operations=[
            {'id': 'native', 'operator': 'SUM', 'ruleRef': 'fixture-additive'}], inputRefs=['native'],
            code='result=[{"name":"double","label":"两倍","value":D(inputs[0]["value"])*2,"unit":"元","inputRefs":[inputs[0]["ref"]]}]',
            purpose='复用既有数据', requestKey='operations')
        self.assertEqual(result['derivedValues'][-1]['value'], '0.6')

    def test_small_outputs_are_inline_large_outputs_keep_original_read_reference(self):
        rows = [['x' * 1000] for _ in range(100)]
        values = [{'derivedId': 'big', 'operator': 'PYTHON', 'outputKind': 'TABLE', 'rows': rows, 'rowCount': 100},
                  {'derivedId': 'small', 'operator': 'PYTHON', 'outputKind': 'TABLE', 'rows': [[None], ['0'], ['0.00001']], 'rowCount': 3}]
        original = deepcopy(values)
        compact = compact_derived(values, analysis_ref='current')
        self.assertFalse(compact[0]['payloadComplete'])
        self.assertNotIn('rows', compact[0])
        self.assertEqual(compact[0]['nextRead']['derivedId'], 'big')
        self.assertTrue(compact[1]['payloadComplete'])
        self.assertEqual(compact[1]['rows'], values[1]['rows'])
        self.assertEqual(values, original)
