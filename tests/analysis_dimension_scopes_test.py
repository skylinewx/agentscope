from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from analysis_extension.dimension_scopes import check_scope_evidence, inherit, snapshot, validate_execution
from analysis_extension.models import AnalysisError
from analysis_extension.query_attempts import failure_key
from numeric_tools import calculate_requests


def scope(identifier='departments', hint='部门', text='所有部门'):
    return {'id': identifier, 'dimensionHint': hint, 'sourceText': text, 'selector': {'type': 'ALL'}}


class DimensionScopeTests(TestCase):
    def test_replacing_a_metric_with_its_bound_scenario_in_the_label_keeps_the_cohort_and_scenario(self):
        from analysis_extension.dimension_scopes import condition_history, retained_scopes

        def confirmed(term, *codes, source='source-one'):
            return {'conditionId': '/'.join(codes), 'appliesToAllTargets': True,
                    'confirmedSource': {'executionId': source, 'contextId': 'context', 'querySessionId': 'session'},
                    'draft': {'id': codes[0], 'dimensionHint': term, 'sourceText': term,
                              'selector': {'type': 'VALUES', 'members': [term]}},
                    'bindings': [{'dimCode': code} for code in codes]}

        subject = confirmed('两家企业', 'MD_ORG')
        metric = confirmed('净利润', 'ACCOUNT', 'INDICATOR')
        metric['metricTargetLabels'] = ['净利润预算', '预算净利润']
        scenario = confirmed('预算', 'MD_SCENE')
        product = confirmed('水泥', 'PRODUCT')
        state = {'followupContext': {'mode': 'CONTINUE', 'changedFields': ['metric']},
                 'previousQueryIntent': {'metric': '净利润预算', 'scenario': '预算'},
                 'queryIntent': {'metric': '营业收入预算', 'scenario': '预算'},
                 'inheritedDimensionScopes': [subject, metric, scenario, product]}
        for label in ('净利润预算', '预算净利润'):
            state['previousQueryIntent']['metric'] = label
            wire = inherit(SimpleNamespace(state=deepcopy(state)), {
                'basis': {'kind': 'MEMBERS', 'sourceRef': 'two', 'memberDimensions': ['MD_ORG']},
                'intentDraft': {'targets': [{'id': 'revenue'}]}})
            self.assertEqual(wire['basis']['sourceRef'], 'two')
            self.assertEqual([s['sourceText'] for s in wire['intentDraft']['dimensionScopes']], ['预算', '水泥'])
            self.assertEqual(retained_scopes(state), [subject, scenario, product])
            self.assertEqual(condition_history(state, []), retained_scopes(state))

        for current, records in [
                ('净利润', [metric, scenario]), ('营业收入', [metric]),
                ('营业收入', [metric, confirmed('预算', 'MD_SCENE', source='another')]),
                ('营业收入', [metric, confirmed('预算', 'VERSION')]),
                ('营业收入', [metric, scenario, confirmed('净利润', 'OTHER_ROLE')]),
                ('营业收入', [metric, scenario, confirmed('销售费用', 'ACCOUNT')]),
                ('营业收入', [{**metric, 'metricTargetLabels': []}, scenario])]:
            with self.subTest(current=current, conditions=[r['conditionId'] for r in records]):
                trial = {**state, 'queryIntent': {'metric': current}, 'inheritedDimensionScopes': records}
                self.assertEqual(retained_scopes(trial), records)

    def test_source_cell_heading_distinguishes_the_queried_metric_from_an_independent_product_filter(self):
        from analysis_extension.condition_context import _metric_target_labels
        def result(key, term, label, headings):
            coord = {'dimensionCode': 'D', 'baseDataCode': 'V', 'objectCode': key}
            return {'targetId': key, 'resolvedTerms': [{**coord, 'term': term, 'label': label}],
                    'rows': [{'conditionEvidence': [{**coord, 'runtimeHeaders': headings}]}]}
        targets = [{'id': k, 'label': term+'预算', 'allOf': [{'term': term}, {'term': '预算'}]}
                   for k, term in [('A', '经营结余'), ('P', '砂石')]]
        data = {'targets': targets, 'resultSets': [result('A','经营结余','六、经营结余',['六、经营结余','预算数']),
                result('P','砂石','砂石',['营业收入','预算数'])]}
        self.assertEqual(_metric_target_labels(data), {('D','V','A'):{'经营结余预算'}})

    def test_named_all_accepts_only_the_executed_root_and_descendants(self):
        from analysis_extension.dimension_scopes import _scope_admitted
        from analysis_extension.scope_changes import _validate_selector
        _validate_selector({'type': 'ALL', 'rootText': '示例集团'})
        expected = {'dimCode': 'MD_ORG', 'hierarchy': {'rootCode': 'G', 'includeSelf': True}}
        physical = {'dimCode': 'MD_ORG', 'values': ['A', 'G']}
        batch = {'diagnostics': {'organizationSubtree': {'condition': expected, 'executedCondition': physical}}}
        for code in ('G', 'A'):
            self.assertTrue(_scope_admitted(expected, physical, batch, {'dimensions': {'MD_ORG': code}}))
        self.assertFalse(_scope_admitted(expected, physical, batch, {'dimensions': {'MD_ORG': 'sibling'}}))
        self.assertFalse(_scope_admitted(expected, {**physical, 'values': ['A']}, batch, {'dimensions': {'MD_ORG': 'A'}}))
        self.assertFalse(_scope_admitted(expected, physical, {}, {'dimensions': {'MD_ORG': 'G'}}))
        self.assertFalse(_scope_admitted({'dimCode': 'MD_ORG', 'hierarchy': {'rootCode': 'G'}}, physical, batch, {'dimensions': {'MD_ORG': 'G'}}))

    def test_followup_without_an_organization_retains_selected_members_and_current_member_edit_is_applied(self):
        from analysis_extension.scope_changes import apply_query_scopes, apply_scope_edits, reconcile_selectors
        hierarchy = {'type': 'HIERARCHY', 'rootText': '测试集团', 'level': 2}
        record = {'draft': {**scope(hint='MD_ORG', text='旗下二级单位'), 'selector': hierarchy},
                  'appliesToAllTargets': True}
        state = {'request': '查另一指标', 'followupContext': {'mode': 'NEW'},
                 'inheritedDimensionScopes': [record]}
        turn = SimpleNamespace(state=deepcopy(state))
        self.assertEqual(inherit(turn, {'intentDraft': {}})['intentDraft']['dimensionScopes'][0]['selector'], hierarchy)
        turn = SimpleNamespace(state={**deepcopy(state), 'request': '查测试集团的另一指标'})
        member = {'type': 'VALUES', 'members': ['测试集团']}
        apply_scope_edits(turn, [{'dimensionHint': '组织机构', 'selector': member}])
        request = {'intentDraft': {}}
        apply_query_scopes(turn.state, request)
        reconcile_selectors(turn, request)
        self.assertEqual([s['selector'] for s in inherit(turn, request)['intentDraft']['dimensionScopes']], [member])

    def test_followup_root_patch_preserves_level_but_explicit_replacement_clears_it(self):
        from analysis_extension.scope_changes import reconcile_selectors
        old = {'id': 'org', 'dimensionHint': '组织机构', 'sourceText': '二级单位',
               'selector': {'type': 'HIERARCHY', 'rootText': '集团', 'level': 2}}
        record = {'draft': old, 'appliesToAllTargets': True,
                  'confirmedSource': {'executionId': 'prior'}}
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                turn = SimpleNamespace(state={'followupContext': {'mode': 'CONTINUE', 'changedFields': ['population']},
                    'inheritedDimensionScopes': [deepcopy(record)],
                    'scopeEdits': [{'dimensionHint': '组织机构', 'selector': {'type': 'HIERARCHY', 'rootText': '集团'}}] if replacement else []})
                request = {'intentDraft': {'dimensionScopes': [{**deepcopy(old),
                    'selector': {'type': 'HIERARCHY', 'rootText': '集团'}}]}}
                reconcile_selectors(turn, request)
                actual = request['intentDraft']['dimensionScopes'][0]['selector']
                self.assertEqual(actual.get('level'), None if replacement else 2)
                self.assertEqual('population' in turn.state['followupContext']['changedFields'], replacement)

    def test_explicit_value_switch_cannot_reuse_previous_fact_identity(self):
        from analysis_extension.followup_scope import prepare_request
        origin = {'originalToolName': 'execute_budget_query'}
        reference = {'executionId': 'old', 'resultSetId': 'old-set', 'factIds': ['old-fact']}
        for field in ('population', 'period', 'scenario', 'metric'):
            with self.subTest(field=field):
                turn = SimpleNamespace(state={'request': '改查另一个值',
                    'followupContext': {'mode': 'CONTINUE', 'changedFields': [field]}})
                with self.assertRaises(AnalysisError) as failure:
                    prepare_request(turn, origin, {'sourceSelection': reference})
                self.assertEqual(failure.exception.code, 'QUERY_CONTRACT_CHANGED')
        turn = SimpleNamespace(state={'request': '解释刚才的结果', 'followupContext': {'mode': 'CONTINUE'}})
        with self.assertRaises(AnalysisError):
            prepare_request(turn, origin, {'sourceSelection': reference})

    def test_confirmed_hierarchy_keeps_root_and_original_wording_on_next_analysis(self):
        old = {'id': 'units', 'dimensionHint': '组织机构', 'sourceText': '二级单位',
               'selector': {'type': 'HIERARCHY', 'rootText': '久其集团', 'level': 2}}
        record = {'draft': old, 'appliesToAllTargets': True,
                  'confirmedSource': {'executionId': 'prior-query', 'scopeId': 'units', 'contextId': 'original-context', 'querySessionId': 'original-session'}}
        turn = SimpleNamespace(state={'followupContext': {'mode': 'CONTINUE'}, 'inheritedDimensionScopes': [record]})
        for current in ([], [{**deepcopy(old), 'sourceText': '久其集团二级单位'}]):
            request = {'question': '分析一下亏损企业', 'intentDraft': {'dimensionScopes': current}}
            result = inherit(turn, request)
            actual = result['intentDraft']['dimensionScopes'][0]
            self.assertEqual(actual['sourceText'], '二级单位')
            self.assertEqual(actual['confirmedSource'], record['confirmedSource'])
            self.assertIn('久其集团', result['contextText'])
            self.assertNotIn('二级单位', result['contextText'])
            check_scope_evidence(result)
        turn.state['followupContext']['changedDimensions'] = ['MD_ORG']
        result = inherit(turn, {'question': '换个机构', 'intentDraft': {'dimensionScopes': []}})
        self.assertNotIn('contextText', result)
        self.assertEqual(result['intentDraft']['dimensionScopes'], [])

    def test_changed_scenario_removes_both_confirmed_scope_and_inherited_text(self):
        from analysis_extension.dimension_scopes import remember
        from analysis_extension.followup_scope import prepare_request
        receipt = {'status': 'SUCCEEDED', 'executionId': 'old-execution', 'context': {'contextId': 'original-context'}, 'querySessionId': 'original-session', 'data': {
            'resultSets': [{'resolvedTerms': [{'term': '预算', 'dimensionCode': 'MD_SCENE',
                'baseDataCode': 'MD_SCENE', 'objectCode': 'BUDGET'}]}],
            'dimensionCatalog': [{'dimCode': 'MD_SCENE', 'valueDomain': 'MD_SCENE',
                                  'label': '业务口径', 'names': ['业务口径', '情景']} ]}}
        capture = {'captureRef': 'old', 'originalToolName': 'execute_budget_query', 'response': receipt,
                   'request': {'question': '久其集团净利润预算', 'intentDraft': {'targets': [{'id': 't'}],
                               'sharedConditions': [{'text': '预算', 'sourceText': '预算'}]}}}
        service = SimpleNamespace(owner='owner',
            store=SimpleNamespace(get_capture=lambda owner, ref: capture),
            sources=SimpleNamespace(for_capture=lambda value: SimpleNamespace(raw_receipt=lambda response: response)))
        turn = SimpleNamespace(state={}, service=service)
        remember(turn, capture)
        scopes = snapshot(turn.state, ['old']) + [{'draft': scope(), 'appliesToAllTargets': True,
            'confirmedSource': {'executionId': 'department-execution', 'scopeId': 'departments', 'contextId': 'original-context', 'querySessionId': 'original-session'}}]
        for change in ({'changedFields': ['scenario']}, {'changedDimensions': ['情景']},
                       {'changedDimensions': ['MD_SCENE']}, {'changedDimensions': ['业务口径']},
                       {'basePendingChanges': ['scenario']}):
            with self.subTest(change=change):
                turn.state = {'request': '改看实际数', 'inheritedDimensionScopes': deepcopy(scopes),
                    'previousQueryIntent': {'scenario': '预算', 'metric': '净利润', 'population': '久其集团'},
                    'inheritedScope': {'captureRefs': ['old']},
                    'basePendingChanges': change.get('basePendingChanges', []),
                    'followupContext': {'mode': 'CONTINUE', **{k: v for k, v in change.items() if k != 'basePendingChanges'}}}
                result = prepare_request(turn, capture, {'intentDraft': {}})
                self.assertEqual([item['dimensionHint'] for item in result['intentDraft']['dimensionScopes']], ['部门'])
                self.assertNotIn('预算', result['contextText'])
                self.assertIn('净利润', result['contextText'])
                self.assertIn('久其集团', result['contextText'])
                self.assertEqual(result['intentDraft']['dimensionScopes'][0]['sourceText'], '所有部门')
                self.assertNotIn('所有部门', result['contextText'])
        turn.state['followupContext'] = {'mode': 'CONTINUE'}
        turn.state['basePendingChanges'] = []
        result = prepare_request(turn, capture, {'intentDraft': {}})
        self.assertEqual(result['intentDraft']['dimensionScopes'][0]['selector']['members'], ['预算'])
        self.assertIn('预算', result['contextText'])

    def test_period_and_population_changes_release_only_their_confirmed_dimension(self):
        from analysis_extension.followup_scope import _changes
        records = [{'draft': scope(hint=label, text=value), 'appliesToAllTargets': True,
                    'bindings': [{'dimCode': code}]} for label, code, value in
                   [('期间', 'DATATIME', '2026年'), ('机构', 'MD_ORG', '久其集团'), ('部门', 'DEPT', '所有部门')]]
        for field, code, removed in [('period', 'DATATIME', '期间'), ('population', 'MD_ORG', '机构')]:
            for declaration in [{'changedFields': [field]}, {'changedDimensions': [code]}]:
                turn = SimpleNamespace(state={'followupContext': declaration, 'inheritedDimensionScopes': records})
                result = inherit(turn, {'intentDraft': {}})
                self.assertNotIn(removed, [item['dimensionHint'] for item in result['intentDraft']['dimensionScopes']])
                self.assertIn(field, _changes(turn))

    def test_singleton_scope_checks_exact_request_and_native_result_instead_of_requiring_range_protocol(self):
        draft = {**scope(text='研发部'), 'selector': {'type': 'VALUES', 'members': ['研发部']}}
        condition = {'dimCode': 'DEPT', 'values': ['A']}
        dimensions = {'DEPT': 'A', 'MD_ORG': 'GT00', 'DATATIME': '2026J0002'}
        target = {'targetRef': 'grid/3:4', 'metric': 'metric1', 'dimensions': dimensions}
        native = {**target, 'measureCode': 'AMT'}
        batch = {'batchId': 'batch', 'disposition': 'SUCCEEDED', 'request': {'cellTargets': [target]},
                 'data': {'cellResults': [native]}}
        request = {'question': '研发部预算', 'intentDraft': {'dimensionScopes': [draft]}}
        data = {'scopeVerification': {'bindingComplete': True},
                'scopeResolution': [{**draft, 'status': 'RESOLVED', 'targetId': 't', 'binding': 'b',
                                     'dimCode': 'DEPT', 'condition': condition}],
                'targets': [{'id': 't'}], 'origins': [{'originId': 'o', 'batchId': 'batch', 'targetRef': 'grid/3:4'}],
                'stepResults': [{'batchResults': [batch]}],
                'resultSets': [{'targetId': 't', 'context': {'target': {'formBindingKey': 'b'}},
                               'rows': [{'valueState': 'PRESENT', 'originRefs': ['o'], 'dimensions': dimensions, 'measureCode': 'AMT'}]}]}
        validate_execution(request, data)
        for changed in ({'targetRef': 'other'}, {'dimensions': {**dimensions, 'DEPT': 'B'}}, {'measureCode': 'OTHER'}):
            batch['data']['cellResults'] = [{**native, **changed}]
            with self.assertRaisesRegex(AnalysisError, '实际批量请求'):
                validate_execution(request, data)
        batch['data']['cellResults'] = [native]
        request['question'] = '所有部门预算'
        request['intentDraft']['dimensionScopes'] = [scope()]
        data['scopeResolution'][0]['condition'] = {'dimCode': 'DEPT', 'all': True}
        with self.assertRaisesRegex(AnalysisError, '实际批量请求'):
            validate_execution(request, data)

    def test_host_defers_range_semantics_and_checks_only_supplied_scope_evidence(self):
        for question in ('查询所有部门预算', '查询所有项目预算', '金额全部用万元展示', '读取全部指标和所有数据'):
            check_scope_evidence({'question': question})
        check_scope_evidence({'question': '所有部门预算', 'intentDraft': {'dimensionScopes': [scope()]}})
        check_scope_evidence({'question': '继续分析', 'contextText': '所有部门',
                              'intentDraft': {'dimensionScopes': [scope()]}})
        with self.assertRaises(AnalysisError):
            check_scope_evidence({'question': '查部门甲预算', 'intentDraft': {'dimensionScopes': [scope()]}})

    def test_followup_changes_only_one_dimension_and_keeps_the_other(self):
        departments, projects = scope(), scope('projects', '项目', '全部项目')
        turn = SimpleNamespace(state={'followupContext': {'mode': 'CONTINUE', 'changedDimensions': ['部门']},
            'inheritedDimensionScopes': [{'draft': item, 'appliesToAllTargets': True} for item in (departments, projects)]})
        new = {**scope(text='研发部'), 'selector': {'type': 'VALUES', 'members': ['研发部']}}
        result = inherit(turn, {'intentDraft': {'dimensionScopes': [new]}})
        self.assertEqual([item['dimensionHint'] for item in result['intentDraft']['dimensionScopes']], ['部门', '项目'])
        self.assertEqual(result['intentDraft']['dimensionScopes'][1]['selector'], {'type': 'ALL'})
        self.assertEqual(result['intentDraft']['dimensionScopes'][1]['sourceText'], '全部项目')
        self.assertNotIn('全部项目', result.get('contextText', ''))

    def test_undeclared_range_change_and_cross_target_expansion_are_rejected(self):
        turn = SimpleNamespace(state={'followupContext': {'mode': 'CONTINUE'},
            'inheritedDimensionScopes': [{'draft': scope(), 'appliesToAllTargets': True}]})
        with self.assertRaisesRegex(AnalysisError, 'scopeEdits'):
            inherit(turn, {'intentDraft': {'dimensionScopes': [{**scope(), 'selector': {'type': 'VALUES', 'members': ['A']}}]}})
        turn.state['inheritedDimensionScopes'][0]['appliesToAllTargets'] = False
        with self.assertRaisesRegex(AnalysisError, '部分指标'):
            inherit(turn, {'intentDraft': {}})

    def test_server_cannot_claim_all_while_executing_the_default_value(self):
        request = {'question': '所有部门预算', 'intentDraft': {'dimensionScopes': [scope()]}}
        data = {'scopeVerification': {'bindingComplete': True},
            'scopeResolution': [{**scope(), 'status': 'RESOLVED', 'targetId': 't', 'binding': 'b', 'dimCode': 'DEPT',
                                 'condition': {'dimCode': 'DEPT', 'all': True}}],
            'targets': [{'id': 't'}], 'origins': [{'originId': 'o', 'batchId': 'batch'}],
            'stepResults': [{'batchResults': [{'batchId': 'batch', 'disposition': 'SUCCEEDED', 'request': {
                'batchDimensions': [{'dimCode': 'DEPT', 'values': ['default']}]} }]}],
            'resultSets': [{'targetId': 't', 'context': {'target': {'formBindingKey': 'b'}},
                            'rows': [{'valueState': 'PRESENT', 'originRefs': ['o']}]}]}
        with self.assertRaisesRegex(AnalysisError, '实际批量请求'):
            validate_execution(request, data)
        data['stepResults'][0]['batchResults'][0]['request']['batchDimensions'] = [{'dimCode': 'DEPT', 'all': True}]
        validate_execution(request, data)

    def test_fixed_coordinate_compilation_requires_matching_runtime_and_range_receipts(self):
        from analysis_extension.dimension_scopes import _scope_admitted
        expected = {'dimCode': 'DEPT', 'all': True}
        actual = {'dimCode': 'DEPT', 'values': ['A']}
        row = {'dimensions': {'DEPT': 'A'}}
        proof = {'condition': expected, 'executedCondition': actual, 'value': 'A',
                 'observationId': 'grid', 'cellKeys': ['3:4']}
        batch = {'metrics': [{'selectionCellKey': '3:4', 'selectionObservationId': 'grid',
                             'selectedDimensions': {'DEPT': 'A'}}], 'diagnostics': {'scopeIntersections': [proof]}}
        self.assertFalse(_scope_admitted(expected, actual, {}, row))
        self.assertTrue(_scope_admitted(expected, actual, batch, row))
        self.assertFalse(_scope_admitted(expected, actual, batch, {'dimensions': {'DEPT': 'B'}}))
        proof['observationId'] = 'wrong'
        self.assertFalse(_scope_admitted(expected, actual, batch, row))
        proof['observationId'] = 'grid'
        expected = {'dimCode': 'DEPT', 'where': {'field': 'sector', 'op': 'EQ', 'value': 'IT'}}
        proof['condition'] = expected
        self.assertFalse(_scope_admitted(expected, actual, batch, row))
        batch['diagnostics']['scopeRangeResolution'] = {'disposition': 'SUCCEEDED', 'request': {'batchDimensions': [expected]},
                                    'data': {'columns': [{'name': 'DEPT'}], 'rows': [['B']]}}
        self.assertFalse(_scope_admitted(expected, actual, batch, row))
        batch['diagnostics']['scopeRangeResolution']['data']['rows'] = [['A']]
        self.assertTrue(_scope_admitted(expected, actual, batch, row))

    def test_bound_dimension_alias_can_replace_one_scope_without_preserving_the_old_range(self):
        previous = {'draft': scope(), 'appliesToAllTargets': True,
                    'dimensionAliases': ['部门', '责任中心', 'DEPT']}
        turn = SimpleNamespace(state={'followupContext': {'changedDimensions': ['责任中心']},
                                      'inheritedDimensionScopes': [previous]})
        new = {**scope(hint='责任中心', text='研发中心'), 'selector': {'type': 'VALUES', 'members': ['研发中心']}}
        result = inherit(turn, {'intentDraft': {'dimensionScopes': [new]}})
        self.assertEqual(result['intentDraft']['dimensionScopes'], [new])

    def test_conflicting_prior_scopes_remain_visible_and_cannot_silently_be_dropped(self):
        first = {'draft': scope(), 'appliesToAllTargets': True}
        second = {'draft': {**scope(text='研发部'), 'selector': {'type': 'VALUES', 'members': ['研发部']}},
                  'appliesToAllTargets': True}
        records = snapshot({'dimensionScopeCaptures': {'a': [first], 'b': [second]}}, ['a', 'b'])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record['ambiguousScope'] for record in records))
        turn = SimpleNamespace(state={'inheritedDimensionScopes': records})
        with self.assertRaisesRegex(AnalysisError, '不同范围'):
            inherit(turn, {'intentDraft': {}})

    def test_new_query_checks_only_applicable_per_target_history_without_erasing_evidence(self):
        def bound(key, term, shared=False, dim='METRIC'):
            return {'conditionId': key, 'confirmedTurn': 2, 'appliesToAllTargets': shared,
                    'draft': {'id': key, 'dimensionHint': dim, 'sourceText': term, 'targetIds': [],
                              'selector': {'type': 'VALUES', 'members': [term]}},
                    'bindings': [{'dimCode': dim, 'valueDomain': 'shared-dictionary'}],
                    'confirmedSource': {'executionId': 'execution', 'scopeId': key,
                                        'contextId': 'context', 'querySessionId': 'session'}}
        a, b = bound('a', '周转额'), bound('b', '调度费')
        org = bound('o', '甲机构', True, 'MD_ORG')
        common = bound('p', '甲产品', True, 'PRODUCT')
        prior = snapshot({'dimensionScopeCaptures': {'capture': [a, b, org, common]}}, ['capture'])
        def query(question, scopes=(), mode='NEW', targets=()):
            turn = SimpleNamespace(state={'inheritedDimensionScopes': deepcopy(prior), 'followupContext': {'mode': mode}})
            result = inherit(turn, {'question': question, 'intentDraft': {'dimensionScopes': deepcopy(list(scopes)), 'targets': list(targets)}})
            self.assertEqual(turn.state['inheritedDimensionScopes'], prior)
            return result['intentDraft']['dimensionScopes']
        self.assertEqual([s['id'] for s in query('查询安全支出')], ['o', 'p'])
        targets = [{'id': 'current', 'businessPhrases': [{'text': '安全支出', 'sourceText': '安全支出'}]}]
        self.assertEqual([s['id'] for s in query('查询安全支出', mode='CONTINUE', targets=targets)], ['o', 'p'])
        selected = {**a['draft'], 'targetIds': ['current'], 'confirmedSource': {'scopeId': 'a'}}
        kept = query('继续查指定项', [selected])
        self.assertEqual(kept[0]['confirmedSource'], a['confirmedSource'])
        self.assertEqual(kept[0]['targetIds'], ['current'])
        with self.assertRaises(AnalysisError) as ambiguous:
            query('继续看', mode='CONTINUE')
        self.assertEqual(ambiguous.exception.code, 'FOLLOWUP_DIMENSION_SCOPE_AMBIGUOUS')
        with self.assertRaises(AnalysisError) as no_owner:
            query('再查周转额')
        self.assertEqual(no_owner.exception.code, 'FOLLOWUP_DIMENSION_TARGET_REQUIRED')

    def test_analysis_uses_verified_discovery_coordinates_even_with_concrete_grouped_values(self):
        condition = {'dimCode': 'DEPT', 'all': True}
        coordinate = {'binding': 'b', 'targetId': 't', 'dimensions': {'DEPT': 'A'},
                      'measureCode': 'AMT', 'modelCode': 'MODEL_N'}
        request = {'question': '分析所有部门预算', 'purpose': 'PREPARE_MATERIALS', 'intentDraft': {'dimensionScopes': [scope()]}}
        data = {'scopeVerification': {'bindingComplete': True, 'analysisCoordinates': [coordinate],
                    'selectorChecks': [{'id': 'departments', 'targetId': 't', 'binding': 'b',
                                        'executedConditions': [condition]}]},
                'scopeResolution': [{**scope(), 'status': 'RESOLVED', 'targetId': 't', 'binding': 'b',
                                     'dimCode': 'DEPT', 'condition': condition}],
                'targets': [{'id': 't'}], 'origins': [{'originId': 'o', 'batchId': 'concrete'}],
                'stepResults': [{'batchResults': [{'batchId': 'concrete', 'disposition': 'SUCCEEDED',
                    'request': {'batchDimensions': [{'dimCode': 'DEPT', 'values': ['A']}]}}]}],
                'resultSets': [{'targetId': 't', 'context': {'target': {'formBindingKey': 'b'}},
                    'rows': [{'valueState': 'PRESENT', 'originRefs': ['o'], 'dimensions': {'DEPT': 'A'},
                              'measureCode': 'AMT', 'cellExp': 'MODEL_N[AMT]'}]}]}
        validate_execution(request, data)
        data['resultSets'][0]['rows'][0]['cellExp'] = 'OTHER_N[AMT]'
        with self.assertRaisesRegex(AnalysisError, '实际批量请求'):
            validate_execution(request, data)

    def test_equivalent_failure_cannot_be_retried_by_renaming_references(self):
        origin = {'originalToolName': 'execute_budget_query'}
        request = {'question': '所有部门预算', 'intentDraft': {'targets': [
            {'id': 't', 'businessPhrases': [{'text': '预算'}]}],
            'relations': [{'type': 'COMPARE', 'targetIds': ['t'], 'sourceText': '预算'}],
            'dimensionScopes': [{**scope(), 'targetIds': ['t']}]}, 'groupBy': [{'scopeId': 'departments'}]}
        renamed = deepcopy(request)
        renamed['intentDraft']['targets'][0]['id'] = 'different'
        renamed['intentDraft']['relations'][0]['targetIds'] = ['different']
        renamed['intentDraft']['dimensionScopes'][0].update(id='new', targetIds=['different'])
        renamed['groupBy'] = [{'scopeId': 'new'}]
        self.assertEqual(failure_key(origin, request), failure_key(origin, renamed))
        renamed['intentDraft']['dimensionScopes'][0]['selector'] = {'type': 'VALUES', 'members': ['研发部']}
        self.assertNotEqual(failure_key(origin, request), failure_key(origin, renamed))

    def test_batch_calculation_preserves_precision_and_reports_partial_failure(self):
        result = calculate_requests(requests=[{'id': 'a', 'expression': '0.1 + 0.2'},
                                             {'id': 'b', 'expression': '1 / 0'}])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertFalse(result['complete'])
        self.assertEqual(result['results'][0]['value'], '0.3')
        self.assertIsNotNone(result['results'][1]['error'])


