"""Character-bounded discovery without truncating values or losing cursors."""
import json
from .models import AnalysisError, bounded_page, digest

INSPECT_CHAR_BUDGET = 24000


def compact_facts(items):
    contexts, rows = {}, []
    context_keys = {'context', 'taskName', 'formName', 'bindingRef', 'executionId', 'observationId', 'period', 'readAt', 'stepId'}
    for item in items:
        context = {k: v for k, v in item.items() if k in context_keys}
        key = 'context_' + digest(context)[:24]
        contexts[key] = context
        row = {k: v for k, v in item.items() if k not in context_keys | {'rawValueJson', 'displayValue', 'contentDigest', 'sourceOrigins', 'precision', 'row', 'column', 'cellAddress'}}
        rows.append({**row, 'contextRef': key})
    return {'items': rows, 'contexts': contexts}


def character_page(items, offset, limit, max_page, pack=None, metadata=None, fields_supported=True):
    source = bounded_page(items, offset, limit, max_page)
    selected = source['items']
    while True:
        packed = pack(selected) if pack else {'items': selected}
        result = {**source, **(metadata or {}), **packed, 'returnedCount': len(selected),
                  'nextOffset': offset + len(selected) if offset + len(selected) < source['total'] else None,
                  'characterBudget': INSPECT_CHAR_BUDGET}
        if len(json.dumps(result, ensure_ascii=False)) <= INSPECT_CHAR_BUDGET:
            return result
        if len(selected) <= 1:
            hint = '选择 fields 或通过捕获引用读取对应回执路径。' if fields_supported else '使用 QueryResultRead 按 captureRef 读取原回执，并按返回的 readOptions 选择子路径或字段。'
            raise AnalysisError('INSPECT_ITEM_TOO_LARGE', '单项内容超过读取容量，请' + hint,
                                {'offset': offset, 'availableFields': list(selected[0]) if selected else [],
                                 'factRef': selected[0].get('factId') if selected else None,
                                 'captureRefs': selected[0].get('captureRefs', [selected[0].get('captureRef')]) if selected else [],
                                 'characterBudget': INSPECT_CHAR_BUDGET})
        selected = selected[:max(1, len(selected) // 2)]
