"""Synthetic evidence with independent source execution / snapshot identities."""
from copy import deepcopy
import json

from analysis_fixtures import OWNER, fixture
from analysis_presentation_fixtures import PresentationCase

CAPABILITY = {"capabilityRef": "capability_fixture", "resourceId": "fixture-resource", "readOnly": True, "roles": ["query"],
              "toolName": "mcp__fixture__execute_semantic_plan", "originalToolName": "execute_semantic_plan"}


def finding_plan(artifact):
    """This test scenario explicitly requests sign/gap/calculation checks."""
    refs = [f["factId"] for f in artifact["facts"]]
    plan = [{"kind": "SIGN", "sign": "negative", "factRefs": refs},
            {"kind": "UNIT_GAP", "factRefs": refs}, {"kind": "MISSING_VALUE", "factRefs": refs},
            {"kind": "COVERAGE_GAP"}]
    if artifact["derivedValues"]:
        plan.append({"kind": "CALCULATION", "derivedRefs": [v["derivedId"] for v in artifact["derivedValues"]]})
    return plan


def action_plan(findings):
    """Test input analogous to a prompt's choices, never production defaults."""
    return [{"findingRefs": [f["findingId"]],
        "check": {"unit": "CONFIRM_UNIT", "coverage": "COMPLETE_COVERAGE", "composition": "VERIFY_COMPOSITION", "formula_gap": "REQUEST_EVIDENCE"}.get(f["type"], "VERIFY_VALUE"),
        "goal": "核对合成测试所选输入", "requiredEvidence": ["新的来源与核验"],
        "expectedDecision": "确认所选检查的结果", "priorityBasis": "测试明确选择此检查"} for f in findings]


class ActionCase(PresentationCase):
    def setUp(self):
        super().setUp()
        self.service.actions_enabled = True
        self.delivery.actions_enabled = True
        async def capabilities():
            return [deepcopy(CAPABILITY), {**CAPABILITY, "originalToolName": "validate_evidence", "roles": ["verify"], "capabilityRef": "capability_verify"}]
        self.service.actions.capability_provider = capabilities

    def capture(self, values=("-8",), unit="元", tag="original", verified=True, snapshot=None, period=None, member=None, metric=None, resource="fixture-resource"):
        capture, selectors, scope = fixture(values, unit=unit)
        receipt = json.loads(capture["response"]["content"][0]["text"])
        receipt["executionId"] = "execution-" + tag
        capture["toolCallId"] = tag
        capture["resourceId"] = resource
        if snapshot:
            receipt["context"]["snapshotId"] = snapshot
            receipt["context"]["contextId"] = "context-" + snapshot
            capture["request"]["contextId"] = receipt["context"]["contextId"]
        receipt["evidence"] = []
        for i, step in enumerate(receipt["data"]["stepResults"][1:]):
            form = step["formResults"][0]
            runtime = form["runtime"]
            runtime["context"]["baseContext"] = deepcopy(receipt["context"])
            runtime["observation"]["observationId"] = f"observation-{i}-{tag}"
            runtime["observation"]["contentDigest"] = "digest-" + tag + str(i)
            form["page"]["observationId"] = runtime["observation"]["observationId"]
            form["page"]["contentDigest"] = runtime["observation"]["contentDigest"]
            if period:
                runtime["context"]["period"] = period
                runtime["context"]["effectiveDimensions"]["DATATIME"] = period
                form["page"]["cells"][0]["dimensions"]["DATATIME"] = period
                capture["request"]["plan"]["steps"][i + 1]["formQuery"]["periodCode"] = period
                scope["expectedDimensions"]["DATATIME"] = period
            if member:
                runtime["context"]["effectiveDimensions"]["MD_ORG"] = member
                capture["request"]["plan"]["steps"][i + 1]["formQuery"]["dimensions"]["MD_ORG"] = member
                scope["members"][i] = member
                receipt["data"]["stepResults"][0]["rows"][i]["id"] = member
            if metric:
                form["page"]["cells"][0]["measureCode"] = metric
                form["page"]["cells"][0]["headers"] = ["合成采购数量"]
            receipt["evidence"].append({"evidenceId": "evidence-" + str(i) + tag, "kind": "FORM_RUNTIME", "fact": deepcopy(runtime)})
        capture["response"]["content"][0]["text"] = json.dumps(receipt)
        ref = self.store.capture(OWNER, capture)
        refs = [ref]
        for selector in selectors:
            selector["captureRef"] = ref
        scope["population"]["captureRef"] = ref
        if verified:
            verification = {"contractVersion": "2.0", "status": "SUCCEEDED", "context": receipt["context"], "querySessionId": "query-A",
                "data": {"action": "verify", "executionId": receipt["executionId"], "verification": {"status": "VALID", "evidenceIds": [e["evidenceId"] for e in receipt["evidence"]]}}}
            refs.append(self.store.capture(OWNER, {"resourceId": resource, "originalToolName": "validate_evidence", "captureState": "success",
                "sourceAdapter": deepcopy(capture["sourceAdapter"]),
                "request": {"action": "verify", "executionId": receipt["executionId"], "contextId": receipt["context"]["contextId"]},
                "response": {"state": "success", "content": [{"type": "text", "text": json.dumps(verification)}]}}))
        return refs, selectors, scope

    async def initial(self, **kwargs):
        refs, selections, scope = self.capture(**kwargs)
        built = await self.service.build(refs, selections, scope, [], "build-initial")
        result = await self.publish_planned(built, "publish-initial")
        self.ref = result["analysisRef"]
        self.path = result["delivery"]["path"]
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        self.action = self.artifact["actions"][0]
        return result

    async def publish_planned(self, built, key, plan=None):
        artifact = self.store.get_analysis(OWNER, built["analysisRef"])
        plan = finding_plan(artifact) if plan is None else plan
        preview = await self.service.inspect("findings", built["analysisRef"], findingPlan=plan)
        views = [{'viewId': 'details', 'scopeId': artifact['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': [f['factId'] for f in artifact['facts']]}]
        return await self.service.publish(built["analysisRef"], [], views, key, built["baseRevision"],
                                          findingPlan=plan, actionPlan=action_plan(preview["items"]))

    async def start(self, key="followup", action=None, text=None, turn="user-turn-1"):
        from analysis_extension.actions import followup_text
        action = action or self.action
        self.service.actions.user_text = text if text is not None else followup_text(action)
        self.service.actions.turn_ref = turn
        response = await self.service.follow_up(action["actionRef"], key)
        self.run = response["actionRun"]["actionRunId"]
        return response

    async def next_build(self, **kwargs):
        refs, selections, scope = self.capture(tag="new", **kwargs)
        for ref in refs:
            self.service.actions.capture(ref, "success")
        return await self.service.build(refs, selections, scope, [], "build-next", baseAnalysisRef=self.ref, actionRunRef=self.run)
