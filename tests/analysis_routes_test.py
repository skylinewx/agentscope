import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx

from analysis_runtime_fixtures import resources_for
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.router import create_analysis_router
from agentscope.app._service._download_token import sign_download_token, verify_download_token


class RouteTests(PresentationCase):
    async def test_preview_route_is_scoped_read_only_and_validates_saved_schema(self):
        from analysis_fixtures import saved_fixture
        from analysis_extension.models import AnalysisError
        ref,selections,scope=saved_fixture(self.store,('10','-1','3'))
        built=await self.service.build([ref],selections,scope,[],'draft-preview')
        draft=self.store.get_analysis(OWNER,built['analysisRef'])
        request={'analysisRef':built['analysisRef'],'claims':[],'viewPlan':[{'viewId':'v','type':'TABLE','scopeId':draft['scope']['scopeId'],'valueRefs':[f['factId'] for f in draft['facts']]}],
                 'expectedBaseRevision':0,'requestKey':'preview-route','previewOnly':True}
        result=await self.service.publish(**request)
        params={**self.params,'preview_ref':result['repairRef']}
        for format in ('html','json'):
            response=await self.client.get('/workspace/analyses/preview',params={**params,'format':format},headers=self.headers)
            self.assertEqual(response.status_code,200,response.text)
            if format=='json':
                self.assertIn('page',response.json()['document'])
                self.assertTrue(response.json()['previewOnly'])
            else: self.assertIn('尚未发布',response.text)
        self.assertEqual(self.store.get_analysis(OWNER,built['analysisRef']),draft)
        self.assertIsNone(self.store.get_presentation(OWNER,'delivery',built['analysisRef']))
        denied=await self.client.get('/workspace/analyses/preview',params=params,headers={'X-User-ID':'other'})
        self.assertEqual(denied.status_code,404)
        invalid={**request,'viewPlan':[{'type':'TABLE'}]}
        with self.assertRaises(AnalysisError) as caught: await self.service.publish(**invalid)
        bad=await self.client.get('/workspace/analyses/preview',params={**params,'preview_ref':caught.exception.details['repairRef']},headers=self.headers)
        self.assertEqual(bad.status_code,409)
        self.assertEqual(bad.json()['detail']['code'],'INVALID_PUBLICATION')
        await self.service.publish(built['analysisRef'],requestKey='final-route',expectedBaseRevision=0,repairRef=result['repairRef'],repairs=[])
        stale=await self.client.get('/workspace/analyses/preview',params=params,headers=self.headers)
        self.assertEqual(stale.status_code,409)

    async def asyncSetUp(self):
        await self.publish()
        config = self.root / 'analysis-config.local.json'
        config.write_text(json.dumps({'schemaVersion': 1, 'rules': self.rules.rules}), 'utf-8')
        self.config = config
        self.app = FastAPI()
        async def session(user, agent, session):
            return SimpleNamespace(user_id=user, agent_id=agent) if (user, agent, session) == (OWNER.user_id, OWNER.agent_id, OWNER.session_id) else None
        self.app.state.storage = SimpleNamespace(get_session=session)
        self.app.state.resource_access_service = SimpleNamespace(get_resource=AsyncMock())
        self.app.state.workspace_service = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(workdir=str(self.root / 'workspace'))),
            sign_download_token=lambda user, path: sign_download_token('synthetic-test-secret', user, path),
            verify_download_token=lambda token, path: verify_download_token('synthetic-test-secret', token, path))
        resources_for(self.app, ("data-analysis", "report-generation"))
        self.app.include_router(create_analysis_router(config))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.params = {'agent_id': OWNER.agent_id, 'session_id': OWNER.session_id}
        self.headers = {'X-User-ID': OWNER.user_id}

    async def test_read_and_projection_do_not_query_budget(self):
        view = await self.client.get('/workspace/analyses/view', params={**self.params, 'path': self.path}, headers=self.headers)
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()['rows'][1]['display'], '-1,033.14')
        projected = await self.client.post('/workspace/analyses/project-view', params=self.params, headers=self.headers, json={'analysisRef': self.ref, 'viewParameters': {'filter': 'negative'}})
        self.assertEqual(projected.status_code, 200)
        self.assertEqual(projected.json()['total'], 2)
        self.assertEqual(view.headers['cache-control'], 'no-store')

    async def test_identity_and_revoked_access(self):
        for headers, params in (({}, self.params), ({'X-User-ID': 'other'}, self.params), (self.headers, {**self.params, 'session_id': 'other'})):
            r = await self.client.get('/workspace/analyses/view', params={**params, 'path': self.path}, headers=headers)
            self.assertIn(r.status_code, (401, 404, 422))
        self.app.state.resource_access_service.get_resource.side_effect = HTTPException(403, 'revoked')
        r = await self.client.get('/workspace/analyses/view', params={**self.params, 'path': self.path}, headers=self.headers)
        self.assertEqual(r.status_code, 403)

    async def test_registered_download_capability_bound_to_session_version_export(self):
        r = await self.client.post('/workspace/analyses/render-report', params=self.params, headers=self.headers, json={'analysisRef': self.ref, 'requestKey': 'r'})
        self.assertEqual(r.status_code, 200)
        query = {**self.params, 'analysis_ref': self.ref, 'export_ref': r.json()['exportRef']}
        minted = await self.client.post('/workspace/analyses/report-token', params=query, headers=self.headers)
        token = minted.json()['token']
        good = await self.client.get('/workspace/analyses/report', params={**query, 'token': token, 'download': 'true'})
        self.assertEqual(good.status_code, 200)
        self.assertIn('attachment', good.headers['content-disposition'])
        self.assertIn('-1,033.14', good.text)
        for field, value in (('session_id', 'other'), ('analysis_ref', self.ref.replace(':1', ':2')), ('export_ref', 'export_other')):
            bad = await self.client.get('/workspace/analyses/report', params={**query, field: value, 'token': token})
            self.assertEqual(bad.status_code, 403)
        self.app.state.resource_access_service.get_resource.side_effect = HTTPException(403, 'revoked')
        revoked = await self.client.get('/workspace/analyses/report', params={**query, 'token': token})
        self.assertEqual(revoked.status_code, 403)

    async def test_invalid_expired_token_and_undelivered_report(self):
        query = {**self.params, 'analysis_ref': self.ref, 'export_ref': 'unregistered'}
        r = await self.client.get('/workspace/analyses/report', params=query, headers=self.headers)
        self.assertEqual(r.status_code, 404)
        for token in ('garbage', '0.eA.eA'):
            r = await self.client.get('/workspace/analyses/report', params={**query, 'token': token})
            self.assertEqual(r.status_code, 403)

    async def test_tamper_recovery_and_path_spelling(self):
        p = self.root / 'workspace' / self.path[2:]
        p.write_text('{"verified":true}', 'utf-8')
        bad = await self.client.get('/workspace/analyses/view', params={**self.params, 'path': self.path}, headers=self.headers)
        self.assertEqual(bad.status_code, 409)
        restored = await self.client.post('/workspace/analyses/restore', params=self.params, headers=self.headers, json={'path': self.path})
        self.assertEqual(restored.status_code, 200)
        for path in (self.path.upper(), self.path.replace('/', '\\'), self.path.replace('analysis/', '%61nalysis/', 1)):
            r = await self.client.get('/workspace/analyses/view', params={**self.params, 'path': path}, headers=self.headers)
            self.assertEqual(r.status_code, 409)

    async def test_extra_amount_fields_and_large_body_rejected(self):
        r = await self.client.post('/workspace/analyses/project-view', params=self.params, headers=self.headers, json={'analysisRef': self.ref, 'amount': '100'})
        self.assertEqual(r.status_code, 422)
        r = await self.client.post('/workspace/analyses/project-view', params=self.params, headers=self.headers, content='x' * 70000)
        self.assertEqual(r.status_code, 413)

    async def test_global_disable_blocks_reads_reports_and_new_work(self):
        result = self.delivery.render_report(self.ref, 'r')
        config = json.loads(self.config.read_text())
        config['activation'] = 'disabled'
        self.config.write_text(json.dumps(config), 'utf-8')
        view = await self.client.get('/workspace/analyses/view', params={**self.params, 'path': self.path}, headers=self.headers)
        self.assertEqual(view.status_code, 409)
        self.assertEqual(view.json()['detail']['code'], 'ANALYSIS_DISABLED')
        query = {**self.params, 'analysis_ref': self.ref, 'export_ref': result['exportRef']}
        report = await self.client.get('/workspace/analyses/report', params=query, headers=self.headers)
        self.assertEqual(report.status_code, 409)
        denied = await self.client.post('/workspace/analyses/render-report', params=self.params, headers=self.headers, json={'analysisRef': self.ref, 'requestKey': 'new'})
        self.assertEqual(denied.status_code, 409)


    async def test_free_document_preview_export_and_access_are_isolated(self):
        from analysis_fixtures import saved_fixture
        cap,selections,scope=saved_fixture(self.store,('42','-5','0'))
        built=await self.service.build([cap],selections,scope,[],'free-route-build')
        artifact=self.store.get_analysis(OWNER,built['analysisRef'])
        preview=await self.service.publish(built['analysisRef'],requestKey='free-route-preview',expectedBaseRevision=0,previewOnly=True,
            document={'title':'自由页面','html':'<html><head></head><body><p>{{x}}</p><script>document.body.dataset.ready="yes"</script></body></html>',
                      'bindings':[{'key':'x','valueRef':artifact['facts'][0]['factId']}]})
        query={**self.params,'preview_ref':preview['repairRef']}
        page=await self.client.get('/workspace/analyses/preview',params=query,headers=self.headers)
        self.assertEqual(page.status_code,200,page.text)
        self.assertIn('sandbox allow-scripts;',page.headers['content-security-policy'])
        self.assertIn("connect-src 'none'",page.headers['content-security-policy'])
        self.assertNotIn('allow-same-origin',page.headers['content-security-policy'])
        denied=await self.client.get('/workspace/analyses/preview',params=query,headers={'X-User-ID':'other'})
        self.assertEqual(denied.status_code,404)
        rendered=await self.service.preview_content(preview['repairRef'])
        doc=rendered['document']['freeDocument']
        from analysis_document_checks_test import measurements
        observed=measurements();observed['orders']=[]
        body={'previewRef':preview['repairRef'],'contentDigest':doc['contentDigest'],'frameKey':doc['frameKey'],'measurements':observed}
        checked=await self.client.post('/workspace/analyses/preview-check',params=self.params,headers=self.headers,json=body)
        self.assertEqual(checked.status_code,200,checked.text)
        self.assertFalse(checked.json()['visualSemanticsValidated'])
        denied=await self.client.post('/workspace/analyses/preview-check',params=self.params,headers={'X-User-ID':'other'},json=body)
        self.assertEqual(denied.status_code,404)
        stale=await self.client.post('/workspace/analyses/preview-check',params=self.params,headers=self.headers,json={**body,'contentDigest':'0'*64})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(stale.json()['detail']['code'],'STALE_PREVIEW_CHECK')
        narrow=measurements(width=420);narrow['orders']=[]
        checked=await self.client.post('/workspace/analyses/preview-check',params=self.params,headers=self.headers,
                                       json={**body,'measurements':narrow})
        self.assertEqual(checked.status_code,200,checked.text)
        published=await self.service.publish(built['analysisRef'],requestKey='free-route-publish',expectedBaseRevision=0,repairRef=preview['repairRef'],repairs=[])
        self.assertTrue(published['delivery']['registered'])
        export=await self.client.post('/workspace/analyses/render-report',params=self.params,headers=self.headers,json={'analysisRef':built['analysisRef'],'requestKey':'free-route-export'})
        self.assertEqual(export.status_code,200,export.text)
        query={**self.params,'analysis_ref':built['analysisRef'],'export_ref':export.json()['exportRef']}
        report=await self.client.get('/workspace/analyses/report',params=query,headers=self.headers)
        self.assertEqual(report.status_code,200,report.text)
        self.assertIn('sandbox="allow-scripts"',report.text)
        self.assertIn("frame-src 'self'",report.headers['content-security-policy'])
        self.assertNotIn('allow-same-origin',report.text)
