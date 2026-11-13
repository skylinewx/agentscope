from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from analysis_extension.models import AnalysisError
from analysis_extension.value_predicates import validate, compile_native
from analysis_extension.language_evidence import bind
from analysis_extension.adapters.budget_query import _validate_value_filter


class NativePredicateTest(TestCase):
    def test_language_evidence_never_reinterprets_typed_period_intent(self):
        for question in ('查询2026年全部单位的净利润预算', '查询26年的预算'):
            for number in (None, 1):
                point = {'type': 'MONTH', 'year': 2026}
                if number is not None:
                    point['period'] = number
                request = {'question': question, 'scope': {'periodRequest': {'kind': 'PERIOD', 'start': point}}}
                before = deepcopy(request)
                bind(None, request)
                self.assertEqual(request, before)
        for question in ('2026年1月预算', '2026年按月查询', '2026年第一季度', '2026年截至目前',
                         '2025年和2026年对比', '2026Y0001', '2026-01-01预算', '2026年去年同期'):
            request = {'question': question, 'scope': {'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'MONTH', 'year': 2026, 'period': 1}}}}
            before = deepcopy(request)
            bind(None, request)
            self.assertEqual(request, before)

    def predicate(self):
        return {'operator': 'AND', 'children': [
            {'operator': 'GT', 'targetId': 'receivable', 'value': '1000'},
            {'operator': 'GT', 'targetId': 'borrowing', 'value': '1000'}]}

    def test_business_targets_compile_to_distinct_aliases_without_changing_thresholds(self):
        compiled = compile_native(self.predicate(), {'receivable': 'metric2', 'borrowing': 'metric1'})
        self.assertEqual(compiled, {'op': 'AND', 'children': [
            {'op': 'GT', 'field': 'metric2', 'value': '1000'},
            {'op': 'GT', 'field': 'metric1', 'value': '1000'}]})
        self.assertEqual(compile_native({'operator': 'NOT', 'children': [{'operator': 'IS_NULL', 'targetId': 'a'}]},
                                       {'a': 'metric1'}), {'op': 'NOT', 'children': [{'op': 'IS_NULL', 'field': 'metric1'}]})
        for bad in ({'operator': 'GT', 'value': '1000'}, {'operator': 'GT', 'value': '1e3', 'targetId': 'a'},
                    {'operator': 'GT', 'value': '1000', 'targetId': 'unseen'}):
            with self.subTest(condition=bad), self.assertRaises(AnalysisError):
                validate(bad, {'a', 'b'})

    def test_single_target_implicit_and_explicit_predicates_are_equivalent(self):
        implicit = {'operator': 'LT', 'value': '0'}
        explicit = {**implicit, 'targetId': 'profit'}
        self.assertEqual(compile_native(implicit, {'profit': 'metric1'}),
                         compile_native(explicit, {'profit': 'metric1'}))

    def test_native_proof_detects_dropped_conjunct_and_preserves_statistical_units(self):
        expected = self.predicate()
        aliases = {'receivable': 'metric1', 'borrowing': 'metric2'}
        where = compile_native(expected, aliases)
        counts = {'alignedRowCount': 11, 'matchedTargetCount': 2, 'filteredOutRowCount': 9}
        batch = {'batchId': 'b', 'disposition': 'SUCCEEDED', 'request': {'resultWhere': where}, 'data': {'rangeSummary': counts}}
        request = {'intentDraft': {'valueFilter': expected}}
        data = {'complete': True, 'resultSets': [], 'valueFilter': {'basis': 'BUDGET_NATIVE_RESULT_WHERE', 'condition': expected,
                'batches': [{'batchId': 'b', 'status': 'SUCCEEDED', 'logicalTargetColumns': aliases,
                             'resultWhere': deepcopy(where), 'rangeSummary': deepcopy(counts)}]}}
        _validate_value_filter(request, data, {}, {'s': {'batchResults': [batch]}})
        batch['request']['resultWhere'] = where['children'][0]
        with self.assertRaises(AnalysisError):
            _validate_value_filter(request, data, {}, {'s': {'batchResults': [batch]}})

    def test_current_range_and_confirmed_historical_root_have_separate_host_spans(self):
        turn = SimpleNamespace(state={})
        request = {'question': '查询二级单位，从大到小排', 'contextText': '久其集团 净利润 预算', 'intentDraft': {
            'targets': [{'id': 't', 'businessPhrases': [{'text': '净利润', 'sourceText': '模型虚构净利润'}]}],
            'dimensionScopes': [{'id': 'org', 'dimensionHint': '机构', 'sourceText': '久其集团的二级单位',
                'selector': {'type': 'HIERARCHY', 'rootText': '久其集团', 'level': 2}}]}}
        result = bind(turn, request)
        self.assertEqual(result['intentDraft']['dimensionScopes'][0]['sourceText'], request['question'])
        records = next(iter(turn.state['businessEvidence'].values()))
        root = next(record for record in records if record['text'] == '久其集团')
        self.assertEqual(root['source'], 'CONFIRMED_CONTEXT')
        self.assertFalse(any('久其集团的二级单位' == record['text'] for record in records))
        request['intentDraft']['targets'][0]['businessPhrases'][0]['text'] = '虚构业务'
        with self.assertRaises(AnalysisError):
            bind(turn, request)

    def test_having_proof_validates_aggregate_stage_and_displays_native_group_counts(self):
        from analysis_extension.business_tables import state_markdown
        expected = {'operator': 'GT', 'value': '1000'}
        having = {'op': 'GT', 'field': 'metric1', 'value': '1000'}
        counts = {'detailRowCount': 11, 'groupCountBeforeHaving': 3, 'filteredOutGroupCount': 1}
        batch = {'batchId': 'b', 'disposition': 'SUCCEEDED', 'request': {'having': having, 'aggregation': {'groupBy': ['sector']}},
                 'data': {'rangeSummary': {}, 'aggregationSummary': counts}}
        item = {'batchId': 'b', 'status': 'SUCCEEDED', 'logicalTargetColumns': {'profit': 'metric1'},
                'filterStage': 'HAVING', 'having': having, 'rangeSummary': {}, 'aggregationSummary': deepcopy(counts)}
        data = {'complete': True, 'resultSets': [], 'valueFilter': {'basis': 'BUDGET_NATIVE_FILTER',
                'condition': expected, 'batches': [item]}}
        request = {'intentDraft': {'valueFilter': expected}}
        _validate_value_filter(request, data, {}, {'s': {'batchResults': [batch]}})
        summary = state_markdown({'status': 'SUCCEEDED', 'data': data})
        self.assertIn('筛选前分组：3', summary)
        self.assertIn('过滤分组：1', summary)
        self.assertNotIn('命中目标组合', summary)
        item['aggregationSummary']['filteredOutGroupCount'] = 0
        with self.assertRaises(AnalysisError):
            _validate_value_filter(request, data, {}, {'s': {'batchResults': [batch]}})
        item['aggregationSummary'] = counts
        batch['request']['resultWhere'] = batch['request'].pop('having')
        with self.assertRaises(AnalysisError):
            _validate_value_filter(request, data, {}, {'s': {'batchResults': [batch]}})

    def test_filtered_formula_selection_is_tied_to_native_coordinates_not_later_batch_id(self):
        predicate = {'operator': 'LT', 'value': '0'}
        where = {'op': 'LT', 'field': 'metric1', 'value': '0'}
        dims = {'DATATIME': '2026N0001', 'MD_ORG': 'child', 'MD_SCENE': 'ACTUAL'}
        context = {'target': {'formBindingKey': 'form'}}
        metric = {'name': 'metric1', 'context': context, 'selectedDimensions': {**dims, 'MD_ORG': 'root'},
                  'measureCode': 'AMT', 'cellExp': 'MODEL_N[AMT]'}
        batch = {'batchId': 'native', 'disposition': 'SUCCEEDED', 'request': {'resultWhere': where},
                 'metrics': [metric], 'columnRoles': {'ORG': {'kind': 'DIMENSION', 'dimCode': 'MD_ORG'}},
                 'data': {'rangeSummary': {}, 'columns': [{'name': 'ORG'}, {'name': 'metric1'}], 'rows': [['child', '-8']]}}
        selection = {'targetId': 't', 'binding': 'form', 'measureCode': 'AMT', 'modelCode': 'MODEL_N',
                     'dimensions': dims, 'batchId': 'native', 'rowIndex': 0, 'sourceColumn': 'metric1'}
        data = {'complete': True, 'valueFilter': {'condition': predicate, 'basis': 'BUDGET_NATIVE_RESULT_WHERE',
                'batches': [{'batchId': 'native', 'status': 'SUCCEEDED', 'resultWhere': where,
                            'logicalTargetColumns': {'t': 'metric1'}, 'rangeSummary': {}}], 'analysisSelection': [selection]},
                'resultSets': [{'targetId': 't', 'context': context, 'rows': [{
                    'dimensions': deepcopy(dims), 'measureCode': 'AMT', 'cellExp': 'MODEL_N[AMT]',
                    'valueState': 'PRESENT', 'originRefs': ['later-formula-observation']}]}]}
        _validate_value_filter({'intentDraft': {'valueFilter': predicate}}, data, {}, {'s': {'batchResults': [batch]}})
        data['resultSets'][0]['rows'][0]['dimensions']['MD_ORG'] = 'unmatched'
        with self.assertRaises(AnalysisError):
            _validate_value_filter({'intentDraft': {'valueFilter': predicate}}, data, {}, {'s': {'batchResults': [batch]}})
