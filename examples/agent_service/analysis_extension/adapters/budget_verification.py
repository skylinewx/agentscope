"""Budget execution evidence verification, scoped to captured origins."""
from .budget_v2 import receipt_from_capture, CONTEXT_FIELDS
from ..actions import source_identity
from ..models import AnalysisError, digest


def evidence_scope(capture):
    receipt = receipt_from_capture(capture)
    return (source_identity(capture),
            digest({key: receipt.get('context', {}).get(key) for key in CONTEXT_FIELDS}),
            receipt.get('querySessionId'))


def matches_selected_execution(facts, captures, candidate):
    """只纳入当前事实同来源、快照、查询会话和执行批次的真实核验回执。"""
    if candidate.get('originalToolName') != 'validate_evidence' or candidate.get('request', {}).get('action') != 'verify':
        return False
    target = candidate['request'].get('executionId')
    scope = evidence_scope(candidate)
    result = receipt_from_capture(candidate).get('data', {}).get('verification', {})
    if result.get('status') == 'VALID' and not result.get('evidenceIds'):
        return False
    return any(fact.get('executionId') == target and evidence_scope(captures[ref]) == scope
               for fact in facts for ref in fact.get('captureRefs', [])
               if ref in captures and captures[ref].get('sourceAdapter', {}).get('id') == 'budget-v2')


def _captured_evidence(fact, capture, receipt, evidence_ids):
    """已授权连接的原始回执可证明来源完整性，不证明分析结论或公式成立。

    store.get_capture 已核对所有者和文件摘要；适配阶段已逐项校验请求、
    表观察/批量摘要和所选维度。缺少连接身份的导入材料仍需外部核验。
    """
    if (not all(source_identity(capture))
            or capture.get('originalToolName') != 'execute_semantic_plan'
            or str(capture.get('captureState', '')).upper() not in ('SUCCESS', 'SUCCEEDED')):
        return set()
    if (receipt.get('status') not in ('SUCCEEDED', 'PARTIAL')
            or fact.get('executionId') != receipt.get('executionId')
            or fact.get('querySessionId') != receipt.get('querySessionId')
            or any(not fact.get('context', {}).get(key)
                   or fact['context'][key] != receipt.get('context', {}).get(key) for key in CONTEXT_FIELDS)):
        return set()
    return set(fact.get('evidenceIds', [])) & evidence_ids


def verification_details(facts, captures, verifier_refs=None):
    """返回累计核验及精确的剩余证据；远程无效结论优先于本地回执。"""
    valid, invalid = set(), set()
    receipts = {ref: receipt_from_capture(capture) for ref, capture in captures.items()}
    scopes = {ref: (source_identity(capture), digest({key: receipts[ref].get('context', {}).get(key)
                    for key in CONTEXT_FIELDS}), receipts[ref].get('querySessionId')) for ref, capture in captures.items()}
    evidence_ids_by_ref = {ref: {item['evidenceId'] for item in receipt.get('evidence', []) if item.get('evidenceId')}
                           for ref, receipt in receipts.items()}
    # IDs may be repeated by a different connection, snapshot or query session.
    origins = {f["factId"]: {scopes[ref] for ref in f.get("captureRefs", []) if ref in captures} for f in facts}
    selected_executions = {(source, f.get("executionId")) for f in facts for source in origins[f["factId"]]}
    checked = []
    for ref, capture in captures.items():
        if verifier_refs is not None and ref not in verifier_refs:
            continue
        if capture.get("originalToolName") != "validate_evidence" or capture["request"].get("action") != "verify":
            continue
        receipt = receipts[ref]
        data = receipt.get("data", {})
        target = capture["request"].get("executionId")
        if target != data.get("executionId") or data.get("action") != "verify":
            raise AnalysisError("VERIFICATION_MISMATCH", "核验回执与核验请求不一致。")
        result = data.get("verification", {})
        state = result.get("status")
        source = scopes[ref]
        if (source, target) not in selected_executions:
            continue
        checked.append({"captureRef": capture["captureRef"], "executionId": target,
                        "context": receipt.get('context'), "querySessionId": receipt.get('querySessionId'),
                        "evidenceIds": result.get('evidenceIds', []), "basis": "REMOTE_VERIFICATION",
                        "status": state, "capturedAt": capture.get("capturedAt"), "diagnostics": result.get("diagnostics", [])})
        if state == "VALID":
            valid.update((source, target, eid) for eid in result.get("evidenceIds", []))
        elif state == "INVALID" and (source, target) in selected_executions:
            invalid.add((source, target))
    # 显式追查行动要求新执行核验，不能用本地读取代替原有行动约束。
    if verifier_refs is None:
        local_records = {}
        for fact in facts:
            for ref in fact.get('captureRefs', []):
                if ref not in captures:
                    continue
                capture = captures[ref]
                evidence_ids = _captured_evidence(fact, capture, receipts[ref], evidence_ids_by_ref[ref])
                if not evidence_ids:
                    continue
                source, execution = scopes[ref], fact.get('executionId')
                valid.update((source, execution, eid) for eid in evidence_ids)
                record = local_records.setdefault(ref, {'captureRef': ref, 'executionId': execution,
                    'context': fact.get('context'), 'querySessionId': fact.get('querySessionId'),
                    'status': 'VALID', 'basis': 'CAPTURE_INTEGRITY', 'capturedAt': capture.get('capturedAt'),
                    'evidenceIds': set(), 'diagnostics': []})
                record['evidenceIds'].update(evidence_ids)
        checked.extend({**record, 'evidenceIds': sorted(record['evidenceIds'])} for record in local_records.values())
    needed = {(source, f.get("executionId"), eid) for f in facts for source in origins[f["factId"]] for eid in f.get("evidenceIds", [])}
    for fact in facts:
        if any((source, fact.get("executionId")) in invalid for source in origins[fact["factId"]]):
            fact["valueState"] = "CONFLICT"
    state = "INVALID" if invalid or any(f.get('valueState') == 'CONFLICT' for f in facts) else "VALID" if needed and all(f.get("evidenceIds") and origins[f["factId"]] for f in facts) and needed <= valid else "UNVERIFIED"
    return state, checked, needed - valid


def verification(facts, captures, verifier_refs=None):
    state, records, _ = verification_details(facts, captures, verifier_refs)
    return state, records
