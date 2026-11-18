"""实测展示与源码占位必须分开；不能接受模型声称的渲染结果。"""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from analysis_extension.document_checks import evaluate
from analysis_extension.models import AnalysisError
from analysis_extension.render_evidence import review_text, snapshot, validate_snapshot, TEXT_LIMIT
from analysis_extension.turn_completion import semantic_review_key
from analysis_document_checks_test import measurements


class RenderEvidenceTests(TestCase):
    def fixture(self, records):
        store = SimpleNamespace(document_checks=Mock(return_value=records))
        return SimpleNamespace(service=SimpleNamespace(store=store, owner='owner'))

    def test_current_host_snapshot_replaces_placeholder_but_retains_source(self):
        record = {'runner': 'headless-service', 'contentDigest': 'current', 'blocking': False,
                  'viewport': {'width': 420}, 'hostRenderedText': snapshot('合计 123.45 元')}
        turn = self.fixture([record, {**record, 'viewport': {'width': 900}}])
        result = review_text(turn, {'analysisRef': 'ref', 'freeDocument': {'contentDigest': 'current'}}, '合计 — 元')
        self.assertEqual(result['documentText'], '合计 123.45 元')
        self.assertEqual(result['documentSourceText'], '合计 — 元')
        self.assertEqual(result['documentTextBasis'], 'HOST_RENDERED')
        turn.service.store.document_checks.assert_called_once_with('owner', 'ref', 'current')

    def test_old_manual_blocked_or_truncated_observations_do_not_prove_rendering(self):
        record = {'runner': 'headless-service', 'contentDigest': 'current', 'blocking': False,
                  'viewport': {'width': 420}, 'hostRenderedText': snapshot('实际内容')}
        records = [{**record, 'runner': 'browser'}, {**record, 'contentDigest': 'old'},
                   {**record, 'blocking': True}, {**record, 'hostRenderedText': snapshot('长' * (TEXT_LIMIT + 1))}]
        result = review_text(self.fixture(records), {'analysisRef': 'ref', 'freeDocument': {'contentDigest': 'current'}}, '原始文字')
        self.assertEqual(result, {'documentText': '原始文字', 'documentTextBasis': 'STATIC_SOURCE'})
        self.assertEqual(snapshot('长' * (TEXT_LIMIT + 1)), {'text': '', 'complete': False})

    def test_manual_measurement_cannot_submit_host_text_and_invalid_host_payload_rejected(self):
        with self.assertRaises(AnalysisError):
            evaluate({}, {**measurements(), 'hostRenderedText': snapshot('伪造内容')})
        for value in ({'text': 'prefix', 'complete': False}, {'text': 'text', 'complete': 1}, {'text': '长' * (TEXT_LIMIT + 1), 'complete': True}):
            with self.assertRaises(AnalysisError):
                validate_snapshot(value)

    def test_whitespace_only_changes_do_not_break_semantic_cache(self):
        original = {'documentText': '合计 123.45 元', 'documentSourceText': '合计 — 元'}
        self.assertEqual(semantic_review_key(original, 'prompt'), semantic_review_key({
            'documentText': '合计\n123.45 元', 'documentSourceText': '合计\n — 元'}, 'prompt'))
