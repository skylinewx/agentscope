from copy import deepcopy
from unittest import TestCase

from analysis_depth_test import chain
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.diagrams import relation_scenes
from analysis_extension.comparison_scenes import comparison_scenes
from analysis_extension.check_visibility import show_checks
from analysis_extension.display_review import review
from analysis_extension.report_scenes import relationship


class DifferenceVisibilityTests(TestCase):
    def artifact(self, kind='FORMULA', **options):
        facts = chain()
        scope = {'scopeId': 'scope', 'coverage': 'UNKNOWN'}
        values = calculate(facts, scope, [{'id':'f','operator':'FORMULA_CHECK','inputRefs':['root']}], RuleRegistry())
        return {'facts':facts, 'derivedValues':values, 'scope':scope, 'technicalDetails':False,
                'presentation':{}, 'viewPlan':[{'viewId':'v','scopeId':'scope','type':kind,
                                               'valueRefs':['derived_f'], 'nodeRefs':['subtotal'], **options}]}

    def test_mismatch_is_visible_without_technical_details_and_preserves_all_values(self):
        a = self.artifact()
        before = deepcopy(a)
        scene = relation_scenes(a)[0]
        check = a['derivedValues'][0]['formulaTrace']['nodes']
        node = next(n for n in check if n['factRef']=='subtotal')
        self.assertEqual(node['localCheck']['difference'], '-7')
        self.assertEqual(scene['relationship']['result']['note'], '原值')
        self.assertEqual([c['display'] for c in scene['relationship']['checks']], ['107','-7'])
        self.assertIn('本层计算值', relationship(scene))
        self.assertIn('本层差额', relationship(scene))
        self.assertEqual(a, before)

    def test_explicit_false_is_respected_and_preview_points_to_hidden_difference(self):
        a = self.artifact(showChecks=False)
        scenes = relation_scenes(a)
        self.assertEqual(scenes[0]['relationship']['checks'], [])
        warnings = review(a, {'scenes':scenes,'tables':[]})['warnings']
        warning = next(w for w in warnings if w['code']=='FORMULA_DIFFERENCE_HIDDEN')
        self.assertEqual(warning['nodeRef'], 'subtotal')
        self.assertEqual(warning['difference'], '-7')
        self.assertEqual(a['viewPlan'][0]['showChecks'], False)

    def test_local_match_does_not_show_recursive_difference_as_local_mismatch(self):
        a = self.artifact(nodeRefs=['root'])
        scene = relation_scenes(a)[0]
        self.assertEqual(scene['relationship']['checks'], [])
        self.assertEqual(scene['relationship']['result']['note'], '')
        self.assertFalse(show_checks(a, {}, {'difference':None}))
        self.assertFalse(show_checks(a, {}, {'difference':'0.000000'}))
        self.assertTrue(show_checks(a, {}, {'difference':'0.000000000000001'}))

    def test_waterfall_adds_comparison_only_when_needed_or_explicitly_selected(self):
        a = self.artifact('WATERFALL')
        default = comparison_scenes(a)[0]
        a['viewPlan'][0]['showChecks'] = False
        hidden = comparison_scenes(a)[0]
        self.assertEqual(len(default['rowKeys']), len(hidden['rowKeys']) + 2)
        text = ' '.join(p['text'] for p in default['primitives'] if p['kind']=='text')
        self.assertIn('差额', text)
        self.assertIn('来源值', ''.join(text.split()))

    def test_hierarchy_difference_uses_the_same_explicit_choice(self):
        a = self.artifact()
        self.assertTrue(show_checks(a, {}, {'childSum':'30','difference':'-10'}))
        self.assertFalse(show_checks(a, {'showChecks':False}, {'difference':'-10'}))
        self.assertTrue(show_checks(a, {'showChecks':True}, {'difference':'0'}))
