"""Older formats are rejected without migration, completion or implicit defaults."""
from copy import deepcopy
from unittest.mock import patch

from analysis_action_fixtures import ActionCase
from analysis_fixtures import OWNER
from analysis_extension.actions import outcome
from analysis_extension.findings import generate
from analysis_extension.models import AnalysisError, canonical, digest
from analysis_extension.protocol import validate_artifact


class CurrentContractTests(ActionCase):
    async def test_required_artifact_fields_and_current_versions(self):
        await self.initial()
        for field in ("technicalDetails", "presentationPolicy", "findingRules", "findingVersion", "findings", "actions",
                      "sourceOrigins", "sourceResourceIds", "verificationRecords", "verificationRecordedAt"):
            value = deepcopy(self.artifact)
            del value[field]
            with self.subTest(field=field), self.assertRaises(AnalysisError):
                validate_artifact(value)
        for field, version in (("schemaVersion", 0), ("engineVersion", "1.0.0"), ("findingVersion", "1.0.0")):
            value = {**self.artifact, field: version}
            with self.subTest(field=field), self.assertRaises(AnalysisError) as failure:
                validate_artifact(value)
            self.assertEqual(failure.exception.code, "UNSUPPORTED_VERSION")
        value = deepcopy(self.artifact)
        del value["facts"][0]["sourceOrigins"]
        with self.assertRaises(AnalysisError):
            validate_artifact(value)

    async def test_store_inspect_and_base_revision_reject_incomplete_record_without_repair(self):
        await self.initial()
        value = deepcopy(self.artifact)
        del value["sourceOrigins"]
        before = canonical(value)
        with self.store._transaction() as db:
            blob = self.store._put(db, value)
            db.execute("UPDATE analyses SET digest=? WHERE owner=? AND ref=?", (blob, OWNER.key, self.ref))
        with self.assertRaises(AnalysisError):
            self.store.get_analysis(OWNER, self.ref)
        with self.assertRaises(AnalysisError):
            await self.service.inspect("findings", self.ref)
        refs, selections, scope = self.capture()
        with self.assertRaises(AnalysisError):
            await self.service.build(refs, selections, scope, [], "new-base", baseAnalysisRef=self.ref)
        self.assertEqual(canonical(self.store._read(blob)), before)

    async def test_recheck_never_infers_predicate_from_old_negative_type(self):
        await self.initial()
        base = deepcopy(self.artifact)
        finding = base["findings"][0]
        finding["type"] = "negative"
        finding["predicate"] = None
        with self.assertRaises(AnalysisError) as failure:
            generate(self.artifact, self.rules, base=base, plan=[{
                "kind": "RECHECK", "previousFindingRef": self.ref + "#" + finding["findingId"],
                "factRefs": finding["factRefs"],
            }])
        self.assertEqual(failure.exception.code, "INVALID_FINDING_PLAN")
        self.assertIn("没有可复核的数值条件", str(failure.exception))

    async def test_old_or_unknown_action_never_completes(self):
        await self.initial()
        for kind in ("BUSINESS_EVIDENCE", "unknown"):
            action = {**self.action, "goalType": kind}
            with self.subTest(kind=kind), self.assertRaises(AnalysisError):
                outcome(self.artifact, action, self.artifact)
            value = {**self.artifact, "actions": [action]}
            with self.assertRaises(AnalysisError):
                validate_artifact(value)

    async def test_current_snapshot_cannot_omit_plan_and_insight_fields(self):
        await self.initial()
        for field in ("technicalDetails", "findings", "actions", "findingsScopeLabel", "chartsRequested"):
            saved = self.store.get_presentation(OWNER, "delivery", self.ref)
            del saved["document"][field]
            saved["manifest"]["snapshotDigest"] = digest(saved["document"])
            with self.subTest(field=field), self.assertRaises(AnalysisError) as failure:
                self.delivery._validate(saved, self.artifact, check_file=False)
            self.assertEqual(failure.exception.code, "INVALID_PRESENTATION")

    async def test_report_layout_is_required_and_request_version_is_not_dropped(self):
        await self.initial()
        report = self.delivery.render_report(self.ref, "current-report")
        read = self.store.get_presentation
        def missing_layout(owner, kind, key):
            result = read(owner, kind, key)
            if kind == "export":
                del result["delivery"]["layout"]
            return result
        with patch.object(self.store, "get_presentation", side_effect=missing_layout):
            with self.assertRaises(AnalysisError) as failure:
                self.delivery.report(self.ref, report["exportRef"])
            self.assertEqual(failure.exception.code, "INVALID_PRESENTATION")
        def old_request(owner, kind, key):
            result = read(owner, kind, key)
            if kind == "report-request":
                result["request"]["rendererVersion"] = "1.0.0"
            return result
        with patch.object(self.store, "get_presentation", side_effect=old_request):
            with self.assertRaises(AnalysisError) as failure:
                self.delivery.render_report(self.ref, "current-report")
            self.assertEqual(failure.exception.code, "REQUEST_CONFLICT")

    async def test_report_file_failure_does_not_hide_current_analysis_details(self):
        await self.initial()
        report = self.delivery.render_report(self.ref, "current-report")
        path = self.root / "workspace" / report["path"][2:]
        path.write_text("modified export", "utf-8")
        view = self.delivery.view(self.path)
        self.assertEqual(view["total"], len(self.artifact["facts"]))
        self.assertEqual(view["reports"], [report])
        with self.assertRaises(AnalysisError) as failure:
            self.delivery.report(self.ref, report["exportRef"])
        self.assertEqual(failure.exception.code, "PROJECTION_CHANGED")
