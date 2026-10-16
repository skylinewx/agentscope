from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
from decimal import Decimal
import math

from analysis_presentation_fixtures import PresentationCase
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.models import AnalysisError, canonical
from analysis_extension.presentation import page, project


class PresentationTests(PresentationCase):
    async def test_independent_nine_value_oracle_and_stable_order(self):
        await self.publish(("71666300", "15000000", "4000000", "500000", "200000", "100000", "50000", "33000", "-8300"))
        v = self.delivery.view(self.path)
        self.assertEqual(v["metrics"][-1]["display"], "91,541,000")
        self.assertEqual([r["exact"] for r in v["rows"]], ["71666300", "15000000", "4000000", "500000", "200000", "100000", "50000", "33000", "-8300"])
        rects = [p for p in comparison(v)["primitives"] if p["kind"] == "rect"]
        self.assertAlmostEqual(rects[-1]["width"] / rects[0]["width"], 8300 / 71666300, places=8)
        self.assertTrue(any("-8,300" in p.get("text", "") for p in comparison(v)["primitives"]))

    async def test_eleven_unknown_unit_counts_and_restricted_chart(self):
        await self.publish(("1103600", "71666300", "3500000", "143700", "967700", "651900", "241400", "13274700", "-8300", "12115300", "-1033.14"), unit=None)
        v = self.delivery.view(self.path)
        self.assertEqual([m["display"] for m in v["metrics"]], ["9", "2", "0", "0"])
        self.assertEqual([r["exact"] for r in v["rows"][-2:]], ["-1033.14", "-8300"])
        self.assertIsNone(comparison(v))
        self.assertNotIn("103,655,266.86", canonical(v).decode())

    async def test_filtered_statistics_and_page_have_same_scope(self):
        await self.publish(tuple(str(i - 10) for i in range(30)))
        first = self.delivery.project_view(self.ref, {"filter": "negative"}, 0, 4)
        next_page = self.delivery.project_view(self.ref, {"filter": "negative"}, 4, 4)
        self.assertNotEqual(first["scopeId"], self.artifact["scope"]["scopeId"])
        self.assertEqual(first["scopeId"], next_page["scopeId"])
        self.assertEqual(first["metrics"], next_page["metrics"])
        self.assertEqual([m["display"] for m in first["metrics"]], ["0", "10", "0", "0", "-55"])
        self.assertEqual(first["claimsScopeLabel"], "结论适用于原发布范围")
        self.assertTrue(all(m["scopeId"] == first["scopeId"] for m in first["metrics"]))

    async def test_topn_not_population_and_sort_preserves_row_identity(self):
        await self.publish()
        default = self.delivery.view(self.path)
        top = self.delivery.project_view(self.ref, {"topN": 1})
        asc = self.delivery.project_view(self.ref, {"sort": "asc"})
        self.assertEqual(top["total"], 1)
        self.assertIn("前 1", top["scopeLabel"])
        self.assertEqual(default["rows"][0]["rowKey"], asc["rows"][-1]["rowKey"])
        self.assertEqual(default["scopeId"], asc["scopeId"])

    async def test_missing_and_zero_not_conflated_and_empty_filter(self):
        await self.publish(("0", None))
        v = self.delivery.view(self.path)
        self.assertEqual([r["display"] for r in v["rows"]], ["0", "—"])
        empty = self.delivery.project_view(self.ref, {"filter": "negative"})
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(comparison(empty))

    async def test_bridge_uses_frozen_coefficients_and_exact_total(self):
        await self.publish(("100", "1133.14"))
        a = deepcopy(self.artifact)
        registry = RuleRegistry({"bridge": {"version": "1", "source": "synthetic", "operators": ["LINEAR"],
                   "terms": [{"match": {"objectId": "object-0"}, "coefficient": "1"}, {"match": {"objectId": "object-1"}, "coefficient": "-1"}]}})
        a["derivedValues"] = calculate(a["facts"], a["scope"], [{"id": "b", "operator": "LINEAR", "ruleRef": "bridge"}], registry)
        a["viewPlan"] = [{"viewId": "bridge", "scopeId": a["scope"]["scopeId"], "type": kind, "valueRefs": [a["derivedValues"][0]["derivedId"]]} for kind in ("METRIC", "BRIDGE")]
        v = project(a, registry)
        self.assertEqual(v["metrics"][0]["display"], "-1,033.14")
        self.assertEqual(diagrams(v)[0]["type"], "BRIDGE")
        self.assertEqual(diagrams(v)[0]["description"], "")
        self.assertTrue(any(p.get("text", "").startswith("-1,133.14") for p in diagrams(v)[0]["primitives"]))

    async def test_long_labels_extreme_geometry_and_public_allowlist(self):
        await self.publish(("1e100", "-0.00000000000001"))
        a = deepcopy(self.artifact)
        a["facts"][0]["objectLabel"] = "非常长的机构名称" * 20
        v = project(a, self.rules)
        self.assertEqual(v["rows"][0]["label"], "非常长的机构名称" * 20)
        value_parts = [p["text"] for p in comparison(v)["primitives"] if p["kind"] == "text" and p["tone"] == "value"]
        self.assertIn(v["rows"][0]["display"] + " 元", "".join(value_parts))
        self.assertTrue(all(len(s) <= 40 for s in value_parts))
        for p in comparison(v)["primitives"]:
            for key, value in p.items():
                if isinstance(value, (int, float)):
                    self.assertTrue(math.isfinite(value) and 0 <= value <= 10000)
        text = canonical(v).decode()
        for private in ("evidenceId", "executionId", "bindingRef", "captureRef", "snapshotId", "ownerUserId"):
            self.assertNotIn(private, text)

    async def test_invalid_views_and_unknown_engine_fail_closed(self):
        await self.publish()
        for params in ({"amount": "100"}, {"topN": True}, {"topN": 0}, {"filter": "profit"}):
            with self.assertRaises(AnalysisError):
                self.delivery.project_view(self.ref, params)
        a = deepcopy(self.artifact); a["engineVersion"] = "future"
        with self.assertRaisesRegex(AnalysisError, "当前分析协议"):
            project(a, self.rules)
        with self.assertRaises(AnalysisError):
            page(project(self.artifact, self.rules), 0, 101)

    async def test_large_result_paginates_without_reducing_totals(self):
        await self.publish(tuple(str(i) for i in range(125)))
        first = self.delivery.view(self.path)
        final = self.delivery.project_view(self.ref, {}, 120, 12)
        self.assertEqual(len(first["rows"]), 12)
        self.assertEqual(len(comparison(first)["rowKeys"]), 12)
        self.assertEqual(first["total"], 125)
        self.assertEqual(len(final["rows"]), 5)
        self.assertIsNone(final["page"]["nextOffset"])
        self.assertEqual(first["metrics"], final["metrics"])
        self.assertEqual(first["metrics"][-1]["display"], "7,750")
