"""本体目标目录到分析构建：范围、角色、公式、默认工作项均保持来源身份。"""
import json
from copy import deepcopy
from jsonschema import Draft202012Validator

from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.analysis_materials import materials
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisBuild


class PreparedInputsTests(PresentationCase):
    async def test_first_build_infers_both_prepared_objects_without_coordinates(self):
        reference, scope, _, _ = self.prepared_capture()
        request = dict(captureRefs=[reference], scope=scope, operations=[], requestKey='auto-first')
        Draft202012Validator(AnalysisBuild.input_schema).validate(request)
        result = await self.service.build(**request)
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(len(artifact['facts']), 6)
        self.assertEqual(len({tuple(value['inputRefs']) for value in artifact['derivedValues']}), 2)
        self.assertEqual((await self.service.build(**request))['analysisRef'], result['analysisRef'])

    async def test_omitted_selections_cannot_silently_drop_second_requested_branch(self):
        _, scope, capture, receipt = self.prepared_capture()
        receipt['data']['stepResults'].pop()
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        with self.assertRaises(AnalysisError) as rejected:
            await self.service.build(captureRefs=[ref], scope=scope, operations=[], requestKey='missing-branch')
        self.assertEqual(rejected.exception.code, 'PREPARATION_INCOMPLETE')

    def prepared_capture(self):
        old_ref, _, scope = mixed_fixture(self.store, 2)
        capture = self.store.get_capture(OWNER, old_ref)
        receipt = json.loads(capture['response']['content'][0]['text'])
        for index, step in enumerate(receipt['data']['stepResults'][1:]):
            form = step['formResults'][0]
            form['runtime']['observation']['entryType'] = 'INPUT_VO'
            cells = form['page']['cells'][:3]
            for position, cell in enumerate(cells):
                cell['modelCode'] = 'PHYSICAL_TABLE'
                cell['formulaStructure'] = {'address': 'ABC'[position] + '1',
                    'expression': cell['formula'], 'references': ['B1', 'C1'] if position == 0 else [],
                    'status': 'PARSED' if position == 0 else 'SOURCE_VALUE', 'issue': None}
            form.update(status='VALUE_FOUND', cells=cells, page=None,
                        formulaGraph={'targetKeys': ['2:1'], 'cellKeys': ['2:1','2:2','2:3'], 'complete': True, 'issues': []},
                        preparation={'targetCount': 1, 'targetKeys': ['2:1'], 'targetKeysComplete': True,
                                     'bindings': [{'modelCode':'PHYSICAL_TABLE','measureCode':'AMOUNT','targetCount':1}],
                                     'bindingsComplete': True, 'varyingDimensions': []})
            capture['request']['plan']['steps'][index+1]['formQuery'].update(
                includeFormulaDependencies=True, targetDimensions={'ACCOUNT': '结果'})
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        reference = self.store.capture(OWNER, capture)
        scope['population']['captureRef'] = reference
        return reference, scope, capture, receipt

    async def test_prepared_targets_build_calculations_and_work_without_retyping_ids(self):
        reference, scope, _, _ = self.prepared_capture()
        projected = materials(self.store.get_capture(OWNER, reference), self.service.sources)
        selectors = projected['selections']
        self.assertEqual(len(selectors), 2)
        self.assertTrue(all(item.get('preparedTargets') and 'cellKeys' not in item for item in selectors))
        self.assertTrue(all('targetKeys' not in item for item in projected['preparedTargets']))
        request = dict(captureRefs=[reference], selections=selectors, scope=scope, operations=[], requestKey='prepared')
        Draft202012Validator(AnalysisBuild.input_schema).validate(request)
        built = await self.service.build(**request)
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['facts']), 6)
        self.assertEqual(len(artifact['derivedValues']), 2)
        self.assertEqual({item['value'] for item in artifact['derivedValues']}, {'-5','20'})
        from analysis_extension.completion import completion_summary
        self.assertEqual(completion_summary(artifact)['state'], 'READY')
        self.assertTrue(all(task['refs'] for task in artifact['plan']['workPlan']))
        self.assertEqual(len(artifact['plan']['workPlan'][1]['targetRefs']), 2)
        replay = await self.service.build(**request)
        self.assertEqual(replay['analysisRef'], built['analysisRef'])

    async def test_structure_only_does_not_become_amount_or_formula_obligation(self):
        reference, scope, _, _ = self.prepared_capture()
        selected = materials(self.store.get_capture(OWNER, reference), self.service.sources)['selections']
        selected[1]['role'] = 'STRUCTURE'
        scope.pop('population')
        scope.pop('members')
        built = await self.service.build([reference], selected, scope, [], 'structure')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['derivedValues']), 1)
        self.assertEqual(len(artifact['facts']), 3)
        self.assertEqual(len(artifact['structureEvidence']), 1)

    async def test_file_backed_formula_preparation_keeps_values_out_of_model_catalog(self):
        import hashlib
        import httpx
        from types import SimpleNamespace
        from unittest.mock import patch
        from analysis_extension.grid_files import receive_files
        _, scope, capture, receipt = self.prepared_capture()
        contents = {}
        for step in receipt['data']['stepResults'][1:]:
            form = step['formResults'][0]
            content = b''.join((json.dumps(cell)+'\n').encode() for cell in form['cells'])
            oid = form['runtime']['observation']['observationId']
            path = '/api/v1/ontology/sources/source-A/snapshots/snapshot-A/forms/observations/'+oid+'/cells'
            form.update(status='GRID_LOADED', cells=[],
                        file={'format':'GRID_JSONL_V1','downloadPath':path,'sha256':hashlib.sha256(content).hexdigest(),
                              'byteSize':len(content),'cellCount':3})
            contents[path] = content
        tool = SimpleNamespace(_entry=SimpleNamespace(client=SimpleNamespace(
            mcp_config=SimpleNamespace(url='http://fixture.invalid/mcp',headers={}),_runtime_headers={})))
        client = httpx.AsyncClient
        with patch('httpx.AsyncClient', side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200,content=contents[request.url.path])), **kwargs)):
            capture['gridFiles'] = await receive_files(receipt,tool,self.store,OWNER)
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        reference = self.store.capture(OWNER,capture)
        scope['population']['captureRef'] = reference
        projected = materials(self.store.get_capture(OWNER,reference),self.service.sources)
        self.assertEqual(projected['items'], [])
        built = await self.service.build([reference],projected['selections'],scope,[],'file-prepared')
        artifact = self.store.get_analysis(OWNER,built['analysisRef'])
        self.assertEqual(len(artifact['facts']),6)
        self.assertEqual({value['value'] for value in artifact['derivedValues']},{'-5','20'})

    async def test_missing_or_truncated_preparation_never_selects_first_candidate(self):
        reference, scope, capture, receipt = self.prepared_capture()
        selectors = materials(self.store.get_capture(OWNER, reference), self.service.sources)['selections']
        for fields in ({'targetKeysComplete':False}, {'targetKeys':['absent']}, {'targetCount':2}):
            changed = deepcopy(receipt)
            changed['data']['stepResults'][1]['formResults'][0]['preparation'].update(fields)
            capture['response']['content'][0]['text'] = json.dumps(changed)
            new_ref = self.store.capture(OWNER, capture)
            current = deepcopy(selectors)
            for item in current:
                item['captureRef'] = new_ref
            with self.assertRaises(AnalysisError) as caught:
                await self.service.build([new_ref], current, scope, [], 'invalid-'+str(fields))
            self.assertIn(caught.exception.code, {'PREPARED_TARGETS_INVALID','PREPARED_TARGETS_INCOMPLETE'})

    def test_prepared_selector_cannot_mix_manual_coordinates(self):
        reference, scope, _, _ = self.prepared_capture()
        selected = materials(self.store.get_capture(OWNER, reference), self.service.sources)['selections']
        selected[0]['cellKeys'] = ['2:1']
        request = dict(captureRefs=[reference], selections=selected, scope=scope, operations=[], requestKey='mixed')
        self.assertTrue(list(Draft202012Validator(AnalysisBuild.input_schema).iter_errors(request)))

    async def test_source_without_formula_is_preserved_as_not_configured(self):
        _, scope, capture, receipt = self.prepared_capture()
        for step in receipt['data']['stepResults'][1:]:
            root = step['formResults'][0]['cells'][0]
            root.update(formula=None, formulaStructure={'address':'A1','expression':None,
                        'references':[],'status':'SOURCE_VALUE','issue':None})
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER,capture)
        scope['population']['captureRef'] = ref
        selected = materials(self.store.get_capture(OWNER,ref),self.service.sources)['selections']
        built = await self.service.build([ref],selected,scope,[],'no-formula')
        artifact = self.store.get_analysis(OWNER,built['analysisRef'])
        from analysis_extension.completion import completion_summary
        self.assertEqual(completion_summary(artifact)['state'],'READY')
        self.assertTrue(all(value['applicability']=='FORMULA_NOT_CONFIGURED' for value in artifact['derivedValues']))

    async def test_physical_model_mismatch_cannot_be_consumed_as_same_metric(self):
        _, scope, capture, receipt = self.prepared_capture()
        capture['request']['plan']['steps'][1]['formQuery']['modelCode'] = 'OTHER_TABLE'
        ref = self.store.capture(OWNER,capture)
        scope['population']['captureRef'] = ref
        selected = materials(self.store.get_capture(OWNER,ref),self.service.sources)['selections']
        with self.assertRaises(AnalysisError) as caught:
            await self.service.build([ref],selected,scope,[],'wrong-model')
        self.assertEqual(caught.exception.code,'REQUEST_TARGET_MISMATCH')

    async def test_resume_projection_inspect_and_build_preserve_prepared_targets(self):
        _, scope, capture, receipt = self.prepared_capture()
        receipt.update(status='PARTIAL', resumeRef=receipt['executionId'])
        capture['receiptSummary'] = {'executionId': receipt['executionId']}
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        parent_ref = self.store.capture(OWNER, capture)
        resumed = deepcopy(capture)
        resumed['request'] = {'contextId': receipt['context']['contextId'],
                              'querySessionId': receipt['querySessionId'], 'resumeRef': receipt['executionId']}
        receipt.update(executionId='resumed-prepared', continuationOf=receipt['executionId'],
                       status='SUCCEEDED', resumeRef=None)
        resumed['receiptSummary'] = {'executionId': receipt['executionId']}
        resumed['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, resumed)
        scope['population']['captureRef'] = ref
        from analysis_extension.analysis_materials import response_materials
        projection = response_materials(self.store.get_capture(OWNER, ref), self.service.sources,
                                        store=self.store, owner=OWNER)
        self.assertEqual(len(projection['preparedTargets']), 2)
        inspected = await self.service.inspect(section='materials', captureRef=ref)
        self.assertEqual(inspected['selections'], projection['selections'])
        inspected_selectors = await self.service.inspect(section='selectors', captureRef=ref)
        self.assertEqual([item for item in inspected_selectors['items'] if item.get('preparedTargets')],
                         projection['selections'])
        built = await self.service.build([parent_ref, ref], projection['selections'], scope, [], 'resume-prepared')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['derivedValues']), 2)
        self.assertNotIn('plan', self.store.get_capture(OWNER, ref)['request'])
        wrong = deepcopy(resumed)
        wrong['request']['resumeRef'] = 'unknown-parent'
        wrong_ref = self.store.capture(OWNER, wrong)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='materials', captureRef=wrong_ref)
        self.assertEqual(caught.exception.code, 'RESUME_UNPROVEN')

    def candidate_capture(self):
        _, scope, capture, receipt = self.prepared_capture()
        for step in receipt['data']['stepResults'][1:]:
            form = step['formResults'][0]
            root = form['cells'][0]
            root['dimensions'].update(PERIOD='year-current', SCENE='budget')
            other = deepcopy(root)
            other.update(key='2:4', column=4, formulaStructure={**root['formulaStructure'], 'address':'D1'})
            other['dimensions'].update(PERIOD='year-previous', SCENE='actual')
            form['cells'].append(other)
            form['formulaGraph']['targetKeys'].append('2:4')
            form['formulaGraph']['cellKeys'].append('2:4')
            form['preparation'].update(targetKeys=['2:1','2:4'], targetCount=2,
                                       varyingDimensions=['PERIOD','SCENE'])
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        scope['population']['captureRef'] = ref
        return ref, scope

    async def test_confirmed_dimensions_only_filter_targets_not_dependencies(self):
        ref, scope = self.candidate_capture()
        projection = materials(self.store.get_capture(OWNER, ref), self.service.sources)
        self.assertEqual(len(projection['preparedTargets'][0]['candidates']), 2)
        selections = projection['selections']
        for item in selections:
            item['targetDimensions'] = {'PERIOD':'year-current','SCENE':'budget'}
        request = dict(captureRefs=[ref], selections=selections, scope=scope, operations=[], requestKey='candidates')
        Draft202012Validator(AnalysisBuild.input_schema).validate(request)
        built = await self.service.build(**request)
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['facts']), 6)
        self.assertEqual(len(artifact['derivedValues']), 2)
        self.assertEqual({value['value'] for value in artifact['derivedValues']}, {'-5','20'})
        self.assertNotIn('2:4', {fact['cellKey'] for fact in artifact['facts']})

    async def test_unknown_target_dimensions_do_not_widen_or_choose_first(self):
        ref, scope = self.candidate_capture()
        projection = materials(self.store.get_capture(OWNER, ref), self.service.sources)
        from analysis_extension.prepared_inputs import resolve
        capture = self.store.get_capture(OWNER, ref)
        self.assertEqual(resolve(capture, projection['selections'][0])['cellKeys'], ['2:1','2:4'])
        for condition in ({'SCENE':'missing'}, {'NONEXISTENT':'budget'}):
            with self.assertRaises(AnalysisError) as caught:
                resolve(capture, {**projection['selections'][0], 'targetDimensions': condition})
            self.assertEqual(caught.exception.code, 'PREPARED_TARGET_NOT_FOUND')
