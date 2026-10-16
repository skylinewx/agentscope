"""Document publication must preserve values, preview identity and isolation."""
import json
from copy import deepcopy
from html.parser import HTMLParser

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.free_document import compile_document, freeze_source
from analysis_extension.models import AnalysisError
from analysis_extension.tools import AnalysisPublish


class FreeDocumentTests(PresentationCase):
    async def request(self):
        cap, selections, scope = saved_fixture(self.store, ('1234567890.123456', '-7', '0'), unit='')
        built = await self.service.build([cap], selections, scope, [], 'build-free')
        self.ref = built['analysisRef']
        artifact = self.store.get_analysis(OWNER, self.ref)
        self.fact = artifact['facts'][0]['factId']
        return {'analysisRef': self.ref, 'expectedBaseRevision': 0, 'requestKey': 'preview-free',
                'document': {'title': '自由布局', 'html': '<!doctype html><html><head><meta charset="utf-8"><style>p{color:teal}</style></head><body><h1>比较</h1><p>{{amount}}</p><details><summary>展开</summary><p>内容</p></details><script>document.body.dataset.value=window.analysisData.amount.value;</script></body></html>',
                             'bindings': [{'key':'amount','valueRef':self.fact,'format':{'grouping':True,'decimalPlaces':2}}]}}

    async def test_preview_publish_reload_and_report_share_frozen_content(self):
        req = await self.request()
        req['renderCheckUnavailableReason'] = '离线冻结与导出契约测试未启动浏览器，实际视觉效果未验证。'
        original = self.store.get_analysis(OWNER, self.ref)
        self.delivery.root.mkdir(parents=True)
        source = self.delivery.root/'draft.html'
        source.write_text(req['document'].pop('html'),'utf-8')
        req['document']['path']='./draft.html'
        preview = await self.service.publish(**req, previewOnly=True)
        self.assertFalse(preview['diagnostics']['visualInspectionPerformed'])
        self.assertEqual(self.store.get_analysis(OWNER,self.ref),original)
        frozen = self.store.get_publication_proposal(OWNER,preview['repairRef'])
        self.assertNotIn('path',frozen['document'])
        source.write_text('<html>changed after preview</html>','utf-8')
        result = await self.service.publish(self.ref,requestKey='publish-free',expectedBaseRevision=0,repairRef=preview['repairRef'],repairs=[])
        self.assertTrue(result['delivery']['registered'])
        view=self.delivery.view(result['delivery']['path'])
        doc=view['freeDocument']
        self.assertIn('1,234,567,890.12',doc['html'])
        self.assertIn('1234567890.123456',doc['html'])
        self.assertNotIn('changed after preview',doc['html'])
        self.assertNotIn('未注明',doc['html'])
        saved=self.store.get_analysis(OWNER,self.ref)
        self.assertEqual(saved['facts'],original['facts'])
        self.assertEqual(saved['derivedValues'],original['derivedValues'])
        export=self.delivery.render_report(self.ref,'report-free')
        rendered=self.delivery.report(self.ref,export['exportRef'])['html']
        class Frames(HTMLParser):
            def handle_starttag(self,tag,attrs):
                if tag=='iframe':self.frame=dict(attrs)
        parsed=Frames();parsed.feed(rendered)
        self.assertEqual(parsed.frame['srcdoc'],doc['html'])
        self.assertEqual(parsed.frame['sandbox'],'allow-scripts')
        self.assertEqual(self.delivery.render_report(self.ref,'report-free'),export)
        self.assertEqual(self.delivery.restore(result['delivery']['path'])['freeDocument'],doc)
        self.assertEqual(self.delivery.view(result['delivery']['path'])['freeDocument'],doc)
        with self.assertRaises(AnalysisError):self.delivery.project_view(self.ref,{'filter':'negative'})
        with self.assertRaises(AnalysisError):self.delivery.render_report(self.ref,'table-free',layout='table')

    async def test_bad_reference_is_located_repairable_and_does_not_publish(self):
        req=await self.request();req['document']['bindings'][0]['valueRef']='fact_not_current'
        response=await AnalysisPublish(self.service).call(**req,previewOnly=True)
        error=json.loads(response.content[0].text)['error']
        self.assertEqual(error['details']['path'],'/document/bindings/0/valueRef')
        self.assertEqual(self.store.get_analysis(OWNER,self.ref)['publicationState'],'DRAFT')
        fixed=await self.service.publish(self.ref,requestKey='repair-free',expectedBaseRevision=0,repairRef=error['details']['repairRef'],previewOnly=True,
            repairs=[{'op':'replace','path':'/document/bindings/0/valueRef','value':self.fact}])
        self.assertTrue(fixed['previewOnly'])
        self.assertEqual(fixed['summary']['document']['bindingCount'],1)

    async def test_external_resources_bad_paths_and_unknown_slots_rejected(self):
        req=await self.request();artifact=self.store.get_analysis(OWNER,self.ref)
        for body in ('<script src="https://example.com/a.js"></script>','<iframe src="/secret"></iframe>',
                     '<img src="https://example.com/i">','<meta http-equiv="refresh" content="0;url=/">',
                     '<base href="/">','<a href="javascript:alert(1)">link</a>','{{missing}}',
                     '<script>const value="{{amount}}";</script>','<!-- {{amount}} -->','<div title="{{amount}}"></div>'):
            doc=deepcopy(req['document']);doc['html']=f'<html><head></head><body>{body}</body></html>'
            with self.subTest(body=body),self.assertRaises(AnalysisError):compile_document(artifact,doc)
        for path in ('../else.html','C:/else.html','/else.html','a\\b.html','./a/../b.html'):
            with self.subTest(path=path),self.assertRaises(AnalysisError):freeze_source({'path':path},self.delivery.root)

    async def test_binding_injection_escapes_labels_and_keeps_missing_units_empty(self):
        req=await self.request();artifact=self.store.get_analysis(OWNER,self.ref)
        artifact['facts'][0]['objectLabel']='</script><script>bad()</script>{{notASlot}}'
        doc=compile_document(artifact,req['document'])
        self.assertNotIn('</script><script>bad()',doc['html'])
        self.assertIn('\\u003c/script>',doc['html'])
        req['document']['bindings'].append({'key':'unit','valueRef':self.fact,'field':'unit'})
        req['document']['html']=req['document']['html'].replace('{{amount}}','{{amount}}{{unit}}')
        doc=compile_document(artifact,req['document'])
        self.assertIn('<p>1,234,567,890.12</p>',doc['html'])

    async def test_dynamic_data_declaration_is_not_claimed_as_visible_topic_coverage(self):
        req=await self.request();artifact=self.store.get_analysis(OWNER,self.ref)
        from analysis_extension.coverage import represented
        artifact['scope']['reportTopics']=[{'title':'主题','question':'问题','factRefs':[self.fact]}]
        artifact['freeDocument']=compile_document(artifact,req['document'])
        self.assertTrue(represented(artifact)['topics'][0]['represented'])
        req['document']['html']=req['document']['html'].replace('{{amount}}','图表')
        artifact['freeDocument']=compile_document(artifact,req['document'])
        artifact['claims']=[{'parts':[{'valueRef':self.fact}]}]
        self.assertFalse(represented(artifact)['topics'][0]['represented'])

    async def test_scalar_binding_fields_scope_and_limits(self):
        req=await self.request();artifact=self.store.get_analysis(OWNER,self.ref)
        for binding in ({'key':'amount','valueRef':self.fact,'field':'localValue'},
                        {'key':'amount','valueRef':self.fact,'format':{'decimalPlaces':99}},
                        {'key':'amount','valueRef':self.fact,'amount':'99'}):
            with self.subTest(binding=binding),self.assertRaises(AnalysisError):
                compile_document(artifact,{**req['document'],'bindings':[binding]})
        with self.assertRaises(AnalysisError) as caught:
            compile_document(artifact,{**req['document'],'html':'x'*(512*1024+1)})
        self.assertEqual(caught.exception.code,'DOCUMENT_TOO_LARGE')
