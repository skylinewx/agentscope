"""Domain-independent formula outcomes, recoverable gaps and free HTML delivery."""
from copy import deepcopy
from html import unescape
import json
import re
from unittest import TestCase

from jsonschema import Draft202012Validator

from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.adapters import default_sources
from analysis_extension.completion import completion_summary
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.formulas import trace_formula
from analysis_extension.free_document import compile_document
from analysis_extension.service import compact_derived
from analysis_extension.tools import AnalysisInspect


def readings():
    shared = {
        "observationId": "sample-47", "bindingRef": "sensor-table",
        "objectId": "probe-kappa", "objectLabel": "探头 K",
        "sourceOrigins": ["lab-source"], "context": {"snapshot": "sample-47"},
        "period": "shift-omega", "unit": None, "scale": None,
        "valueState": "PRESENT", "precision": "raw_json",
    }
    return [
        {**shared, "factId": ref, "cellAddress": address, "cellKey": address,
         "headers": [label], "value": value, "formula": formula}
        for ref, address, label, value, formula in [
            ("output", "A1", "校准结果", "8", "B1-C1"),
            ("combined", "B1", "合成信号", "30", "D1+E1"),
            ("offset", "C1", "偏移量", "22", None),
            ("channel-x", "D1", "通道甲", "17", None),
            ("channel-y", "E1", "通道乙", "11", None),
        ]
    ]


def run_check(facts, target="output"):
    return calculate(facts, {"scopeId": "scope-lab"},
                     [{"id": "check", "operator": "FORMULA_CHECK", "inputRefs": [target], "checkScope": "RECURSIVE"}],
                     RuleRegistry())[0]


def completion(facts, value):
    return completion_summary({
        "facts": facts, "derivedValues": [value], "relations": [],
        "plan": {"workPlan": [{"taskId": "inspect", "title": "核对所选信号公式",
                              "kind": "COMPUTATION", "operator": "FORMULA_CHECK",
                              "targetRefs": ["output"], "refs": [value["derivedId"]]}]},
    })


