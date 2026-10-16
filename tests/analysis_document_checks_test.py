from copy import deepcopy

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.document_checks import evaluate
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError, Owner


def measurements(refs=(), width=900, partial=False):
    return {'version':3,'viewport':{'width':width,'height':500},'pageOverflow':False,'overflow':[],
            'numericChecks': {'counts': {'declared': 0, 'measured': 0, 'failed': 0},
                              'linearBars': {'declared': 0, 'measured': 0, 'invalid': 0, 'scaleGroups': 0, 'checkedGroups': 0, 'failedGroups': 0}},
            'overlaps':[],'smallText':[],'clippedText':[],'errors':[],'truncated':False,
            'orders':[{'key':'ranking','refs':list(refs),'partial':partial,'truncated':False}]}


class DocumentCheckTests(PresentationCase):
    async def test_invalid_geometry_blocks_even_without_diagnostic_text_or_with_unavailable_reason(self):
        await self.setup_preview()
        for width in (420, 900):
            sample = measurements(self.refs, width)
            sample['numericChecks']['linearBars'].update(declared=4, measured=4, invalid=4)
            result = await self.record(sample)
            self.assertEqual(result['numericChecks']['linearBars']['status'], 'FAIL')
            self.assertTrue(result['blocking'])
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(**self.preview['confirmPublish'])
        self.assertEqual(caught.exception.code, 'DOCUMENT_RENDER_ERRORS')
        from analysis_extension.document_checks import require_publication_checks
        with self.assertRaises(AnalysisError):
            require_publication_checks(self.compiled, [result], 'offline')
        # A real fresh measurement of the same snapshot can clear a transient
        # layout failure. A different snapshot cannot reuse these records.
        for width in (420, 900):
            sample = measurements(self.refs, width)
            sample['numericChecks']['linearBars'].update(declared=4, measured=4, scaleGroups=2, checkedGroups=2)
            await self.record(sample)
        published = await self.service.publish(**self.preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])

    async def setup_preview(self):
        cap, selectors, scope = saved_fixture(self.store, ('-8300','-1033.14','100'))
        built = await self.service.build([cap],selectors,scope,[{'id':'s','operator':'SORT'}],'b')
        self.ref = built['analysisRef']
        self.doc = {'title':'实测','html':'<html><head></head><body><main></main></body></html>',
                    'datasets':[{'key':'ranking','valueRef':'derived_s'}]}
        artifact = self.store.get_analysis(OWNER,self.ref)
        self.compiled = compile_document(artifact,self.doc)
        self.refs = [row['factRef'] for row in self.compiled['datasets']['ranking']['rows']]
        self.preview = await self.service.publish(self.ref,requestKey='p',expectedBaseRevision=0,document=self.doc,previewOnly=True)

    async def record(self, observation):
        return await self.service.record_preview_check(self.preview['repairRef'],self.compiled['contentDigest'],self.compiled['frameKey'],observation)

    async def test_known_bad_order_blocks_publish_and_recheck_uses_current_width(self):
        await self.setup_preview()
        self.assertEqual(self.preview['diagnostics']['renderChecks']['status'],'NOT_CHECKED')
        bad = await self.record(measurements(list(reversed(self.refs))))
        self.assertEqual(bad['orders'][0]['status'],'MISMATCH')
        with self.assertRaises(AnalysisError) as caught: await self.service.publish(**self.preview['confirmPublish'])
        self.assertEqual(caught.exception.code,'DOCUMENT_RENDER_ERRORS')
        fixed = await self.record(measurements(self.refs))
        self.assertEqual(fixed['status'],'MEASURED')
        await self.record(measurements(self.refs,420))
        inspected = await self.service.inspect(section='preview',previewRef=self.preview['repairRef'])
        checks = inspected['diagnostics']['renderChecks']
        self.assertEqual([r['viewport']['width'] for r in checks['viewports']],[420,900])
        self.assertFalse(checks['visualSemanticsValidated'])
        published = await self.service.publish(**self.preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])
        with self.assertRaises(AnalysisError): await self.record(measurements(self.refs))

    async def test_digest_revision_and_owner_do_not_inherit_visual_checks(self):
        await self.setup_preview()
        await self.record(measurements(self.refs))
        with self.assertRaises(AnalysisError) as caught:
            await self.service.record_preview_check(self.preview['repairRef'],'0'*64,self.compiled['frameKey'],measurements(self.refs))
        self.assertEqual(caught.exception.code,'STALE_PREVIEW_CHECK')
        updated = {**self.doc,'html':self.doc['html'].replace('<main>','<main class="new-layout">')}
        new = await self.service.publish(self.ref,requestKey='p2',expectedBaseRevision=0,document=updated,previewOnly=True)
        self.assertEqual(new['diagnostics']['renderChecks']['status'],'NOT_CHECKED')
        with self.assertRaises(AnalysisError): self.store.document_checks(Owner('other',OWNER.agent_id,OWNER.session_id),self.ref,self.compiled['contentDigest'])
        self.store.collect_expired(10**15)
        self.assertEqual(len(self.store.document_checks(OWNER,self.ref,self.compiled['contentDigest'])),1)

    async def test_partial_missing_duplicate_unmarked_and_script_observations(self):
        await self.setup_preview()
        for refs, partial, status in ((self.refs[:2],True,'PASS'),(self.refs[:2],False,'MISMATCH'),
                                      ([self.refs[0],self.refs[0]],True,'MISMATCH'),([],True,'EMPTY')):
            self.assertEqual(evaluate(self.compiled,measurements(refs,partial=partial))['orders'][0]['status'],status)
        sample=measurements(self.refs);sample['orders']=[]
        checked=evaluate(self.compiled,sample)
        self.assertEqual(checked['unmeasuredDatasets'],['ranking'])
        self.assertEqual(checked['status'],'REVIEW_REQUIRED')
        sample=measurements(self.refs);sample['errors']=['script failed']
        self.assertTrue(evaluate(self.compiled,sample)['blocking'])
        sample=measurements(self.refs);sample['overlaps']=[{'first':'甲','second':'乙'}]
        self.assertFalse(evaluate(self.compiled,sample)['blocking'])
        self.assertEqual(evaluate(self.compiled,sample)['status'],'REVIEW_REQUIRED')
        for invalid in ({}, {'version':1}, {**measurements(self.refs),'viewport':{'width':True,'height':500}},
                        {**measurements(self.refs),'errors':['x'*301]}):
            with self.assertRaises(AnalysisError): evaluate(self.compiled,invalid)

    async def test_readability_warnings_are_recorded_without_prescribing_layout(self):
        await self.setup_preview()
        sample = measurements(self.refs)
        sample['smallText'] = [{'text': '营业利润', 'fontPx': 7.25}]
        sample['clippedText'] = [{'text': '-2,184.21', 'container': '利润构成', 'edges': ['left']}]
        record = await self.record(sample)
        self.assertEqual(record['status'], 'REVIEW_REQUIRED')
        self.assertFalse(record['blocking'])
        self.assertEqual(record['smallText'], sample['smallText'])
        self.assertEqual(record['clippedText'], sample['clippedText'])
        self.assertTrue(record['readability']['advisoryOnly'])
        # Re-checking the same document and width clears resolved warnings.
        await self.record(measurements(self.refs))
        inspected = await self.service.inspect(section='preview', previewRef=self.preview['repairRef'])
        self.assertEqual(inspected['diagnostics']['renderChecks']['status'], 'MEASURED')
        await self.record(measurements(self.refs,420))
        published = await self.service.publish(**self.preview['confirmPublish'])
        self.assertTrue(published['delivery']['registered'])

    async def test_publication_requires_actual_measurements_at_two_widths(self):
        await self.setup_preview()
        self.assertEqual(self.preview['nextAction'], 'RUN_RENDER_CHECKS')
        for sample in (None, measurements(self.refs), measurements(self.refs)):
            if sample is not None: await self.record(sample)
            with self.assertRaises(AnalysisError) as caught: await self.service.publish(**self.preview['confirmPublish'])
            self.assertEqual(caught.exception.code, 'DOCUMENT_NOT_CHECKED')
            self.assertEqual(caught.exception.details['nextAction'], 'RUN_RENDER_CHECKS')
            self.assertIn('previewPage', caught.exception.details)
        await self.record(measurements(self.refs,420))
        result = await self.service.publish(**self.preview['confirmPublish'])
        self.assertTrue(result['diagnostics']['renderChecks']['measurementRequirementMet'])

    async def test_explicit_unavailable_reason_retains_unchecked_state_and_cannot_bypass_errors(self):
        await self.setup_preview()
        reason = '离线契约夹具未启动浏览器，未验证实际视觉效果。'
        edits = [{'op':'replace','path':'/renderCheckUnavailableReason','value':reason}]
        preview = await self.service.publish(**{**self.preview['confirmPublish'], 'requestKey':'unavailable', 'repairs':edits}, previewOnly=True)
        checks = preview['diagnostics']['renderChecks']
        self.assertFalse(checks['performed'])
        self.assertEqual(checks['status'], 'NOT_CHECKED')
        self.assertEqual(checks['unavailableReason'], reason)
        sample = measurements(self.refs);sample['errors']=['broken script']
        await self.record(sample)
        with self.assertRaises(AnalysisError) as caught: await self.service.publish(**preview['confirmPublish'])
        self.assertEqual(caught.exception.code,'DOCUMENT_RENDER_ERRORS')
        await self.record(measurements(self.refs))
        result = await self.service.publish(**preview['confirmPublish'])
        self.assertFalse(result['diagnostics']['renderChecks']['measurementRequirementMet'])
        self.assertEqual(result['diagnostics']['renderChecks']['unavailableReason'],reason)

    async def test_declaring_a_dataset_without_measuring_any_rows_is_not_order_verification(self):
        await self.setup_preview()
        for width in (420,900):
            sample = measurements(self.refs,width);sample['orders']=[]
            await self.record(sample)
        with self.assertRaises(AnalysisError) as caught: await self.service.publish(**self.preview['confirmPublish'])
        self.assertEqual(caught.exception.code,'DOCUMENT_ORDER_NOT_CHECKED')
        self.assertEqual(caught.exception.details['missingDatasetKeys'],['ranking'])

    async def test_readability_payload_is_bounded_and_rejects_invalid_geometry(self):
        await self.setup_preview()
        for font in (True, -1, 0, 12, 10**1000, float('nan'), float('inf'), '8'):
            sample = measurements(self.refs)
            sample['smallText'] = [{'text': 'text', 'fontPx': font}]
            with self.assertRaises(AnalysisError): evaluate(self.compiled, sample)
        for edges in ([], ['left', 'left'], ['diagonal'], [None], [['left']]):
            sample = measurements(self.refs)
            sample['clippedText'] = [{'text': 'text', 'container': 'chart', 'edges': edges}]
            with self.assertRaises(AnalysisError): evaluate(self.compiled, sample)
        sample = measurements(self.refs)
        sample['smallText'] = [{'text': 'text', 'fontPx': 8}] * 31
        with self.assertRaises(AnalysisError): evaluate(self.compiled, sample)
        sample = measurements(self.refs); sample['version'] = True
        with self.assertRaises(AnalysisError): evaluate(self.compiled, sample)
