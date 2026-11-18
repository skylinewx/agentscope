"""预算批量回执的真实形状回归：同字段多科目、聚合粒度、完整度和大表读取。"""
from copy import deepcopy
import json
from unittest import TestCase, IsolatedAsyncioTestCase
import tempfile
import analysis_fixtures

from analysis_extension.adapters.budget_v2 import adapt, catalog, BudgetV2Adapter
from analysis_extension.models import AnalysisError
from analysis_extension.query_results import compact_receipt, read_receipt, encode
from analysis_extension.sources import SourceRegistry
from analysis_extension.tools import SELECTOR
from numeric_tools import calculate
import jsonschema


def batch_fixture(aggregate=True, null_value=False):
    context = deepcopy(analysis_fixtures.CONTEXT)
    outside = {'DATATIME': '2026N0001', 'MD_CURRENCY': 'CNY', 'MD_STAGE': 'FINAL', 'MD_ORG': 'seed'}
    query_metrics, request_metrics, origins = [], [], []
    for index, account in enumerate(('0205', '0206')):
        name = f'metric{index + 1}'
        expression = f'MX_FINANCIAL_N[ZB_JE2,-0N,MD_BUDACCOUNT="{account}",MD_SCENE="BUDGET"]'
        selection = {'periodCode': '2026N0001', 'measureCode': 'ZB_JE2', 'targetDimensions': {'MD_BUDACCOUNT': account}}
        query_metrics.append({'selection': selection})
        request_metrics.append({'name': name, 'viewId': 'view-A', 'cellExp': expression, 'dimValMap': outside})
        origins.append({'name': name, 'measureCode': 'ZB_JE2', 'cellExp': expression,
                        'selectedDimensions': {'MD_BUDACCOUNT': account, 'MD_SCENE': 'BUDGET'},
                        'requestedExpression': None, 'selectionCellKey': f'{index+1}:1', 'selectionObservationId': f'probe-{index}',
                        'headers': ['净利润' if index == 0 else '年初未分配利润'], 'formName': '利润表', 'taskName': '年度预算',
                        'dimensionModels': {'MD_ORG': 'MD_ORG_BUD'},
                        'context': {'baseContext': context, 'period': '2026N0001', 'effectiveDimensions': outside,
                                    'target': {'formBindingKey': 'binding-A', 'viewId': 'view-A', 'mainTaskId': 'task-A', 'organizationCategory': 'MD_ORG_BUD'}}})
    query = {'metrics': query_metrics, 'batchDimensions': [{'dimCode': 'MD_ORG', 'where': {'field': 'MD_YWBK', 'op': 'IN', 'value': ['07', '05']}}],
             'orderBy': [{'field': 'metric1', 'direction': 'DESC'}]}
    if aggregate:
        query['aggregation'] = {'groupBy': [{'dimCode': 'MD_ORG', 'field': 'MD_YWBK'}],
                                'measures': [{'metric': 'metric1'}, {'metric': 'metric2'}]}
    request = {**deepcopy(query), 'metrics': request_metrics, 'budTaskId': 'task-A'}
    dimension = 'MD_ORG__MD_YWBK' if aggregate else 'MD_ORG'
    columns = [{'name': dimension, 'label': '组织机构_所属板块' if aggregate else '组织机构', 'type': 'STRING'},
               {'name': dimension + '_NAME', 'label': '名称', 'type': 'STRING'}]
    for index, account in enumerate(('0205', '0206')):
        columns.append({'name': f'metric{index+1}', 'label': '净利润' if index == 0 else '年初未分配利润', 'type': 'DECIMAL_STRING',
                        'unit': '元', 'calcType': 'SUM', 'fixedDimValMap': {'MD_BUDACCOUNT': account},
                        'fixedDimNameMap': {'MD_BUDACCOUNT': '净利润' if index == 0 else '年初未分配利润'}})
    roles = {dimension: {'kind': 'PROPERTY' if aggregate else 'DIMENSION', 'dimCode': 'MD_ORG', 'valueColumn': dimension},
             dimension + '_NAME': {'kind': 'NAME', 'dimCode': 'MD_ORG', 'valueColumn': dimension},
             'metric1': {'kind': 'METRIC'}, 'metric2': {'kind': 'METRIC'}}
    if aggregate:
        roles[dimension]['field'] = roles[dimension + '_NAME']['field'] = 'MD_YWBK'
    data = {'fixedDimValMap': {'DATATIME': '2026N0001', 'MD_CURRENCY': 'CNY', 'MD_SCENE': 'BUDGET'},
            'fixedDimNameMap': {'DATATIME': '2026年', 'MD_CURRENCY': '人民币', 'MD_SCENE': '预算数'},
            'columns': columns, 'rows': [['07', '综合', '85084700.00', '2965.89'], ['05', '投资', None if null_value else '1209100.00', '609.20']],
            'rowCount': 2, 'rangeSummary': {'targetCombinationCount': 5}}
    if aggregate:
        data['aggregationSummary'] = {'leafTargetCombinationCount': 5, 'groupCountBeforeHaving': 2, 'filteredOutGroupCount': 0}
    table = {'resultType': 'BATCH_RESULT', 'batchId': 'batch-A', 'disposition': 'SUCCEEDED', 'message': '批量查询成功',
             'readAt': '2026-01-01T00:00:01Z', 'request': request, 'metrics': origins, 'columnRoles': roles, 'data': data}
    plan = {'steps': [{'id': 'binding', 'operation': 'RESOLVE_ENTITY', 'resourceIris': ['binding-A']},
                      {'id': 'batch', 'operation': 'BATCH_DATA_QUERY', 'inputs': ['binding'], 'batchQuery': query}]}
    receipt = {'contractVersion': '2.0', 'status': 'SUCCEEDED', 'context': context, 'querySessionId': 'session-A', 'executionId': 'execution-A',
               'data': {'stepResults': [{'stepId': 'batch', 'operation': 'BATCH_DATA_QUERY', 'batchResults': [table], 'rows': [], 'formResults': [], 'exactness': 'EXACT'}]},
               'evidence': [{'evidenceId': 'e-batch', 'kind': 'BUDGET_BATCH', 'fact': {'batchId': 'batch-A'}}],
               'completeness': {'enumerationExhausted': True, 'semanticCompleteness': 'UNVERIFIED'}}
    capture = {'captureRef': 'batch-ref', 'captureState': 'success', 'originalToolName': 'execute_semantic_plan', 'resourceId': 'test-source',
               'sourceAdapter': {'id': 'budget-v2', 'version': '1', 'options': {}},
               'request': {'plan': plan, 'contextId': context['contextId'], 'querySessionId': 'session-A'},
               'response': {'content': [{'type': 'text', 'text': json.dumps(receipt, ensure_ascii=False)}]}}
    selection = {'captureRef': 'batch-ref', 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1', 'metric2']}
    scope = {'label': '综合、投资板块' if aggregate else '所查机构', 'selectionReason': '预算查询范围', 'memberDimension': dimension, 'members': ['07', '05']}
    return capture, selection, scope, receipt


def update(capture, receipt):
    capture['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)


class BudgetBatchTest(TestCase):
    def test_partial_metric_selection_is_not_hidden_by_union_of_rows(self):
        capture, selection, scope, _ = batch_fixture()
        scope.pop('members')
        selections = [{**selection, 'columnNames': ['metric1'], 'rowIndices': [0]},
                      {**selection, 'columnNames': ['metric2'], 'rowIndices': [1]}]
        result = SourceRegistry([BudgetV2Adapter]).adapt({'batch-ref': capture}, selections, scope, 100)
        audit = result['scope']['sourceScopes'][0]
        self.assertEqual(audit['selectedRowCount'], 2)
        self.assertEqual(audit['missingSelectedCellCount'], 2)
        self.assertEqual(audit['omittedRows'][0]['missingColumns'], ['metric2'])
        self.assertEqual(audit['omittedRows'][1]['missingColumns'], ['metric1'])
        self.assertFalse(result['scope']['selectedDataComplete'])
        self.assertEqual(result['scope']['coverage'], 'PARTIAL')

    def test_members_are_derived_from_actual_rows_including_null_group(self):
        capture, selection, scope, receipt = batch_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['rows'][0][:2] = [None, None]
        update(capture, receipt)
        scope.pop('members')
        scope.pop('memberDimension')
        result = SourceRegistry([BudgetV2Adapter]).adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(result['scope']['memberDimension'], 'MD_ORG__MD_YWBK')
        self.assertEqual({fact['sourceObjectId'] for fact in result['facts']}, {None, '05'})
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')
        self.assertEqual(result['scope']['sourceScopes'][0]['returnedRowCount'], 2)
        self.assertEqual(result['scope']['sourceScopes'][0]['omittedRows'], [])

    def test_dropping_null_group_requires_explicit_exclusion_and_preserves_amount(self):
        capture, selection, scope, receipt = batch_fixture()
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        table['data']['rows'][0][:3] = [None, None, '238432697.67']
        update(capture, receipt)
        registry = SourceRegistry([BudgetV2Adapter])
        scope.pop('members')
        selection['rowIndices'] = [1]
        with self.assertRaises(AnalysisError) as caught:
            registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(caught.exception.code, 'UNDECLARED_SCOPE_EXCLUSION')
        entries = registry.catalog(capture, scope['memberDimension'])
        null_ref = next(item['memberRef'] for item in entries if item['sourceObjectId'] is None)
        scope['excludedMembers'] = [{'captureRef': 'batch-ref', 'objectId': null_ref, 'reason': '用户明确只分析有归属的分组'}]
        result = registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        audit = result['scope']['sourceScopes'][0]
        self.assertEqual(audit['omittedRows'][0]['values']['metric1'], '238432697.67')
        self.assertEqual(audit['selectedRowCount'], 1)
        self.assertFalse(result['scope']['populationComplete'])
        # 主体使用业务 key，排除空属性时单独使用 memberRef，也必须正确归一化。
        scope['members'] = ['05']
        result = registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(result['scope']['sourceScopes'][0]['omittedRows'][0]['values']['metric1'], '238432697.67')

    def test_bad_member_reference_identifies_exact_parameter_without_creating_raw_key(self):
        capture, selection, scope, _ = batch_fixture()
        registry = SourceRegistry([BudgetV2Adapter])
        scope['members'] = list(dict.fromkeys(item['memberRef'] for item in registry.catalog(capture, scope['memberDimension'])))
        scope['members'][1] = 'member_' + '0' * 24
        with self.assertRaises(AnalysisError) as caught:
            registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(caught.exception.code, 'UNKNOWN_MEMBER_REFERENCE')
        self.assertEqual(caught.exception.details['path'], '/scope/members/1')

    def test_two_accounts_of_same_measure_remain_independent_with_source_labels(self):
        capture, selection, scope, _ = batch_fixture()
        result = adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual([fact['value'] for fact in result['facts']], ['85084700.00', '2965.89', '1209100.00', '609.20'])
        self.assertEqual(len({fact['factId'] for fact in result['facts']}), 4)
        self.assertEqual({fact['sourceMeasureCode'] for fact in result['facts']}, {'ZB_JE2'})
        self.assertEqual({fact['measureCode'] for fact in result['facts']}, {'metric1', 'metric2'})
        self.assertEqual(result['facts'][0]['dimensions']['MD_BUDACCOUNT'], '0205')
        self.assertEqual(result['facts'][1]['dimensions']['MD_BUDACCOUNT'], '0206')
        self.assertEqual(result['facts'][0]['objectLabel'], '综合')
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')
        self.assertTrue(all(fact['cellAddress'] is None for fact in result['facts']))
        jsonschema.validate(selection, SELECTOR)

    def test_null_remains_missing_and_zero_remains_present(self):
        capture, selection, scope, receipt = batch_fixture(null_value=True)
        receipt['data']['stepResults'][0]['batchResults'][0]['data']['rows'][0][2] = '0.00'
        update(capture, receipt)
        result = adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual((result['facts'][0]['value'], result['facts'][0]['valueState']), ('0', 'PRESENT'))
        self.assertEqual((result['facts'][2]['value'], result['facts'][2]['valueState']), (None, 'NO_VALUE'))
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')

    def test_sector_cannot_be_reinterpreted_as_organization(self):
        capture, selection, scope, _ = batch_fixture()
        scope['memberDimension'] = 'MD_ORG'
        with self.assertRaisesRegex(AnalysisError, '实际结果粒度'):
            adapt({'batch-ref': capture}, [selection], scope, 100)

    def test_shape_and_original_request_are_checked(self):
        for mutation in ('where', 'order', 'account', 'binding', 'row', 'numeric'):
            capture, selection, scope, receipt = batch_fixture()
            table = receipt['data']['stepResults'][0]['batchResults'][0]
            if mutation == 'where': table['request']['batchDimensions'][0]['where']['value'] = ['01']
            if mutation == 'order': table['request']['orderBy'][0]['direction'] = 'ASC'
            if mutation == 'account': table['metrics'][0]['selectedDimensions']['MD_BUDACCOUNT'] = '0206'
            if mutation == 'binding': table['metrics'][0]['context']['target']['formBindingKey'] = 'another'
            if mutation == 'row': table['data']['rows'][0].pop()
            if mutation == 'numeric': table['data']['rows'][0][2] = 123.01
            update(capture, receipt)
            with self.subTest(mutation=mutation), self.assertRaises(AnalysisError):
                adapt({'batch-ref': capture}, [selection], scope, 100)

    def test_failed_or_fallback_is_not_empty_success(self):
        capture, selection, scope, receipt = batch_fixture()
        receipt['data']['stepResults'][0]['batchResults'][0]['disposition'] = 'PARTIAL'
        update(capture, receipt)
        self.assertEqual(catalog(capture), [])
        with self.assertRaisesRegex(AnalysisError, '批量查询成功'):
            adapt({'batch-ref': capture}, [selection], scope, 100)

    def test_registry_selection_names_and_numeric_tools_reuse(self):
        capture, selection, scope, _ = batch_fixture()
        registry = SourceRegistry([BudgetV2Adapter])
        self.assertEqual(len(registry.catalog(capture, scope['memberDimension'])), 4)
        result = registry.adapt({'batch-ref': capture}, [selection], scope, 100)
        first = result['facts'][0]
        self.assertEqual(calculate('a-b', {'a': first['value'], 'b': result['facts'][2]['value']})['value'], '83875600')
        self.assertEqual(BudgetV2Adapter().selectors(capture)[-1]['memberDimensions'], ['MD_ORG__MD_YWBK'])

    def test_column_name_is_identity_even_when_labels_collide(self):
        capture, selection, scope, receipt = batch_fixture()
        for column in receipt['data']['stepResults'][0]['batchResults'][0]['data']['columns'][2:]:
            column['label'] = '2位小数货币型指标'
        update(capture, receipt)
        self.assertEqual(len(adapt({'batch-ref': capture}, [selection], scope, 100)['facts']), 4)

    def test_large_read_keeps_columns_null_and_decimal_text(self):
        capture, _, _, receipt = batch_fixture()
        data = receipt['data']['stepResults'][0]['batchResults'][0]['data']
        data['rows'] = [[str(index), '成员', '12345678901234567890.00000001', None] for index in range(1501)]
        data['rowCount'] = len(data['rows'])
        original = encode(receipt)
        compact = compact_receipt(receipt, 'batch-ref', len(original))
        self.assertIsNotNone(compact)
        self.assertLessEqual(len(encode(compact)), 24000)
        page = read_receipt(receipt, 'batch-ref', '/data/stepResults/0/batchResults/0/data/rows', 1490, 20)
        self.assertEqual(len(page['items']), 11)
        self.assertEqual(page['items'][0][2:], ['12345678901234567890.00000001', None])
        self.assertEqual(page['tableContext']['columns'], data['columns'])
        self.assertIsNone(page['nextOffset'])
        self.assertEqual(original, encode(receipt))

    def test_scope_or_rows_missing_stays_partial(self):
        capture, selection, scope, _ = batch_fixture()
        selection['rowIndices'] = [0]
        result = adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(result['scope']['coverage'], 'PARTIAL')
        self.assertEqual(result['facts'][-1]['valueState'], 'NOT_QUERIED')

    def test_invalid_row_selectors_fail_with_a_business_diagnostic(self):
        for indices in (None, '0', [True], [0, 0], [{}], [-1], [2]):
            capture, selection, scope, _ = batch_fixture()
            selection['rowIndices'] = indices
            with self.subTest(indices=indices), self.assertRaises(AnalysisError):
                adapt({'batch-ref': capture}, [selection], scope, 100)

    def test_null_and_boolean_property_groups_use_typed_member_references(self):
        registry = SourceRegistry([BudgetV2Adapter])
        for column_type, keys in [('STRING', [None, '05']), ('BOOLEAN', [False, True])]:
            capture, selection, scope, receipt = batch_fixture()
            table = receipt['data']['stepResults'][0]['batchResults'][0]
            table['data']['columns'][0]['type'] = column_type
            for row, key in zip(table['data']['rows'], keys):
                row[0] = key
                if key is None or type(key) is bool:
                    row[1] = None
            update(capture, receipt)
            entries = registry.catalog(capture, scope['memberDimension'])
            scope['members'] = list(dict.fromkeys(entry['memberRef'] for entry in entries))
            result = registry.adapt({'batch-ref': capture}, [selection], scope, 100)
            self.assertEqual(len(result['facts']), 4)
            self.assertEqual(result['scope']['coverage'], 'COMPLETE')
            self.assertEqual({fact['sourceObjectId'] for fact in result['facts']}, set(keys))
            self.assertTrue(all(fact['objectLabel'] for fact in result['facts']))

    def test_period_rows_keep_missing_year_and_actual_data_period(self):
        capture, selection, scope, receipt = batch_fixture(aggregate=False)
        table = receipt['data']['stepResults'][0]['batchResults'][0]
        query = capture['request']['plan']['steps'][1]['batchQuery']
        period = {'dimCode': 'DATATIME', 'periodRequest': {'kind': 'RANGE', 'start': {'type': 'N', 'year': 2024}, 'end': {'type': 'N', 'year': 2026}}}
        query['batchDimensions'] = [period]
        table['request']['batchDimensions'] = deepcopy(query['batchDimensions'])
        table['data']['fixedDimValMap'].pop('DATATIME')
        table['data']['columns'][0]['name'] = 'DATATIME'
        table['data']['columns'][1]['name'] = 'DATATIME_NAME'
        table['columnRoles'] = {'DATATIME': {'kind': 'DIMENSION', 'valueColumn': 'DATATIME', 'dimCode': 'DATATIME'},
                                'DATATIME_NAME': {'kind': 'NAME', 'valueColumn': 'DATATIME', 'dimCode': 'DATATIME'},
                                'metric1': {'kind': 'METRIC'}, 'metric2': {'kind': 'METRIC'}}
        table['data']['rows'] = [['2024N0001', '2024年', None, None], ['2025N0001', '2025年', '10', '3'], ['2026N0001', '2026年', '20', '4']]
        table['data']['rowCount'] = 3
        scope.update(memberDimension='DATATIME', members=['2024N0001', '2025N0001', '2026N0001'])
        update(capture, receipt)
        result = adapt({'batch-ref': capture}, [selection], scope, 100)
        self.assertEqual(result['scope']['coverage'], 'COMPLETE')
        self.assertEqual(result['facts'][0]['executionPeriod'], '2024N0001')
        self.assertIsNone(result['facts'][0]['value'])


class BatchHostIntegrationTest(IsolatedAsyncioTestCase):
    async def test_identical_bad_build_skips_execution_until_input_is_repaired(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from analysis_extension.tools import AnalysisBuild
        diagnostic = AnalysisError('UNKNOWN_MEMBER_REFERENCE', '成员引用错误', {'path': '/scope/members/0'})
        service = SimpleNamespace(turn=SimpleNamespace(state={}), build=AsyncMock(side_effect=diagnostic))
        tool = AnalysisBuild(service)
        await tool.call(scope={'members': ['bad']}, requestKey='a')
        repeated = await tool.call(scope={'members': ['bad']}, requestKey='b')
        self.assertEqual(service.build.await_count, 1)
        error = json.loads(repeated.content[0].text)['error']
        self.assertTrue(error['details']['repeatedRequest'])
        self.assertEqual(error['details']['path'], '/scope/members/0')
        service.build.side_effect = None
        service.build.return_value = {'analysisRef': 'repaired'}
        await tool.call(scope={}, requestKey='c')
        self.assertEqual(service.build.await_count, 2)

    async def test_capture_inventory_build_and_report_values_keep_sector_grain(self):
        from analysis_extension.store import AnalysisStore
        from analysis_extension.service import AnalysisService
        from analysis_extension.inventory import inventory
        from analysis_extension.tables import measure_key
        capture, selection, scope, _ = batch_fixture(null_value=True)
        capture.update(toolCallId='batch-call', toolName='execute_semantic_plan', capturedAt='2026-01-01T00:00:01Z')
        async def authorized():
            return None
        with tempfile.TemporaryDirectory() as directory:
            store = AnalysisStore(directory)
            ref = store.capture(analysis_fixtures.OWNER, capture)
            selection['captureRef'] = ref
            service = AnalysisService(store, analysis_fixtures.OWNER, authorized)
            contexts = await service.inspect(section='queryContexts', limit=1)
            self.assertEqual(contexts['order'], 'NEWEST_FIRST')
            reused = contexts['items'][0]['queryContext']['batchQueries'][0]
            self.assertEqual(reused['requestedScope']['metrics'], capture['request']['plan']['steps'][1]['batchQuery']['metrics'])
            self.assertEqual(reused['results'][0]['metricContexts'][0]['context']['target']['organizationCategory'], 'MD_ORG_BUD')
            self.assertEqual(reused['results'][0]['metricContexts'][0]['reuseSelection'],
                             {'observationId': 'probe-0', 'cellKey': '1:1', 'periodCode': '2026N0001'})
            self.assertEqual(reused['results'][0]['budTaskId'], 'task-A')
            values = inventory(store, analysis_fixtures.OWNER, 0, 100, 'MD_ORG__MD_YWBK')
            self.assertEqual(values['items'][0]['objectLabel'], '综合')
            self.assertEqual(values['items'][0]['objectLabelState'], 'RESOLVED')
            matched = await service.inspect(section='capture', captureRef=ref, search='85084700', memberDimension='MD_ORG__MD_YWBK')
            self.assertEqual(len(matched['items']), 2)
            built = await service.build([ref], [selection], scope, [], 'batch-build')
            artifact = store.get_analysis(analysis_fixtures.OWNER, built['analysisRef'])
            self.assertEqual(len(artifact['facts']), 4)
            self.assertEqual(len({measure_key(fact, artifact['scope']) for fact in artifact['facts']}), 2)
            self.assertEqual({fact['objectId'] for fact in artifact['facts']}, {'07', '05'})
            self.assertEqual(artifact['facts'][2]['valueState'], 'NO_VALUE')
            self.assertTrue(built['verificationTasks'])
