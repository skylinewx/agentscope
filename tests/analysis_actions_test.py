from copy import deepcopy
from contextlib import closing
import json
import shutil
import sqlite3
from unittest.mock import patch

from analysis_fixtures import OWNER
from analysis_action_fixtures import ActionCase
from analysis_extension.actions import followup_text, resolve
from analysis_extension.models import AnalysisError, Owner, canonical
from analysis_extension.store import AnalysisStore
from analysis_extension.engine import RuleRegistry


class ActionTests(ActionCase):
    async def test_first_read_after_host_restart_marks_unpublished_run_partial(self):
        await self.initial()
        await self.start()
        with patch('analysis_extension.store.HOST_ID', 'new-process-generation'):
            view = self.delivery.view(self.path)
            self.assertEqual(view['actionRuns'][0]['status'], 'PARTIAL')
            self.assertIsNone(view['actionRuns'][0]['resultAnalysisRef'])
        self.assertEqual(self.store.get_action_run(OWNER, self.run)['status'], 'PARTIAL')

    async def test_known_formula_followup_requires_new_inputs_and_host_recalculation(self):
        registry = RuleRegistry({"composition": {"source": "合成公式", "version": "1", "operators": ["LINEAR"],
            "terms": [{"match": {"objectId": "object-0"}, "coefficient": "1"}, {"match": {"objectId": "object-1"}, "coefficient": "-1"}]}})
        self.service.rules = self.delivery.rules = registry
        operations = [{"id": "composition", "operator": "LINEAR", "ruleRef": "composition"}]
        refs, selections, scope = self.capture(values=("100", "120"))
        draft = await self.service.build(refs, selections, scope, operations, "original")
        published = await self.publish_planned(draft, "original-publish")
        self.ref = published["analysisRef"]
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.action = next(a for a in self.artifact["actions"] if a["goalType"] == "VERIFY_COMPOSITION")
        self.assertEqual(self.action["availability"], "AVAILABLE")
        await self.start()
        refs, selections, scope = self.capture(values=("100", "125"), tag="new")
        for ref in refs:
            self.service.actions.capture(ref, "success")
        new = await self.service.build(refs, selections, scope, operations, "new", baseAnalysisRef=self.ref, actionRunRef=self.run)
        await self.publish_planned(new, "new-publish")
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "COMPLETED")
        result = self.store.get_analysis(OWNER, new["analysisRef"])
        self.assertEqual(result["derivedValues"][0]["value"], "-25")
        self.assertIn("仅支持所选计算关系", result["findings"][0]["statement"])

    async def test_late_capture_is_retained_without_reopening_cancelled_run(self):
        await self.initial()
        await self.start()
        self.service.actions.finish("CANCELLED", "USER_INTERRUPTED")
        refs, _, _ = self.capture(tag="late")
        for ref in refs:
            self.store.record_action_capture(OWNER, self.run, ref, "success")
        run = self.store.get_action_run(OWNER, self.run)
        self.assertEqual(run["status"], "CANCELLED")
        self.assertEqual(run["newCaptureRefs"], refs)
        self.assertIsNone(run["resultAnalysisRef"])
        with self.assertRaises(AnalysisError):
            self.store.update_action_run(OWNER, self.run, "COMPLETED", "not-allowed")

    async def test_new_verified_revision_completes_atomically_and_contradicts_old_finding(self):
        await self.initial()
        old_bytes = canonical(self.artifact)
        report = self.delivery.render_report(self.ref, "old-report")
        old_html = self.delivery.report(self.ref, report["exportRef"])["html"]
        started = await self.start()
        self.assertEqual(started["sourceRequery"], "NOT_CHECKED")
        self.assertEqual(started["facts"][0]["value"], "-8")
        built = await self.next_build(values=("2",), snapshot="new-snapshot")
        plan = [{"kind": "RECHECK", "previousFindingRef": self.ref + "#" + self.artifact["findings"][0]["findingId"],
                 "factRefs": [self.store.get_analysis(OWNER, built["analysisRef"])["facts"][0]["factId"]]}]
        result = await self.service.publish(built["analysisRef"], [], [], "publish-next", built["baseRevision"], findingPlan=plan)
        run = self.store.get_action_run(OWNER, self.run)
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["resultAnalysisRef"], result["analysisRef"])
        current = self.store.get_analysis(OWNER, result["analysisRef"])
        self.assertEqual(current["findings"][0]["support"], "CONTRADICTED")
        self.assertEqual(current["sourceTransition"], "NEW_CONTEXT_INDEPENDENTLY_VERIFIED")
        self.assertEqual(canonical(self.store.get_analysis(OWNER, self.ref)), old_bytes)
        self.assertEqual(self.delivery.report(self.ref, report["exportRef"])["html"], old_html)
        self.assertEqual(self.delivery.view(self.path)["actionRuns"][0]["resultAnalysisRef"], result["analysisRef"])
        retry = await self.service.publish(built["analysisRef"], [], [], "publish-next", built["baseRevision"], findingPlan=plan)
        self.assertEqual(retry["analysisRef"], result["analysisRef"])
        self.assertEqual((await self.start())["actionRun"]["status"], "COMPLETED")
        current_plan = current["plan"]
        replay = await self.service.build(current["captureRefs"], current_plan["selections"], current_plan["scope"], current_plan["operations"], "build-next", baseAnalysisRef=self.ref, actionRunRef=self.run)
        self.assertEqual(replay["analysisRef"], result["analysisRef"])

    async def test_missing_unit_or_unverified_result_stays_partial(self):
        await self.initial(unit=None)
        unit_action = next(a for a in self.artifact["actions"] if a["goalType"] == "CONFIRM_UNIT")
        await self.start(action=unit_action)
        built = await self.next_build(unit=None)
        await self.service.publish(built["analysisRef"], [], [], "publish-next", built["baseRevision"])
        run = self.store.get_action_run(OWNER, self.run)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["diagnostics"][-1]["code"], "UNIT_STILL_MISSING")
        self.assertIsNotNone(run["resultAnalysisRef"])

    async def test_forged_and_cross_owner_references_are_rejected(self):
        await self.initial()
        for owner in (Owner("other", OWNER.agent_id, OWNER.session_id), Owner(OWNER.user_id, "other", OWNER.session_id), Owner(OWNER.user_id, OWNER.agent_id, "other")):
            with self.assertRaises(AnalysisError):
                resolve(self.store, owner, self.action["actionRef"])
        for ref in (self.action["actionRef"].upper(), self.action["actionRef"] + "?query=x", self.ref + "#action_" + "0" * 24):
            with self.assertRaises(AnalysisError):
                resolve(self.store, OWNER, ref)

    async def test_request_key_is_idempotent_and_same_key_other_action_conflicts(self):
        await self.initial(unit=None)
        first = await self.start()
        again = await self.start()
        self.assertEqual(first["actionRun"]["actionRunId"], again["actionRun"]["actionRunId"])
        other = self.artifact["actions"][1]
        with self.assertRaises(AnalysisError) as failure:
            await self.start(action=other)
        self.assertEqual(failure.exception.code, "REQUEST_CONFLICT")
        with self.assertRaises(AnalysisError) as failure:
            await self.start(key="different")
        self.assertEqual(failure.exception.code, "NO_NEW_EVIDENCE")
        self.assertEqual(len(self.store.list_action_runs(OWNER)), 1)

    async def test_current_user_edits_do_not_inherit_old_goal_or_execute_on_click(self):
        await self.initial()
        self.service.actions.user_text = followup_text(self.action)
        with self.assertRaises(AnalysisError):
            await self.service.follow_up(self.action["actionRef"], "no-user-turn")
        for addition in ("\n改查下一年度", "\n换成其他机构", "\n改为查采购量"):
            with self.assertRaises(AnalysisError) as failure:
                await self.start(text=followup_text(self.action) + addition)
            self.assertEqual(failure.exception.code, "ACTION_INTENT_CHANGED")
        self.assertEqual(self.store.list_action_runs(OWNER), [])
        # Changed intent is still supported by a new plan and version relation.
        refs, selections, scope = self.capture(period="2027N0001", tag="changed")
        built = await self.service.build(refs, selections, scope, [], "changed", baseAnalysisRef=self.ref)
        draft = self.store.get_analysis(OWNER, built["analysisRef"])
        self.assertEqual(draft["baseAnalysisRef"], self.ref)
        self.assertNotIn("actionRunRef", draft)

    async def test_fresh_capture_plan_checks_period_object_source_metric_and_provenance(self):
        await self.initial()
        await self.start()
        for change in ({"period": "2027N0001"}, {"member": "expanded-object"}, {"metric": "other"}, {"resource": "other-resource"}):
            with self.assertRaises(AnalysisError) as failure:
                await self.next_build(**change)
            self.assertIn(failure.exception.code, ("ACTION_SCOPE_CHANGED", "ACTION_INTENT_CHANGED", "ACTION_SOURCE_CHANGED"))
        old = self.artifact
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build(old["captureRefs"], old["plan"]["selections"], old["plan"]["scope"], [], "reuse-old", baseAnalysisRef=self.ref, actionRunRef=self.run)
        self.assertEqual(failure.exception.code, "ACTION_NEW_EVIDENCE_REQUIRED")

    async def test_same_receipt_without_new_observation_or_verification_stops(self):
        await self.initial(verified=False)
        await self.start()
        original = self.store.get_capture(OWNER, self.artifact["captureRefs"][0])
        original["toolCallId"] = "another-read-of-same-receipt"
        original.pop("captureRef")
        new_ref = self.store.capture(OWNER, original)
        self.service.actions.capture(new_ref, "success")
        selectors = deepcopy(self.artifact["plan"]["selections"])
        for s in selectors:
            s["captureRef"] = new_ref
        scope = deepcopy(self.artifact["plan"]["scope"])
        scope["population"]["captureRef"] = new_ref
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build([new_ref], selectors, scope, [], "duplicate-source", baseAnalysisRef=self.ref, actionRunRef=self.run)
        self.assertEqual(failure.exception.code, "NO_NEW_EVIDENCE")
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "PARTIAL")

    async def test_cancel_prevents_prepared_draft_from_completing_and_restart_is_honest(self):
        await self.initial()
        await self.start()
        built = await self.next_build()
        self.service.actions.finish("CANCELLED", "USER_INTERRUPTED")
        with self.assertRaises(AnalysisError):
            await self.service.publish(built["analysisRef"], [], [], "after-cancel", built["baseRevision"])
        self.assertEqual(self.store.get_analysis(OWNER, built["analysisRef"])["publicationState"], "DRAFT")
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "CANCELLED")
        await self.start(key="new-user-request", turn="user-turn-2")
        reopened = AnalysisStore(self.store.root)
        reopened.recover_action_runs(OWNER)
        run = reopened.get_action_run(OWNER, self.run)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertIsNone(run["resultAnalysisRef"])

    async def test_backup_restore_preserves_runs_events_and_pinned_evidence(self):
        await self.initial()
        await self.start()
        built = await self.next_build()
        await self.service.publish(built["analysisRef"], [], [], "publish-next", built["baseRevision"])
        backup = self.root / "backup"
        backup.mkdir()
        with closing(sqlite3.connect(self.store.database)) as source, closing(sqlite3.connect(backup / "analysis.sqlite3")) as target:
            source.backup(target)
        shutil.copytree(self.store.blobs, backup / "blobs")
        restored = AnalysisStore(backup)
        self.assertEqual(restored.get_action_run(OWNER, self.run), self.store.get_action_run(OWNER, self.run))
        restored.collect_expired(10**12)
        for ref in restored.get_action_run(OWNER, self.run)["newCaptureRefs"]:
            self.assertEqual(restored.get_capture(OWNER, ref), self.store.get_capture(OWNER, ref))
        with closing(sqlite3.connect(restored.database)) as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM action_events").fetchone()[0], 2)
