"""Narrative placement and independent chart plans through real publication."""
from copy import deepcopy
from html.parser import HTMLParser

from analysis_presentation_fixtures import PresentationCase
import analysis_model_presentation_test as helpers
from analysis_fixtures import OWNER
from analysis_extension.models import AnalysisError
from analysis_extension.presentation import project
from analysis_extension.report import render
from analysis_extension.presentation_options import validate_presentation


class ClaimsParser(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.indexes=[]; self.widths=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if 'data-claim-index' in attrs: self.indexes.append(int(attrs['data-claim-index']))
        if 'data-width' in attrs: self.widths.append(attrs['data-width'])


class CompositionTests(PresentationCase):
    build=helpers.ModelPresentationTests.build
    plan=helpers.ModelPresentationTests.plan

    def claims(self):
        return [{'claimId': f'claim-{i}', 'kind':'fact', 'scopeId':self.sid,
                 'parts':[{'text':f'**判断 {i}**：'}, {'valueRef':ref}]} for i,ref in enumerate(self.refs[:3])]

    async def release(self, plans, options, claims=None):
        result=await self.service.publish(self.ref, self.claims() if claims is None else claims, plans, 'publish',0,presentation=options)
        self.document=self.delivery.view(result['delivery']['path'])
        return self.document, render(self.document)

    async def test_conclusions_render_once_in_model_selected_positions(self):
        await self.build()
        original=deepcopy(self.artifact['facts'])
        plans=[self.plan('BAR_VALUES',groupId='a'), self.plan(groupId='b')]
        options={'summaryPosition':'start','summaryTitle':'先看结论', 'reportTitle':'本期业务报告', 'groups':[
            {'groupId':'a','title':'主题甲','width':'half','claimRefs':['claim-1'],'claimsPosition':'before'},
            {'groupId':'b','title':'主题乙','width':'half','claimRefs':['claim-2'],'claimsPosition':'after'}]}
        view,html=await self.release(plans,options)
        self.assertEqual(view['summaryClaimIndexes'],[0])
        self.assertEqual([s['claimIndexes'] for s in view['sections']],[[1],[2]])
        self.assertEqual(ClaimsParser(html).indexes,[0,1,2])
        self.assertEqual(ClaimsParser(html).widths,['half','half'])
        self.assertLess(html.index('先看结论</h2>'),html.index('主题甲</h2>'))
        self.assertLess(html.index('data-claim-index="1"'),html.index('<svg'))
        self.assertLess(html.index('<table>'),html.index('data-claim-index="2"'))
        self.assertIn('<h1 id="report-title">本期业务报告</h1>',html)
        self.assertIn('<strong>判断 0</strong>',html)
        self.assertEqual(self.store.get_analysis(OWNER,self.ref)['facts'],original)

    async def test_default_end_and_all_local_avoid_empty_or_duplicate_summary(self):
        await self.build()
        view,html=await self.release([self.plan()],{})
        self.assertEqual(view['summaryPosition'],'end')
        self.assertLess(html.index('<table>'),html.index('id="summary-title"'))
        modified=deepcopy(self.store.get_analysis(OWNER,self.ref))
        modified['presentation']={'groups':[{'groupId':'a','title':'本主题解读','claimRefs':[c['claimId'] for c in modified['claims']]}]}
        modified['viewPlan'][0]['groupId']='a'
        rendered=render(project(modified,self.rules))
        self.assertNotIn('id="summary-title"',rendered)
        self.assertEqual(sorted(ClaimsParser(rendered).indexes),[0,1,2])

    async def test_claim_references_reject_unknown_duplicate_and_unrendered_groups(self):
        await self.build()
        views=[self.plan(groupId='a')]
        for refs,group in [(['unknown'],'a'),(['claim-0','claim-0'],'a'),(['claim-0'],'unused')]:
            with self.subTest(refs=refs,group=group),self.assertRaises(AnalysisError) as error:
                validate_presentation(self.artifact,{'groups':[{'groupId':group,'title':'主题','claimRefs':refs}]},views,self.claims())
            self.assertIn('/presentation/groups/0/claimRefs',error.exception.details['path'])
        with self.assertRaises(AnalysisError):
            validate_presentation(self.artifact,{'groups':[{'groupId':'a','title':'甲','claimRefs':['claim-0']},{'groupId':'b','title':'乙','claimRefs':['claim-0']}]},
                                  views+[self.plan(viewId='b',groupId='b')],self.claims())

    async def test_invalid_composition_returns_exact_repair_path_and_preserves_charts(self):
        await self.build()
        plans=[self.plan('BAR_VALUES',groupId='a')]
        with self.assertRaises(AnalysisError) as error:
            await self.service.publish(self.ref,self.claims(),plans,'bad',0,presentation={'groups':[{'groupId':'a','title':'主题','claimRefs':['wrong']}]})
        self.assertEqual(error.exception.details['path'],'/presentation/groups/0/claimRefs/0')
        repaired=await self.service.publish(self.ref,requestKey='repair',expectedBaseRevision=0,repairRef=error.exception.details['repairRef'],
            repairs=[{'op':'replace','path':'/presentation/groups/0/claimRefs/0','value':'claim-1'}])
        view=self.delivery.view(repaired['delivery']['path'])
        self.assertEqual(len(view['scenes']),1)
        self.assertEqual(view['sections'][0]['claimIndexes'],[1])
        self.assertEqual(view['summaryClaimIndexes'],[0,2])

    async def test_multi_comparison_uses_distinct_metrics_orders_titles_and_labels(self):
        await self.build(formula=True)
        income=[f['factId'] for f in self.artifact['facts'] if f['measureCode']=='收入']
        cost=[f['factId'] for f in self.artifact['facts'] if f['measureCode']=='成本']
        plans=[self.plan('BAR_VALUES',viewId='income',valueRefs=income,title='第一项比较',numberFormat={'decimalPlaces':1}),
               self.plan('BAR_VALUES',viewId='cost',valueRefs=list(reversed(cost)),title='第二项比较',collapsed=True)]
        view,html=await self.release(plans,{},claims=[])
        self.assertEqual([s['viewId'] for s in view['scenes']],['income','cost'])
        def amounts(scene):return [p['text'] for p in scene['primitives'] if p['kind']=='text' and p['tone']=='value']
        self.assertEqual(amounts(view['scenes'][0]),['10.0','50.0'])
        self.assertEqual(amounts(view['scenes'][1]),['30','15'])
        self.assertTrue(view['scenes'][1]['collapsed'])
        self.assertEqual(html.count('<svg'),2)
        self.assertNotEqual(view['scenes'][0]['zeroBaseline'],None)
        self.assertEqual([b['sceneIndexes'] for s in view['sections'] for b in s['blocks']],[[0],[1]])

    async def test_raw_and_rank_views_can_coexist_with_independent_order(self):
        await self.publish(values=('12','-5','20'))
        artifact=deepcopy(self.artifact)
        artifact['viewPlan']=[v for v in artifact['viewPlan'] if v['type']!='ORDER']
        facts=[f['factId'] for f in artifact['facts']]
        artifact['viewPlan'] += [{'viewId':'raw','scopeId':artifact['scope']['scopeId'],'type':'BAR_VALUES','valueRefs':list(reversed(facts))}]
        view=project(artifact,self.rules)
        self.assertEqual([s['type'] for s in view['scenes']],['BAR_RANK','BAR_VALUES'])
        self.assertNotEqual(view['scenes'][0]['rowKeys'],view['scenes'][1]['rowKeys'])

    async def test_table_mode_and_empty_filtered_chart_keep_local_conclusions(self):
        await self.build(values=('2','3','4'))
        view,html=await self.release([self.plan('BAR_VALUES',groupId='a')],{'groups':[{'groupId':'a','title':'本主题','claimRefs':['claim-1']}]})
        table=render(view,'table')
        self.assertEqual(sorted(ClaimsParser(table).indexes),[0,1,2])
        self.assertNotIn('<svg',table)
        filtered=project(self.store.get_analysis(OWNER,self.ref),self.rules,{'filter':'negative'})
        self.assertEqual(filtered['scenes'],[])
        self.assertEqual(filtered['sections'][0]['claimIndexes'],[1])
        self.assertIn('原发布范围',filtered['claimsScopeLabel'])
        self.assertEqual(sorted(ClaimsParser(render(filtered)).indexes),[0,1,2])

    async def test_titles_and_local_claims_escape_html_without_changing_values(self):
        await self.build()
        options={'reportTitle':'<script>标题</script>','summaryTitle':'<img src=x>', 'groups':[{'groupId':'a','title':'<svg onload=x>','claimRefs':['claim-0']}]}
        view,html=await self.release([self.plan(groupId='a')],options)
        self.assertNotIn('<script>',html)
        self.assertNotIn('<img src=x>',html)
        self.assertIn('&lt;script&gt;标题&lt;/script&gt;',html)
        self.assertEqual(sorted(ClaimsParser(html).indexes),[0,1,2])
