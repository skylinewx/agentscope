"""Independent source inputs, frozen interpretation and multi-query reports."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types
from agentscope.app._service._resources import _LeasedTool
from agentscope.tool._adapters import MCPTool
from agentscope.tool import ToolResponse
from agentscope.message import TextBlock

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters import default_sources
from analysis_extension.capture import ReceiptCapture, leased_mcp_origin
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.models import AnalysisError


OPTIONS = {'rowsPointer': '/records', 'idField': 'id', 'labelField': 'name',
           'values': {'SALES': 'amount'}, 'unitField': 'unit', 'completePointer': '/complete',
           'memberDimension': 'MD_ORG'}


class SourceBoundaryTests(PresentationCase):
    def json_capture(self, values=('12.30', '-2.1'), unit=None):
        body = {'records': [{'id': 'product-' + str(i), 'name': '商品 ' + str(i), 'amount': value, 'unit': unit}
                            for i, value in enumerate(values)], 'complete': True}
        adapter = default_sources().create('json-records', OPTIONS)
        capture = {'resourceId': 'sales-source', 'serverName': 'sales', 'connectionFingerprint': 'sales-connection',
                   'originalToolName': 'list_sales', 'toolCallId': 'sales-call', 'request': {}, 'captureState': 'success',
                   'sourceAdapter': adapter.snapshot(), 'response': {'state': 'success', 'content': [{'type': 'text', 'text': json.dumps(body)}]}}
        ref = self.store.capture(OWNER, capture)
        saved = self.store.get_capture(OWNER, ref)
        catalog = adapter.catalog(saved)
        selections = [{'captureRef': ref, 'stepId': 'rows', 'cellKeys': [row['cellKey'] for row in catalog]}]
        scope = {'label': '商品销售', 'members': [row['id'] for row in body['records']], 'memberDimension': 'MD_ORG',
                 'selectionReason': '完整来源商品清单', 'population': {'captureRef': ref, 'stepId': 'rows', 'memberPointer': '/id'}}
        return ref, selections, scope

    def budget_capture(self, query, values=('100',), period='2026N0001', snapshot='snapshot-A', context_id=None, as_of=None):
        capture, selections, scope = fixture(values)
        receipt = json.loads(capture['response']['content'][0]['text'])
        capture['request']['querySessionId'] = receipt['querySessionId'] = query
        receipt['context']['snapshotId'] = snapshot
        receipt['context']['contextId'] = capture['request']['contextId'] = context_id or 'context-' + query
        if as_of is not None:
            receipt['context']['asOf'] = as_of
        for i, step in enumerate(receipt['data']['stepResults'][1:]):
            form = step['formResults'][0]
            form['runtime']['context']['baseContext'] = deepcopy(receipt['context'])
            form['runtime']['context']['period'] = period
            form['runtime']['context']['effectiveDimensions']['DATATIME'] = period
            form['page']['cells'][0]['dimensions']['DATATIME'] = period
            capture['request']['plan']['steps'][i+1]['formQuery']['periodCode'] = period
        for evidence in receipt['evidence']:
            evidence['fact']['context']['baseContext'] = deepcopy(receipt['context'])
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        scope['expectedDimensions'] = {}
        ref = self.store.capture(OWNER, capture)
        for selection in selections:
            selection['captureRef'] = ref
        scope['population']['captureRef'] = ref
        return ref, selections, scope

    async def test_nonbudget_json_runs_through_build_publish_and_report_without_budget_fields(self):
        ref, selectors, scope = self.json_capture()
        built = await self.service.build([ref], selectors, scope, [], 'sales-build')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual([fact['value'] for fact in artifact['facts']], ['12.30', '-2.1'])
        self.assertTrue(all(fact['unit'] is None for fact in artifact['facts']))
        self.assertEqual(built['verificationTasks'], [])
        self.assertEqual(artifact['receiptVerification'], 'UNVERIFIED')
        self.assertEqual(artifact['scope']['coverage'], 'COMPLETE')
        views = [{'viewId': 'sales', 'scopeId': artifact['scope']['scopeId'], 'type': 'TABLE',
                  'valueRefs': [fact['factId'] for fact in artifact['facts']]},
                 {'viewId': 'sales-chart', 'scopeId': artifact['scope']['scopeId'], 'type': 'BAR_VALUES',
                  'valueRefs': [fact['factId'] for fact in artifact['facts']]}]
        await self.service.publish(built['analysisRef'], [], views, 'sales-publish', 0)
        exported = await self.service.render_report(built['analysisRef'], 'sales-report')
        html = self.delivery.report(built['analysisRef'], exported['exportRef'])['html']
        self.assertIn('12.3', html)
        self.assertIn('商品 0', html)
        self.assertNotIn('未注明', html)
        read = await self.service.read_query_result(ref, '/records/0/amount')
        self.assertIn('12.30', json.dumps(read))
        inventory = await self.service.inspect('inventory', memberDimension='MD_ORG')
        self.assertEqual(inventory['validCaptureCount'], 1)
        self.assertEqual(inventory['items'][0]['objectLabel'], '商品 0')

    async def test_adapter_options_are_frozen_and_model_cannot_replace_values(self):
        ref, selectors, scope = self.json_capture(unit='元')
        changed = {**OPTIONS, 'values': {'SALES': 'not-present'}}
        self.service.sources = default_sources([{'resourceId': 'sales-source', 'toolName': 'list_sales', 'adapter': 'json-records', 'options': changed}])
        built = await self.service.build([ref], selectors, scope, [], 'frozen-mapping')
        self.assertEqual(self.store.get_analysis(OWNER, built['analysisRef'])['facts'][0]['value'], '12.30')
        selectors[0]['cellKeys'] = ['/records/999/amount']
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build([ref], selectors, scope, [], 'invalid-location')
        self.assertEqual(failure.exception.code, 'CELL_NOT_CAPTURED')

    async def test_generic_metric_identity_is_independent_of_row_location(self):
        ref, selectors, scope = self.json_capture(unit='元')
        built = await self.service.build([ref], selectors, scope, [{'id': 'rank', 'operator': 'RANK'}], 'generic-rank')
        self.assertEqual(built['derivedValues'][0]['calculationValidation'], 'PASS')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        from analysis_extension.tables import measure_key
        self.assertEqual(len({measure_key(f, artifact['scope']) for f in artifact['facts']}), 1)

    async def test_generic_missing_value_is_not_zero(self):
        ref, selectors, scope = self.json_capture(values=(None, '2'))
        built = await self.service.build([ref], selectors, scope, [], 'missing-value')
        fact = self.store.get_analysis(OWNER, built['analysisRef'])['facts'][0]
        self.assertIsNone(fact['value'])
        self.assertEqual(fact['valueState'], 'NO_VALUE')

    async def test_real_leased_generic_mcp_is_captured_only_when_registered(self):
        registry = default_sources([{'resourceId': 'sales-source', 'toolName': 'list_sales', 'adapter': 'json-records', 'options': OPTIONS}])
        inner = MCPTool('sales', mcp.types.Tool(name='list_sales', inputSchema={}), session=SimpleNamespace())
        tool = _LeasedTool(inner, SimpleNamespace(resource_id='sales-source', fingerprint='sales-connection'))
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        kwargs = {'tool_call': SimpleNamespace(name=tool.name, id='call-1', input='{}')}
        async def next_handler(**kwargs):
            yield ToolResponse(content=[TextBlock(text=json.dumps({'records': [], 'complete': True}))])
        capture = ReceiptCapture(self.store, OWNER, frozenset({'sales-source'}), AsyncMock(), sources=registry)
        result = [event async for event in capture.run(agent, kwargs, next_handler)]
        saved = self.store.get_capture(OWNER, result[0].metadata['analysisCaptureRef'])
        self.assertEqual(saved['sourceAdapter']['id'], 'json-records')
        capture.sources = default_sources()
        result = [event async for event in capture.run(agent, kwargs, next_handler)]
        self.assertNotIn('analysisCaptureRef', result[0].metadata)
        self.assertEqual(len(result), 1)

    async def test_budget_contract_cannot_be_downgraded_by_generic_binding(self):
        registry = default_sources([{'resourceId': 'budget', 'toolName': 'execute_semantic_plan', 'adapter': 'json-records', 'options': OPTIONS}])
        definition = mcp.types.Tool(name='execute_semantic_plan', inputSchema={'properties': {'contractVersion': {'const': '2.0'}}},
                                    _meta={'version': '2.0', 'exactContractResource': 'semantic://contracts/external-query'})
        tool = _LeasedTool(MCPTool('budget', definition, session=SimpleNamespace()), SimpleNamespace(resource_id='budget', fingerprint='connection'))
        origin = leased_mcp_origin(tool, frozenset({'budget'}))
        self.assertEqual(registry.for_tool(tool, origin).adapter_id, 'budget-v2')

    async def test_multiple_query_sessions_retain_distinct_observations_in_one_report(self):
        first, a, scope = self.budget_capture('query-first', ('100',))
        second, b, _ = self.budget_capture('query-second', ('120',))
        built = await self.service.build([first, second], a+b, scope, [], 'two-queries')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['sourceContexts']), 2)
        self.assertEqual(len({fact['factId'] for fact in artifact['facts']}), 2)
        self.assertEqual({fact['valueState'] for fact in artifact['facts']}, {'PRESENT'})
        self.assertEqual({fact['value'] for fact in artifact['facts']}, {'100', '120'})
        view = {'viewId': 'both', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': [f['factId'] for f in artifact['facts']]}
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(built['analysisRef'], [], [view], 'ambiguous-query-publication', 0)
        self.assertEqual(failure.exception.code, 'AMBIGUOUS_TABLE')
        views = [{**view, 'viewId': 'query-' + str(i), 'title': '第 ' + str(i + 1) + ' 次查询',
                  'valueRefs': [fact['factId']]} for i, fact in enumerate(artifact['facts'])]
        await self.service.publish(built['analysisRef'], [], views, 'two-query-publication', 0)
        exported = await self.service.render_report(built['analysisRef'], 'two-query-report')
        html = self.delivery.report(built['analysisRef'], exported['exportRef'])['html']
        self.assertIn('100', html)
        self.assertIn('120', html)

    async def test_evidence_is_not_shared_across_query_sessions_with_reused_ids(self):
        first, a, scope = self.budget_capture('query-first')
        second, b, _ = self.budget_capture('query-second', ('120',))
        verified_capture = self.store.get_capture(OWNER, first)
        receipt = json.loads(verified_capture['response']['content'][0]['text'])
        receipt['data'] = {'action': 'verify', 'executionId': 'execution-A',
                           'verification': {'status': 'VALID', 'evidenceIds': ['evidence-observation-0']}}
        verified_capture.update(originalToolName='validate_evidence', request={'action': 'verify', 'executionId': 'execution-A'})
        verified_capture['response']['content'][0]['text'] = json.dumps(receipt)
        verifier = self.store.capture(OWNER, verified_capture)
        built = await self.service.build([first, second, verifier], a+b, scope, [], 'isolated-verification')
        self.assertEqual(built['receiptVerification'], 'UNVERIFIED')
        self.assertEqual({t['querySessionId'] for t in built['verificationTasks']}, {'query-first', 'query-second'})
        receipt['data']['verification']['status'] = 'INVALID'
        verified_capture['response']['content'][0]['text'] = json.dumps(receipt)
        invalid = self.store.capture(OWNER, verified_capture)
        built = await self.service.build([first, second, invalid], a+b, scope, [], 'isolated-invalid')
        facts = self.store.get_analysis(OWNER, built['analysisRef'])['facts']
        self.assertEqual({f['querySessionId']: f['valueState'] for f in facts}, {'query-first': 'CONFLICT', 'query-second': 'PRESENT'})

    async def test_invalid_source_configuration_is_not_silently_defaulted(self):
        for bindings in ({}, False):
            with self.assertRaises(AnalysisError):
                default_sources(bindings)
        with self.assertRaises(AnalysisError):
            default_sources().create('budget-v2', [])

    async def test_same_metric_across_member_queries_shares_column_and_filter(self):
        for reopened in (False, True):
            with self.subTest(reopened=reopened):
                tag = str(reopened)
                first, a, scope = self.budget_capture('first-' + tag, ('-1', '-2'), context_id='context-A')
                second, b, _ = self.budget_capture('second-' + tag, ('-1', '-2'),
                                                 context_id='context-B' if reopened else 'context-A')
                built = await self.service.build([first, second], [a[0], b[1]], scope, [], 'member-queries-' + tag)
                artifact = self.store.get_analysis(OWNER, built['analysisRef'])
                self.assertEqual(len({f['sourceContextId'] for f in artifact['facts']}), 2)
                view = {'viewId': 'members', 'scopeId': built['scope']['scopeId'], 'type': 'TABLE',
                        'valueRefs': [f['factId'] for f in artifact['facts']]}
                published = await self.service.publish(built['analysisRef'], [], [view], 'member-table-' + tag, 0)
                shown = self.delivery.view(published['delivery']['path'])
                table = shown['tables'][0]
                self.assertEqual([c['label'] for c in table['columns']], ['净利润'])
                key = table['columns'][0]['key']
                self.assertEqual([r['cells'][key]['exact'] for r in table['rows']], ['-1', '-2'])
                filtered = self.delivery.project_view(built['analysisRef'], {'filter': 'negative', 'sort': 'desc'})
                self.assertEqual(filtered['objectCount'], 2)
                self.assertEqual([r['exact'] for r in filtered['rows']], ['-1', '-2'])
                first_page = self.delivery.project_view(built['analysisRef'], {'filter': 'negative', 'sort': 'asc'}, 0, 1)
                next_page = self.delivery.project_view(built['analysisRef'], {'filter': 'negative', 'sort': 'asc'}, 1, 1)
                self.assertEqual([r['exact'] for r in first_page['rows']], ['-2'])
                self.assertEqual(first_page['page']['nextOffset'], 1)
                self.assertEqual([r['exact'] for r in next_page['rows']], ['-1'])

    async def test_business_column_does_not_merge_different_data_boundaries(self):
        from analysis_extension.tables import measure_key
        first, selections, scope = self.budget_capture('query-first')
        built = await self.service.build([first], selections, scope, [], 'column-boundaries')
        fact = self.store.get_analysis(OWNER, built['analysisRef'])['facts'][0]
        for field in ('snapshotId', 'asOf', 'schemaHash'):
            changed = deepcopy(fact)
            changed['dataContext'][field] = 'another-' + field
            self.assertNotEqual(measure_key(fact, scope), measure_key(changed, scope), field)
        for field in ('period', 'bindingRef', 'unit', 'scale'):
            changed = {**fact, field: 'another-' + field}
            self.assertNotEqual(measure_key(fact, scope), measure_key(changed, scope), field)
        changed = deepcopy(fact)
        changed['dimensions']['SCENE'] = 'actual'
        self.assertNotEqual(measure_key(fact, scope), measure_key(changed, scope))

    async def test_capture_without_adapter_is_not_inferred_from_budget_content_or_request(self):
        capture, selections, scope = fixture()
        capture.pop('sourceAdapter')
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                candidate = deepcopy(capture)
                if interrupted:
                    candidate.update(captureState='interrupted', response={})
                ref = self.store.capture(OWNER, candidate)
                selected = [{**s, 'captureRef': ref} for s in selections]
                local_scope = {**scope, 'population': {**scope['population'], 'captureRef': ref}}
                with self.assertRaises(AnalysisError) as failure:
                    await self.service.build([ref], selected, local_scope, [], 'missing-adapter-' + str(interrupted))
                self.assertEqual(failure.exception.code, 'SOURCE_ADAPTER_UNAVAILABLE')

    async def test_capture_requires_complete_pinned_adapter_metadata(self):
        registry = default_sources()
        good = {'id': 'budget-v2', 'version': '1', 'options': {}}
        for saved in ({}, {**good, 'id': []}, {**good, 'version': None}, {**good, 'version': ''},
                      {**good, 'options': None}, {**good, 'options': []}):
            with self.subTest(saved=saved), self.assertRaises(AnalysisError) as failure:
                registry.for_capture({'sourceAdapter': saved})
            self.assertEqual(failure.exception.code, 'INVALID_SOURCE_ADAPTER')
        with self.assertRaises(AnalysisError) as failure:
            registry.for_capture({'sourceAdapter': {**good, 'version': 'unavailable-version'}})
        self.assertEqual(failure.exception.code, 'SOURCE_ADAPTER_UNAVAILABLE')
        self.assertEqual(registry.for_capture({'sourceAdapter': good}).snapshot(), good)

    async def test_formula_uses_its_own_query_even_when_observation_ids_repeat(self):
        from analysis_closeout_test import mixed_fixture
        first, selections, scope = mixed_fixture(self.store, 1)
        capture = self.store.get_capture(OWNER, first)
        receipt = json.loads(capture['response']['content'][0]['text'])
        capture['request']['querySessionId'] = receipt['querySessionId'] = 'query-other'
        cells = receipt['data']['stepResults'][1]['formResults'][0]['page']['cells']
        cells[1].update(rawValueJson='"30"', displayValue='30')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        second = self.store.capture(OWNER, capture)
        both = selections + [{**selection, 'captureRef': second} for selection in selections]
        built = await self.service.build([first, second], both, scope,
            [{'id': 'formula', 'operator': 'FORMULA_CHECK',
              'select': {'cellKey': '2:1', 'sourceContextId': default_sources().for_capture(self.store.get_capture(OWNER, first)).catalog(self.store.get_capture(OWNER, first))[0]['sourceContextId']}}], 'query-formula')
        self.assertEqual(built['derivedValues'][0]['value'], '-5')
        self.assertEqual(built['derivedValues'][0]['calculationValidation'], 'PASS')

    async def test_cross_snapshot_is_explicit_and_does_not_grant_comparability(self):
        first, a, scope = self.budget_capture('query-first')
        second, b, _ = self.budget_capture('query-second', ('120',), snapshot='snapshot-B')
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build([first, second], a+b, scope, [], 'implicit-mix')
        self.assertEqual(failure.exception.code, 'CONTEXT_CONFLICT')
        scope['contextMode'] = 'COMPOSITE'
        built = await self.service.build([first, second], a+b, scope, [], 'explicit-mix')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(artifact['context']['kind'], 'COMPOSITE')
        result = calculate(artifact['facts'], artifact['scope'], [{'id': 'diff', 'operator': 'DIFFERENCE',
                            'inputRefs': [f['factId'] for f in artifact['facts']]}], RuleRegistry())[0]
        self.assertEqual(result['diagnostics'][0]['code'], 'COMPARABILITY_UNCONFIRMED')

    async def test_mixed_budget_and_json_report_uses_both_adapters(self):
        budget, a, scope = self.budget_capture('query-budget')
        sales, b, sales_scope = self.json_capture(values=('7',), unit='元')
        scope.pop('population')
        scope.update(contextMode='COMPOSITE', members=scope['members'] + sales_scope['members'])
        built = await self.service.build([budget, sales], a+b, scope, [], 'mixed-source')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual({f['value'] for f in artifact['facts']}, {'100', '7'})
        self.assertEqual({f['sourceObjectId'] for f in artifact['facts']}, {'object-0', 'product-0'})
        self.assertEqual(artifact['scope']['coverage'], 'UNKNOWN')
        self.assertEqual(len(artifact['sourceContexts']), 2)

    async def test_cross_period_and_binding_need_complete_versioned_mapping(self):
        first, a, scope = self.budget_capture('query-first', ('100',), period='2025N0001')
        second, b, _ = self.budget_capture('query-second', ('120',), period='2026N0001')
        built = await self.service.build([first, second], a+b, scope, [], 'period-input')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        facts = artifact['facts']
        refs = [facts[1]['factId'], facts[0]['factId']]
        operation = {'id': 'growth', 'operator': 'DIFFERENCE', 'inputRefs': refs}
        blocked = calculate(facts, artifact['scope'], [operation], RuleRegistry())[0]
        self.assertEqual(blocked['calculationValidation'], 'BLOCKED')
        rule = {'version': '1', 'source': 'Synthetic comparison definition', 'operators': ['DIFFERENCE', 'RATIO'],
                'comparability': {'periods': ['2025N0001', '2026N0001'], 'varyingDimensions': ['DATATIME']}}
        operation['comparabilityRef'] = 'year-comparison'
        allowed = calculate(facts, artifact['scope'], [operation], RuleRegistry({'year-comparison': rule}))[0]
        self.assertEqual(allowed['value'], '20')
        self.assertEqual(allowed['comparabilityVersion'], '1')
        wrong_rule = deepcopy(rule)
        wrong_rule['comparability']['periods'] = ['2027N0001', '2028N0001']
        outside = calculate(facts, artifact['scope'], [operation], RuleRegistry({'year-comparison': wrong_rule}))[0]
        self.assertEqual(outside['diagnostics'][0]['code'], 'RULE_NOT_APPLICABLE')
        facts[1]['bindingRef'] = 'new-table'
        blocked = calculate(facts, artifact['scope'], [operation], RuleRegistry({'year-comparison': rule}))[0]
        self.assertEqual(blocked['calculationValidation'], 'BLOCKED')
        rule['comparability']['metrics'] = [{k: f[k] for k in ('bindingRef', 'measureCode', 'cellKey')} for f in facts]
        allowed = calculate(facts, artifact['scope'], [operation], RuleRegistry({'year-comparison': rule}))[0]
        self.assertEqual(allowed['value'], '20')
        facts[1]['unit'] = None
        blocked = calculate(facts, artifact['scope'], [operation], RuleRegistry({'year-comparison': rule}))[0]
        self.assertEqual(blocked['diagnostics'][0]['code'], 'UNIT_UNCONFIRMED')
