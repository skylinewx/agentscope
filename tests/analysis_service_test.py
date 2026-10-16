"""Immutable publication, permissions, recovery and semantic counterexamples."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import time
import unittest

from analysis_fixtures import OWNER, fixture, saved_fixture, rules
from analysis_extension.engine import calculate, RuleRegistry
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.service import AnalysisService
from analysis_extension.store import AnalysisStore
from analysis_extension.adapters.budget_v2 import adapt


async def authorized():
    pass


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AnalysisStore(self.temp.name)
        self.service = AnalysisService(self.store, OWNER, authorized, rules())
        self.ref, self.selectors, self.scope = saved_fixture(self.store)

    def tearDown(self):
        self.temp.cleanup()

    async def build(self, key="build", base=None, ops=None):
        return await self.service.build([self.ref], self.selectors, self.scope,
            ops if ops is not None else [{"id": "count", "operator": "COUNT"}, {"id": "order", "operator": "SORT"}], key, base)

    async def publish(self, draft, key="publish", claims=None, views=None):
        return await self.service.publish(draft["analysisRef"], claims or [], views or [], key, draft["baseRevision"])

    async def test_idempotent_build_publish_restart(self):
        draft = await self.build()
        self.assertEqual(draft["analysisRef"], (await self.build())["analysisRef"])
        result = await self.publish(draft)
        self.assertEqual(result, await self.publish(draft))
        reopened = AnalysisStore(self.temp.name).get_analysis(OWNER, result["analysisRef"])
        self.assertEqual(reopened["publicationState"], "PUBLISHED")
        self.assertNotIn('markdown', result)
        self.assertEqual(result['summary']['factCount'], 3)
        self.assertEqual(result['summary']['derivedRefs'], ['derived_count', 'derived_order'])
        with self.assertRaisesRegex(AnalysisError, "不可覆盖"):
            await self.publish(draft, "different-key")

    async def test_request_key_conflict(self):
        await self.build()
        with self.assertRaisesRegex(AnalysisError, "请求标识"):
            await self.build(ops=[])

    async def test_revision_compare_and_swap(self):
        published = await self.publish(await self.build())
        drafts = [await self.build("v" + str(i), published["analysisRef"]) for i in range(2)]
        await self.publish(drafts[0], "publish-v2")
        with self.assertRaisesRegex(AnalysisError, "基础版本"):
            await self.publish(drafts[1], "publish-v3")

    async def test_concurrent_publish_only_one_wins(self):
        published = await self.publish(await self.build())
        drafts = [await self.build("parallel" + str(i), published["analysisRef"]) for i in range(2)]
        def attempt(i):
            try:
                return asyncio.run(self.publish(drafts[i], "concurrent" + str(i)))["publicationState"]
            except AnalysisError as error:
                return error.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ["PUBLISHED", "VERSION_CONFLICT"])

    async def test_cross_owner_and_session_denied(self):
        for owner in (Owner("other", OWNER.agent_id, OWNER.session_id), Owner(OWNER.user_id, OWNER.agent_id, "other")):
            with self.assertRaisesRegex(AnalysisError, "当前会话"):
                self.store.get_capture(owner, self.ref)
        async def denied():
            raise AnalysisError("ACCESS_DENIED", "revoked")
        self.service.authorize = denied
        with self.assertRaisesRegex(AnalysisError, "revoked"):
            await self.service.inspect()

    async def test_tamper_detected_before_publish(self):
        draft = await self.build()
        with self.store._transaction() as db:
            row = db.execute("SELECT digest FROM captures WHERE ref=?", (self.ref,)).fetchone()
        self.store._blob_path(row[0]).write_text("{}", "utf-8")
        with self.assertRaisesRegex(AnalysisError, "摘要不一致"):
            await self.publish(draft)

    async def test_gc_keeps_published_sources_and_removes_orphans(self):
        result = await self.publish(await self.build())
        unused = fixture()[0]
        unused["toolCallId"] = "unused"
        ref = self.store.capture(OWNER, unused)
        orphan = self.store.blobs / ("a" * 64 + ".json")
        orphan.write_text("{}", "utf-8")
        future = time.time() + self.store.limits.retention_seconds + 10
        self.store.collect_expired(future)
        self.assertTrue(self.store.get_capture(OWNER, self.ref))
        self.assertTrue(self.store.get_analysis(OWNER, result["analysisRef"]))
        with self.assertRaises(AnalysisError):
            self.store.get_capture(OWNER, ref)
        self.assertFalse(orphan.exists())

    async def test_numeric_claims_and_views(self):
        draft = await self.build()
        claim = {"claimId": "positive", "scopeId": draft["scope"]["scopeId"], "kind": "computed",
                 "parts": [{"valueRef": "derived_count", "field": "positive"}]}
        result = await self.publish(draft, claims=[claim])
        self.assertEqual(result['summary']['claims'], ['1'])
        new = await self.build("bad-claim")
        claim["parts"] = [{"text": "正值对象数：99"}]
        with self.assertRaisesRegex(AnalysisError, "实际值引用"):
            await self.publish(new, "bad-publish", [claim])

    async def test_unitless_business_rank_view_blocked(self):
        draft = await self.build()
        view = {"viewId": "v", "scopeId": draft["scope"]["scopeId"], "type": "BAR_RANK", "valueRefs": ["derived_order"]}
        with self.assertRaisesRegex(AnalysisError, "可比性"):
            await self.publish(draft, views=[view])

    async def test_invalid_verification_blocks_claim(self):
        cap = self.store.get_capture(OWNER, self.ref)
        import json
        receipt = json.loads(cap["response"]["content"][0]["text"])
        receipt["data"] = {"executionId": "execution-A", "action": "verify", "verification": {"status": "INVALID", "evidenceIds": []}}
        cap.update(originalToolName="validate_evidence", request={"action": "verify", "executionId": "execution-A"})
        cap["response"]["content"][0]["text"] = json.dumps(receipt)
        verify_ref = self.store.capture(OWNER, cap)
        result = await self.service.build([self.ref, verify_ref], self.selectors, self.scope, [{"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"}], "invalid")
        self.assertEqual(result["receiptVerification"], "INVALID")
        self.assertEqual(result["calculationValidation"], "BLOCKED")

    async def test_overlapping_derived_sum_and_count_unit_rejected(self):
        data = adapt({self.ref: self.store.get_capture(OWNER, self.ref)}, self.selectors, self.scope, 100)
        first = data["facts"][0]["factId"]
        result = calculate(data["facts"], data["scope"], [
            {"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"},
            {"id": "dup", "operator": "SUM", "ruleRef": "fixture-additive", "inputRefs": ["derived_sum", first]},
            {"id": "count", "operator": "COUNT"},
            {"id": "ratio", "operator": "RATIO", "inputRefs": ["derived_count", "derived_sum"]}], rules())
        self.assertEqual(result[1]["diagnostics"][0]["code"], "OVERLAPPING_INPUTS")
        self.assertEqual(result[3]["diagnostics"][0]["code"], "OPERAND_UNIT_CONFLICT")

    async def test_precise_difference_and_conversion(self):
        ref, selections, scope = saved_fixture(self.store, ("123456789012345678901234567890.01", "123456789012345678901234567890"))
        data = adapt({ref: self.store.get_capture(OWNER, ref)}, selections, scope, 100)
        registry = RuleRegistry({"convert": {"source": "synthetic", "version": "1", "operators": ["CONVERT"], "fromUnit": "元", "toUnit": "万元", "factor": "0.0001"}})
        result = calculate(data["facts"], data["scope"], [{"id": "diff", "operator": "DIFFERENCE"}, {"id": "conv", "operator": "CONVERT", "ruleRef": "convert", "inputRefs": [data["facts"][0]["factId"]]}], registry)
        self.assertEqual(result[0]["value"], "0.01")
        self.assertEqual(result[1]["value"], "12345678901234567890123456.789001")

    async def test_historical_eleven_values_regression(self):
        # Original session's 2026 BUDGET net-profit row, excluding parent GT00.
        values = ("1103600", "71666300", "3500000", "143700", "967700", "651900", "241400", "13274700", "-8300", "12115300", "-1033.14")
        ref, selections, scope = saved_fixture(self.store, values, unit=None)
        data = adapt({ref: self.store.get_capture(OWNER, ref)}, selections, scope, 100)
        result = calculate(data["facts"], data["scope"], [{"id": "signs", "operator": "COUNT"}, {"id": "order", "operator": "SORT"}, {"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"}], rules())
        self.assertEqual((result[0]["counts"]["positive"], result[0]["counts"]["negative"]), (9, 2))
        from decimal import Decimal
        self.assertEqual(sum(map(Decimal, values)), Decimal("103655266.86"))
        self.assertEqual(result[2]["calculationValidation"], "BLOCKED")

    async def test_linear_complete_and_missing_terms(self):
        data = adapt({self.ref: self.store.get_capture(OWNER, self.ref)}, self.selectors, self.scope, 100)
        registry = RuleRegistry({"bridge": {"version": "1", "source": "synthetic composition", "operators": ["LINEAR"],
            "terms": [{"match": {"objectId": "object-0"}, "coefficient": "1"}, {"match": {"objectId": "object-1"}, "coefficient": "-1"}]}})
        refs = [f["factId"] for f in data["facts"][:2]]
        results = calculate(data["facts"], data["scope"], [{"id": "full", "operator": "LINEAR", "ruleRef": "bridge", "inputRefs": refs}, {"id": "partial", "operator": "LINEAR", "ruleRef": "bridge", "inputRefs": refs[:1]}], registry)
        self.assertEqual(results[0]["value"], "1133.14")
        self.assertEqual(results[0]["meaning"], "arithmetic_composition_not_business_cause")
        self.assertEqual(results[1]["calculationValidation"], "BLOCKED")

    async def test_continuation_pages_without_selected_cells(self):
        import json
        cap, selectors, scope = fixture(values=("100",))
        receipt = json.loads(cap["response"]["content"][0]["text"])
        page = receipt["data"]["stepResults"][1]["formResults"][0]["page"]
        page.update(totalRows=6, endRowExclusive=3, observationExhausted=False, nextCursor="opaque-next")
        cap["response"]["content"][0]["text"] = json.dumps(receipt)
        first = self.store.capture(OWNER, cap)
        second = deepcopy(cap)
        page.update(startRow=3, endRowExclusive=6, observationExhausted=True, nextCursor=None, cells=[])
        second["response"]["content"][0]["text"] = json.dumps(receipt)
        last = self.store.capture(OWNER, second)
        selectors[0]["captureRef"] = first
        scope["population"]["captureRef"] = first
        data = adapt({r: self.store.get_capture(OWNER, r) for r in (first, last)}, selectors, scope, 100)
        self.assertEqual(data["scope"]["coverage"], "COMPLETE")
        page["startRow"] = 4
        second["response"]["content"][0]["text"] = json.dumps(receipt)
        gap = self.store.capture(OWNER, second)
        data = adapt({r: self.store.get_capture(OWNER, r) for r in (first, gap)}, selectors, scope, 100)
        self.assertEqual(data["scope"]["coverage"], "PARTIAL")

    async def test_protocol_rejects_number_instead_of_exact_string(self):
        from analysis_extension.protocol import validate_artifact
        draft = await self.build()
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        validate_artifact(artifact)
        artifact["facts"][0]["value"] = 100.0
        with self.assertRaisesRegex(AnalysisError, "协议版本"):
            validate_artifact(artifact)

    async def test_resume_requires_captured_original_and_server_link(self):
        import json
        cap, selectors, scope = fixture(values=("100",))
        receipt = json.loads(cap["response"]["content"][0]["text"])
        receipt.update(status="PARTIAL", resumeRef=receipt["executionId"])
        receipt["completeness"]["enumerationExhausted"] = False
        page = receipt["data"]["stepResults"][1]["formResults"][0]["page"]
        page.update(totalRows=6, endRowExclusive=3, observationExhausted=False, nextCursor="opaque-next")
        cap["response"]["content"][0]["text"] = json.dumps(receipt)
        first = self.store.capture(OWNER, cap)
        resumed = deepcopy(cap)
        resumed["request"] = {"contractVersion": "2.0", "contextId": receipt["context"]["contextId"], "querySessionId": "query-A", "resumeRef": receipt["executionId"]}
        receipt.update(executionId="execution-B", continuationOf="execution-A", status="SUCCEEDED", resumeRef=None)
        receipt["completeness"]["enumerationExhausted"] = True
        page.update(startRow=3, endRowExclusive=6, observationExhausted=True, nextCursor=None, cells=[])
        resumed["response"]["content"][0]["text"] = json.dumps(receipt)
        last = self.store.capture(OWNER, resumed)
        selectors[0]["captureRef"] = first
        scope.pop("population")
        data = adapt({r: self.store.get_capture(OWNER, r) for r in (first, last)}, selectors, scope, 100)
        self.assertTrue(data["scope"]["selectedDataComplete"])
        receipt["continuationOf"] = "foreign-execution"
        resumed["response"]["content"][0]["text"] = json.dumps(receipt)
        bad = self.store.capture(OWNER, resumed)
        with self.assertRaisesRegex(AnalysisError, "上游执行"):
            adapt({r: self.store.get_capture(OWNER, r) for r in (first, bad)}, selectors, scope, 100)

    async def test_failed_registration_leaves_no_visible_analysis(self):
        draft = await self.build()
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        artifact["captureRefs"] = ["cap_missing"]
        with self.assertRaises(AnalysisError):
            self.store.build(OWNER, {"invalid": True}, artifact, "failed", None)
        with self.store._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT ref FROM requests WHERE key='failed'").fetchone())
        self.store.collect_expired(time.time() + self.store.limits.retention_seconds + 10)
        self.assertTrue(self.store.get_analysis(OWNER, draft["analysisRef"]))

    async def test_targeted_request_cannot_return_another_measure(self):
        cap, selectors, scope = fixture(values=("100",), page=False)
        cap["request"]["plan"]["steps"][1]["formQuery"]["measureCode"] = "OTHER"
        ref = self.store.capture(OWNER, cap)
        selectors[0]["captureRef"] = ref
        scope["population"]["captureRef"] = ref
        with self.assertRaisesRegex(AnalysisError, "原请求的指标"):
            adapt({ref: self.store.get_capture(OWNER, ref)}, selectors, scope, 100)

    async def test_unknown_receipt_status_is_not_success(self):
        import json
        cap, selectors, scope = fixture(values=("100",))
        receipt = json.loads(cap["response"]["content"][0]["text"])
        receipt["status"] = "NEW_STATUS"
        cap["response"]["content"][0]["text"] = json.dumps(receipt)
        ref = self.store.capture(OWNER, cap)
        selectors[0]["captureRef"] = ref
        with self.assertRaisesRegex(AnalysisError, "未完成或失败"):
            adapt({ref: self.store.get_capture(OWNER, ref)}, selectors, scope, 100)

    async def test_claim_kind_and_unit_are_bound_to_values(self):
        draft = await self.build()
        artifact = self.store.get_analysis(OWNER, draft["analysisRef"])
        from analysis_extension.validation import validate_claims
        claim = {"claimId": "value", "scopeId": draft["scope"]["scopeId"], "kind": "fact", "parts": [{"valueRef": artifact["facts"][0]["factId"]}]}
        self.assertEqual(validate_claims(artifact, [claim])[0]["rendered"], "100")
        claim["kind"] = "computed"
        with self.assertRaisesRegex(AnalysisError, "实际派生计算"):
            validate_claims(artifact, [claim])
