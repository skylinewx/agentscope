"""Successful artifact receipts expose deterministic process summary metadata."""
import json

from analysis_presentation_fixtures import PresentationCase
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.tools import AnalysisRenderReport


class ProcessSummaryTests(PresentationCase):
    async def test_published_analysis_and_report_receipts(self):
        published = await self.publish()
        self.assertEqual(published['delivery']['processSummary'],
                         {'kind': 'analysis', 'id': self.ref.split(':')[0]})
        tool = AnalysisRenderReport(self.service)
        first = await tool.call(analysisRef=self.ref, requestKey='process-summary')
        value = json.loads(first.content[0].text)
        self.assertEqual(first.metadata['process_summary'],
                         {'kind': 'report', 'id': value['exportRef'], 'topicCount': 0})
        retry = await tool.call(analysisRef=self.ref, requestKey='process-summary')
        self.assertEqual(first.metadata, retry.metadata)

    async def test_failed_export_does_not_claim_a_report(self):
        tool = AnalysisRenderReport(self.service)
        response = await tool.call(analysisRef='missing', requestKey='no-report')
        self.assertEqual(response.state, 'error')
        self.assertNotIn('process_summary', response.metadata)

    async def test_report_topic_count_requires_published_coverage(self):
        capture, selectors, scope = saved_fixture(self.store)
        base = await self.service.build([capture], selectors, scope, [], 'base')
        refs = [fact['factId'] for fact in self.store.get_analysis(OWNER, base['analysisRef'])['facts']]
        scope['reportTopics'] = [
            {'title': f'Topic {index}', 'question': 'Value?', 'factRefs': [ref]}
            for index, ref in enumerate(refs)
        ]
        built = await self.service.build([capture], selectors, scope, [], 'with-topics')
        await self.service.publish(built['analysisRef'], [], [{
            'viewId': 'values', 'scopeId': built['scope']['scopeId'],
            'type': 'TABLE', 'valueRefs': refs,
        }], 'published', built['baseRevision'])
        receipt = await AnalysisRenderReport(self.service).call(
            analysisRef=built['analysisRef'], requestKey='covered-topics',
        )
        self.assertIs(json.loads(receipt.content[0].text)['registered'], True)
        self.assertEqual(receipt.metadata['process_summary']['topicCount'], 3)
