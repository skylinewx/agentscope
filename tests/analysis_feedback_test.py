"""Actionable errors without relaxing scope or publication boundaries."""
from copy import deepcopy
import json
from unittest import TestCase

from analysis_fixtures import fixture, saved_fixture, OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters.budget_v2 import adapt
from analysis_extension.models import AnalysisError
from analysis_extension.publication import schema_issues
from analysis_extension.tools import AnalysisPublish


class ScopeFeedbackTests(TestCase):
    def test_all_missing_members_and_dimension_conflicts_are_returned_without_mutation(self):
        capture, selections, scope = fixture()
        scope['members'] = scope['members'][:1]
        scope['expectedDimensions']['ACCOUNT'] = 'different'
        before = deepcopy((capture, selections, scope))
        with self.assertRaises(AnalysisError) as caught:
            adapt({'replace': capture}, selections, scope, 50000)
        self.assertEqual(caught.exception.code, 'SCOPE_MISMATCH')
        details = caught.exception.details
        self.assertEqual(details['missingMembers'], ['object-1', 'object-2'])
        self.assertEqual(len(details['issues']), 3)
        issue = next(i for i in details['issues'] if i['field'] == 'scope.expectedDimensions')
        self.assertEqual((issue['dimension'], issue['expected'], issue['actual']), ('ACCOUNT', 'different', 'profit'))
        self.assertEqual(issue['affectedCellCount'], 3)
        self.assertEqual((capture, selections, scope), before)

    def test_explicit_reference_members_are_accepted_without_changing_subject_population(self):
        capture, selections, scope = fixture()
        scope.pop('population')
        scope['members'], scope['referenceMembers'] = scope['members'][:1], scope['members'][1:]
        result = adapt({'replace': capture}, selections, scope, 50000)
        self.assertEqual(len(result['facts']), 3)
        self.assertEqual(result['scope']['members'], ['object-0'])
        self.assertEqual(result['scope']['referenceMembers'], ['object-1', 'object-2'])

    def test_wrapped_payload_is_rejected_concisely_without_echoing_or_unwrapping_content(self):
        request = {'arguments': {'analysisRef': 'a:1', 'claims': [{'text': 'sensitive-prose' * 4000}]}}
        before = deepcopy(request)
        issues = schema_issues(request)
        self.assertEqual(issues[0]['path'], '/arguments')
        self.assertLess(len(json.dumps(issues, ensure_ascii=False)), 600)
        self.assertNotIn('sensitive-prose', json.dumps(issues))
        self.assertEqual(request, before)

    def test_invalid_publication_variant_does_not_echo_the_entire_plan(self):
        request = {'analysisRef': 'a:1', 'requestKey': 'key', 'expectedBaseRevision': 0,
                   'claims': [], 'viewPlan': [], 'repairRef': 'repair', 'repairs': []}
        issues = schema_issues(request)
        self.assertEqual(issues[0]['validator'], 'oneOf')
        self.assertEqual(issues[0]['unexpectedFields'], ['claims', 'viewPlan'])
        self.assertEqual(issues[0]['path'], '/claims')
        self.assertIn('repairs', issues[0]['message'])
        self.assertNotIn(str(request), issues[0]['message'])


class ServiceFeedbackTests(PresentationCase):
    async def test_fields_error_lists_real_fields_and_retry_reads_capture_refs(self):
        cap, selectors, scope = saved_fixture(self.store)
        built = await self.service.build([cap], selectors, scope, [], 'build')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.inspect(section='facts', analysisRef=built['analysisRef'], fields=['factId', 'captureRef'])
        self.assertEqual(caught.exception.code, 'INVALID_FIELDS')
        self.assertEqual(caught.exception.details['fields'], ['captureRef'])
        self.assertIn('captureRefs', caught.exception.details['availableFields'])
        actual = await self.service.inspect(section='facts', analysisRef=built['analysisRef'], fields=['captureRefs'])
        self.assertEqual(actual['items'][0]['captureRefs'], [cap])
        self.assertIn('factId', actual['items'][0])

    async def test_repair_preview_remains_draft_preserves_presentation_and_can_be_published(self):
        cap, selectors, scope = saved_fixture(self.store)
        built = await self.service.build([cap], selectors, scope, [], 'build')
        ref, sid = built['analysisRef'], built['scope']['scopeId']
        fact = self.store.get_analysis(OWNER, ref)['facts'][0]['factId']
        claims = [{'claimId': 'c', 'scopeId': sid, 'kind': 'computed', 'parts': [{'valueRef': fact}]}]
        views = [{'viewId': 'v', 'scopeId': sid, 'type': 'BAR_VALUES', 'valueRefs': [fact]}]
        presentation = {'labels': [{'valueRef': fact, 'role': 'measure', 'label': '合成指标'}]}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(ref, claims, views, 'bad', 0, presentation=presentation, previewOnly=True)
        self.assertEqual(caught.exception.code, 'CLAIM_KIND')
        repair = caught.exception.details['repairRef']
        result = await self.service.publish(ref, requestKey='preview-repair', expectedBaseRevision=0,
            repairRef=repair, repairs=[{'op': 'replace', 'path': '/claims/0/kind', 'value': 'fact'}], previewOnly=True)
        self.assertTrue(result['previewOnly'])
        self.assertIn('diagnostics', result)
        self.assertEqual(self.store.get_analysis(OWNER, ref)['publicationState'], 'DRAFT')
        final = await self.service.publish(ref, requestKey='commit', expectedBaseRevision=0,
            repairRef=result['repairRef'], repairs=[])
        self.assertTrue(final['delivery']['registered'])
        saved = self.store.get_analysis(OWNER, ref)
        self.assertEqual(saved['viewPlan'], views)
        self.assertEqual(saved['presentation'], presentation)

    async def test_wrapper_error_runs_through_tool_without_publishing(self):
        chunk = await AnalysisPublish(self.service).call(arguments={'claims': [], 'viewPlan': []})
        response = json.loads(chunk.content[0].text)
        self.assertEqual(response['error']['code'], 'INVALID_PUBLICATION')
        self.assertEqual(response['error']['details']['path'], '/arguments')
