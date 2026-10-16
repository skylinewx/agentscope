"""Regressions for calculation labels and actionable publication repairs."""
from copy import deepcopy
from unittest import TestCase

from analysis_depth_test import chain
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.diagrams import relation_scenes
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.models import AnalysisError
from analysis_extension.publication import repair_request
from analysis_extension.tools import AnalysisPublish, CLAIM, VIEW, TEXT
from analysis_extension.validation import render_parts, selected_value


class ReviewContractTests(TestCase):
    def request(self):
        return {'analysisRef': 'analysis:1', 'expectedBaseRevision': 0, 'claims': [],
                'viewPlan': [{'viewId': 'keep'}], 'presentation': {'labels': []}}

    def test_array_append_is_sequential_and_preserves_the_saved_proposal(self):
        saved = self.request()
        before = deepcopy(saved)
        edits = [{'op': 'add', 'path': '/presentation/labels/-', 'value': {'label': label}}
                 for label in ('first', 'last')]
        edits.insert(1, {'op': 'add', 'path': '/presentation/labels/0', 'value': {'label': 'inserted'}})
        result = repair_request(saved, saved['analysisRef'], edits)
        self.assertEqual([item['label'] for item in result['presentation']['labels']], ['inserted', 'first', 'last'])
        self.assertEqual(result['viewPlan'], before['viewPlan'])
        result['presentation']['labels'][1]['label'] = 'changed'
        self.assertEqual(edits[0]['value']['label'], 'first')
        self.assertEqual(saved, before)

    def test_invalid_append_and_paths_are_atomic_with_specific_reasons(self):
        cases = [('replace', '/presentation/labels/-', 'append requires add'),
                 ('remove', '/presentation/labels/-', 'append requires add'),
                 ('add', '/presentation/labels/-/label', 'invalid array index'),
                 ('add', '/viewPlan/01/title', 'invalid array index'),
                 ('add', '/viewPlan/2/title', 'index out of range'),
                 ('add', '/facts/-', 'read-only field'),
                 ('add', '/presentation/missing/-', 'missing field')]
        saved = self.request()
        before = deepcopy(saved)
        for op, path, reason in cases:
            edit = {'op': op, 'path': path, **({'value': 'bad'} if op != 'remove' else {})}
            with self.subTest(path=path, op=op), self.assertRaises(AnalysisError) as caught:
                repair_request(saved, saved['analysisRef'], [
                    {'op': 'add', 'path': '/presentation/labels/-', 'value': {'label': 'temporary'}}, edit])
            self.assertEqual(caught.exception.details['reason'], reason)
            self.assertEqual(caught.exception.details['path'], '/repairs/1')
            self.assertEqual(caught.exception.details['targetPath'], path)
            self.assertEqual(saved, before)

    def test_field_guidance_does_not_leak_to_unrelated_identity_fields(self):
        value = CLAIM['properties']['parts']['items']['oneOf'][1]['properties']
        self.assertNotIn('description', TEXT)
        self.assertNotIn('description', AnalysisPublish.input_schema['properties']['requestKey'])
        self.assertIn('groupId', VIEW['properties']['groupId']['description'])
        self.assertIn('factId', value['valueRef']['description'])
        self.assertIn('不继承', value['format']['description'])
        self.assertNotIn('description', VIEW['properties']['numberFormat'])

    def test_formula_check_labels_identify_local_basis_and_keep_values(self):
        facts = chain()
        scope = {'scopeId': 'scope', 'coverage': 'UNKNOWN'}
        derived = calculate(facts, scope, [{'id': 'f', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}], RuleRegistry())
        artifact = {'facts': facts, 'derivedValues': derived, 'scope': scope, 'technicalDetails': False,
                    'viewPlan': [{'viewId': 'path', 'scopeId': 'scope', 'type': 'FORMULA',
                                  'valueRefs': ['derived_f'], 'nodeRefs': ['root'], 'showChecks': True}]}
        before = deepcopy(artifact)
        checks = relation_scenes(artifact)[0]['relationship']['checks']
        self.assertEqual([c['label'] for c in checks], ['本层计算值', '本层差额（原值 − 本层计算值）'])
        self.assertEqual([c['display'] for c in checks], ['-8', '0'])
        self.assertEqual(selected_value(derived[0]), '-1')
        self.assertEqual(artifact, before)

    def test_number_format_is_explicit_and_never_changes_source_values(self):
        fact = {'factId': 'f', 'value': '12345.6700', 'valueState': 'PRESENT', 'unit': ''}
        formatted, refs = render_parts({'f': fact}, [{'text': '结果 '},
            {'valueRef': 'f', 'format': {'grouping': True, 'decimalPlaces': 2}}])
        self.assertEqual(formatted, '结果 12,345.67')
        self.assertEqual(refs, ['f'])
        self.assertEqual(render_parts({'f': fact}, [{'valueRef': 'f'}])[0], '12345.6700')
        self.assertEqual(fact['value'], '12345.6700')


