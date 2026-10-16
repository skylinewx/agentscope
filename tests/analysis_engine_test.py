from copy import deepcopy
from decimal import Decimal
from unittest import TestCase

from analysis_fixtures import fixture, rules
from analysis_extension.adapters.budget_v2 import adapt, cell_value
from analysis_extension.engine import calculate
from analysis_extension.models import AnalysisError, precise_sum


class EngineTest(TestCase):
    def data(self, values=("100", "-1033.14", "-8300"), unit="元"):
        capture, selectors, scope = fixture(values, unit)
        return adapt({"replace": capture}, selectors, scope, 50000)

    def calc(self, operations, **kwargs):
        data = self.data(**kwargs)
        return calculate(data["facts"], data["scope"], operations, rules())

    def test_negative_descending_sort_and_stable_ties(self):
        data = self.data(("-8300", "-1033.14", "0", "-1033.14"))
        result = calculate(data["facts"], data["scope"], [{"id": "s", "operator": "SORT"}], rules())[0]
        facts = {f["factId"]: f for f in data["facts"]}
        self.assertEqual([facts[f]["objectId"] for f in result["orderedFactIds"]], ["object-2", "object-1", "object-3", "object-0"])

    def test_sum_requires_registered_semantic_rule(self):
        bad, good = self.calc([{"id": "bad", "operator": "SUM"}, {"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"}])
        self.assertEqual(bad["diagnostics"][0]["code"], "RULE_REQUIRED")
        self.assertEqual(good["value"], "-9233.14")

    def test_missing_unit_allows_source_order_but_blocks_business_totals(self):
        result = self.calc([{"id": "s", "operator": "SORT"}, {"id": "r", "operator": "RANK"}, {"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"}], unit=None)
        self.assertEqual(result[0]["calculationValidation"], "PASS")
        self.assertEqual([x["diagnostics"][0]["code"] for x in result[1:]], ["UNIT_UNCONFIRMED", "UNIT_UNCONFIRMED"])

    def test_missing_not_zero_and_counts_are_explicit(self):
        result = self.calc([{"id": "c", "operator": "COUNT"}], values=("5", "-2", "0", None))[0]
        self.assertEqual(result["counts"], {"positive": 1, "negative": 1, "zero": 1, "observed": 3, "unavailable": 1})

    def test_duplicate_member_not_double_counted(self):
        data = self.data()
        data["facts"][1]["objectId"] = data["facts"][0]["objectId"]
        result = calculate(data["facts"], data["scope"], [{"id": "c", "operator": "COUNT"}], rules())[0]
        self.assertEqual(result["diagnostics"][0]["code"], "DUPLICATE_MEMBER")

    def test_exact_sum_and_ratio_have_independent_oracle(self):
        # Synthetic distribution matching the comparison's nine-value total.
        values = ("71666300", "15000000", "4000000", "500000", "200000", "100000", "50000", "33000", "-8300")
        data = self.data(values)
        numerator = data["facts"][0]["factId"]
        result = calculate(data["facts"], data["scope"], [{"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"},
            {"id": "ratio", "operator": "RATIO", "inputRefs": [numerator, "derived_sum"]}], rules())
        self.assertEqual(result[0]["value"], "91541000")
        self.assertEqual(result[1]["value"], "0.78288745")
        self.assertEqual(result[1]["ratioMeaning"], "signed_net_ratio")

    def test_zero_denominator_and_unknown_operator_blocked(self):
        data = self.data(("1", "0"))
        refs = [f["factId"] for f in data["facts"]]
        result = calculate(data["facts"], data["scope"], [{"id": "ratio", "operator": "RATIO", "inputRefs": refs}, {"id": "bad", "operator": "PYTHON"}], rules())
        self.assertEqual(result[0]["diagnostics"][0]["code"], "ZERO_OR_MISSING_DENOMINATOR")
        self.assertEqual(result[1]["diagnostics"][0]["code"], "UNSUPPORTED_OPERATOR")

    def test_high_precision_sum_no_default_context_rounding(self):
        self.assertEqual(str(precise_sum([Decimal("123456789012345678.123456789"), Decimal("0.000000001")])), "123456789012345678.123456790")

    def test_number_format_must_be_registered(self):
        cell = {"rawValueJson": '"1,370.77"', "displayValue": "1,370.77"}
        self.assertNotEqual(cell_value(cell)[1], "PRESENT")
        self.assertEqual(cell_value(cell, "grouped-dot")[:2], ("1370.77", "PRESENT"))
        self.assertNotEqual(cell_value({"rawValueJson": '"1,37.77"'}, "grouped-dot")[1], "PRESENT")

    def test_metric_and_scene_conflict(self):
        data = self.data()
        data["facts"][1]["dimensions"]["SCENE"] = "actual"
        result = calculate(data["facts"], data["scope"], [{"id": "r", "operator": "RANK"}], rules())[0]
        self.assertEqual(result["diagnostics"][0]["code"], "METRIC_CONFLICT")

    def test_incomplete_population_cannot_make_whole_claim(self):
        data = self.data()
        data["scope"]["coverage"] = "PARTIAL"
        result = calculate(data["facts"], data["scope"], [{"id": "c", "operator": "COUNT", "wholePopulation": True}], rules())[0]
        self.assertEqual(result["diagnostics"][0]["code"], "POPULATION_INCOMPLETE")
