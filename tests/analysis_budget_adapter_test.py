from copy import deepcopy
import json
from unittest import TestCase

from analysis_fixtures import fixture
from analysis_extension.adapters.budget_v2 import adapt
from analysis_extension.models import AnalysisError


class BudgetAdapterTest(TestCase):
    def test_grid_page_cells_when_top_level_empty(self):
        capture, selections, scope = fixture()
        result = adapt({"replace": capture}, selections, scope, 50000)
        self.assertEqual([f["value"] for f in result["facts"]], ["100", "-1033.14", "-8300"])
        self.assertEqual(result["scope"]["coverage"], "COMPLETE")

    def test_targeted_cells(self):
        capture, selections, scope = fixture(page=False)
        result = adapt({"replace": capture}, selections, scope, 50000)
        self.assertEqual(len(result["facts"]), 3)

    def test_consistent_responses_still_checked_against_original_request(self):
        capture, selections, scope = fixture()
        capture["request"]["plan"]["steps"][1]["formQuery"]["periodCode"] = "2025N0001"
        with self.assertRaisesRegex(AnalysisError, "时期"):
            adapt({"replace": capture}, selections, scope, 50000)

    def test_wrong_binding_rejected(self):
        capture, selections, scope = fixture()
        capture["request"]["plan"]["steps"][0]["resourceIris"] = ["different-binding"]
        with self.assertRaises(AnalysisError):
            adapt({"replace": capture}, selections, scope, 50000)

    def test_replayed_page_deduplicated(self):
        capture, selections, scope = fixture()
        clone = deepcopy(capture)
        result = adapt({"replace": capture, "replay": clone}, selections + [{**s, "captureRef": "replay"} for s in selections], scope, 50000)
        self.assertEqual(len(result["facts"]), 3)
        self.assertEqual(set(result["facts"][0]["captureRefs"]), {"replace", "replay"})

    def test_conflicting_value_for_same_observation_not_silently_overwritten(self):
        capture, selections, scope = fixture()
        clone = deepcopy(capture)
        receipt = json.loads(clone["response"]["content"][0]["text"])
        receipt["data"]["stepResults"][1]["formResults"][0]["page"]["cells"][0]["rawValueJson"] = '"999"'
        clone["response"]["content"][0]["text"] = json.dumps(receipt)
        result = adapt({"replace": capture, "replay": clone}, selections + [{**s, "captureRef": "replay"} for s in selections], scope, 50000)
        self.assertEqual(result["facts"][0]["valueState"], "CONFLICT")

    def test_missing_member_retained_as_not_queried(self):
        capture, selections, scope = fixture()
        result = adapt({"replace": capture}, selections[:2], scope, 50000)
        self.assertEqual(result["scope"]["coverage"], "PARTIAL")
        self.assertEqual(result["facts"][-1]["valueState"], "NOT_QUERIED")

    def test_cross_snapshot_rejected(self):
        capture, selections, scope = fixture()
        clone = deepcopy(capture)
        receipt = json.loads(clone["response"]["content"][0]["text"])
        receipt["context"]["snapshotId"] = "other-snapshot"
        clone["response"]["content"][0]["text"] = json.dumps(receipt)
        with self.assertRaises(AnalysisError):
            adapt({"replace": capture, "other": clone}, selections, scope, 50000)

    def test_unfinished_page_never_complete(self):
        capture, selections, scope = fixture()
        receipt = json.loads(capture["response"]["content"][0]["text"])
        page = receipt["data"]["stepResults"][1]["formResults"][0]["page"]
        page["observationExhausted"], page["nextCursor"] = False, "opaque-next"
        capture["response"]["content"][0]["text"] = json.dumps(receipt)
        result = adapt({"replace": capture}, selections, scope, 50000)
        self.assertEqual(result["scope"]["coverage"], "PARTIAL")

    def test_wrong_cell_period_does_not_borrow_form_period(self):
        capture, selections, scope = fixture()
        receipt = json.loads(capture["response"]["content"][0]["text"])
        receipt["data"]["stepResults"][1]["formResults"][0]["page"]["cells"][0]["dimensions"]["DATATIME"] = "2024N0001"
        capture["response"]["content"][0]["text"] = json.dumps(receipt)
        with self.assertRaisesRegex(AnalysisError, "单元格"):
            adapt({"replace": capture}, selections, scope, 50000)
