"""Adversarial regressions from the complete Batch 3 self-review."""
from copy import deepcopy
from unittest.mock import patch

from analysis_action_fixtures import ActionCase, finding_plan, action_plan
from analysis_fixtures import OWNER
from analysis_extension.engine import RuleRegistry
from analysis_extension.findings import generate
from analysis_extension.models import AnalysisError
from analysis_extension.models import digest
from analysis_extension.actions import proposals, source_identity
from analysis_action_fixtures import CAPABILITY


def formula(version="1", coefficient="-1"):
    return RuleRegistry({"composition": {"source": "合成公式", "version": version, "operators": ["LINEAR"],
        "terms": [{"match": {"objectId": "object-0"}, "coefficient": "1"},
                  {"match": {"objectId": "object-1"}, "coefficient": coefficient}]}})


class ReviewTests(ActionCase):
    async def test_old_finding_version_cannot_be_published_with_new_host_rules(self):
        refs, selections, scope = self.capture()
        with patch('analysis_extension.findings.FINDING_VERSION', '1.0.0'):
            built = await self.service.build(refs, selections, scope, [], 'old-host-draft')
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(built['analysisRef'], [], [], 'new-host-publish', 0)
        self.assertEqual(failure.exception.code, 'UNSUPPORTED_VERSION')
        with self.assertRaises(AnalysisError):
            self.store.get_analysis(OWNER, built['analysisRef'])

    async def test_verifier_from_another_resource_cannot_prove_the_source(self):
        refs, selections, scope = self.capture()
        other = self.store.get_capture(OWNER, refs[1])
        other.pop("captureRef")
        other["resourceId"] = "another-source-resource"
        refs[1] = self.store.capture(OWNER, other)
        built = await self.service.build(refs, selections, scope, [], "cross-resource")
        self.assertEqual(built["receiptVerification"], "UNVERIFIED")

    async def test_changed_connection_cannot_reuse_matching_execution_and_evidence_ids(self):
        refs, selections, scope = self.capture()
        other = self.store.get_capture(OWNER, refs[1])
        other.pop("captureRef")
        other["connectionFingerprint"] = "different-connection"
        refs[1] = self.store.capture(OWNER, other)
        built = await self.service.build(refs, selections, scope, [], "cross-connection")
        self.assertEqual(built["receiptVerification"], "UNVERIFIED")

    async def test_unrelated_fresh_verification_does_not_refresh_old_valid_evidence(self):
        await self.initial()
        await self.start()
        repeated = self.store.get_capture(OWNER, self.artifact["captureRefs"][0])
        repeated.pop("captureRef")
        repeated["toolCallId"] = "repeat-existing-observation"
        fresh_ref = self.store.capture(OWNER, repeated)
        unrelated, _, _ = self.capture(tag="unrelated")
        for ref in (fresh_ref, unrelated[1]):
            self.service.actions.capture(ref, "success")
        selections = deepcopy(self.artifact["plan"]["selections"])
        for selection in selections:
            selection["captureRef"] = fresh_ref
        scope = deepcopy(self.artifact["plan"]["scope"])
        scope["population"]["captureRef"] = fresh_ref
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build([fresh_ref, self.artifact["captureRefs"][1], unrelated[1]], selections, scope,
                [], "irrelevant-verification", baseAnalysisRef=self.ref, actionRunRef=self.run)
        self.assertEqual(failure.exception.code, "NO_NEW_EVIDENCE")
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "PARTIAL")

    async def test_different_source_unit_or_rounded_zero_cannot_contradict_old_exact_negative(self):
        await self.initial()
        for change in ("resource", "source", "connection", "unit", "precision"):
            with self.subTest(change=change):
                current = deepcopy(self.artifact)
                current["facts"][0]["value"] = "0"
                if change == "resource":
                    current["sourceResourceIds"] = ["different-resource"]
                elif change == "source":
                    current["context"]["sourceId"] = "different-source"
                elif change == "unit":
                    current["facts"][0]["unit"] = "件"
                elif change == "connection":
                    current["sourceOrigins"] = ["different-connection"]
                else:
                    current["facts"][0]["precision"] = "display_only"
                output = generate(current, self.rules, base=self.artifact, plan=[{"kind": "RECHECK", "previousFindingRef": self.ref + "#" + self.artifact["findings"][0]["findingId"], "factRefs": [current["facts"][0]["factId"]]}])
                self.assertFalse(any(f["support"] == "CONTRADICTED" for f in output), change)

    async def test_formula_change_between_build_and_publish_keeps_draft_unpublished(self):
        self.service.rules = self.delivery.rules = formula()
        refs, selections, scope = self.capture(values=("100", "120"))
        built = await self.service.build(refs, selections, scope,
            [{"id": "composition", "operator": "LINEAR", "ruleRef": "composition"}], "formula-draft")
        self.service.rules = self.delivery.rules = formula("2", "1")
        with self.assertRaises(AnalysisError):
            await self.service.inspect("findings", built["analysisRef"])
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(built["analysisRef"], [], [], "changed-rule", 0)
        self.assertEqual(failure.exception.code, "RULE_VERSION_UNAVAILABLE")
        self.assertEqual(self.store.get_analysis(OWNER, built["analysisRef"])["publicationState"], "DRAFT")

    async def test_formula_followup_does_not_complete_a_different_rule_under_same_key(self):
        self.service.rules = self.delivery.rules = formula()
        operations = [{"id": "composition", "operator": "LINEAR", "ruleRef": "composition"}]
        refs, selections, scope = self.capture(values=("100", "120"))
        built = await self.service.build(refs, selections, scope, operations, "formula-original")
        original = await self.publish_planned(built, "formula-publish")
        self.ref = original["analysisRef"]
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.action = next(a for a in self.artifact["actions"] if a["goalType"] == "VERIFY_COMPOSITION")
        await self.start()
        self.service.rules = self.delivery.rules = formula("2", "1")
        refs, selections, scope = self.capture(values=("100", "120"), tag="new")
        for ref in refs:
            self.service.actions.capture(ref, "success")
        built = await self.service.build(refs, selections, scope, operations, "changed-formula", self.ref, self.run)
        await self.service.publish(built["analysisRef"], [], [], "changed-formula-publish", built["baseRevision"])
        run = self.store.get_action_run(OWNER, self.run)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["diagnostics"][-1]["code"], "FORMULA_RULE_CHANGED")

    async def test_oversize_findings_fail_before_publication_or_action_completion(self):
        await self.initial()
        await self.start()
        built = await self.next_build()
        plan = finding_plan(self.store.get_analysis(OWNER, built["analysisRef"]))
        findings = await self.service.inspect("findings", built["analysisRef"], findingPlan=plan)
        hypotheses = [{"findingId": findings["items"][0]["findingId"], "statement": "待核查原因 " + str(i),
            "missingEvidence": ["合成待补资料" * 150] * 20} for i in range(30)]
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(built["analysisRef"], [], [], "oversize-findings", built["baseRevision"], hypotheses=hypotheses, findingPlan=plan)
        self.assertEqual(failure.exception.code, "VIEW_TOO_LARGE")
        self.assertEqual(self.store.get_analysis(OWNER, built["analysisRef"])["publicationState"], "DRAFT")
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "RUNNING")
        self.assertIsNone(self.store.get_action_run(OWNER, self.run)["resultAnalysisRef"])

    async def test_whitespace_is_not_specific_missing_evidence(self):
        await self.initial()
        for statement, missing in (("   ", ["事项明细"]), ("有待验证的解释", [" \n "])):
            with self.subTest(statement=statement):
                with self.assertRaises(AnalysisError) as failure:
                    generate(self.artifact, self.rules, plan=finding_plan(self.artifact), hypotheses=[{"findingId": self.artifact["findings"][0]["findingId"],
                        "statement": statement, "missingEvidence": missing}])
                self.assertEqual(failure.exception.code, "INVALID_EXPLANATION")

    async def test_current_matching_verification_can_recheck_the_same_observation(self):
        await self.initial()
        await self.start()
        refs = []
        for index, old in enumerate(self.artifact["captureRefs"]):
            capture = self.store.get_capture(OWNER, old)
            capture.pop("captureRef")
            capture["toolCallId"] = "new-call-" + str(index)
            ref = self.store.capture(OWNER, capture)
            refs.append(ref)
            self.service.actions.capture(ref, "success")
        selections = deepcopy(self.artifact["plan"]["selections"])
        selections[0]["captureRef"] = refs[0]
        scope = deepcopy(self.artifact["plan"]["scope"])
        scope["population"]["captureRef"] = refs[0]
        built = await self.service.build(refs, selections, scope, [], "fresh-verify", self.ref, self.run)
        await self.service.publish(built["analysisRef"], [], [], "fresh-verify-publish", built["baseRevision"])
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "COMPLETED")

    async def test_verification_not_captured_in_this_run_cannot_complete_new_values(self):
        await self.initial()
        await self.start()
        refs, selections, scope = self.capture(tag="new")
        self.service.actions.capture(refs[0], "success")
        built = await self.service.build(refs, selections, scope, [], "uncaptured-verify", self.ref, self.run)
        self.assertEqual(built["receiptVerification"], "UNVERIFIED")
        await self.service.publish(built["analysisRef"], [], [], "uncaptured-verify-publish", built["baseRevision"])
        self.assertEqual(self.store.get_action_run(OWNER, self.run)["status"], "PARTIAL")

    async def test_connection_change_mid_run_requires_a_new_plan(self):
        await self.initial()
        await self.start()
        refs, selections, scope = self.capture(tag="new")
        changed = []
        for ref in refs:
            capture = self.store.get_capture(OWNER, ref)
            capture.pop("captureRef")
            capture["connectionFingerprint"] = "changed-mid-run"
            changed.append(self.store.capture(OWNER, capture))
            self.service.actions.capture(changed[-1], "success")
        selections[0]["captureRef"] = changed[0]
        scope["population"]["captureRef"] = changed[0]
        with self.assertRaises(AnalysisError) as failure:
            await self.service.build(changed, selections, scope, [], "changed-connection", self.ref, self.run)
        self.assertEqual(failure.exception.code, "ACTION_SOURCE_CHANGED")

    async def test_execution_and_verifier_capabilities_must_pair_on_each_selected_source(self):
        await self.initial()
        artifact = deepcopy(self.artifact)
        other = {**CAPABILITY, "resourceId": "other-resource", "originalToolName": "validate_evidence", "roles": ["verify"], "capabilityRef": "other-verify"}
        artifact["sourceResourceIds"].append(other["resourceId"])
        artifact["sourceOrigins"].append(digest(source_identity(other)))
        no_pair = proposals(artifact, [CAPABILITY, other], action_plan(artifact["findings"]))
        self.assertEqual(no_pair[0]["availability"], "UNAVAILABLE")
        matching = {**CAPABILITY, "originalToolName": "validate_evidence", "roles": ["verify"], "capabilityRef": "same-verify"}
        correct = proposals(artifact, [CAPABILITY, matching, other], action_plan(artifact["findings"]))
        self.assertEqual(correct[0]["availability"], "AVAILABLE")
        self.assertNotIn("other-verify", correct[0]["capabilityRefs"])
