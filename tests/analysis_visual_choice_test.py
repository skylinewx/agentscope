from copy import deepcopy
from unittest import TestCase
from analysis_extension.formula_contributions import contributions
from analysis_extension.comparison_scenes import comparison_scenes
from analysis_extension.diagrams import relation_scenes
from analysis_extension.report_scenes import relationship
from analysis_extension.models import canonical
from analysis_extension.validation import validate_views
from analysis_extension.models import AnalysisError
from analysis_presentation_fixtures import PresentationCase
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER


class ContributionTests(TestCase):
    def test_negative_operand_and_coefficient_produce_correct_impact(self):
        facts = {'a': {'valueState':'PRESENT','value':'10'}, 'b':{'valueState':'PRESENT','value':'-3'}}
        node = {'expression':'A1-2*B1','dependencies':[{'address':'A1','factRef':'a'},{'address':'B1','factRef':'b'}]}
        result=contributions(node,facts,{})
        self.assertEqual(result['b']['operator'],'−')
        self.assertEqual(result['b']['coefficient'],'2')
        self.assertEqual(result['b']['contribution'],'6')
        self.assertEqual(result['b']['tone'],'positive')
        self.assertEqual(result['b']['weight'],60)
        rendered=relationship({'type':'FORMULA','relationship':{'equation':'A − 2 × B = C',
            'inputs':[{'key':'b','label':'B','display':'-3',**result['b']}],
            'result':{'key':'c','label':'C','display':'16'},'checks':[],'note':''}})
        self.assertIn('>− 2 ×</span>B',rendered)
        node['expression']='A1*B1'
        self.assertEqual(contributions(node,facts,{}),{})


class VisualChoiceTests(PresentationCase):
    async def artifact(self, kind='FORMULA', **options):
        cap, selectors, scope=mixed_fixture(self.store)
        built=await self.service.build([cap],selectors,scope,[{'id':'formula','operator':'FORMULA_CHECK','select':{'cellKey':'2:1','objectId':'object-0'}}],'build')
        artifact=self.store.get_analysis(OWNER,built['analysisRef'])
        artifact['viewPlan']=[{'viewId':'formula','scopeId':artifact['scope']['scopeId'],'type':kind,'valueRefs':['derived_formula'],**options}]
        return artifact

    async def test_formula_operators_tones_emphasis_and_checks_are_explicit(self):
        a=await self.artifact()
        original=canonical(a['facts'])
        graph=relation_scenes(a)[0]['relationship']
        self.assertEqual(graph['layout'],'compact')
        self.assertEqual(graph['checks'],[])
        self.assertEqual([n['operator'] for n in graph['inputs']],['+','−'])
        self.assertEqual([n['tone'] for n in graph['inputs']],['positive','negative'])
        a['viewPlan'][0].update(showChecks=True,relationLayout='flow',emphasisRefs=[a['facts'][0]['factId']])
        validate_views(a,a['viewPlan'])
        scene=relation_scenes(a)[0]
        self.assertEqual(len(scene['relationship']['checks']),2)
        html=relationship(scene)
        self.assertIn('relation-flow',html)
        self.assertIn('data-emphasis="true"',html)
        self.assertEqual(html.count('class="relation-equation"'),1)
        self.assertEqual(original,canonical(a['facts']))

    async def test_waterfall_connects_contributions_without_forced_audit_rows(self):
        a=await self.artifact('WATERFALL')
        original=canonical(a['derivedValues'])
        scene=comparison_scenes(a)[0]
        labels=[p['text'] for p in scene['primitives'] if p['kind']=='text']
        self.assertNotIn('来源值',labels)
        self.assertNotIn('差额（来源 − 计算）',labels)
        self.assertEqual(len(scene['rowKeys']),3)
        self.assertTrue(any(p['kind']=='line' and p.get('x1')!=24 for p in scene['primitives']))
        a['viewPlan'][0].update(showChecks=True,orientation='horizontal')
        self.assertEqual(len(comparison_scenes(a)[0]['rowKeys']),5)
        self.assertEqual(original,canonical(a['derivedValues']))

    async def test_unsupported_layout_and_unknown_emphasis_are_rejected(self):
        a=await self.artifact()
        for option in ({'orientation':'vertical'},{'relationLayout':'invented'},{'emphasisRefs':['missing']}):
            with self.assertRaises(AnalysisError): validate_views(a,[{**a['viewPlan'][0],**option}])