class PublicationValueLocationTests(PresentationCase):
    async def build(self):
        cap, selections, scope = mixed_fixture(self.store)
        result = await self.service.build([cap], selections, scope,
            [{'id': 'check', 'operator': 'FORMULA_CHECK', 'select': {'cellKey': '2:1', 'objectId': 'object-0'}}], 'build')
        self.ref, self.sid = result['analysisRef'], result['scope']['scopeId']
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.fact = next(f['factId'] for f in self.artifact['facts'] if f['objectId'] == 'object-0' and f['cellKey'] == '2:1')

    def view(self, vid='values', **extra):
        return {'viewId': vid, 'scopeId': self.sid, 'type': 'TABLE', 'valueRefs': [self.fact], **extra}

    async def test_errors_locate_claim_view_and_node_parts_and_offer_actual_fields(self):
        await self.build()
        bad = {'valueRef': self.fact, 'field': 'localValue'}
        claims = [{'claimId': 'claim', 'scopeId': self.sid, 'kind': 'computed',
                   'parts': [{'text': '本层 '}, bad]}]
        views = [self.view(descriptionParts=[bad]), self.view('formula', type='FORMULA', valueRefs=['derived_check'],
            nodeDescriptions=[{'nodeRef': self.fact, 'parts': [{'text': '节点 '}, bad]}])]
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, claims, views, 'bad', 0, previewOnly=True)
        issues = caught.exception.details['issues']
        self.assertEqual({i['path'] for i in issues}, {
            '/claims/0/parts/1', '/viewPlan/0/descriptionParts/0', '/viewPlan/1/nodeDescriptions/0/parts/1'})
        for issue in issues:
            self.assertEqual(issue['valueRef'], self.fact)
            self.assertEqual(issue['valueField'], 'localValue')
            self.assertEqual(issue['availableFields'], ['value', 'unit'])
            self.assertIn('derivedId', issue['hint'])
        correct = {'valueRef': 'derived_check', 'nodeRef': self.fact, 'field': 'localValue'}
        result = await self.service.publish(self.ref, requestKey='correct', expectedBaseRevision=0,
            repairRef=caught.exception.details['repairRef'], previewOnly=True,
            repairs=[{'op': 'replace', 'path': i['path'], 'value': correct} for i in issues])
        self.assertTrue(result['previewOnly'])
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')

    async def test_group_value_error_has_full_path_and_unknown_node_lists_allowed_nodes(self):
        await self.build()
        view = self.view(groupId='group')
        presentation = {'groups': [{'groupId': 'group', 'title': '主题',
            'descriptionParts': [{'valueRef': 'derived_check', 'nodeRef': 'not-a-node', 'field': 'localValue'}]}]}
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, [], [view], 'bad-group', 0, presentation=presentation, previewOnly=True)
        details = caught.exception.details
        self.assertEqual(details['path'], '/presentation/groups/0/descriptionParts/0')
        self.assertIn(self.fact, details['allowedNodeRefs'])
        self.assertEqual(details['nodeRef'], 'not-a-node')

    async def test_append_still_obeys_schema_limits_and_preserves_original_draft(self):
        await self.build()
        original = [{'text': '说明'}] * 99 + [{'valueRef': self.fact}]
        claims = [{'claimId': 'c', 'scopeId': self.sid, 'kind': 'fact', 'parts': original}]
        preview = await self.service.publish(self.ref, claims, [self.view()], 'preview', 0, previewOnly=True)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.publish(self.ref, requestKey='too-many', expectedBaseRevision=0,
                repairRef=preview['repairRef'], repairs=[{'op': 'add', 'path': '/claims/0/parts/-', 'value': {'text': 'extra'}}])
        self.assertEqual(caught.exception.code, 'INVALID_PUBLICATION')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['publicationState'], 'DRAFT')
        published = await self.service.publish(self.ref, requestKey='original', expectedBaseRevision=0,
            repairRef=preview['repairRef'], repairs=[])
        self.assertEqual(published['publicationState'], 'PUBLISHED')
        self.assertEqual(self.store.get_analysis(OWNER, self.ref)['claims'][0]['parts'], original)
