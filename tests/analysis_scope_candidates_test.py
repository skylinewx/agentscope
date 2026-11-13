from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from analysis_extension.models import AnalysisError
from analysis_extension.scope_candidates import candidates, bind, inherit
from analysis_extension.capture import ReceiptCapture
from analysis_extension.business_tables import model_text


class ScopeCandidateTest(TestCase):
    def setUp(self):
        self.origin = dict(resourceId='budget', serverName='ontology', connectionFingerprint='connection')
        self.capture = dict(self.origin, originalToolName='search_entities', captureRef='cap_abc123', request={'entityMention': '年度预算二上'})
        self.receipt = {'status': 'SUCCEEDED', 'executionId': 'execution', 'context': {'contextId': 'context'}, 'querySessionId': 'session',
                        'data': {'candidates': [dict(candidateId='first', modelCode='BUDGET_TASK', kind='RESOURCE', key='A', label='年度预算二上其他版', matchedBy=['RESOURCE_RDF_VERIFIED']),
                                                dict(candidateId='second', modelCode='BUDGET_TASK', kind='RESOURCE', key='B', label='年度预算二上终版', matchedBy=['RESOURCE_RDF_VERIFIED'])]}}
        self.adapter = Mock()
        self.adapter.raw_receipt.return_value = self.receipt
        self.capture['response'] = self.receipt
        self.service = SimpleNamespace(store=Mock(), sources=Mock(), owner='owner')
        self.service.store.get_capture.return_value = self.capture
        self.service.sources.for_capture.return_value = self.adapter
        self.turn = SimpleNamespace(service=self.service)

    def test_selected_reference_is_resolved_by_owner_and_never_defaults_to_first(self):
        options = candidates(self.receipt, self.capture)
        request = {'scope': {'bindings': [{'text': '年度预算二上', 'candidateRef': options[1]['candidateRef']}]}}
        original = deepcopy(request)
        result = bind(self.turn, self.origin, request)
        self.assertEqual(request, original)
        self.assertEqual(result['scope']['bindings'][0], {'text': '年度预算二上', 'modelCode': 'BUDGET_TASK',
            'executionId': 'execution', 'candidateId': 'second', 'contextId': 'context', 'querySessionId': 'session'})
        self.service.store.get_capture.assert_called_once_with('owner', 'cap_abc123')
        with self.assertRaises(AnalysisError):
            bind(self.turn, {**self.origin, 'connectionFingerprint': 'other'}, request)
        self.receipt['data']['candidates'][1]['matchedBy'] = []
        with self.assertRaises(AnalysisError):
            bind(self.turn, self.origin, request)

    def test_binding_and_original_quote_inherit_and_clear_by_business_role(self):
        prior = {'bindings': [dict(text='任务原话', modelCode='BUDGET_TASK', candidateId='a'),
                              dict(text='表原话', modelCode='BUDGET_FORM_TEMPLATE', candidateId='b')]}
        inherited = {}
        inherit(inherited, prior, set())
        self.assertEqual(inherited, prior)
        changed = {}
        inherit(changed, prior, {'task'})
        self.assertEqual(changed['bindings'], [prior['bindings'][1]])
        changed = {}
        inherit(changed, prior, {'metric'})
        self.assertEqual(changed['bindings'], [prior['bindings'][0]])

    def test_small_search_also_exposes_refs_as_markdown(self):
        capture = ReceiptCapture.__new__(ReceiptCapture)
        capture.sources = self.service.sources
        capture.store, capture.owner = self.service.store, self.service.owner
        from unittest.mock import patch
        with patch('analysis_extension.analysis_materials.response_materials', return_value=None):
            view = capture._material_projection(self.adapter, self.receipt, self.capture, 100)
        rendered = model_text(view)
        self.assertIn('| 任务 | 年度预算二上终版 | B | scand:cap_abc123:1 |', rendered)
        self.assertIn('不能默认第一条', rendered)
        self.adapter.compact.assert_not_called()

    def test_followup_inherits_native_proof_and_original_text_before_evidence_validation(self):
        from unittest.mock import patch
        from analysis_extension.followup_scope import _inherit_business_request
        from analysis_extension.language_evidence import bind as bind_evidence
        proof = {'text': '年度预算二上', 'modelCode': 'BUDGET_TASK', 'candidateId': 'second',
                 'executionId': 'search', 'contextId': 'old-context', 'querySessionId': 'old-session'}
        prior = {'question': '查一下年度预算二上中26年水泥的销售预算',
                 'scope': {'bindings': [proof], 'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'YEAR', 'year': 2026}}}}
        capture = dict(self.origin, originalToolName='execute_budget_query', request=prior)
        turn = SimpleNamespace(state={'request': '沿用刚才口径，第三季度金额是多少',
                                      'followupContext': {'mode': 'CONTINUE'}, 'queryIntent': {}, 'previousQueryIntent': {}}, service=self.service)
        def confirmed(turn, origin, changes, released, source_texts, confirmed_requests, proposed_scopes=(), retained_scenarios=None):
            confirmed_requests.append(prior)
            source_texts.append(prior['question'])
            return []
        request = {'purpose': 'QUERY', 'scope': {'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'QUARTER', 'year': 2026, 'period': 3}}}}
        with patch('analysis_extension.followup_scope._changes', return_value={'period'}), \
             patch('analysis_extension.followup_scope._selectors', return_value=[]), \
             patch('analysis_extension.followup_scope._confirmed_business_terms', side_effect=confirmed), \
             patch('analysis_extension.followup_scope._inherited_tables', return_value=[(None, capture, None, None)]):
            prepared = _inherit_business_request(turn, self.origin, request)
        bind_evidence(turn, prepared)
        self.assertEqual(prepared['scope']['bindings'], [proof])
        self.assertEqual(prepared['scope']['periodRequest'], request['scope']['periodRequest'])
        self.assertIn('年度预算二上', prepared['contextText'])
        self.assertNotIn('年度预算二上终版', prepared['contextText'])