class GenericFormulaTests(TestCase):
    def test_no_captured_formula_is_not_a_failure_or_a_calculated_number(self):
        for formula in (None, "", " ", "=", '{"f":""}'):
            for value in ("-19.125", "0", "23.7"):
                with self.subTest(formula=formula, value=value):
                    facts = readings()
                    facts[0].update(formula=formula, value=value)
                    original = deepcopy(facts)
                    result = run_check(facts)
                    self.assertEqual(result["executionStatus"], "COMPLETED")
                    self.assertEqual(result["valueState"], "NOT_APPLICABLE")
                    self.assertEqual(result["applicability"], "NO_CAPTURED_FORMULA")
                    self.assertEqual(result["sourceConsistency"], "NOT_CHECKED")
                    self.assertIsNone(result["value"])
                    self.assertEqual(result["formulaTrace"]["status"], "NO_FORMULA")
                    self.assertEqual(completion(facts, result)["state"], "READY")
                    self.assertEqual(facts, original)

    def test_no_formula_result_cannot_be_reused_as_zero_or_copied_source_value(self):
        facts = readings()
        facts[0]["formula"] = None
        results = calculate(facts, {"scopeId": "scope"}, [
            {"id": "check", "operator": "FORMULA_CHECK", "inputRefs": ["output"]},
            {"id": "sum", "operator": "ARITHMETIC", "inputRefs": ["derived_check"], "expression": "v0+1"},
        ], RuleRegistry())
        self.assertEqual(results[1]["diagnostics"][0]["code"], "UPSTREAM_CALCULATION_BLOCKED")
        self.assertFalse(results[1]["diagnostics"][0]["details"]["requiresSourceQuery"])
        self.assertIsNone(results[1]["value"])

    def test_unavailable_target_does_not_claim_a_captured_formula(self):
        facts = readings()
        facts[0].update(formula=None, value=None, valueState="NO_VALUE")
        result = run_check(facts)
        self.assertEqual(result["executionStatus"], "BLOCKED")
        self.assertEqual(result["applicability"], "UNDETERMINED")
        self.assertEqual(result["sourceConsistency"], "NOT_CHECKED")
        self.assertEqual(result["formulaTrace"]["recovery"]["blockers"][0]["code"],
                         "FORMULA_SOURCE_VALUE_MISSING")
        self.assertNotIn('completionReason', result)
        self.assertEqual(completion(facts, result)["state"], "INCOMPLETE")

    def test_local_match_and_recursive_difference_remain_distinct_for_nonfinancial_data(self):
        facts = readings()
        result = run_check(facts)
        trace = result["formulaTrace"]
        self.assertEqual(trace["targetLocalCheck"], {"status": "MATCH", "value": "8", "difference": "0"})
        self.assertEqual(trace["value"], "6")
        self.assertEqual(trace["nodes"][0]["propagatedDifference"], "2")
        self.assertEqual(trace["differenceOriginRefs"], ["combined"])
        self.assertEqual(result["sourceConsistency"], "MISMATCH")
        self.assertEqual(completion(facts, result)["state"], "READY")

    def test_missing_dependencies_are_actionable_and_do_not_complete_declared_work(self):
        facts = readings()[:-1]
        result = run_check(facts)
        detail = result["diagnostics"][0]["details"]
        self.assertEqual(detail["targetFactRef"], "output")
        self.assertEqual(detail["pendingDependencies"], [
            {"targetFactRef": "combined", "address": "E1", "reason": "DEPENDENCY_NOT_RESOLVED"}])
        self.assertIn("FORMULA_DEPENDENCY_MISSING", [b["code"] for b in detail["blockers"]])
        self.assertEqual(completion(facts, result)["state"], "INCOMPLETE")
        compact = compact_derived([result], analysis_ref="analysis-test")[0]
        Draft202012Validator(AnalysisInspect.input_schema).validate(compact["nextRead"])
        self.assertEqual(compact["nextRead"]["factRef"], "output")

    def test_cycle_ambiguity_and_unsupported_formula_keep_the_actual_cause(self):
        variants = [("A1", "FORMULA_CYCLE"),
                    ('[{"f":"D1"},{"f":"E1"}]', "FORMULA_AMBIGUOUS"),
                    ("ABS(D1)", "FORMULA_UNSUPPORTED")]
        for formula, code in variants:
            with self.subTest(code=code):
                facts = readings()
                facts[1]["formula"] = formula
                result = run_check(facts)
                self.assertEqual(result["executionStatus"], "BLOCKED")
                self.assertIn(code, [b["code"] for b in result["diagnostics"][0]["details"]["blockers"]])
                self.assertEqual(result["formulaTrace"]["targetLocalCheck"]["status"], "MATCH")

    def test_dependency_recovery_is_bounded_and_not_cross_observation(self):
        facts = readings()
        facts[0]["formula"] = "+".join(f"Z{i}" for i in range(1, 26))
        trace = trace_formula(facts, "output")
        self.assertEqual(trace["recovery"]["pendingDependencyCount"], 25)
        self.assertEqual(len(trace["recovery"]["pendingDependencies"]), 10)
        self.assertEqual(len(trace["pendingDependencies"]), 25)
        facts = readings()
        facts[-1]["observationId"] = "other-observation"
        self.assertEqual(run_check(facts)["executionStatus"], "BLOCKED")

    def test_empty_work_plan_is_undeclared_and_does_not_add_business_checks(self):
        state = completion_summary({"facts": readings(), "derivedValues": [], "relations": [], "plan": {"workPlan": []}})
        self.assertEqual(state["state"], "UNDECLARED")
        self.assertEqual(state["items"], [])
        self.assertEqual(calculate(readings(), {"scopeId": "scope"}, [], RuleRegistry()), [])

    def test_free_html_formula_bindings_keep_the_selected_node_and_calculation_basis(self):
        facts = readings()
        value = run_check(facts)
        artifact = {"facts": facts, "derivedValues": [value], "plan": {}, "relations": []}
        doc = compile_document(artifact, {
            "title": "信号对照", "html": "<html><head></head><body>{{local}} / {{expanded}}</body></html>",
            "bindings": [
                {"key": "local", "valueRef": value["derivedId"], "nodeRef": "combined", "field": "localValue"},
                {"key": "expanded", "valueRef": value["derivedId"], "field": "value"},
            ],
        })
        payload = json.loads(re.search(r'<script id="analysis-host-data" type="application/json">(.*?)</script>', doc["html"], re.S)[1])["values"]
        self.assertEqual(payload["local"]["measure"], "合成信号")
        self.assertEqual(payload["local"]["context"]["period"], "shift-omega")
        self.assertEqual(payload["local"]["basis"]["targetFactRef"], "combined")
        self.assertEqual(payload["local"]["basis"]["field"], "localValue")
        self.assertEqual(payload["expanded"]["measure"], "校准结果")
        self.assertEqual(payload["expanded"]["basis"]["targetFactRef"], "output")
        self.assertEqual(payload["expanded"]["basis"]["field"], "value")


