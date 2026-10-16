"""Budget execution evidence verification, scoped to captured origins."""
from .budget_v2 import receipt_from_capture, CONTEXT_FIELDS
from ..actions import source_identity
from ..models import AnalysisError, digest


def evidence_scope(capture):
    receipt = receipt_from_capture(capture)
    return (source_identity(capture),
            digest({key: receipt.get('context', {}).get(key) for key in CONTEXT_FIELDS}),
            receipt.get('querySessionId'))

def verification(facts, captures, verifier_refs=None):
    valid, invalid = set(), set()
    # IDs may be repeated by a different connection, snapshot or query session.
    origins = {f["factId"]: {evidence_scope(captures[ref]) for ref in f.get("captureRefs", []) if ref in captures} for f in facts}
    selected_executions = {(source, f.get("executionId")) for f in facts for source in origins[f["factId"]]}
    checked = []
    for ref, capture in captures.items():
        if verifier_refs is not None and ref not in verifier_refs:
            continue
        if capture.get("originalToolName") != "validate_evidence" or capture["request"].get("action") != "verify":
            continue
        receipt = receipt_from_capture(capture)
        data = receipt.get("data", {})
        target = capture["request"].get("executionId")
        if target != data.get("executionId") or data.get("action") != "verify":
            raise AnalysisError("VERIFICATION_MISMATCH", "核验回执与核验请求不一致。")
        result = data.get("verification", {})
        state = result.get("status")
        source = evidence_scope(capture)
        checked.append({"captureRef": capture["captureRef"], "executionId": target,
                        "context": receipt.get('context'), "querySessionId": receipt.get('querySessionId'),
                        "status": state, "capturedAt": capture.get("capturedAt"), "diagnostics": result.get("diagnostics", [])})
        if state == "VALID":
            valid.update((source, target, eid) for eid in result.get("evidenceIds", []))
        elif state == "INVALID" and (source, target) in selected_executions:
            invalid.add((source, target))
    needed = {(source, f.get("executionId"), eid) for f in facts for source in origins[f["factId"]] for eid in f.get("evidenceIds", [])}
    for fact in facts:
        if any((source, fact.get("executionId")) in invalid for source in origins[fact["factId"]]):
            fact["valueState"] = "CONFLICT"
    state = "INVALID" if invalid else "VALID" if needed and all(f.get("evidenceIds") and origins[f["factId"]] for f in facts) and needed <= valid else "UNVERIFIED"
    return state, checked
