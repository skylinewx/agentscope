"""Business invariants for the coordinated query/analysis contract upgrade."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase

from analysis_extension.followup_scope import prepare_request
from analysis_extension.turn_completion import TurnCompletion


class QueryContractStateTests(TestCase):
    def turn(self):
        return TurnCompletion(SimpleNamespace(), {
            'turnKey': 'contract', 'request': '改看乙集团25年EVA预算，按地区分析并与全部单位比较',
            'previousQueryIntent': {'metric': '净利润', 'population': '甲集团',
                                    'period': '26年', 'scenario': '预算', 'grouping': '板块'},
            'inheritedScope': {}, 'failures': {}, 'buildRefs': []})

    def declare(self, turn, intent=None):
        return turn.task(action='declare', mode='DIRECT', goal=turn.state['request'], intent=intent)

    def query(self, predicate=None):
        draft = {'targets': [{'id': 'profit', 'periodText': '25年', 'businessPhrases': [{'text': 'EVA'}]}],
                 'sharedConditions': [{'text': '预算'}],
                 'dimensionScopes': [{'id': 'org', 'dimensionHint': '组织机构',
                                     'selector': {'type': 'VALUES', 'members': ['乙集团']}}]}
        if predicate is not None:
            draft['valueFilter'] = predicate
        return {'purpose': 'QUERY', 'intentDraft': draft}

    def test_partial_declaration_preserves_all_current_changes(self):
        turn = self.turn()
        changed = {'metric': 'EVA', 'population': '乙集团', 'period': '25年'}
        self.declare(turn, changed)
        actual = self.declare(turn, {'grouping': '地区'})['intent']
        for field, value in changed.items():
            self.assertEqual(actual.get(field), value)
        self.assertEqual(actual['grouping'], '地区')

    def test_omitted_presentation_keeps_same_turn_decision(self):
        turn = self.turn()
        self.declare(turn, {'grouping': '地区', 'order': '从小到大'})
        result = self.declare(turn, {'period': '25年'})
        self.assertEqual(result['intent']['grouping'], '地区')
        self.assertEqual(result['intent']['order'], '从小到大')

    def test_clear_all_fields_survives_json_resume(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'scenario': None, 'grouping': None})
        restored = TurnCompletion(turn.service, json.loads(json.dumps(turn.state)))
        result = self.declare(restored, {'period': '25年'})
        self.assertNotIn('scenario', result['intent'])
        self.assertNotIn('grouping', result['intent'])
        self.assertEqual(result['intent']['metric'], 'EVA')

    def test_independent_queries_do_not_share_predicates(self):
        turn = self.turn()
        self.declare(turn, {'metric': 'EVA', 'population': '乙集团', 'period': '25年'})
        origin = {'originalToolName': 'execute_budget_query'}
        negative = prepare_request(turn, origin, self.query({'operator': 'LT', 'value': '0'}))
        all_members = prepare_request(turn, origin, self.query())
        positive = prepare_request(turn, origin, self.query({'operator': 'GT', 'value': '0'}))
        self.assertEqual(negative['intentDraft']['valueFilter']['operator'], 'LT')
        self.assertNotIn('valueFilter', all_members['intentDraft'])
        self.assertEqual(positive['intentDraft']['valueFilter']['operator'], 'GT')
        self.assertNotIn('budgetValueFilter', turn.state)

    def test_invalid_patch_leaves_state_unchanged(self):
        from analysis_extension.models import AnalysisError
        turn = self.turn()
        self.declare(turn, {'grouping': '地区'})
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError):
            self.declare(turn, {'period': ['25年']})
        self.assertEqual(before, turn.state)
