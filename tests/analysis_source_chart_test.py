"""Explicit source plots remain useful when business ranking cannot be proved."""
from analysis_scene_assertions import comparison, diagrams
from copy import deepcopy
from decimal import Decimal

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError, canonical
from analysis_extension.presentation import project
from analysis_extension.validation import validate_views


class SourceChartTests(PresentationCase):
    async def source_plot(self, values=("71666300", "-8300", "0", "-1033.14")):
        capture, selectors, scope = saved_fixture(self.store, values, unit=None)
        built = await self.service.build([capture], selectors, scope, [{"id": "order", "operator": "SORT"}], "source-build")
        self.ref = built["analysisRef"]
        draft = self.store.get_analysis(OWNER, self.ref)
        self.plan = [{"viewId": "values", "scopeId": draft["scope"]["scopeId"], "type": "BAR_VALUES",
                      "valueRefs": [f["factId"] for f in draft["facts"] if f["valueState"] == "PRESENT"]}]
        self.plan[0]['description'] = '来源数值比较'
        self.plan.append({'viewId':'details','scopeId':draft['scope']['scopeId'],'type':'TABLE','valueRefs':[f['factId'] for f in draft['facts']]})
        result = await self.service.publish(self.ref, [], self.plan, "source-publish", 0)
        self.assertTrue(result["delivery"]["registered"])
        self.path = result["delivery"]["path"]
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        return self.delivery.view(self.path)

    async def test_unknown_unit_source_values_reach_real_html_without_rank_or_conversion(self):
        view = await self.source_plot()
        self.assertEqual(view["statuses"]["comparability"], "UNCONFIRMED")
        self.assertEqual(comparison(view)["type"], "BAR_VALUES")
        self.assertTrue(view["chartsRequested"])
        self.assertNotIn("未注明", comparison(view)["description"])
        self.assertIn("来源数值", view["sections"][0]["blocks"][0]["description"])
        self.assertEqual(view["limitations"], [])
        self.assertEqual([v["operator"] for v in self.artifact["derivedValues"]], ["SORT"])
        export = self.delivery.render_report(self.ref, "source-report")
        html = self.delivery.report(self.ref, export["exportRef"])["html"]
        self.assertEqual(html.count('<svg '), 1)
        self.assertIn('content="business-reference-1"', html)
        self.assertIn("来源原值分布", html)
        for row in view["rows"]:
            self.assertIn(row["display"], html)
            self.assertEqual(row["unit"], "")
        self.assertNotIn("未注明", html)
        self.assertTrue(all(fact["unit"] is None and fact["scale"] is None for fact in self.artifact["facts"]))
        self.assertNotIn('<svg ', self.delivery.report(self.ref, self.delivery.render_report(self.ref, "table-only", layout="table")["exportRef"])["html"])

    async def test_zero_negative_and_tiny_values_keep_exact_direction_and_scale(self):
        view = await self.source_plot()
        scene = comparison(view)
        rects = {p["rowKey"]: p for p in scene["primitives"] if p["kind"] == "rect"}
        positive = next(r for r in view["rows"] if Decimal(r["exact"]) > 0)
        for row in view["rows"]:
            number = Decimal(row["exact"])
            bar = rects[row["rowKey"]]
            if number < 0:
                self.assertLess(bar["x"], scene["zeroBaseline"])
                self.assertEqual(bar["tone"], "negative")
                self.assertAlmostEqual(bar["width"] / rects[positive["rowKey"]]["width"], float(-number / Decimal(positive["exact"])), places=8)
            if number == 0:
                self.assertEqual(bar["width"], 0)
        markers = [p for p in scene["primitives"] if p["kind"] == "line" and p["tone"] == "negative"]
        self.assertEqual(len(markers), 2)
        self.assertTrue(all(p["x1"] < scene["zeroBaseline"] for p in markers))
        self.assertIn("不代表柱形长度", scene["description"])

    async def test_source_plot_never_appears_without_explicit_plan_and_rank_stays_blocked(self):
        await self.source_plot()
        artifact = deepcopy(self.artifact)
        artifact["viewPlan"] = []
        view = project(artifact, self.rules)
        self.assertFalse(view["chartsRequested"])
        self.assertIsNone(comparison(view))
        rank_plan = [{**self.plan[0], "type": "BAR_RANK"}]
        with self.assertRaises(AnalysisError) as failure:
            validate_views(artifact, rank_plan)
        self.assertEqual(failure.exception.code, "RANK_UNCONFIRMED")

    async def test_refuse_mixed_metrics_contexts_units_missing_values_and_duplicates(self):
        await self.source_plot()
        for changes in (
            {"cellKey": "9:9"}, {"measureCode": "OTHER"}, {"period": "other-period"},
            {"bindingRef": "other-binding"}, {"sourceOrigins": ["a" * 64]},
            {"unit": "元", "scale": "1"}, {"scale": "10000"},
            {"dimensions": {"scenario": "different"}}, {"precision": "display_only"},
            {"valueState": "NO_VALUE", "value": None}, {"valueState": "CONFLICT"},
            {"objectId": self.artifact["facts"][1]["objectId"]},
        ):
            artifact = deepcopy(self.artifact)
            artifact["facts"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(AnalysisError):
                validate_views(artifact, self.plan)
        duplicate = [{**self.plan[0], "valueRefs": self.plan[0]["valueRefs"] * 2}]
        with self.assertRaises(AnalysisError):
            validate_views(self.artifact, duplicate)
        # Independent comparison views are supported, but each keeps its own metric contract.
        self.assertEqual(len(validate_views(self.artifact, self.plan + [{**self.plan[0], "viewId": "second"}])),len(self.plan)+1)

    async def test_missing_values_are_left_in_details_and_empty_chart_is_explained(self):
        view = await self.source_plot(("20", "0", None))
        self.assertEqual(len(view["rows"]), 3)
        self.assertEqual(len(comparison(view)["rowKeys"]), 2)
        self.assertEqual(view["rows"][-1]["display"], "—")
        filtered = self.delivery.project_view(self.ref, {"filter": "negative"})
        self.assertIsNone(comparison(filtered))
        self.assertTrue(filtered["chartUnavailable"])
        export = self.delivery.render_report(self.ref, "empty-report", {"filter": "negative"})
        html = self.delivery.report(self.ref, export["exportRef"])["html"]
        self.assertIn(filtered["chartUnavailable"], html)
        self.assertNotIn('<svg ', html)

    async def test_chart_only_uses_selected_metric_and_tracks_view_scope(self):
        await self.source_plot()
        artifact = deepcopy(self.artifact)
        extra = {**artifact["facts"][0], "factId": "fact_extra", "cellKey": "9:9", "headers": ["另一指标"], "value": "999"}
        artifact["facts"].append(extra)
        view = project(artifact, self.rules)
        self.assertEqual(view["total"], 4)
        self.assertEqual(len(comparison(view)["rowKeys"]), 4)
        self.assertNotIn("另一指标", canonical(comparison(view)).decode())
        filtered = project(artifact, self.rules, {"filter": "negative"})
        self.assertEqual(len(comparison(filtered)["rowKeys"]), 2)
        self.assertEqual(filtered["sections"][0]["blocks"][0]["description"], "来源数值比较")
        self.assertTrue(filtered["isFiltered"])
        for direction, expected in (("asc", ["-8300", "-1033.14", "0", "71666300"]),
                                    ("desc", ["71666300", "0", "-1033.14", "-8300"])):
            ordered = project(artifact, self.rules, {"sort": direction})
            chart_rows = [r for r in ordered["rows"] if r["rowKey"] in comparison(ordered)["rowKeys"]]
            self.assertEqual([r["exact"] for r in chart_rows], expected)
            self.assertFalse(any(r["measure"] == "另一指标" for r in ordered["rows"]))
            self.assertEqual(ordered["total"], 4)

    async def test_plot_truncation_is_explicit_and_protocol_exposes_capability(self):
        view = await self.source_plot(tuple(str(i) for i in range(16)))
        self.assertEqual(view["total"], 16)
        self.assertEqual(len(comparison(view)["rowKeys"]), 12)
        self.assertIn("12 / 16", comparison(view)["description"])
        protocol = await self.service.inspect("protocol")
        self.assertIn("BAR_VALUES", protocol["viewTypes"])
