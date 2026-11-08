"""Resolve receipt-local shared records once; keep native source evidence intact."""
from .models import AnalysisError

MARKER = '$budgetReceiptRef'


def expand_receipt(receipt):
    data = receipt.get('data')
    if not isinstance(data, dict) or 'receiptLayout' not in data:
        return receipt
    if data['receiptLayout'] != 'REFERENCE_POOL_V1':
        raise AnalysisError('INVALID_RECEIPT_REFERENCE', '不支持的预算回执引用布局或缺少引用池。')
    raw_pool = data.get('referencePool')
    chunks = [raw_pool] if isinstance(raw_pool, dict) else raw_pool
    if not isinstance(chunks, list):
        raise AnalysisError('INVALID_RECEIPT_REFERENCE', '预算回执缺少引用池。')
    pool, resolved, active = {}, {}, set()
    for chunk in chunks:
        if not isinstance(chunk, dict) or any(not isinstance(k, str) or k in pool for k in chunk):
            raise AnalysisError('INVALID_RECEIPT_REFERENCE', '预算回执引用池分块无效或标识重复。')
        pool.update(chunk)

    def read(value, depth=0):
        if depth > 64:
            raise AnalysisError('INVALID_RECEIPT_REFERENCE', '预算回执引用层级超过支持范围。')
        if isinstance(value, dict):
            if MARKER in value:
                key = value[MARKER]
                if len(value) != 1 or not isinstance(key, str) or key not in pool:
                    raise AnalysisError('INVALID_RECEIPT_REFERENCE', '预算回执来源引用不存在或格式无效。')
                if key in resolved:
                    return resolved[key]
                if key in active:
                    raise AnalysisError('INVALID_RECEIPT_REFERENCE', '预算回执来源引用出现循环。')
                active.add(key)
                result = read(pool[key], depth + 1)
                active.remove(key); resolved[key] = result
                return result
            return {k: read(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [read(v, depth + 1) for v in value]
        return value

    # Shared lists and contexts remain shared objects in memory. No per-cell
    # reconstruction of a duplicate source list and no additional remote read.
    return {**receipt, 'data': {k: read(v) for k, v in data.items() if k not in ('receiptLayout', 'referencePool')}}
