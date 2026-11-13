from copy import deepcopy
from unittest.mock import AsyncMock, patch
from analysis_presentation_fixtures import PresentationCase
from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_fact_view_fixtures import fact_view
from analysis_fixtures import OWNER
from analysis_extension.models import AnalysisError
from analysis_extension.source_views import register, select, install, query_basis
from analysis_extension.turn_completion import TurnCompletion
from analysis_extension.followup_scope import prepare_request
from analysis_extension.adapters.budget_v2 import receipt_from_capture


class BasisUpgradeTests(PresentationCase):
    async def test_adopted_build_accepts_scope_without_repeating_capture_references(self):
        from jsonschema import Draft202012Validator
        from analysis_extension.tools import AnalysisBuild
        source, _, _ = self.source()
        await self.service.task(mode='ANALYSIS', goal='分析原结果', subjectSources=[{'sourceRef': source['sourceRef']}])
        request = {'scope': {'label': '本题标题', 'selectionReason': '已明确采用的原结果'},
                   'operations': [], 'requestKey': 'adopted-with-scope'}
        Draft202012Validator(AnalysisBuild.input_schema).validate(request)
        result = await self.service.build(**request)
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(artifact['scope']['label'], '本题标题')
        self.assertEqual(len(artifact['facts']), source['rowCount'])
        self.service.turn.state.pop('adoptedInputs')
        with self.assertRaises(AnalysisError) as missing:
            await self.service.build(**{**request, 'requestKey': 'missing-input'})
        self.assertEqual(missing.exception.code, 'INVALID_BUILD_INPUT')

    def source(self, filtered=False):
        capture, receipt = business_fixture()
        if filtered:
            predicate = {'operator': 'GT', 'value': '0'}
            capture['request']['intentDraft'] = {'valueFilter': predicate}
            receipt['data']['resolution']['intentDraft'] = deepcopy(capture['request']['intentDraft'])
            evaluated = [{**deepcopy(row), 'resultSetId': result['resultSetId'], 'matched': row['valueState'] == 'PRESENT'}
                for result in receipt['data']['resultSets'] for row in result['rows']]
            for result in receipt['data']['resultSets']:
                result['rows'] = [row for row in result['rows'] if row['valueState'] == 'PRESENT']
            receipt['data']['valueFilter'] = {**predicate, 'populationCount': len(evaluated),
                'matchedCount': sum(row['matched'] for row in evaluated), 'populationComplete': True, 'evaluatedFacts': evaluated}
            update(capture, receipt)
            receipt_from_capture(capture)
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'basis', 'request': '分析前面的结果',
            'mode': 'ANALYSIS', 'goal': '分析前面的结果', 'requiredEvidence': 'VALUES', 'failures': {}, 'buildRefs': [],
            'followupContext': {'mode': 'CONTINUE'}, 'queryCaptureRefs': [ref]})
        record = register(self.service.turn, {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']})
        return record, self.store.get_capture(OWNER, ref), receipt

    async def test_repeated_fact_views_keep_original_filtered_query_proof(self):
        source, _, _ = self.source(filtered=True)
        first, _ = fact_view(self.service.turn, source['sourceRef'], {'operator': 'GT', 'value': '0'}, reopened=True)
        second, capture = fact_view(self.service.turn, first['sourceRef'], reopened=True)
        self.assertEqual(second['rowCount'], first['rowCount'])
        receipt_from_capture(capture)

    async def test_facts_filter_then_analyze_uses_original_values_and_members_without_repeating_filter(self):
        source, _, _ = self.source()
        filtered, capture = fact_view(self.service.turn, source['sourceRef'], {'operator': 'GT', 'value': '0'}, reopened=True)
        receipt = receipt_from_capture(capture)
        self.assertEqual(receipt['data']['sourceBasis']['operation'], 'IMMUTABLE_FACT_VIEW')
        await self.service.task(mode='ANALYSIS', goal='分析原结果', subjectSources=[{'sourceRef': filtered['sourceRef']}])
        built = await self.service.build(operations=[], requestKey='facts-view')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['facts']), filtered['rowCount'])
        self.assertNotIn('budgetValueFilter', self.service.turn.state)
        request = query_basis(self.service.turn, capture, {'basis': {'kind': 'MEMBERS', 'sourceRef': filtered['sourceRef']},
            'intentDraft': {'targets': [{'id': 'income', 'businessPhrases': [{'text': '收入'}]}]}})
        self.assertEqual(request['basis']['memberDimensions'], ['MD_ORG'])
        self.assertNotIn('valueFilter', request['intentDraft'])

    async def test_archive_view_detects_changed_value_and_cross_source(self):
        source, capture, _ = self.source()
        _, view = fact_view(self.service.turn, source['sourceRef'])
        receipt = self.service.sources.for_capture(view).raw_receipt(view['response'])
        receipt['data']['resultSets'][0]['rows'][0]['value'] = '99999999'
        update(view, receipt)
        with self.assertRaises(AnalysisError):
            receipt_from_capture(view)
        with self.assertRaises(AnalysisError) as error:
            query_basis(self.service.turn, {**capture, 'connectionFingerprint': 'other'},
                        {'basis': {'kind': 'FACTS', 'sourceRef': source['sourceRef']}})
        self.assertEqual(error.exception.code, 'SOURCE_CONNECTION_MISMATCH')

    async def test_members_after_fact_filter_can_explicitly_reuse_source_form_without_old_metric(self):
        source, _, _ = self.source()
        filtered, capture = fact_view(self.service.turn, source['sourceRef'], {'operator': 'GT', 'value': '0'}, reopened=True)
        self.service.turn.state['request'] = '沿用这些对象，查询2026年收入预算'
        request = {'purpose': 'QUERY', 'basis': {'kind': 'MEMBERS', 'sourceRef': filtered['sourceRef']},
                   'scope': {'taskNames': ['年度预算'], 'formNames': ['利润表']},
                   'intentDraft': {'targets': [{'id': 'income', 'periodText': '2026年',
                       'businessPhrases': [{'text': '收入'}, {'text': '预算'}]}],
                       'sharedConditions': [{'text': '利润表'}]}}
        before = deepcopy(self.service.turn.state)
        prepared = prepare_request(self.service.turn, capture, request)
        self.assertIn('利润表', prepared['contextText'])
        self.assertEqual(prepared['intentDraft']['sharedConditions'][0]['sourceText'], '利润表')
        self.assertNotIn('科目0', prepared['contextText'])
        self.assertNotIn('valueFilter', prepared['intentDraft'])
        self.assertEqual(prepared['scope']['formNames'], ['利润表'])
        self.assertEqual({k: v for k, v in self.service.turn.state.items() if k != 'businessEvidence'}, before)
        unscoped = deepcopy(request)
        unscoped.pop('scope')
        unscoped['intentDraft']['sharedConditions'] = []
        self.assertNotIn('formNames', prepare_request(self.service.turn, capture, unscoped).get('scope', {}))
        forged = deepcopy(request)
        forged['scope']['formNames'] = ['不存在的已确认表样']
        forged['intentDraft']['sharedConditions'] = [{'text': '不存在的已确认表样'}]
        with self.assertRaises(AnalysisError):
            prepare_request(self.service.turn, capture, forged)

    async def test_source_index_survives_store_reopen_and_is_rebuilt_from_historical_selection(self):
        source, capture, _ = self.source()
        from analysis_extension.store import AnalysisStore
        self.assertEqual(self.store.get_source_view(OWNER, source['sourceRef'])['selection'], source['selection'])
        # Construct a second store against the same database; no in-memory registry.
        reopened = AnalysisStore(self.store.root, self.store.limits)
        self.assertEqual(reopened.get_source_view(OWNER, source['sourceRef'])['selection'], source['selection'])
        self.assertEqual(register(self.service.turn, source['selection'])['sourceRef'], source['sourceRef'])

    async def test_bound_scopes_survive_repeated_fact_views_then_explicit_member_reuse(self):
        from analysis_extension.scope_candidates import resolve
        source, capture, receipt = self.source()
        public = []
        for model, code, text, label in [('BUDGET_TASK', 'TASK', '任务简称', '年度预算'),
                                         ('BUDGET_FORM_TEMPLATE', 'LRB', '表样简称', '利润表')]:
            search = deepcopy(capture)
            search.pop('captureRef', None)
            search.update(originalToolName='search_entities', request={'entityMention': text})
            found = deepcopy(receipt)
            found.update(executionId='search-' + code, status='SUCCEEDED', data={'candidates': [
                dict(modelCode=model, key=code, label=label, candidateId=code, kind='RESOURCE', matchedBy=['RESOURCE_RDF_VERIFIED'])]})
            update(search, found)
            ref = self.store.capture(OWNER, search)
            public.append({'text': text, 'candidateRef': f'scand:{ref}:0'})
        proofs = resolve(self.service.turn, capture, public)
        capture['request']['scope'] = {'bindings': [proof['binding'] for proof in proofs]}
        receipt['data']['resolution']['scopeBindings'] = proofs
        update(capture, receipt)
        capture.pop('captureRef', None)
        ref = self.store.capture(OWNER, capture)
        source = register(self.service.turn, {**source['selection'], 'captureRef': ref})
        filtered, _ = fact_view(self.service.turn, source['sourceRef'], {'operator': 'GT', 'value': '0'}, reopened=True)
        filtered, view = fact_view(self.service.turn, filtered['sourceRef'], reopened=True)
        self.service.turn.state['request'] = '沿用这些企业，查2026年收入预算'
        self.service.turn.state['queryCaptureRefs'] = [view['captureRef']]
        request = {'purpose': 'QUERY', 'basis': {'kind': 'MEMBERS', 'sourceRef': filtered['sourceRef']},
                   'scope': {'bindings': public},
                   'intentDraft': {'targets': [{'id': 'income', 'periodText': '2026年',
                       'businessPhrases': [{'text': '收入'}, {'text': '预算'}]}]}}
        prepared = prepare_request(self.service.turn, view, request)
        self.assertEqual(prepared['scope']['bindings'], [proof['binding'] for proof in proofs])
        self.assertIn('任务简称', prepared['contextText'])
        self.assertIn('表样简称', prepared['contextText'])
        self.assertNotIn('科目0', prepared['contextText'])
        self.assertNotIn('valueFilter', prepared['intentDraft'])
        self.assertEqual(prepared['basis']['memberDimensions'], ['MD_ORG'])
        # No explicit reuse means no forced restoration of the old task/form.
        unscoped = deepcopy(request)
        unscoped.pop('scope')
        self.assertFalse(prepare_request(self.service.turn, view, unscoped).get('scope', {}).get('bindings'))
        # A valid candidate for another task must not borrow this source's quotation evidence.
        other = deepcopy(search)
        other['request'] = {'entityMention': '另一个表样'}
        found['data']['candidates'][0].update(key='OTHER', label='另一个表样', candidateId='OTHER')
        update(other, found)
        other.pop('captureRef', None)
        other_ref = self.store.capture(OWNER, other)
        changed = deepcopy(request)
        changed['scope']['bindings'][1] = {'text': '另一个表样', 'candidateRef': f'scand:{other_ref}:0'}
        with self.assertRaises(AnalysisError) as invalid:
            prepare_request(self.service.turn, view, changed)
        self.assertEqual(invalid.exception.code, 'QUERY_DRAFT_EVIDENCE_INVALID')

    async def test_replaced_input_cannot_publish_or_increment_old_calculations(self):
        source, _, _ = self.source()
        await self.service.task(mode='ANALYSIS', goal='当前结果', subjectSources=[{'sourceRef': source['sourceRef']}])
        first = await self.service.build(operations=[], requestKey='first')
        subset = register(self.service.turn, {**source['selection'], 'rowIndices': [0]})
        await self.service.task(mode='ANALYSIS', goal='改为子集', subjectSources=[{'sourceRef': subset['sourceRef']}])
        with self.assertRaises(AnalysisError) as stale:
            await self.service.build(baseAnalysisRef=first['analysisRef'], operations=[], requestKey='stale')
        self.assertEqual(stale.exception.code, 'STALE_INPUT_SET')
        with self.assertRaises(AnalysisError) as stale:
            await self.service.publish(first['analysisRef'], requestKey='stale-publish')
        self.assertEqual(stale.exception.code, 'STALE_INPUT_SET')
        with patch('analysis_extension.python_analysis.execute_python', new_callable=AsyncMock) as execute:
            with self.assertRaises(AnalysisError) as stale:
                await self.service.python(analysisRef=first['analysisRef'], code='result=[]',
                    purpose='继续统计', requestKey='stale-python')
            self.assertEqual(stale.exception.code, 'STALE_INPUT_SET')
            execute.assert_not_awaited()
        rebuilt = await self.service.build(operations=[], requestKey='new-input')
        self.assertEqual(len(self.store.get_analysis(OWNER, rebuilt['analysisRef'])['facts']), 1)

    async def test_build_finishing_after_input_replacement_cannot_adopt_old_facts(self):
        source, _, _ = self.source()
        await self.service.task(mode='ANALYSIS', goal='原输入', subjectSources=[{'sourceRef': source['sourceRef']}])
        subset = register(self.service.turn, {**source['selection'], 'rowIndices': [0]})
        prepare = self.service.budget_preparation.for_build
        async def replace_during_preparation(*args):
            result = await prepare(*args)
            await self.service.task(mode='ANALYSIS', goal='新输入', subjectSources=[{'sourceRef': subset['sourceRef']}])
            return result
        with patch.object(self.service.budget_preparation, 'for_build', side_effect=replace_during_preparation):
            with self.assertRaises(AnalysisError) as changed:
                await self.service.build(operations=[], requestKey='late-build')
        self.assertEqual(changed.exception.code, 'TASK_INPUTS_CHANGED')
        self.assertEqual(self.service.turn.state['adoptedInputs'][0]['sourceRef'], subset['sourceRef'])
        self.assertNotIn('analysisRef', self.service.turn.state)

    async def test_python_receives_overlapping_group_memberships_without_duplicate_facts(self):
        source, _, _ = self.source()
        await self.service.task(mode='ANALYSIS', goal='各组统计', subjectSources=[
            {'sourceRef': source['sourceRef'], 'role': role} for role in ('PRIMARY', 'REFERENCE')])
        built = await self.service.build(operations=[], requestKey='python-groups')
        result = await self.service.python(analysisRef=built['analysisRef'], purpose='按输入组统计', requestKey='python-memberships',
            code='result=[{"name":"group_count","label":"组归属数量","unit":"个","value":sum(len(x["inputMemberships"]) for x in inputs),'
                 '"inputRefs":[x["ref"] for x in inputs]}]')
        self.assertEqual(result['derivedValues'][-1]['value'], str(source['rowCount'] * 2))

    async def test_scope_clear_and_invalid_scope_patch_are_atomic(self):
        self.source()
        await self.service.task(mode='ANALYSIS', goal='查询', intent={'population': '甲单位', 'grouping': '板块'})
        result = await self.service.task(mode='ANALYSIS', goal='清除范围', scopeEdits=[{'dimensionHint': '组织机构', 'selector': None}])
        self.assertNotIn('population', result['intent'])
        self.assertEqual(result['intent']['grouping'], '板块')
        before = deepcopy(self.service.turn.state)
        with self.assertRaises(AnalysisError):
            await self.service.task(mode='ANALYSIS', goal='invalid', intent={'period': '2025年'}, scopeEdits=[{
                'dimensionHint': '项目', 'selector': {'type': 'FILTER', 'where': {'op': 'NOT', 'children': []}}}])
        self.assertEqual(before, self.service.turn.state)

    async def test_model_schema_does_not_expose_host_proofs(self):
        from analysis_extension.query_contract import model_schema
        from jsonschema import Draft202012Validator
        native = {'type': 'object', 'properties': {'sourceSelection': {'type': 'object'},
            'basis': {'type': 'object'}, 'intentDraft': {'type': 'object', 'properties': {'sourceText': {'type': 'string'},
                'confirmedSource': {'type': 'object'}}, 'required': ['sourceText']}}}
        schema = model_schema(native)
        self.assertNotIn('sourceSelection', schema['properties'])
        self.assertNotIn('sourceText', schema['properties']['intentDraft']['properties'])
        Draft202012Validator(schema).validate({'basis': {'kind': 'FACTS', 'sourceRef': 'src_actual'}})
        self.assertIn('sourceText', native['properties']['intentDraft']['properties'])

    async def test_model_projection_never_strips_host_evidence_from_execution_schema(self):
        import json
        from types import SimpleNamespace
        from analysis_extension.query_contract import configure
        from agentscope.tool._types import RegisteredTool
        from agentscope.tool import ToolBase
        from agentscope._utils._common import _json_loads_with_repair
        class NativeTool(ToolBase):
            name, description = 'query', 'query'
            is_concurrency_safe, is_read_only = True, True
            input_schema = {'type': 'object', 'additionalProperties': False, 'properties': {
                'intentDraft': {'type': 'object', 'additionalProperties': False, 'properties': {
                    'sourceText': {'type': 'string'}, 'confirmedSource': {'type': 'object'}}},
                'basis': {'type': 'object', 'additionalProperties': False, 'properties': {
                    'kind': {'type': 'string'}, 'source': {'type': 'object'},
                    'contextId': {'type': 'string'}, 'querySessionId': {'type': 'string'}}}}}
            async def call(self, **kwargs):
                return kwargs
            async def check_permissions(self, *args, **kwargs):
                raise AssertionError('No tool dispatch in this schema regression')
        tool = NativeTool()
        native = deepcopy(tool.input_schema)
        toolkit = SimpleNamespace(tool_groups=[], _get_available_tools=AsyncMock(return_value={'query': RegisteredTool(tool)}))
        with patch('analysis_extension.query_contract.leased_mcp_origin', return_value={'originalToolName': 'execute_budget_query'}):
            await configure(toolkit, frozenset())
        visible = RegisteredTool(tool).get_tool_schema()['function']['parameters']
        self.assertNotIn('sourceText', visible['properties']['intentDraft']['properties'])
        self.assertIn('sourceRef', visible['properties']['basis']['properties'])
        payload = {'intentDraft': {'sourceText': '上一轮原话', 'confirmedSource': {'executionId': 'committed'}},
            'basis': {'kind': 'FACTS', 'source': {'factIds': ['f']}, 'contextId': 'original', 'querySessionId': 'session'}}
        self.assertEqual(_json_loads_with_repair(json.dumps(payload), tool.input_schema), payload)
        self.assertEqual(tool.input_schema, native)
