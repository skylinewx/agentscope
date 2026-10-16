from copy import deepcopy

from analysis_fixtures import OWNER
from analysis_action_fixtures import ActionCase, finding_plan
from analysis_extension.engine import RuleRegistry
from analysis_extension.findings import generate
from analysis_extension.models import AnalysisError
from analysis_extension.protocol import validate_artifact


class FindingTests(ActionCase):
    async def test_negative_observations_are_per_object_without_unit_or_name_inferences(self):
        await self.initial(values=("-8300", "-1033.14"), unit=None)
        negatives = [f for f in self.artifact["findings"] if f["type"] == "sign"]
        self.assertEqual(len(negatives), 2)
        self.assertNotEqual(negatives[0]["objectRefs"], negatives[1]["objectRefs"])
        self.assertEqual(negatives[0]["support"], "SUPPORTED")
        self.assertNotIn("未注明", negatives[0]["statement"])
        self.assertIn("仅说明数值符号", negatives[0]["statement"])
        self.assertEqual(len(self.artifact["scope"]["members"]), 2)
        self.assertTrue(all("同比" not in f["statement"] and "环比" not in f["statement"] for f in self.artifact["findings"]))
        renamed = deepcopy(self.artifact)
        for fact in renamed["facts"]:
            fact["objectLabel"] = "新增差额相似机构"
        actual = generate(renamed, self.rules, plan=finding_plan(renamed))
        self.assertEqual([f["type"] for f in actual], [f["type"] for f in self.artifact["findings"]])

    async def test_hypotheses_never_become_supported_and_need_specific_missing_evidence(self):
        await self.initial()
        fid = self.artifact["findings"][0]["findingId"]
        hypothesis = {"findingId": fid, "statement": "可能涉及一次性支出", "missingEvidence": ["同期该对象的支出项目金额及一次性事项说明"]}
        output = generate(self.artifact, self.rules, plan=finding_plan(self.artifact), hypotheses=[hypothesis])
        self.assertEqual(output[0]["explanations"][-1]["support"], "NEEDS_EVIDENCE")
        for changed in ({**hypothesis, "support": "SUPPORTED"}, {**hypothesis, "missingEvidence": []}, {**hypothesis, "findingId": "forged"}):
            with self.assertRaises(AnalysisError):
                generate(self.artifact, self.rules, plan=finding_plan(self.artifact), hypotheses=[changed])

    async def test_user_threshold_is_explicit_assumption_and_unregistered_policy_fails(self):
        await self.initial()
        threshold = {"factRefs": [self.artifact["facts"][0]["factId"]], "threshold": "0", "operator": "lt", "unit": "元", "source": "用户本轮约定"}
        output = generate(self.artifact, self.rules, thresholds=[threshold])
        self.assertIn("用户假设（非企业制度）", output[-1]["statement"])
        self.assertEqual(output[-1]["rule"]["sourceType"], "user_assumption")
        rounded = deepcopy(self.artifact)
        rounded["facts"][0]["precision"] = "display_only"
        with self.assertRaises(AnalysisError) as failure:
            generate(rounded, self.rules, thresholds=[threshold])
        self.assertEqual(failure.exception.code, "PRECISION_UNPROVEN")
        for invalid in ({**threshold, "sourceType": "policy"}, {**threshold, "source": ""}):
            with self.assertRaises(AnalysisError):
                generate(self.artifact, self.rules, thresholds=[invalid])
        with self.assertRaises(AnalysisError):
            generate(self.artifact, self.rules, configured={"bad": {"threshold": "0"}})

    async def test_formula_complete_and_missing_term_are_not_causal_proof(self):
        self.service.rules = RuleRegistry({"composition": {"source": "合成公式说明", "version": "1", "operators": ["LINEAR"],
            "terms": [{"match": {"objectId": "object-0"}, "coefficient": "1"}, {"match": {"objectId": "object-1"}, "coefficient": "-1"}]}})
        for count, expected in ((2, "composition"), (1, "formula_gap")):
            refs, selectors, scope = self.capture(values=("100", "120")[:count], tag=str(count))
            draft = await self.service.build(refs, selectors, scope, [{"id": "linear", "operator": "LINEAR", "ruleRef": "composition"}], "build" + str(count))
            output = (await self.service.inspect("findings", draft["analysisRef"], findingPlan=[{"kind": "CALCULATION", "derivedRefs": [draft["derivedValues"][0]["derivedId"]]}]))["items"]
            finding = next(f for f in output if f["type"] == expected)
            if count == 2:
                self.assertIn("-20", finding["statement"])
                self.assertEqual(finding["explanations"][0]["kind"], "arithmetic_composition")
            else:
                self.assertEqual(finding["support"], "NEEDS_EVIDENCE")
                self.assertIsNone(finding["explanations"][0]["residualRef"])
                self.assertTrue(finding["explanations"][0]["missingEvidence"])

    async def test_unknown_or_missing_capability_is_not_presented_as_executable(self):
        self.service.actions.capability_provider = None
        await self.initial()
        self.assertEqual(self.action["availability"], "UNAVAILABLE")
        response = await self.start()
        self.assertEqual(response["actionRun"]["status"], "AWAITING_INPUT")
        self.assertEqual(response["capabilities"], [])

    async def test_another_metric_uses_same_rules_and_missing_fields_are_rejected(self):
        await self.initial(metric="PURCHASE_QUANTITY", unit="件")
        self.assertIn("合成采购数量", self.artifact["findings"][0]["statement"])
        self.assertNotIn("净利润", self.artifact["findings"][0]["statement"])
        old = deepcopy(self.artifact)
        del old["findings"], old["actions"]
        with self.assertRaises(AnalysisError) as failure:
            validate_artifact(old)
        self.assertEqual(failure.exception.code, "INVALID_ARTIFACT")

    async def test_unverified_source_is_not_supported_and_filtered_report_keeps_scope_label(self):
        await self.initial(verified=False)
        self.assertEqual(self.artifact["findings"][0]["support"], "NEEDS_EVIDENCE")
        report = self.delivery.render_report(self.ref, "report", {"filter": "negative"})
        html = self.delivery.report(self.ref, report["exportRef"])["html"]
        self.assertIn("原发布范围的发现与建议", html)
        self.assertNotIn("待补证据", html)
        self.assertNotIn("不代表已经执行", html)
