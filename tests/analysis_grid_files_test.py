"""完整文件、精确计算、尾部读取及原有事实/报告链路的回归。"""
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.grid_files import receive_files, resolve_input
from analysis_extension.models import AnalysisError


class GridFileTest(PresentationCase):
    async def test_only_a_fully_scanned_single_grid_supplies_missing_output_lineage(self):
        ref, file_ref, _, _, _ = await self.make_grid(3)
        grid = {'captureRef': ref, 'fileRef': file_ref}
        code = """count = sum(1 for row in grid_rows(inputs[0]['ref']))
result = [{'name':'count','label':'已读记录','value':count,'unit':None}]"""
        built = await self.service.python(gridInputs=[grid], code=code, requestKey='verified-file-source', purpose='统计已读记录')
        run = self.store.get_python_run(OWNER, built['pythonRunRef'])
        self.assertEqual(run['singleGridSourceBinding'], {'ref': file_ref, 'outputIndices': [0]})
        self.assertEqual(run['outputs'][0]['inputRefs'], [file_ref])
        from analysis_extension.python_products import bind_single_grid
        source = resolve_input(self.service, grid)
        read = run['fileReads'][0]
        for sources, reads in (([source, source], [read]), ([source], []), ([source], [{**read, 'exhausted': False}]),
                               ([source], [{**read, 'sha256': 'other'}]), ([source], [{**read, 'matchedCount': 0}])):
            output = [{}]
            self.assertEqual(bind_single_grid(output, sources, reads), [])
            self.assertNotIn('inputRefs', output[0])
        wrong = code.replace("'unit':None", "'unit':None,'inputRefs':['unknown']")
        with self.assertRaises(AnalysisError) as rejected:
            await self.service.python(gridInputs=[grid], code=wrong, requestKey='invalid-file-source', purpose='统计已读记录')
        self.assertEqual(rejected.exception.code, 'PYTHON_RESULT_INVALID')

    async def test_resumed_file_input_recovers_plan_without_changing_raw_request(self):
        from copy import deepcopy
        reference, file_ref, _, _, _ = await self.make_grid(3)
        parent = self.store.get_capture(OWNER, reference)
        receipt = json.loads(parent['response']['content'][0]['text'])
        receipt.update(executionId='file-parent', status='PARTIAL', resumeRef='file-parent')
        parent['receiptSummary'] = {'executionId': 'file-parent'}
        parent['response']['content'][0]['text'] = json.dumps(receipt)
        parent_ref = self.store.capture(OWNER, parent)
        resumed = deepcopy(parent)
        resumed['request'] = {'contextId': receipt['context']['contextId'],
                              'querySessionId': receipt['querySessionId'], 'resumeRef': 'file-parent'}
        receipt.update(executionId='file-tail', status='SUCCEEDED', resumeRef=None, continuationOf='file-parent')
        resumed['receiptSummary'] = {'executionId': 'file-tail'}
        resumed['response']['content'][0]['text'] = json.dumps(receipt)
        tail_ref = self.store.capture(OWNER, resumed)
        resolved = resolve_input(self.service, {'captureRef': tail_ref, 'fileRef': file_ref})
        self.assertEqual(resolved['captureRef'], tail_ref)
        self.assertNotIn('plan', self.store.get_capture(OWNER, tail_ref)['request'])
        self.assertIn('plan', self.store.get_capture(OWNER, parent_ref)['request'])
        # 相同文件仍不能跨上下文借用计划。
        receipt['continuationOf'] = 'unrelated-execution'
        resumed['response']['content'][0]['text'] = json.dumps(receipt)
        bad_ref = self.store.capture(OWNER, resumed)
        with self.assertRaises(AnalysisError) as error:
            resolve_input(self.service, {'captureRef': bad_ref, 'fileRef': file_ref})
        self.assertEqual(error.exception.code, 'RESUME_UNPROVEN')

    async def test_structure_role_reads_the_same_file_backed_cells(self):
        from analysis_extension.analysis_inputs import structure_evidence
        ref, _, selectors, _, _ = await self.make_grid(100)
        capture = self.store.get_capture(OWNER, ref)
        evidence = structure_evidence({ref:capture}, selectors[0])
        self.assertEqual(evidence[0]['cells'][0]['key'], '101:1')
        self.assertEqual(evidence[0]['cells'][0]['formula'], '=A1+A2')
    async def make_grid(self, count=3, padding=0):
        capture, selections, scope = fixture(('0.1',))
        receipt = json.loads(capture['response']['content'][0]['text'])
        form = receipt['data']['stepResults'][1]['formResults'][0]
        template = form['page']['cells'][0]
        cells = [{**template, 'key': f'{row+2}:1', 'row': row+2, 'mapType': 'ZB',
                  'formula': '=A1+A2' if row == count-1 else None,
                  'headers': ['合成指标', 'x' * padding]} for row in range(count)]
        content = b''.join((json.dumps(cell, ensure_ascii=False) + '\n').encode() for cell in cells)
        sha = hashlib.sha256(content).hexdigest()
        form['file'] = {'format': 'GRID_JSONL_V1', 'downloadPath': '/api/v1/ontology/sources/source-A/snapshots/snapshot-A/forms/observations/observation-0/cells',
                        'sha256': sha, 'byteSize': len(content), 'cellCount': count}
        form['page'] = None
        form['runtime']['observation']['entryType'] = 'INPUT_VO'
        tool = SimpleNamespace(_entry=SimpleNamespace(client=SimpleNamespace(
            mcp_config=SimpleNamespace(url='http://fixture.invalid/mcp', headers={'Authorization': 'test-only'}), _runtime_headers={})))
        def respond(request):
            self.assertEqual(request.headers['Authorization'], 'test-only')
            self.assertEqual(request.url.path, form['file']['downloadPath'])
            return httpx.Response(200, content=content)
        client_type = httpx.AsyncClient
        with patch('httpx.AsyncClient', side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)):
            files = await receive_files(receipt, tool, self.store, OWNER)
        capture['gridFiles'] = files
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        ref = self.store.capture(OWNER, capture)
        selections[0].update(captureRef=ref, cellKeys=[f'{count+1}:1'])
        scope['population']['captureRef'] = ref
        return ref, files[0]['ref'], selections, scope, content

    async def test_over_four_mib_streams_to_exact_sum_without_full_payload(self):
        ref, file_ref, _, _, content = await self.make_grid(6000, 450)
        self.assertGreater(len(content), 4 * 1024 * 1024)
        code = '''total = D("0")
for cell in grid_rows(inputs[0]["ref"], {"measureCode":"AMOUNT"}):
    if cell["value"] is not None:
        total += D(cell["value"])
result = [{"name":"total","label":"合成金额合计","value":total,"unit":"元","inputRefs":[inputs[0]["ref"]]}]
'''
        result = await self.service.python(gridInputs=[{'captureRef': ref, 'fileRef': file_ref}],
                                           code=code, purpose='精确累加合成金额', requestKey='large-grid', timeoutSeconds=30)
        from decimal import Decimal
        self.assertEqual(Decimal(result['derivedValues'][0]['value']), Decimal('600'))
        read = result['fileReads'][0]
        self.assertTrue(read['exhausted'])
        self.assertEqual(read['matchedCount'], 6000)
        self.assertFalse(read['selectedKeysComplete'])
        run = self.store.get_python_run(OWNER, result['pythonRunRef'])
        self.assertNotIn('_path', run['fileInputs'][0])
        self.assertNotIn('data', run['fileInputs'][0])
        self.assertEqual(len(read['selectedKeysPreview']), 12)
        self.assertEqual(len(run['fileReads'][0]['selectedKeys']), 5000)

    async def test_file_tail_can_build_original_facts_with_formula_and_publish(self):
        ref, file_ref, selections, scope, _ = await self.make_grid(100)
        built = await self.service.build([ref], selections, scope, [], 'tail')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(artifact['facts'][0]['value'], '0.1')
        self.assertEqual(artifact['facts'][0]['cellKey'], '101:1')
        self.assertEqual(artifact['facts'][0]['formula'], '=A1+A2')
        self.assertEqual(artifact['facts'][0]['dimensions']['MD_ORG'], 'object-0')
        from jsonschema import validate
        from analysis_extension.tools import AnalysisPython
        validate({'analysisRef':built['analysisRef'], 'inputRefs':[],
                  'gridInputs':[{'captureRef':ref, 'fileRef':file_ref}],
                  'code':'result=[]', 'purpose':'文件补充已有分析',
                  'fallbackReason':'需要扫描原生算子尚未加载的完整网格文件',
                  'requestKey':'schema-check'}, AnalysisPython.input_schema)
        computed = await self.service.python(analysisRef=built['analysisRef'], inputRefs=[],
            gridInputs=[{'captureRef': ref, 'fileRef': file_ref}], requestKey='tail-products', purpose='复用尾部实际单元格',
            code='''rows = list(grid_rows(inputs[0]['ref'], {'cellKeys':['101:1']}))
result=[{'name':'value','label':'选中金额','value':D(rows[0]['value']),'unit':'元','inputRefs':[inputs[0]['ref']]},
 {'kind':'TABLE','name':'detail','label':'选中明细','inputRefs':[inputs[0]['ref']],
  'columns':[{'name':'cell','label':'坐标','type':'STRING'},{'name':'value','label':'金额','type':'DECIMAL_STRING','unit':'元'}],
  'rows':[[row['key'],D(row['value'])] for row in rows]}]''')
        updated = self.store.get_analysis(OWNER, computed['analysisRef'])
        from analysis_extension.document_data import compile_datasets
        dataset, _ = compile_datasets(updated, [{'key':'details','valueRef':updated['derivedValues'][-1]['derivedId']}])
        self.assertEqual(dataset['details']['rows'], [['101:1', '0.1']])
        published = await self.service.publish(computed['analysisRef'], [], [
            {'viewId':'amount','scopeId':computed['scope']['scopeId'],'type':'METRIC',
             'valueRefs':[updated['derivedValues'][0]['derivedId']]}], 'grid-publish')
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        code = '''result=[{"name":"first","label":"样例","kind":"DIAGNOSTIC","data":{},"inputRefs":[inputs[0]["ref"]]}]
for cell in grid_rows(inputs[0]["ref"]):
    result[0]["data"]["key"] = cell["key"]
    break
'''
        result = await self.service.python(gridInputs=[{'captureRef': ref, 'fileRef': file_ref}],
                                           code=code, purpose='仅读取样例', requestKey='preview')
        self.assertFalse(result['fileReads'][0]['exhausted'])
        self.assertEqual(result['fileReads'][0]['matchedCount'], 1)

    async def test_tampered_file_is_not_accepted_as_budget_evidence(self):
        ref, file_ref, selections, scope, _ = await self.make_grid()
        value = resolve_input(self.service, {'captureRef': ref, 'fileRef': file_ref})
        Path(value['_path']).write_text('{}\n', encoding='utf-8')
        with self.assertRaises(AnalysisError) as failure:
            resolve_input(self.service, {'captureRef': ref, 'fileRef': file_ref})
        self.assertEqual(failure.exception.code, 'GRID_FILE_INTEGRITY')
        with self.assertRaises(AnalysisError) as selected_failure:
            await self.service.build([ref], selections, scope, [], 'tampered-build')
        self.assertEqual(selected_failure.exception.code, 'GRID_FILE_INTEGRITY')

    async def test_file_only_calculation_remains_in_final_query_evidence(self):
        from agentscope.state import AgentState
        from agentscope.message import UserMsg, ToolCallBlock, TextBlock
        from agentscope.tool import ToolResponse
        from analysis_extension.turn_completion import TurnCompletion, current_turn, query_evidence
        ref, file_ref, _, _, _ = await self.make_grid()
        turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', '读取所选数据')))
        turn.task(mode='DIRECT', goal='读取所选数据')
        self.service.turn = turn
        result = await self.service.python(gridInputs=[{'captureRef':ref,'fileRef':file_ref}],
            code="for cell in grid_rows(inputs[0]['ref']):\n    value = D(cell['value'])\n    break\nresult=[{'name':'sample','label':'选中值','value':value,'unit':'元','inputRefs':[inputs[0]['ref']]}]",
            purpose='读取来源原值', requestKey='file-proof')
        turn.observe(ToolCallBlock(id='file-query', name='AnalysisPython', input='{}'),
                     ToolResponse(content=[TextBlock(text=json.dumps(result))]))
        evidence = query_evidence(turn)
        self.assertEqual(evidence['pythonRuns'][0]['outputs'][0]['value'], '0.1')
        self.assertFalse(evidence['pythonRuns'][0]['fileReads'][0]['exhausted'])
        self.assertEqual(evidence['sources'][0]['source']['gridFiles'][0]['ref'], file_ref)
        self.assertNotIn('_path', json.dumps(evidence))

    async def test_expired_orphan_file_is_removed_but_calculation_evidence_is_pinned(self):
        ref, file_ref, _, _, _ = await self.make_grid(3)
        retained = resolve_input(self.service, {'captureRef':ref, 'fileRef':file_ref})
        orphan_ref, orphan_file, _, _, _ = await self.make_grid(4)
        orphan = resolve_input(self.service, {'captureRef':orphan_ref, 'fileRef':orphan_file})
        await self.service.python(gridInputs=[{'captureRef':ref,'fileRef':file_ref}], requestKey='pin-grid', purpose='保留来源',
            code="result=[{'name':'count','label':'扫描记录数','value':sum(1 for cell in grid_rows(inputs[0]['ref'])),'unit':None,'inputRefs':[inputs[0]['ref']]}]")
        collected = self.store.collect_expired(10**12)
        self.assertEqual(collected['gridFilesRemoved'], 1)
        self.assertFalse(Path(orphan['_path']).exists())
        self.assertTrue(Path(retained['_path']).exists())
        self.assertEqual(resolve_input(self.service, {'captureRef':ref,'fileRef':file_ref})['cellCount'], 3)