class GenericFreeHtmlTests(PresentationCase):
    async def test_json_records_keep_arbitrary_periods_and_free_layout_through_export(self):
        options = {"rowsPointer": "/records", "idField": "id", "labelField": "label",
                   "values": {"LATENCY": "reading"}, "periodField": "window", "completePointer": "/complete"}
        adapter = default_sources().create("json-records", options)
        body = {"records": [
            {"id": "n-west", "label": "西侧探头", "reading": "-19.125", "window": "shift-omega"},
            {"id": "n-east", "label": "东侧探头", "reading": "23.7", "window": "shift-omega"},
        ], "complete": True}
        cap = self.store.capture(OWNER, {
            "resourceId": "lab-mcp", "originalToolName": "read_samples", "toolCallId": "sample-call",
            "captureState": "success", "request": {}, "sourceAdapter": adapter.snapshot(),
            "response": {"state": "success", "content": [{"type": "text", "text": json.dumps(body)}]},
        })
        catalog = adapter.catalog(self.store.get_capture(OWNER, cap))
        built = await self.service.build(
            [cap], [{"captureRef": cap, "stepId": "rows", "cellKeys": [f["cellKey"] for f in catalog]}],
            {"label": "实验读数", "members": ["n-west", "n-east"], "memberDimension": "recordId",
             "selectionReason": "本次采样", "population": {"captureRef": cap, "stepId": "rows", "memberPointer": "/id"}},
            [], "lab-build",
        )
        artifact = self.store.get_analysis(OWNER, built["analysisRef"])
        self.assertEqual(built["completion"]["state"], "UNDECLARED")
        self.assertEqual(artifact["derivedValues"], [])
        self.assertTrue(all(f["period"] == "shift-omega" and f["unit"] is None for f in artifact["facts"]))
        html = '<html><head><style>main{display:grid;gap:17px;color:teal}</style></head><body><main><h1>探头对照</h1><aside>{{west}}</aside><section>{{east}}</section></main></body></html>'
        preview = await self.service.publish(
            built["analysisRef"], requestKey="lab-preview",  previewOnly=True,
            document={"title": "探头对照", "html": html, "bindings": [
                {"key": "west", "valueRef": artifact["facts"][0]["factId"]},
                {"key": "east", "valueRef": artifact["facts"][1]["factId"]}]},
            renderCheckUnavailableReason="本项为导出一致性单元测试；实际浏览器验证单独执行。",
        )
        published = await self.service.publish(**preview["confirmPublish"])
        exported = await self.service.render_report(built["analysisRef"], "lab-report")
        rendered = self.delivery.report(built["analysisRef"], exported["exportRef"])["html"]
        self.assertIn("main{display:grid;gap:17px;color:teal}", rendered)
        self.assertIn("<aside>-19.125</aside><section>23.7</section>", unescape(rendered))
        card = self.delivery.view(published["delivery"]["path"])["freeDocument"]["html"]
        self.assertIn(card, unescape(rendered))
        self.assertNotIn("未注明", rendered)
        self.assertEqual(published["summary"]["completion"]["state"], "UNDECLARED")
        protocol = await self.service.inspect("protocol")
        self.assertEqual(protocol["documentPresentation"]["layout"], "model_authored")
        self.assertIsNone(protocol["formulaInspection"]["implicitBusinessPolicy"])
