"""Publication recovery preserves model labels and exposes calculation bases."""
from copy import deepcopy
from unittest import TestCase

from analysis_depth_test import chain
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.comparison_scenes import comparison_scenes
from analysis_extension.display_review import review
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.validation import selected_value


class FormulaPublicationBasisTests(TestCase):
    def artifact(self, **options):
        facts = chain()
        scope = {'scopeId': 'scope', 'coverage': 'UNKNOWN'}
        values = calculate(facts, scope, [{'id': 'f', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}], RuleRegistry())
        return {'facts': facts, 'derivedValues': values, 'scope': scope, 'technicalDetails': False,
                'presentation': {}, 'viewPlan': [{'viewId': 'path', 'scopeId': 'scope', 'type': 'WATERFALL',
                    'valueRefs': ['derived_f'], 'nodeRefs': ['subtotal', 'root'], 'showChecks': False, **options}]}

    def warnings(self, artifact):
        scenes = comparison_scenes(artifact)
        return review(artifact, {'scenes': scenes, 'tables': []})['warnings']

    def test_target_and_explicit_node_bindings_keep_three_distinct_meanings(self):
        value = self.artifact()['derivedValues'][0]
        self.assertEqual(selected_value(value, 'sourceValue'), '-8')
        self.assertEqual(selected_value(value, 'localValue'), '-8')
        self.assertEqual(selected_value(value), '-1')
        self.assertEqual(selected_value(value, 'localDifference'), '0')
        self.assertEqual(selected_value(value, 'difference'), '-7')
        self.assertEqual(selected_value(value, 'sourceValue', 'subtotal'), '100')
        self.assertEqual(selected_value(value, 'localValue', 'subtotal'), '107')
        self.assertEqual(selected_value(value, 'value', 'subtotal'), '107')

    def test_preview_exposes_basis_conflict_without_rewriting_model_content(self):
        artifact = self.artifact(nodeDescriptions=[{'nodeRef': 'root', 'parts': [
            {'text': '逐层结果 '}, {'valueRef': 'derived_f', 'nodeRef': 'root', 'field': 'localValue'}]}])
        before = deepcopy(artifact)
        warning = next(w for w in self.warnings(artifact) if w['code'] == 'FORMULA_CALCULATION_BASES_DIFFER')
        self.assertEqual((warning['nodeRef'], warning['valueRef']), ('root', 'derived_f'))
        self.assertEqual((warning['sourceValue'], warning['localValue'], warning['value']), ('-8', '-8', '-1'))
        self.assertEqual((warning['localDifference'], warning['difference']), ('0', '-7'))
        self.assertEqual(comparison_scenes(artifact)[1]['description'], '逐层结果 -8')
        self.assertEqual(artifact, before)

    def test_waterfall_transition_names_both_nodes_and_actual_values(self):
        artifact = self.artifact()
        warning = next(w for w in self.warnings(artifact) if w['code'] == 'WATERFALL_SOURCE_TRANSITION')
        self.assertEqual((warning['nodeRef'], warning['consumerNodeRef']), ('subtotal', 'root'))
        self.assertEqual((warning['sourceValue'], warning['localValue'], warning['localDifference']), ('100', '107', '-7'))
        # A visible source bridge or an unselected lower layer needs no transition warning.
        for options in ({'showChecks': True}, {'nodeRefs': ['root']}, {'nodeRefs': ['subtotal']}):
            with self.subTest(options=options):
                self.assertNotIn('WATERFALL_SOURCE_TRANSITION', [w['code'] for w in self.warnings(self.artifact(**options))])

    def test_matching_chain_does_not_warn_about_calculation_bases(self):
        artifact = self.artifact()
        artifact['facts'][1]['value'] = '107'
        artifact['facts'][0]['value'] = '-1'
        artifact['derivedValues'] = calculate(artifact['facts'], artifact['scope'],
            [{'id': 'f', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}], RuleRegistry())
        codes = [w['code'] for w in self.warnings(artifact)]
        self.assertNotIn('FORMULA_CALCULATION_BASES_DIFFER', codes)
        self.assertNotIn('WATERFALL_SOURCE_TRANSITION', codes)


class PresentationRecoveryTests(PresentationCase):
    async def test_preview_publish_inspect_and_rebuild_preserve_explicit_labels(self):
        cap, selections, scope = saved_fixture(self.store, ('7', '9'), unit='')
        first = await self.service.build([cap], selections, scope, [], 'initial')
        artifact = self.store.get_analysis(OWNER, first['analysisRef'])
        refs = [fact['factId'] for fact in artifact['facts']]
        presentation = {'labels': [{'valueRef': refs[0], 'role': 'measure', 'label': '结果简称'},
                                  {'valueRef': refs[1], 'role': 'object', 'label': '对象简称'}]}
        view = {'viewId': 'values', 'scopeId': first['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': refs}
        preview = await self.service.publish(first['analysisRef'], [], [view], 'preview', first['baseRevision'],
                                             presentation=presentation, previewOnly=True)
        self.assertEqual(preview['summary']['presentation'], presentation)
        published = await self.service.publish(first['analysisRef'], requestKey='publish', expectedBaseRevision=first['baseRevision'],
                                               repairRef=preview['repairRef'], repairs=[])
        inspected = await self.service.inspect(section='summary', analysisRef=first['analysisRef'])
        self.assertEqual(published['summary']['presentation'], presentation)
        self.assertEqual(inspected['summary']['presentation'], presentation)
        recovered = deepcopy(inspected['summary']['presentation'])
        second = await self.service.build([cap], selections, scope, [], 'rebuild', baseAnalysisRef=first['analysisRef'])
        # Reuse is explicit and does not make Build impose old titles or layouts.
        self.assertEqual(self.store.get_analysis(OWNER, second['analysisRef']).get('presentation', {}), {})
        view['scopeId'] = second['scope']['scopeId']
        released = await self.service.publish(second['analysisRef'], [], [view], 'republish', second['baseRevision'], presentation=recovered)
        document = self.delivery.view(released['delivery']['path'])
        self.assertEqual(document['tables'][0]['columns'][0]['label'], '结果简称')
        self.assertEqual(document['tables'][0]['rows'][1]['label'], '对象简称')
        self.assertEqual(self.store.get_analysis(OWNER, first['analysisRef'])['presentation'], presentation)
