"""Headless checks must bind to the exact preview, never fabricate a pass."""
import json
from copy import deepcopy
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from analysis_presentation_fixtures import PresentationCase
from analysis_document_checks_test import measurements
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.models import AnalysisError
from analysis_extension.publication import repair_fields, repair_request
from analysis_extension.render_client import RenderChecker
from analysis_extension.completion import completion_summary


class AutomaticRenderTests(PresentationCase):
    async def prepare(self):
        cap, selectors, scope = saved_fixture(self.store, ('2','0','-4'))
        built = await self.service.build([cap], selectors, scope, [], 'b')
        self.ref = built['analysisRef']
        artifact = self.store.get_analysis(OWNER,self.ref)
        self.document = {'title':'阅读结果','html':'<html><head></head><body><p>本次结果 {{v}}</p></body></html>',
                         'bindings':[{'key':'v','valueRef':artifact['facts'][0]['factId']}]}
        samples = [measurements(width=w) for w in (420,900)]
        for sample in samples: sample['orders'] = []
        self.checker = AsyncMock()
        self.checker.check.return_value = samples
        self.service.render_checker = self.checker

    async def preview(self, **extra):
        return await self.service.publish(self.ref, requestKey='p', expectedBaseRevision=0,
                                          document=self.document, previewOnly=True, **extra)

    async def test_exact_preview_records_reused_and_publication_succeeds(self):
        await self.prepare()
        preview = await self.preview()
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED')
        self.assertTrue(preview['diagnostics']['renderChecks']['measurementRequirementMet'])
        self.assertEqual(preview['nextAction'], 'REVIEW_AND_CONFIRM')
        again = await self.preview()
        self.assertEqual(again['diagnostics']['renderService']['status'], 'REUSED')
        self.checker.check.assert_awaited_once()
        published = await self.service.publish(**preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])

    async def test_unavailable_checker_does_not_grant_publication(self):
        await self.prepare()
        self.checker.check.side_effect = AnalysisError('RENDER_SERVICE_UNAVAILABLE','offline')
        preview = await self.preview()
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'UNAVAILABLE')
        self.assertEqual(preview['nextAction'], 'RUN_RENDER_CHECKS')
        self.assertFalse(preview['diagnostics']['renderChecks']['performed'])
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_NOT_CHECKED')

    async def test_all_records_validated_before_any_write_and_one_width_rejected(self):
        await self.prepare()
        good = self.checker.check.return_value[0]
        for samples in ([good], [good,good], [good,{'version':2}]):
            self.checker.check.return_value = samples
            preview = await self.preview()
            self.assertEqual(preview['diagnostics']['renderService']['status'], 'UNAVAILABLE')
            self.assertFalse(preview['diagnostics']['renderChecks']['performed'])

    async def test_measured_errors_remain_blocking_with_unavailability_reason(self):
        await self.prepare()
        self.checker.check.return_value[0]['errors'] = ['count differs from visible rows']
        preview = await self.preview(renderCheckUnavailableReason='Browser unavailable elsewhere')
        self.assertEqual(preview['nextAction'], 'REPAIR_DOCUMENT')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_RENDER_ERRORS')

    async def test_content_change_requires_new_measurements(self):
        await self.prepare()
        await self.preview()
        self.document['html'] = '<html><head></head><body><p>新的内容 {{v}}</p></body></html>'
        preview = await self.preview()
        self.assertEqual(self.checker.check.await_count, 2)
        self.assertEqual(preview['diagnostics']['renderService']['status'], 'COMPLETED')

    async def test_permission_rechecked_after_worker_returns(self):
        await self.prepare()
        async def denied(): raise AnalysisError('ACCESS_DENIED','revoked')
        async def completed(document):
            self.service.authorize = denied
            return [measurements(width=420), measurements(width=900)]
        self.checker.check.side_effect = completed
        with self.assertRaises(AnalysisError) as caught: await self.preview()
        self.assertEqual(caught.exception.code, 'ACCESS_DENIED')
        self.assertEqual(self.store.get_analysis(OWNER,self.ref)['publicationState'],'DRAFT')


class RenderProtocolTests(IsolatedAsyncioTestCase):
    async def test_response_identity_and_widths_are_verified(self):
        document = {'html':'<p>test</p>','contentDigest':'a'*64,'frameKey':'bound-key'}
        answer = {'version':1,'contentDigest':'a'*64,'frameKey':'bound-key',
                  'measurements':[measurements(width=w) for w in (420,900)]}
        real_client = httpx.AsyncClient
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            self.assertEqual(request.headers['Authorization'],'Bearer private-token')
            return httpx.Response(200,json=answer)
        def client(**kwargs): return real_client(transport=httpx.MockTransport(respond),**kwargs)
        with patch('analysis_extension.render_client.httpx.AsyncClient',client):
            checker = RenderChecker('http://renderer:8932','private-token')
            self.assertEqual(len(await checker.check(document)),2)
            for key, value in [('contentDigest','b'*64),('frameKey','other'),('version',True),('measurements',[])]:
                old = answer[key];answer[key]=value
                with self.assertRaises(AnalysisError) as caught: await checker.check(document)
                self.assertNotIn('private-token',str(caught.exception))
                answer[key]=old
        self.assertEqual(seen[0]['widths'],[420,900])

    def test_invalid_configuration_fails_clearly(self):
        for url,token,widths in [('file:///tmp/x','t',[420,900]),('http://r:bad','t',[420,900]),
                                 ('http://u:p@r','t',[420,900]),('http://r','',[420,900]),
                                 ('http://r','t',[420,[]]),('http://r','t',[420,420]),('http://r','t',None)]:
            with self.assertRaises(AnalysisError): RenderChecker(url,token,widths)


class RepairGuidanceTests(TestCase):
    def test_null_existing_field_returns_replace_without_mutating_input(self):
        saved = {'analysisRef':'a','expectedBaseRevision':0,'claims':[], 'viewPlan':[],
                 'renderCheckUnavailableReason':None}
        before = deepcopy(saved)
        fields = repair_fields(saved)
        self.assertEqual(fields['renderCheckUnavailableReason']['op'],'replace')
        self.assertEqual(fields['document']['op'],'add')
        edit = {'op':'add','path':'/renderCheckUnavailableReason','value':'actual reason'}
        with self.assertRaises(AnalysisError) as caught: repair_request(saved,'a',[edit])
        suggestion = caught.exception.details['suggestedEdit']
        fixed = repair_request(saved,'a',[suggestion])
        self.assertEqual(fixed['renderCheckUnavailableReason'],'actual reason')
        self.assertEqual(saved,before)

    def test_relation_error_distinguishes_computation_reference(self):
        artifact = {'facts':[], 'derivedValues':[{'derivedId':'derived_r'}], 'relations':[],
                    'plan':{'workPlan':[{'taskId':'r','title':'关系','kind':'RELATION','refs':['derived_r']}]}}
        with self.assertRaises(AnalysisError) as caught: completion_summary(artifact)
        self.assertEqual(caught.exception.details['actualKind'],'COMPUTATION')
        self.assertEqual(caught.exception.details['nextSection'],'relations')
