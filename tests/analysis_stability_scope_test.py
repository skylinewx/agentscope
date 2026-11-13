"""Scope changes are derived once and remain consistent through evidence binding and recovery."""
from copy import deepcopy
from unittest import TestCase

import analysis_source_evidence_test as source_evidence
from analysis_extension.models import AnalysisError
from analysis_extension.scope_changes import intent_context


class StableScopeTests(TestCase):
    def setup_request(self):
        fixture = source_evidence.SourceEvidenceTest()
        turn, capture, request = fixture.fixture()
        request.pop('groupBy')
        request['intentDraft']['dimensionScopes'] = [deepcopy(turn.state['inheritedDimensionScopes'][0]['draft'])]
        return fixture, turn, capture, request

    def test_subset_filter_keeps_historical_root_despite_redundant_population_flag(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '分析这些单位中低于目标的对象'
        turn.state['followupContext']['changedFields'] = ['analysisFocus', 'population']
        request['intentDraft']['valueFilter'] = {'operator': 'LT', 'value': '10', 'targetId': 't'}
        result = fixture.prepare(turn, capture, request)
        scope = result['intentDraft']['dimensionScopes'][0]
        self.assertEqual(scope['sourceText'], capture['request']['question'])
        self.assertEqual(scope['selector']['rootText'], '久其集团')
        self.assertIn('confirmedSource', scope)
        self.assertIn('population', turn.state['followupContext']['changedFields'])  # query-local reconciliation
        self.assertEqual(result['intentDraft']['valueFilter']['value'], '10')

    def test_same_selector_releases_duplicate_dimension_and_pending_flags_together(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state.update(basePendingChanges=['population'], basePendingDimensions=['MD_ORG'])
        turn.state['followupContext']['changedDimensions'] = ['机构']
        result = fixture.prepare(turn, capture, request)
        self.assertIn('confirmedSource', result['intentDraft']['dimensionScopes'][0])
        self.assertEqual(turn.state['basePendingDimensions'], ['MD_ORG'])
        self.assertEqual(turn.state['basePendingChanges'], ['population'])
        self.assertEqual(turn.state['followupContext']['changedDimensions'], ['机构'])

    def test_new_hierarchy_level_keeps_only_confirmed_root_not_whole_old_scope(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '改看三级单位'
        request['intentDraft']['dimensionScopes'][0]['selector']['level'] = 3
        result = fixture.prepare(turn, capture, request)
        scope = result['intentDraft']['dimensionScopes'][0]
        self.assertEqual(scope['selector'], {'type': 'HIERARCHY', 'level': 3, 'rootText': '久其集团'})
        self.assertNotIn('confirmedSource', scope)
        self.assertEqual(scope['sourceText'], '改看三级单位')
        self.assertNotIn('population', turn.state['followupContext'].get('changedFields', []))

    def test_explicit_new_root_derives_change_without_old_name_leakage(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '改查乙集团二级单位'
        request['intentDraft']['dimensionScopes'][0]['selector']['rootText'] = '乙集团'
        result = fixture.prepare(turn, capture, request)
        self.assertNotIn('久其集团', result.get('contextText', ''))
        self.assertNotIn('confirmedSource', result['intentDraft']['dimensionScopes'][0])
        self.assertEqual(result['intentDraft']['dimensionScopes'][0]['sourceText'], turn.state['request'])

    def test_unproven_root_replacement_is_atomic_and_cannot_fall_back_to_defaults(self):
        fixture, turn, capture, request = self.setup_request()
        before = deepcopy(turn.state)
        request['intentDraft']['dimensionScopes'][0]['selector']['rootText'] = '未知集团'
        with self.assertRaises(AnalysisError) as caught:
            fixture.prepare(turn, capture, request)
        self.assertEqual(caught.exception.code, 'QUERY_DRAFT_EVIDENCE_INVALID')
        self.assertEqual(turn.state, before)

    def test_intent_changes_derive_period_scenario_and_population_without_flags(self):
        state = {'request': '改查乙集团25年实际收入', 'previousQueryIntent': {
            'population': '甲集团', 'period': '26年', 'scenario': '预算', 'metric': '收入'}}
        context = intent_context(state, {'mode': 'CONTINUE'}, {
            'population': '乙集团', 'period': '25年', 'scenario': '实际', 'metric': '收入'})
        self.assertEqual(set(context['changedFields']), {'population', 'period', 'scenario'})
        self.assertEqual(context['userQuote'], state['request'])

    def test_identical_intent_clears_duplicate_flag_but_preserves_unspecified_changes(self):
        state = {'previousQueryIntent': {'population': '甲集团', 'period': '26年'}}
        context = intent_context(state, {'mode': 'CONTINUE', 'changedFields': ['population', 'period']},
                                 {'population': '甲集团'})
        self.assertEqual(context['changedFields'], ['period'])

    def test_period_and_metric_replacement_never_restore_history_sentence(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '改看25年营业收入'
        turn.state['followupContext'] = intent_context(turn.state, {'mode': 'CONTINUE'},
                                                       {'period': '25年', 'metric': '营业收入'})
        request['intentDraft']['targets'][0].update(periodText='25年', businessPhrases=[{'text': '营业收入'}])
        result = fixture.prepare(turn, capture, request)
        self.assertIn('久其集团', result['contextText'])
        for old in ('26年', '净利润', '利润表'):
            self.assertNotIn(old, result['contextText'])
        self.assertNotIn('periodCode', result.get('scope', {}))

    def test_source_selection_cannot_reuse_old_facts_after_derived_root_change(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '改查乙集团二级单位'
        request['intentDraft']['dimensionScopes'][0]['selector']['rootText'] = '乙集团'
        fixture.prepare(turn, capture, request)
        with self.assertRaises(AnalysisError) as caught:
            fixture.prepare(turn, capture, {'sourceSelection': {'executionId': 'old', 'resultSetId': 'old', 'factIds': ['old']}})
        self.assertEqual(caught.exception.code, 'QUERY_CONTRACT_CHANGED')

    def test_explicit_new_period_in_query_is_not_overwritten_when_flag_omitted(self):
        fixture, turn, capture, request = self.setup_request()
        turn.state['request'] = '改看25年的数据'
        request['intentDraft']['targets'][0]['periodText'] = '25年'
        result = fixture.prepare(turn, capture, request)
        self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '25年')
        self.assertNotIn('26年', result.get('contextText', ''))
        self.assertNotIn('periodCode', result.get('scope', {}))
        self.assertIn('confirmedSource', result['intentDraft']['dimensionScopes'][0])

    def test_unquoted_period_guess_cannot_replace_confirmed_annual_scope(self):
        fixture, turn, capture, request = self.setup_request()
        request['intentDraft']['targets'][0]['periodText'] = '1月'
        result = fixture.prepare(turn, capture, request)
        self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '26年')
        self.assertEqual(result['scope']['periodCode'], '2026N0001')

    def test_custom_dimension_change_keeps_unrelated_organization_proof(self):
        fixture, turn, capture, request = self.setup_request()
        project = {'draft': {'id': 'project', 'dimensionHint': '项目', 'sourceText': '甲项目',
                            'selector': {'type': 'MEMBERS', 'members': ['甲项目']}},
                   'confirmedSource': {'executionId': 'execution', 'scopeId': 'project', 'contextId': 'original-context', 'querySessionId': 'original-session'},
                   'captureRef': 'previous', 'appliesToAllTargets': True}
        turn.state['inheritedDimensionScopes'].append(project)
        turn.state['request'] = '改看乙项目'
        request['intentDraft']['dimensionScopes'].append({**deepcopy(project['draft']),
            'selector': {'type': 'MEMBERS', 'members': ['乙项目']}})
        result = fixture.prepare(turn, capture, request)
        self.assertIn('confirmedSource', result['intentDraft']['dimensionScopes'][0])
        self.assertNotIn('confirmedSource', result['intentDraft']['dimensionScopes'][1])
        self.assertEqual(turn.state['followupContext'].get('changedDimensions', []), [])
