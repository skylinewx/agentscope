"""Host-owned status reads preserve physical execution identity and do not involve another model turn."""
import os
import json
from datetime import datetime

from .query_empty_result import business_outcome, incomplete_result_rows


def superseded_status_reads(captures_and_receipts):
    """Retain audit receipts, but a terminal state replaces earlier waiting states for the same physical execution."""
    def request_key(capture):
        request = capture.get('request', {})
        if not request.get('requestId'):
            return None
        return (capture.get('resourceId'), capture.get('serverName'), capture.get('originalToolName'),
                json.dumps(request, sort_keys=True, ensure_ascii=False))

    executions, terminals = {}, set()
    for capture, receipt in captures_and_receipts:
        key, execution = request_key(capture), receipt.get('executionId')
        if key is None or not execution:
            continue
        executions.setdefault(key, set()).add(execution)
        if receipt.get('status') in ('SUCCEEDED', 'PARTIAL', 'FAILED') and not waiting(receipt):
            terminals.add((key, execution))
    superseded = set()
    for capture, receipt in captures_and_receipts:
        key, execution = request_key(capture), receipt.get('executionId')
        if key is None or not waiting(receipt):
            continue
        # The initial UNKNOWN receipt can precede allocation of an executionId.
        # An exact idempotent request with one known execution still identifies it;
        # conflicting execution IDs must never be silently reconciled.
        known = executions.get(key, set())
        if not execution and len(known) == 1:
            execution = next(iter(known))
        if (key, execution) in terminals:
            superseded.add(capture['captureRef'])
    return superseded


def superseded_query_reads(captures_and_receipts):
    """Select current delivery evidence while preserving each physical attempt for audit."""
    superseded = superseded_status_reads(captures_and_receipts)
    if not any(receipt.get('status') == 'PARTIAL' for _, receipt in captures_and_receipts):
        return superseded

    pending = []
    completed = {}
    for capture, receipt in captures_and_receipts:
        data = receipt.get('data') or {}
        request = capture.get('request') or {}
        if (capture.get('originalToolName') != 'execute_budget_query' or not request.get('requestId')
                or request.get('purpose') != 'QUERY' or data.get('purpose') != 'QUERY'
                or request.get('selection', 'CELLS') != 'CELLS' or data.get('selection') != 'CELLS'):
            continue
        connection = (capture.get('connectionFingerprint'), capture.get('turnKey'),
                      request.get('contextId'), request.get('querySessionId'))
        if (not all(connection) or (receipt.get('context') or {}).get('contextId') != connection[2]
                or receipt.get('querySessionId') != connection[3]):
            continue
        try:
            at = datetime.fromisoformat(capture.get('capturedAt') or '')
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            continue

        # The service defaults selection to CELLS. A new transport request ID does not change the business query.
        intent = dict(request, selection='CELLS')
        del intent['requestId']
        key = connection, json.dumps(intent, ensure_ascii=False, sort_keys=True)
        readings = {((result.get('context', {}).get('target') or {}).get('formBindingKey'),
                     result.get('targetId'), result.get('interpretationId')) for result in data.get('resultSets', [])}
        if not readings or any(not all(identity) for identity in readings):
            continue
        groups = data.get('unresolvedGroups') or []
        if (receipt.get('status') == 'PARTIAL' and data.get('complete') is False and groups
                and all(group.get('binding') and (group.get('diagnostics') or {}).get('retryable') is True
                        for group in groups)):
            pending.append((capture['captureRef'], key, at, readings, {g['binding'] for g in groups}))
        elif (receipt.get('status') == 'SUCCEEDED' and data.get('complete') is True and not groups
              and business_outcome(data) in ('ROWS_RETURNED', 'NO_DATA') and not incomplete_result_rows(data)):
            completed.setdefault(key, []).append((at, readings, {binding for binding, _, _ in readings}))
    for ref, key, partial_at, readings, bindings in pending:
        if any(done_at > partial_at and readings <= done_readings and bindings <= done_bindings
               for done_at, done_readings, done_bindings in completed.get(key, [])):
            superseded.add(ref)
    return superseded


def waiting(receipt):
    if not isinstance(receipt, dict):
        return False
    error = receipt.get('error') or {}
    return receipt.get('status') == 'IN_PROGRESS' or (
        receipt.get('executionState') == 'UNKNOWN' and error.get('recoveryAction') == 'READ_STATUS')


def wait_limit(request):
    configured = float(os.environ.get('BUDGET_QUERY_ACTIVE_SECONDS', '1800'))
    return configured * (2 if request.get('purpose') == 'PREPARE_MATERIALS' else 1) + 90
