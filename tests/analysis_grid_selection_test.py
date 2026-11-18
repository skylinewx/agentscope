"""裁剪材料的范围、下载边界与既有事实组装回归。"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
import httpx

from analysis_fixtures import fixture, OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.grid_selection import validate_selection, download_request, resolved_form_query
from analysis_extension.grid_files import receive_files, resolve_input
from analysis_extension.models import AnalysisError


def cropped_fixture():
    capture, selectors, scope = fixture(('0', '-12'))
    receipt = json.loads(capture['response']['content'][0]['text'])
    for index, step in enumerate(receipt['data']['stepResults'][1:]):
        form = step['formResults'][0]
        query = capture['request']['plan']['steps'][index+1]['formQuery']
        query.update(targetDimensions={'ACCOUNT':'profit'}, measureCode='AMOUNT')
        filter_ = {key: query.get(key) for key in ('measureCode','expression','headerText','cellKey')}
        filter_.update(targetDimensions=query['targetDimensions'], includeFormulaDependencies=False)
        form['selection'] = dict(filter=filter_, sourceCellCount=18389, targetCellCount=1,
                                 returnedCellCount=1, complete=True, dimensions=[], issues=[], contentDigest='a'*64)
        form['page'].update(startCell=0, endCellExclusive=1, totalCells=1)
    capture['response']['content'][0]['text'] = json.dumps(receipt)
    return capture, selectors, scope, receipt


class GridSelectionTest(PresentationCase):
    async def test_reference_resolved_crop_builds_from_server_normalized_conditions(self):
        capture, selectors, scope, receipt = cropped_fixture()
        normalized = deepcopy(capture['request']['plan'])
        for index, step in enumerate(capture['request']['plan']['steps'][1:]):
            step['formQuery'].pop('targetDimensions')
            step['formQuery'].pop('measureCode')
            refs = ['ft1.execution.' + 'a'*64 + '.0']
            step['formQuery']['targetRefs'] = refs
            normalized['steps'][index+1]['formQuery']['targetRefs'] = refs
        receipt['data']['normalizedPlan'] = normalized
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        for selector in selectors:
            selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        built = await self.service.build([ref], selectors, scope, [], 'target-ref-crop')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual([fact['value'] for fact in artifact['facts']], ['0', '-12'])

    async def test_resolved_target_cannot_override_user_scope_or_lose_reference(self):
        query = dict(targetRefs=['target'], targetDimensions={'PRODUCT': 'one'}, dimensions={'MD_ORG': 'org'}, periodCode='2026N0001')
        resolved = dict(query, targetDimensions={'PRODUCT': 'one', 'CHANNEL':'self'}, measureCode='AMOUNT')
        receipt = {'data': {'normalizedPlan': {'steps': [{'id':'form', 'formQuery':resolved}]}}}
        self.assertEqual(resolved_form_query(query, receipt, 'form'), resolved)
        for mutate in (lambda item: item.update(targetRefs=['other']),
                       lambda item: item['targetDimensions'].update(PRODUCT='two'),
                       lambda item: item.update(periodCode='2025N0001'),
                       lambda item: item.update(dimensions={'MD_ORG':'other'}),
                       lambda item: item.update(observationId='unasked')):
            wrong = deepcopy(receipt)
            mutate(wrong['data']['normalizedPlan']['steps'][0]['formQuery'])
            with self.assertRaises(AnalysisError):
                resolved_form_query(query, wrong, 'form')

    async def test_scoped_pages_keep_source_identity_and_exact_values(self):
        capture, selectors, scope, _ = cropped_fixture()
        ref = self.store.capture(OWNER, capture)
        for selector in selectors:
            selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        built = await self.service.build([ref], selectors, scope, [], 'crop-inline')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual([fact['value'] for fact in artifact['facts']], ['0','-12'])
        self.assertTrue(built['scope']['sourcePagesComplete'])
        self.assertEqual(artifact['facts'][0]['cellKey'], '2:1')

    async def test_crop_filter_and_manifest_cannot_silently_change(self):
        capture, _, _, receipt = cropped_fixture()
        form = receipt['data']['stepResults'][1]['formResults'][0]
        query = capture['request']['plan']['steps'][1]['formQuery']
        validate_selection(form, query)
        for mutate in (lambda item: item['selection']['filter']['targetDimensions'].update(ACCOUNT='other'),
                       lambda item: item['selection'].update(returnedCellCount=2),
                       lambda item: item['selection'].update(targetCellCount=2),
                       lambda item: item['selection'].update(contentDigest='invalid')):
            wrong = deepcopy(form)
            mutate(wrong)
            with self.assertRaises(AnalysisError):
                validate_selection(wrong, query)

    async def test_download_fetches_crop_only_and_matches_same_observation_digest(self):
        capture, selectors, scope, receipt = cropped_fixture()
        receipt['data']['stepResults'] = receipt['data']['stepResults'][:2]
        form = receipt['data']['stepResults'][1]['formResults'][0]
        content = b''.join((json.dumps(cell, ensure_ascii=False)+'\n').encode() for cell in form['page']['cells'])
        sha = hashlib.sha256(content).hexdigest()
        form['selection']['contentDigest'] = sha
        path = '/api/v1/ontology/sources/source-A/snapshots/snapshot-A/forms/observations/observation-0/cells'
        form['file'] = dict(format='GRID_JSONL_V1', downloadPath=path+'/selection',method='POST',sha256=sha,byteSize=len(content),cellCount=1)
        form['page'] = None
        self.assertEqual(download_request(form,path),('POST',path+'/selection',{'json':form['selection']['filter']}))
        bad = deepcopy(form)
        bad['file']['downloadPath'] += '?other=x'
        with self.assertRaises(AnalysisError): download_request(bad,path)
        bad['file']['downloadPath'] = 'http://other.invalid'+path+'/selection'
        with self.assertRaises(AnalysisError): download_request(bad,path)
        tool = SimpleNamespace(_entry=SimpleNamespace(client=SimpleNamespace(
            mcp_config=SimpleNamespace(url='http://fixture.invalid/mcp',headers={}),_runtime_headers={})))
        requests=[]
        def respond(request):
            requests.append(request)
            self.assertEqual(request.method,'POST')
            self.assertEqual(json.loads(request.content),form['selection']['filter'])
            self.assertEqual(str(request.url), 'http://fixture.invalid'+form['file']['downloadPath'])
            return httpx.Response(200,content=content)
        client = httpx.AsyncClient
        with patch('httpx.AsyncClient',side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond),**kwargs)):
            files = await receive_files(receipt,tool,self.store,OWNER)
        self.assertEqual(len(requests),1)
        self.assertEqual(files[0]['cellCount'],1)
        self.assertEqual(files[0]['selection']['sourceCellCount'],18389)
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        capture['gridFiles'] = files
        ref = self.store.capture(OWNER,capture)
        resolved = resolve_input(self.service,dict(captureRef=ref,fileRef=files[0]['ref']))
        self.assertEqual(resolved['sha256'],sha)
        self.assertIn('当前裁剪范围',resolved['structure']['meaning'])

    async def test_incomplete_crop_is_not_complete_source_pages(self):
        capture, selectors, scope, receipt = cropped_fixture()
        receipt['data']['stepResults'][1]['formResults'][0]['selection']['complete'] = False
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        ref = self.store.capture(OWNER,capture)
        for selector in selectors: selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        built = await self.service.build([ref],selectors,scope,[],'partial-crop')
        self.assertFalse(built['scope']['sourcePagesComplete'])