class SharedResultReadTests(IsolatedAsyncioTestCase):
    async def test_shared_reference_single_all_pages_and_per_item_override(self):
        from analysis_extension.service import AnalysisService
        reads = []
        def capture(owner, ref):
            reads.append(ref)
            return {'response': {'data': list(range(7)) if ref == 'first' else ['second']}}
        service = SimpleNamespace(authorize=AsyncMock(), owner='owner',
            store=SimpleNamespace(get_capture=capture),
            sources=SimpleNamespace(for_capture=lambda value: SimpleNamespace(raw_receipt=lambda receipt: receipt)))
        result = await AnalysisService.read_query_result(service, captureRef='first', requests=[
            {'pointer': '/data', 'limit': 3}, {'captureRef': 'second', 'pointer': '/data'}], allPages=True)
        self.assertTrue(result['readComplete'])
        self.assertEqual([value for page in result['results'][0]['pages'] for value in page['items']], list(range(7)))
        self.assertEqual(result['results'][1]['pages'][0]['items'], ['second'])
        self.assertEqual(reads, ['first', 'second'])
        single = await AnalysisService.read_query_result(service, captureRef='first', pointer='/data', limit=3, allPages=True)
        self.assertTrue(single['readComplete'])
        self.assertEqual(single['results'][0]['pageCount'], 3)
