from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import batch_fixture
from analysis_extension.adapters import default_sources
from analysis_extension.adapters.budget_query import _validate, validate
from analysis_extension.analysis_inputs import adapt_inputs
from analysis_extension.models import AnalysisError
from analysis_extension.receipt_reads import receipt_read_scope


class ReceiptReadsTests(TestCase):
    def test_followup_review_context_reuses_runtime_request_validation_and_expires_after_call(self):
        from types import SimpleNamespace
        from analysis_extension.followup_scope import review_context
        from analysis_extension.adapters import budget_v2, budget_query
        capture, _ = business_fixture()
        _, selection, _, _ = batch_fixture(False)
        selections = [{**selection, 'rowIndices': [index % 2], 'columnNames': ['metric1']}
                      for index in range(64)]
        turn = SimpleNamespace(service=SimpleNamespace(owner='current-owner',
            store=SimpleNamespace(get_capture=lambda owner, ref: capture)),
            state={'inheritedScope': {'selections': selections}, 'selectedContextRefs': ['selected']})
        with patch.object(budget_v2, '_budget_receipt', wraps=budget_v2._budget_receipt) as reads, \
             patch.object(budget_query, '_validate', wraps=budget_query._validate) as checks:
            result = review_context(turn)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(checks.call_count, 1)
        self.assertEqual(len(result['inheritedSubject']), 64)
        self.assertTrue(all(item['selectedRowCount'] == 1 for item in result['inheritedSubject']))
        capture['request']['question'] = 'different request after this operation'
        with self.assertRaises(AnalysisError):
            review_context(turn)

    def test_many_cell_selectors_read_and_validate_each_table_once_without_widening_rows(self):
        from analysis_extension.batch_scope import selected_tables
        from analysis_extension.adapters import budget_v2, budget_batch
        capture, selection, _, _ = batch_fixture(False)
        captures = {selection['captureRef']: capture}
        selections = [{**selection, 'rowIndices': [index % 2], 'columnNames': ['metric1']}
                      for index in range(64)]
        original = deepcopy(capture)
        with patch.object(budget_v2, '_receipt_from_capture', wraps=budget_v2._receipt_from_capture) as reads, \
             patch.object(budget_batch, 'validate', wraps=budget_batch.validate) as checks:
            selected = list(selected_tables(captures, selections))
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(checks.call_count, 1)
        self.assertEqual([rows for _, _, _, _, rows in selected], [{index % 2} for index in range(64)])
        self.assertEqual(capture, original)
        with self.assertRaises(AnalysisError):
            list(selected_tables(captures, selections + [{**selection, 'rowIndices': [99]}]))
        capture['request']['plan']['steps'][1]['batchQuery']['metrics'][0]['selection']['measureCode'] = 'OTHER'
        with self.assertRaises(AnalysisError):
            list(selected_tables(captures, selections))

    def test_build_reuses_full_validation_but_checks_each_selection_and_next_build(self):
        capture, _ = business_fixture()
        _, selection, scope, _ = batch_fixture(False, True)
        ref = selection['captureRef']
        captures = {ref: capture}
        scope = {key: scope[key] for key in ('label', 'selectionReason')}
        before = deepcopy(capture)
        with patch('analysis_extension.adapters.budget_query._validate', wraps=_validate) as checked:
            result = adapt_inputs(default_sources(), captures, [selection], scope, 1000)
        self.assertEqual(len(result['facts']), 4)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(capture, before)
        bad_selection = {**selection, 'columnNames': ['not-returned-column']}
        with self.assertRaises(AnalysisError):
            adapt_inputs(default_sources(), captures, [selection, bad_selection], scope, 1000)
        capture['request']['question'] = 'different request with the same ref'
        with self.assertRaises(AnalysisError):
            adapt_inputs(default_sources(), captures, [selection], scope, 1000)

    def test_distinct_requests_cannot_borrow_validation_and_scope_is_discarded(self):
        capture, receipt = business_fixture()
        other = deepcopy(capture)
        other['request']['question'] = 'other owner/request'
        with receipt_read_scope():
            validate(capture, receipt)
            with self.assertRaises(AnalysisError):
                validate(other, receipt)
        receipt['data']['question'] = 'changed after first operation'
        with receipt_read_scope(), self.assertRaises(AnalysisError):
            validate(capture, receipt)

    def test_ordinary_reads_and_failed_scopes_never_reuse_prior_checks(self):
        capture, receipt = business_fixture()
        with patch('analysis_extension.adapters.budget_query._validate', wraps=_validate) as checked:
            validate(capture, receipt)
            validate(capture, receipt)
        self.assertEqual(checked.call_count, 2)
        with self.assertRaises(RuntimeError):
            with receipt_read_scope():
                validate(capture, receipt)
                raise RuntimeError('aborted build')
        capture['request']['question'] = 'changed after failed scope'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
