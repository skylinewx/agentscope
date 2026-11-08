"""Host-owned status reads preserve physical execution identity and do not involve another model turn."""
import os
import json


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


def waiting(receipt):
    if not isinstance(receipt, dict):
        return False
    error = receipt.get('error') or {}
    return receipt.get('status') == 'IN_PROGRESS' or (
        receipt.get('executionState') == 'UNKNOWN' and error.get('recoveryAction') == 'READ_STATUS')


def wait_limit(request):
    configured = float(os.environ.get('BUDGET_QUERY_ACTIVE_SECONDS', '600'))
    return configured * (2 if request.get('purpose') == 'ANALYZE' else 1) + 90
