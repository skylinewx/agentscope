"""Model-authored narration and views, source-bound formula follow-ups."""
from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
import json

from jsonschema import Draft202012Validator
from analysis_action_fixtures import ActionCase
from analysis_closeout_test import mixed_fixture
from analysis_fixtures import OWNER, saved_fixture
from analysis_extension.models import AnalysisError
from analysis_extension.actions import followup_text, outcome
from analysis_extension.presentation import project
from analysis_extension.validation import validate_claims, validate_views, summary
from analysis_extension.tools import AnalysisPublish


class ExpressionTests(ActionCase):
    async def simple(self, operations=None, tag='simple'):
        refs, selections, scope = self.capture(values=('12','-8','0'), tag=tag, metric='READING', unit='件')
        draft = await self.service.build(refs, selections, scope, operations or [], 'build-' + tag)
        return draft, self.store.get_analysis(OWNER, draft['analysisRef'])

    async def publish_views(self, draft, views, claims=None):
        result = await self.service.publish(draft['analysisRef'], claims or [], views, 'publish-' + draft['analysisRef'], draft['baseRevision'])
        return self.delivery.view(result['delivery']['path'])

    async def test_narrative_preserves_model_text_order_dates_and_bound_values(self):
        draft, artifact = await self.simple([{'id':'n','operator':'COUNT'}])
        claim = {'claimId':'narrative', 'scopeId':draft['scope']['scopeId'], 'kind':'computed',
                 'parts':[{'text':'2026 年本轮所选范围：'}, {'valueRef':'derived_n','field':'observed'},
                          {'text':'。其中需关注的来源读数是 '}, {'valueRef':artifact['facts'][1]['factId']},
                          {'text':'；这里只描述来源结果。'}]}
        result = validate_claims(artifact, [claim])[0]
        self.assertEqual(result['support'], 'REFERENCES_VALID')
        self.assertEqual(result['rendered'], '2026 年本轮所选范围：3。其中需关注的来源读数是 -8；这里只描述来源结果。')
        view = await self.publish_views(draft, [], [claim])
        self.assertEqual(view['claims'], [result['rendered']])

    async def test_narrative_keeps_reference_kind_size_and_hypothesis_guards(self):
        draft, artifact = await self.simple()
        base = {'claimId':'x', 'scopeId':draft['scope']['scopeId'], 'kind':'fact'}
        for parts in ([{'text':'没有引用的断言'}], [{'text':'解释：'},{'valueRef':'absent'}]):
            with self.subTest(parts=parts), self.assertRaises(AnalysisError):
                validate_claims(artifact, [{**base,'parts':parts}])
        with self.assertRaisesRegex(AnalysisError, '证据'):
            validate_claims(artifact, [{**base,'kind':'hypothesis','parts':[{'text':'可能存在原因'}]}])
        with self.assertRaisesRegex(AnalysisError, '容量'):
            validate_claims(artifact, [{**base,'parts':[{'text':'长' * 2000}] * 9 + [{'valueRef':artifact['facts'][0]['factId']}]}])
        view = await self.publish_views(draft, [], [{**base,'parts':[{'text':'<script>unsafe()</script>'}, {'valueRef':artifact['facts'][0]['factId']}]}])
        report = self.delivery.render_report(draft['analysisRef'], 'escaped')
        html = (self.root / 'workspace' / report['path'].removeprefix('./')).read_text('utf-8')
        self.assertIn('&lt;script&gt;unsafe()&lt;/script&gt;', html)
        self.assertNotIn('<script>unsafe()', html)
        self.assertIn('<script>unsafe()', view['claims'][0])  # React renders text, never raw HTML.

    async def test_count_metric_defaults_to_one_scalar_not_four_categories(self):
        draft, _ = await self.simple([{'id':'n','operator':'COUNT'}])
        view = await self.publish_views(draft, [{'viewId':'total','scopeId':draft['scope']['scopeId'], 'type':'METRIC','valueRefs':['derived_n']}])
        self.assertEqual([(m['label'],m['display']) for m in view['metrics']], [('有效数值','3')])

    async def test_metric_fields_titles_and_order_are_explicit(self):
        draft, artifact = await self.simple([{'id':'n','operator':'COUNT'}])
        common = {'scopeId':draft['scope']['scopeId'], 'type':'METRIC', 'valueRefs':['derived_n']}
        views = [{**common,'viewId':'negative','field':'negative','title':'需关注的对象'},
                 {**common,'viewId':'all','field':'observed','title':'本次取得'},
                 {**common,'viewId':'positive','field':'positive','title':'正值数量'}]
        view = await self.publish_views(draft, views)
        self.assertEqual([(m['label'],m['display']) for m in view['metrics']], [('需关注的对象','1'),('本次取得','3'),('正值数量','1')])
        self.assertEqual(len({m['valueRef'] for m in view['metrics']}), 3)
        for bad in ({**common,'viewId':'bad','field':'invented'}, {**views[0],'valueRefs':[artifact['facts'][0]['factId']]}, {**views[0],'type':'TABLE'}):
            with self.assertRaises(AnalysisError): validate_views(artifact,[bad])

    async def test_filtered_mixed_metric_count_keeps_selected_fields_and_titles(self):
        ref, selections, scope = mixed_fixture(self.store)
        draft = await self.service.build([ref], selections, scope, [{'id':'n','operator':'COUNT','select':{'cellKey':'2:1'}}], 'mixed-count')
        plans = [{'viewId':'n','scopeId':draft['scope']['scopeId'],'type':'METRIC','valueRefs':['derived_n'],'field':'observed','title':'当前成员'}]
        view = await self.publish_views(draft, plans)
        column = next(c['key'] for c in view['columns'] if c['label']=='结果')
        filtered = self.delivery.project_view(draft['analysisRef'], {'filter':'negative','columnKey':column})
        self.assertEqual(filtered['total'], 1)
        self.assertEqual([(m['label'],m['display']) for m in filtered['metrics']], [('当前成员','1')])
        empty = self.delivery.project_view(draft['analysisRef'], {'filter':'zero','columnKey':column})
        self.assertEqual(empty['metrics'][0]['display'], '0')

    async def test_calculating_multiple_orders_does_not_choose_display_order(self):
        draft, _ = await self.simple([{'id':'asc','operator':'SORT','direction':'asc'},{'id':'desc','operator':'SORT','direction':'desc'}])
        view = await self.publish_views(draft, [])
        self.assertEqual(view['rows'], [])
        self.assertEqual(view['tables'], [])

    async def test_explicit_order_selects_second_result_for_tables_and_chart(self):
        draft, artifact = await self.simple([{'id':'asc','operator':'SORT','direction':'asc'},{'id':'desc','operator':'SORT','direction':'desc'}])
        views = [{'viewId':'order','scopeId':draft['scope']['scopeId'],'type':'ORDER','valueRefs':['derived_desc']},
                 {'viewId':'chart','scopeId':draft['scope']['scopeId'],'type':'BAR_VALUES','valueRefs':[f['factId'] for f in artifact['facts']]}]
        views.append({'viewId':'table', 'scopeId':draft['scope']['scopeId'], 'type':'TABLE', 'valueRefs':[f['factId'] for f in artifact['facts']]})
        view = await self.publish_views(draft, views)
        self.assertEqual([r['exact'] for r in view['rows']], ['12','0','-8'])
        self.assertEqual(comparison(view)['rowKeys'], [r['rowKey'] for r in view['rows']])
        self.assertEqual([r['label'] for r in view['tables'][0]['rows']], [r['label'] for r in view['rows']])

    async def test_explicit_fact_order_retains_all_member_details_and_rejects_ambiguity(self):
        ref, selections, scope = mixed_fixture(self.store)
        draft = await self.service.build([ref], selections, scope, [], 'manual-order')
        artifact = self.store.get_analysis(OWNER, draft['analysisRef'])
        selected = next(f for f in artifact['facts'] if f['objectId']=='object-1')
        plan = {'viewId':'manual','scopeId':draft['scope']['scopeId'],'type':'ORDER','valueRefs':[selected['factId']]}
        view = await self.publish_views(draft,[plan, {'viewId':'table','scopeId':draft['scope']['scopeId'],'type':'TABLE','valueRefs':[f['factId'] for f in artifact['facts']]}])
        self.assertEqual([r['label'] for r in view['rows']], ['合成成员 1']*3 + ['合成成员 0']*3)
        published = self.store.get_analysis(OWNER, draft['analysisRef'])
        self.assertEqual(summary(published)['viewPlan'][0]['valueRefs'], [selected['factId']])
        self.assertEqual(summary(published)['factCount'], 6)
        for plans in ([plan,{**plan,'viewId':'duplicate'}], [{**plan,'valueRefs':['unknown']}], [{**plan,'field':'positive'}]):
            with self.assertRaises(AnalysisError): validate_views(artifact, plans)
        self.assertFalse(list(Draft202012Validator(AnalysisPublish.input_schema).iter_errors({
            'analysisRef':draft['analysisRef'],'claims':[], 'viewPlan':[plan], 'requestKey':'schema','expectedBaseRevision':0})))

    def formula_capture(self, tag, missing=False, changed=False, extra=False, mismatch=False):
        base, selectors, scope = mixed_fixture(self.store, 1)
        capture = self.store.get_capture(OWNER, base)
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['executionId'] = 'formula-execution-' + tag
        capture['toolCallId'] = tag
        receipt['evidence'] = []
        form = receipt['data']['stepResults'][1]['formResults'][0]
        runtime = form['runtime']; grid = form['page']
        runtime['observation']['observationId'] = grid['observationId'] = 'observation-' + tag
        runtime['observation']['contentDigest'] = grid['contentDigest'] = 'digest-' + tag
        target = grid['cells'][0]
        if changed: target['formula'] = 'B1+C1'
        if mismatch: target['rawValueJson'] = '"-6"'; target['displayValue'] = '-6'
        if missing:
            grid['cells'] = [c for c in grid['cells'] if c.get('key') != '2:3']
            selectors[0]['cellKeys'].remove('2:3')
        if extra:
            grid['cells'].append({**deepcopy(target),'key':'2:4','column':4,'measureCode':'OTHER','headers':['无关指标'],
                                  'formula':None,'rawValueJson':'"99"','displayValue':'99'})
            grid['cells'].append({'row':0,'column':4,'mapType':'COL_INDEX','displayValue':'D'})
            grid['totalColumns'] = 5
            selectors[0]['cellKeys'].append('2:4')
        receipt['evidence'].append({'evidenceId':'evidence-' + tag,'kind':'FORM_RUNTIME','fact':deepcopy(runtime)})
        capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        ref = self.store.capture(OWNER,capture)
        for selector in selectors: selector['captureRef'] = ref
        scope['population']['captureRef'] = ref
        verification = {'contractVersion':'2.0','status':'SUCCEEDED','context':receipt['context'],'querySessionId':'query-A',
            'data':{'action':'verify','executionId':receipt['executionId'],'verification':{'status':'VALID','evidenceIds':['evidence-' + tag]}}}
        verified = self.store.capture(OWNER, {'resourceId':'fixture-resource','originalToolName':'validate_evidence','captureState':'success',
            'sourceAdapter':deepcopy(capture['sourceAdapter']),
            'request':{'action':'verify','executionId':receipt['executionId'],'contextId':receipt['context']['contextId']},
            'response':{'state':'success','content':[{'type':'text','text':json.dumps(verification)}]}})
        return [ref,verified],selectors,scope

    async def formula_draft(self, tag, action_run=None, base=None, operations=None, **changes):
        refs, selectors, scope = self.formula_capture(tag,**changes)
        if action_run:
            for ref in refs: self.service.actions.capture(ref,'success')
        ops = [{'id':'source','operator':'FORMULA_CHECK','select':{'cellKey':'2:1'}}] if operations is None else operations
        return await self.service.build(refs, selectors, scope, ops, 'build-' + tag, baseAnalysisRef=base, actionRunRef=action_run)

    async def formula_action(self, tag='old', **changes):
        draft = await self.formula_draft(tag, **changes)
        plan = [{'kind':'CALCULATION','derivedRefs':['derived_source']}]
        findings = (await self.service.inspect('findings',draft['analysisRef'],findingPlan=plan))['items']
        action = {'findingRefs':[findings[0]['findingId']], 'check':'VERIFY_COMPOSITION', 'goal':'继续核对本次表内公式',
                  'requiredEvidence':['同一目标和依赖的新来源及核验'], 'expectedDecision':'区分一致、差额和断点', 'priorityBasis':'用户指定'}
        result = await self.service.publish(draft['analysisRef'], [], [], 'publish-' + tag, draft['baseRevision'],findingPlan=plan,actionPlan=[action])
        artifact = self.store.get_analysis(OWNER,draft['analysisRef'])
        actual = artifact['actions'][0]
        self.assertEqual(actual['availability'],'AVAILABLE')
        self.assertEqual(len(actual['policy']['formulaTargets']),1)
        self.service.actions.user_text = followup_text(actual)
        self.service.actions.turn_ref = 'turn-' + tag
        running = await self.service.follow_up(actual['actionRef'],'follow-' + tag)
        self.assertEqual(running['actionRun']['status'],'RUNNING')
        return draft, artifact, running['actionRun']['actionRunId'], result['delivery']['path']

    async def test_source_formula_followup_completes_with_fresh_matching_evidence(self):
        old, _, run, path = await self.formula_action()
        new = await self.formula_draft('new',action_run=run,base=old['analysisRef'])
        await self.service.publish(new['analysisRef'],[],[],'publish-new',new['baseRevision'])
        actual = self.store.get_action_run(OWNER,run)
        self.assertEqual((actual['status'],actual['diagnostics'][-1]['code']),('COMPLETED','FORMULA_CHECKED_MATCH'))
        self.assertIn('一致',self.delivery.view(path)['actionRuns'][0]['resultNote'])

    async def test_source_formula_followup_completes_check_but_reports_difference(self):
        old, _, run, path = await self.formula_action()
        new = await self.formula_draft('new',action_run=run,base=old['analysisRef'],mismatch=True)
        await self.service.publish(new['analysisRef'],[],[],'publish-new',new['baseRevision'])
        actual = self.store.get_action_run(OWNER,run)
        self.assertEqual((actual['status'],actual['diagnostics'][-1]['code']),('COMPLETED','FORMULA_CHECKED_WITH_DIFFERENCE'))
        self.assertIn('不代表公式一致',self.delivery.view(path)['actionRuns'][0]['resultNote'])

    async def test_source_formula_followup_can_add_only_reachable_missing_dependencies(self):
        old, _, run, _ = await self.formula_action(missing=True)
        new = await self.formula_draft('new',action_run=run,base=old['analysisRef'])
        await self.service.publish(new['analysisRef'],[],[],'publish-new',new['baseRevision'])
        self.assertEqual(self.store.get_action_run(OWNER,run)['status'],'COMPLETED')

    async def test_source_formula_followup_rejects_changed_formula(self):
        old, _, run, _ = await self.formula_action()
        with self.assertRaises(AnalysisError) as failure:
            await self.formula_draft('new',action_run=run,base=old['analysisRef'],changed=True)
        self.assertEqual(failure.exception.code,'ACTION_FORMULA_CHANGED')
        self.assertEqual(self.store.get_action_run(OWNER,run)['status'],'PARTIAL')

    async def test_source_formula_followup_rejects_unrelated_added_fact(self):
        old, _, run, _ = await self.formula_action(missing=True)
        with self.assertRaises(AnalysisError) as failure:
            await self.formula_draft('new',action_run=run,base=old['analysisRef'],extra=True)
        self.assertEqual(failure.exception.code,'ACTION_INTENT_CHANGED')

    async def test_source_formula_requires_actual_target_check_not_just_new_values(self):
        old, _, run, _ = await self.formula_action()
        new = await self.formula_draft('new',action_run=run,base=old['analysisRef'],operations=[])
        await self.service.publish(new['analysisRef'],[],[],'publish-new',new['baseRevision'])
        actual = self.store.get_action_run(OWNER,run)
        self.assertEqual((actual['status'],actual['diagnostics'][-1]['code']),('PARTIAL','FORMULA_EVIDENCE_REQUIRED'))

    async def test_source_formula_remaining_dependency_gap_cannot_complete(self):
        old, _, run, _ = await self.formula_action(missing=True)
        new = await self.formula_draft('new',action_run=run,base=old['analysisRef'],missing=True)
        await self.service.publish(new['analysisRef'],[],[],'publish-new',new['baseRevision'])
        self.assertEqual(self.store.get_action_run(OWNER,run)['status'],'PARTIAL')
