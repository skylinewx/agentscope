from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.delivery import AnalysisDelivery, reference_from_path
from analysis_extension.engine import RuleRegistry
from analysis_extension.models import AnalysisError, Owner, canonical, digest
from analysis_extension.report import REPORT_CSS, render
from analysis_extension.store import AnalysisStore


class ReportTests(PresentationCase):
    async def test_template_change_generates_new_export_without_reusing_cached_html(self):
        await self.publish()
        first = self.delivery.render_report(self.ref, 'before-style-change')
        original_html = self.delivery.report(self.ref, first['exportRef'])['html']
        with patch('analysis_extension.delivery.REPORT_TEMPLATE_VERSION', 'fixture-new-style'), \
                patch('analysis_extension.report.REPORT_CSS', REPORT_CSS + '\n/* refreshed style */'):
            refreshed = self.delivery.render_report(self.ref, 'after-style-change')
            retried = self.delivery.render_report(self.ref, 'after-style-change')
            self.assertEqual(refreshed, retried)
            self.assertNotEqual(first['exportRef'], refreshed['exportRef'])
            refreshed_html = self.delivery.report(self.ref, refreshed['exportRef'])['html']
            self.assertIn('/* refreshed style */', refreshed_html)
            for row in self.delivery.view(self.path)['rows']:
                self.assertIn(row['display'], refreshed_html)
            self.assertEqual(self.delivery.report(self.ref, first['exportRef'])['html'], original_html)

    async def test_details_hide_capture_progress_but_keep_actual_gaps(self):
        await self.publish()
        document = self.store.get_presentation(OWNER, 'delivery', self.ref)['document']
        complete = render(document)
        self.assertNotIn('已取得', complete)
        self.assertNotIn('说明</th>', complete)
        next(iter(document['tables'][0]['rows'][1]['cells'].values())).update(exact=None, display='—', state='读取失败')
        incomplete = render(document)
        self.assertIn('说明</th>', incomplete)
        self.assertIn('读取失败</td>', incomplete)
        self.assertIn('>—</td>', incomplete)
        self.assertNotIn('已取得', incomplete)

    async def test_renderer_upgrade_refreshes_card_without_mutating_publication_or_export(self):
        from analysis_extension.presentation import RENDERER_VERSION
        with patch('analysis_extension.delivery.RENDERER_VERSION', '1.0.0'), patch('analysis_extension.presentation.RENDERER_VERSION', '1.0.0'):
            await self.publish()
            export = self.delivery.render_report(self.ref, 'before-upgrade')
        saved = self.store.get_presentation(OWNER, 'delivery', self.ref)
        frozen = canonical(saved)
        publication = canonical(self.store.get_analysis(OWNER, self.ref))
        html = self.delivery.report(self.ref, export['exportRef'])['html']
        manifest = (self.root/'workspace'/self.path[2:]).read_bytes()
        refreshed = self.delivery.view(self.path)
        self.assertEqual(refreshed['rendererVersion'], RENDERER_VERSION)
        self.assertEqual(refreshed['reports'], [export])
        self.assertEqual(refreshed['rows'], saved['document']['rows'])
        self.assertEqual(self.delivery.ensure(self.ref)['rendererVersion'], RENDERER_VERSION)
        self.assertEqual(self.delivery.project_view(self.ref, {'filter':'negative'})['rendererVersion'], RENDERER_VERSION)
        # A current card is reconstructed from the artifact, not legacy scene fields.
        older = deepcopy(saved)
        older['document'].pop('scenes')
        older['document'].pop('sections')
        older['manifest']['snapshotDigest'] = digest(older['document'])
        with patch.object(self.delivery, '_saved', return_value=(self.artifact, older)):
            self.assertEqual(self.delivery.view(self.path), refreshed)
        self.assertEqual(canonical(self.store.get_presentation(OWNER, 'delivery', self.ref)), frozen)
        self.assertEqual(canonical(self.store.get_analysis(OWNER, self.ref)), publication)
        self.assertEqual((self.root/'workspace'/self.path[2:]).read_bytes(), manifest)
        self.assertEqual(self.delivery.restore(self.path), refreshed)
        new_export = self.delivery.render_report(self.ref, 'after-upgrade')
        self.assertNotEqual(new_export['exportRef'], export['exportRef'])
        self.assertEqual(self.delivery.report(self.ref, export['exportRef'])['html'], html)

    async def test_export_matches_saved_view_and_html_escape(self):
        await self.publish()
        result = self.delivery.render_report(self.ref, "r")
        html = self.delivery.report(self.ref, result["exportRef"])["html"]
        for row in self.delivery.view(self.path)["rows"]:
            self.assertIn(row["display"], html)
            self.assertIn(row["rowKey"], html)
        self.assertEqual(hashlib.sha256(html.encode()).hexdigest(), result["contentDigest"])
        self.assertNotIn("<script", html)
        saved = self.store.get_presentation(OWNER, "delivery", self.ref)["document"]
        saved['tables'][0]['rows'][0]['label'] = '<img src=x onerror="alert(1)">'
        self.assertIn('&lt;img', render(saved))
        self.assertNotIn('<img src=x', render(saved))

    async def test_full_report_after_filter_keeps_original_scope(self):
        await self.publish()
        full = self.delivery.render_report(self.ref, "full")
        self.delivery.project_view(self.ref, {"filter": "negative"})
        same = self.delivery.render_report(self.ref, "full2")
        negative = self.delivery.render_report(self.ref, "negative", {"filter": "negative"})
        self.assertEqual(full, same)
        self.assertFalse(full["isFiltered"])
        self.assertTrue(negative["isFiltered"])
        self.assertNotEqual(full["exportRef"], negative["exportRef"])
        self.assertIn("-9,333.14", self.delivery.report(self.ref, negative["exportRef"])["html"])

    async def test_current_restart_reuses_registered_snapshot_and_export(self):
        await self.publish()
        first = self.delivery.view(self.path)
        export = self.delivery.render_report(self.ref, "r")
        restarted = AnalysisDelivery(AnalysisStore(self.root / "analysis-data"), OWNER, self.root / "workspace", self.rules)
        replay = restarted.view(self.path)
        self.assertEqual({k: v for k, v in first.items() if k != 'reports'},
                         {k: v for k, v in replay.items() if k != 'reports'})
        self.assertEqual(restarted.report(self.ref, export["exportRef"])["delivery"], export)
        self.assertEqual(restarted.render_report(self.ref, "r"), export)
        self.store.collect_expired(10**12)
        self.assertEqual(self.delivery.render_report(self.ref, "r"), export)

    async def test_modified_manifest_refused_and_explicit_restore(self):
        await self.publish()
        original = self.delivery.view(self.path)
        path = self.root / "workspace" / self.path[2:]
        path.write_text('{"verified":true}', "utf-8")
        with self.assertRaisesRegex(AnalysisError, "不一致"):
            self.delivery.view(self.path)
        self.assertEqual(self.delivery.restore(self.path), original)
        path.unlink()
        with self.assertRaisesRegex(AnalysisError, "缺失"):
            self.delivery.view(self.path)
        self.assertEqual(self.delivery.restore(self.path), original)

    async def test_unregistered_cross_owner_and_path_variants(self):
        await self.publish()
        other = AnalysisDelivery(self.store, Owner("other", OWNER.agent_id, OWNER.session_id), self.root / "workspace", self.rules)
        with self.assertRaises(AnalysisError):
            other.view(self.path)
        for path in (self.path.upper(), self.path.replace('/', '\\'), self.path + '?x=1', self.path.replace('analysis/', 'analysis/../analysis/'), self.path.replace('analysis/', '%61nalysis/', 1)):
            with self.assertRaises(AnalysisError): reference_from_path(path)
        with self.assertRaises(AnalysisError):
            self.delivery.view('./analysis/ana_' + '0' * 32 + '/revisions/1/analysis.json')

    async def test_interrupted_export_retries_without_half_report(self):
        await self.publish()
        with patch.object(self.delivery, '_write', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): self.delivery.render_report(self.ref, 'r')
        self.assertEqual(self.store.list_presentations(OWNER, 'export', self.ref), [])
        result = self.delivery.render_report(self.ref, 'r')
        self.assertTrue(result['registered'])
        self.assertEqual(len(self.store.list_presentations(OWNER, 'export', self.ref)), 1)

    async def test_concurrent_retry_idempotency_and_parameter_conflict(self):
        await self.publish()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.delivery.render_report(self.ref, 'r'), range(3)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.list_presentations(OWNER, 'export', self.ref)), 1)
        with self.assertRaises(AnalysisError):
            self.delivery.render_report(self.ref, 'r', {'topN': 1})

    async def test_report_tamper_oversize_and_generation_disabled(self):
        await self.publish()
        with patch('analysis_extension.report.REPORT_MAX_BYTES', 100):
            with self.assertRaisesRegex(AnalysisError, '超过'): self.delivery.render_report(self.ref, 'r')
        self.delivery.allow_reports = False
        with self.assertRaisesRegex(AnalysisError, '未启用'): self.delivery.render_report(self.ref, 'r')
        self.delivery.allow_reports = True
        result = self.delivery.render_report(self.ref, 'r')
        (self.root / 'workspace' / result['path'][2:]).write_text('modified', 'utf-8')
        with self.assertRaisesRegex(AnalysisError, '不一致'): self.delivery.report(self.ref, result['exportRef'])
