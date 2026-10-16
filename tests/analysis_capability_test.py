"""Scenario choices are explicit inputs, independent of labels and value signs."""
from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
import unittest

from jsonschema import Draft202012Validator
from analysis_action_fixtures import ActionCase
from analysis_fixtures import OWNER
from analysis_extension.models import AnalysisError, canonical
from analysis_extension.presentation import project
from analysis_extension.tools import AnalysisInspect, AnalysisPublish


class CapabilityTests(ActionCase):
    async def draft(self, values=("-8", "0", "12"), unit="件", tag="explicit", operations=None, base=None):
        refs, selections, scope = self.capture(values=values, unit=unit, tag=tag, metric="UNSEEN_METRIC")
        return await self.service.build(refs, selections, scope, operations or [], "build-" + tag, baseAnalysisRef=base)

    def facts(self, draft):
        return self.store.get_analysis(OWNER, draft["analysisRef"])["facts"]

    async def preview(self, draft, sign):
        plan = [{"kind": "SIGN", "sign": sign, "factRefs": [f["factId"] for f in self.facts(draft)]}]
        preview = await self.service.inspect("findings", draft["analysisRef"], findingPlan=plan)
        return plan, preview["items"]

    async def test_omitted_plans_never_infer_negative_unit_gaps_or_actions(self):
        draft = await self.draft(unit=None)
        self.assertEqual((await self.service.inspect("findings", draft["analysisRef"]))["items"], [])
        await self.service.publish(draft["analysisRef"], [], [], "no-plan", 0)
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        self.assertEqual(artifact["findings"], [])
        self.assertEqual(artifact["actions"], [])
        self.assertEqual(len(artifact["facts"]), 3)

    async def test_same_input_supports_prompt_selected_positive_zero_or_negative(self):
        draft = await self.draft()
        for sign, index in (("negative", 0), ("zero", 1), ("positive", 2)):
            plan, findings = await self.preview(draft, sign)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["factRefs"], [self.facts(draft)[index]["factId"]])
            self.assertEqual(findings[0]["support"], "SUPPORTED")
            self.assertEqual(findings[0]["explanations"][0]["missingEvidence"], [])
        await self.service.publish(draft["analysisRef"], [], [], "positive", 0, findingPlan=plan)
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        self.assertEqual(artifact["findings"][0]["findingId"], findings[0]["findingId"])
        self.assertEqual(artifact["actions"], [])

    async def test_only_prompt_proposed_action_and_priority_are_registered(self):
        draft = await self.draft()
        plan, findings = await self.preview(draft, "positive")
        action = {"findingRefs": [findings[0]["findingId"]], "check": "VERIFY_VALUE",
            "goal": "核对传感器校准后的读数", "requiredEvidence": ["同一位置新的仪器读数和核验"],
            "expectedDecision": "判断读数条件是否仍成立", "priorityBasis": "用户本轮首先关注该传感器"}
        await self.service.publish(draft["analysisRef"], [], [], "sensor", 0, findingPlan=plan, actionPlan=[action])
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        self.assertEqual(len(artifact["actions"]), 1)
        actual = artifact["actions"][0]
        for key in ("goal", "requiredEvidence", "expectedDecision", "priorityBasis"):
            self.assertEqual(actual[key], action[key])
        self.assertEqual(actual["availability"], "AVAILABLE")
        self.assertEqual(actual["objectRefs"], [self.facts(draft)[2]["objectId"]])

    async def test_action_plan_cannot_forge_support_scope_capabilities_or_completion(self):
        draft = await self.draft()
        plan, findings = await self.preview(draft, "negative")
        action = {"findingRefs": [findings[0]["findingId"]], "check": "VERIFY_VALUE", "goal": "核对读数",
                  "requiredEvidence": ["新读数"], "expectedDecision": "确认结果", "priorityBasis": "用户选择"}
        for change in ({"availability": "AVAILABLE"}, {"completed": True}, {"scopeId": "forged"}, {"capabilityRefs": ["forged"]}, {"findingRefs": ["forged"]}, {"goal": "  "}):
            with self.subTest(change=change), self.assertRaises(AnalysisError):
                await self.service.publish(draft["analysisRef"], [], [], "bad", 0, findingPlan=plan, actionPlan=[{**action, **change}])
        self.assertEqual(self.store.get_analysis(OWNER, draft["analysisRef"])["publicationState"], "DRAFT")

    async def test_explicit_view_plan_does_not_add_statistics_or_charts(self):
        draft = await self.draft(operations=[{"id": "count", "operator": "COUNT"}, {"id": "rank", "operator": "RANK"}])
        await self.service.publish(draft["analysisRef"], [], [], "table-only", 0)
        document = self.delivery.view(self.delivery.ensure(draft["analysisRef"])["path"])
        self.assertEqual(document["metrics"], [])
        self.assertIsNone(comparison(document))
        self.assertIsNone(document["chartUnavailable"])
        self.assertEqual(diagrams(document), [])
        self.assertFalse(document["chartsRequested"])
        self.assertEqual(document["viewDerivedValues"], [])
        self.assertEqual(document["rows"], [])
        self.assertEqual(document["tables"], [])

    async def test_requested_scalar_metric_does_not_add_count_or_ranking(self):
        draft = await self.draft()
        view = {"viewId": "one-reading", "scopeId": draft["scope"]["scopeId"], "type": "METRIC", "valueRefs": [self.facts(draft)[2]["factId"]]}
        await self.service.publish(draft["analysisRef"], [], [view], "metric", 0)
        document = self.delivery.view(self.delivery.ensure(draft["analysisRef"])["path"])
        self.assertEqual([m["display"] for m in document["metrics"]], ["12"])
        self.assertIsNone(comparison(document))
        self.assertEqual(self.delivery.project_view(draft["analysisRef"], {"filter": "negative"})["metrics"], [])

    async def test_rank_uses_only_explicitly_selected_inputs(self):
        draft = await self.draft(operations=[{"id": "rank-one", "operator": "RANK", "select": {"objectId": "object-2"}}])
        views = [{"viewId": "one", "scopeId": draft["scope"]["scopeId"], "type": "BAR_RANK", "valueRefs": [draft["derivedValues"][0]["derivedId"]]}]
        await self.service.publish(draft["analysisRef"], [], views, "rank-one", 0)
        view = self.delivery.view(self.delivery.ensure(draft["analysisRef"])["path"])
        self.assertEqual(comparison(view)["rowKeys"], [view["rows"][0]["rowKey"]])
        self.assertEqual(view["metrics"], [])

    async def test_metric_capacity_is_checked_before_publication(self):
        draft = await self.draft(values=tuple(str(i) for i in range(201)))
        views = [{"viewId": "too-many", "scopeId": draft["scope"]["scopeId"], "type": "METRIC", "valueRefs": [f["factId"] for f in self.facts(draft)]}]
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(draft["analysisRef"], [], views, "too-many", 0)
        self.assertEqual(failure.exception.code, "VIEW_LIMIT")
        self.assertEqual(self.store.get_analysis(OWNER, draft["analysisRef"])["publicationState"], "DRAFT")

    async def test_registered_threshold_does_not_select_itself(self):
        self.service.finding_rules = {"temperature-limit": {"source": "合成传感器规范", "version": "1", "sourceType": "policy",
            "operator": "gt", "threshold": "10", "unit": "件", "scale": "1", "appliesTo": {"measureCode": "UNSEEN_METRIC"}}}
        draft = await self.draft()
        self.assertEqual((await self.service.inspect("findings", draft["analysisRef"]))["items"], [])
        findings = await self.service.inspect("findings", draft["analysisRef"], findingPlan=[{"kind": "THRESHOLD", "ruleRef": "temperature-limit"}])
        self.assertEqual(len(findings["items"]), 1)
        self.assertEqual(findings["items"][0]["factRefs"], [self.facts(draft)[2]["factId"]])

    async def test_positive_condition_recheck_is_explicit_and_generic(self):
        original = await self.draft(values=("2",), tag="old-positive")
        plan, old = await self.preview(original, "positive")
        await self.service.publish(original["analysisRef"], [], [], "old", 0, findingPlan=plan)
        before = canonical(self.store.get_analysis(OWNER, original["analysisRef"]))
        new = await self.draft(values=("0",), tag="new-zero", base=original["analysisRef"])
        self.assertEqual((await self.service.inspect("findings", new["analysisRef"]))["items"], [])
        plan = [{"kind": "RECHECK", "previousFindingRef": original["analysisRef"] + "#" + old[0]["findingId"], "factRefs": [self.facts(new)[0]["factId"]]}]
        findings = await self.service.inspect("findings", new["analysisRef"], findingPlan=plan)
        self.assertEqual(findings["items"][0]["support"], "CONTRADICTED")
        await self.service.publish(new["analysisRef"], [], [], "new", new["baseRevision"], findingPlan=plan)
        self.assertEqual(canonical(self.store.get_analysis(OWNER, original["analysisRef"])), before)

    async def test_unknown_or_forged_check_fails_and_plans_participate_in_idempotency(self):
        draft = await self.draft()
        for plan in ([{"kind": "NET_PROFIT"}], [{"kind": "SIGN", "sign": "negative", "factRefs": ["fake"]}], [{"kind": "COVERAGE_GAP", "support": "SUPPORTED"}]):
            with self.assertRaises(AnalysisError):
                await self.service.inspect("findings", draft["analysisRef"], findingPlan=plan)
        positive, _ = await self.preview(draft, "positive")
        negative, _ = await self.preview(draft, "negative")
        result = await self.service.publish(draft["analysisRef"], [], [], "key", 0, findingPlan=positive)
        self.assertEqual((await self.service.publish(draft["analysisRef"], [], [], "key", 0, findingPlan=positive))["analysisRef"], result["analysisRef"])
        with self.assertRaises(AnalysisError) as failure:
            await self.service.publish(draft["analysisRef"], [], [], "key", 0, findingPlan=negative)
        self.assertEqual(failure.exception.code, "REQUEST_CONFLICT")

    async def test_projection_requires_explicit_current_policy(self):
        draft = await self.draft()
        await self.service.publish(draft["analysisRef"], [], [], "publish", 0)
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        artifact.pop("presentationPolicy")
        before = canonical(artifact)
        with self.assertRaises(AnalysisError) as failure:
            project(artifact, self.rules)
        self.assertEqual(failure.exception.code, "UNSUPPORTED_VERSION")
        self.assertEqual(canonical(artifact), before)

    async def test_public_tool_schemas_expose_explicit_capabilities_and_reject_extra_state(self):
        protocol = await self.service.inspect("protocol")
        self.assertEqual(protocol["planPolicy"], "EXPLICIT_ONLY")
        self.assertIn("RECHECK", protocol["findingChecks"])
        self.assertIn("REQUEST_EVIDENCE", protocol["actionChecks"])
        for cls in (AnalysisInspect, AnalysisPublish):
            Draft202012Validator.check_schema(cls.input_schema)
        validator = Draft202012Validator(AnalysisInspect.input_schema)
        valid = {"section": "findings", "analysisRef": "ref", "findingPlan": [{"kind": "SIGN", "sign": "positive", "factRefs": ["fact"]}]}
        self.assertEqual(list(validator.iter_errors(valid)), [])
        valid["findingPlan"][0]["support"] = "SUPPORTED"
        self.assertTrue(list(validator.iter_errors(valid)))


if __name__ == "__main__":
    unittest.main()
