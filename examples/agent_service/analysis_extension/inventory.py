"""Read-only, bounded discovery over captured data in one authorized session."""
import json
from .adapters import default_sources
from .actions import source_identity
from .models import AnalysisError, digest, bounded_page


def session_inputs(store, owner, sources=None):
    """Store/ownership/integrity failures stay fatal; bad receipts are isolated."""
    sources = sources or default_sources()
    captures, cursor = [], 0
    while cursor is not None:
        page = store.list_captures(owner, cursor, store.limits.page_size)
        if page['total'] > store.limits.max_selections:
            raise AnalysisError('INVENTORY_LIMIT', '会话输入超过索引容量，请用 captures/capture 分页选择。')
        captures.extend(store.get_capture(owner, item['captureRef']) for item in page['items'])
        cursor = page['nextOffset']
    valid, issues, non_data = [], [], 0
    for capture in captures:
        try:
            adapter = sources.for_capture(capture)
            if not adapter.is_data(capture):
                non_data += 1
                continue
            adapter.receipt(capture)
            # Check the entire catalog before adding this record to the index.
            adapter.catalog(capture)
            valid.append(capture)
        except AnalysisError as exc:
            issues.append({'captureRef': capture['captureRef'], 'tool': capture.get('originalToolName'),
                           'captureState': capture.get('captureState'), 'diagnostic': exc.as_dict(),
                           'usableAsInput': False})
    return valid, issues, {'captureCount': len(captures), 'validCaptureCount': len(valid),
                           'nonDataCaptureCount': non_data, 'invalidCaptureCount': len(issues)}


def inventory(store, owner, offset, limit, member_dimension=None, search=None, detail="compact", sources=None, **filters):
    sources = sources or default_sources()
    if detail not in ("compact", "full"):
        raise AnalysisError("INVALID_DETAIL", "detail 仅支持 compact 或 full。")
    if filters.get('objectId') is not None and not member_dimension:
        raise AnalysisError('MEMBER_DIMENSION_REQUIRED', '按对象筛选会话输入时须指定来源的成员维度。')
    captures, issues, counts = session_inputs(store, owner, sources)
    labels, label_refs, rows, contexts = {}, {}, [], set()
    for capture in captures:
        adapter = sources.for_capture(capture)
        receipt = adapter.receipt_info(capture)
        origin = digest({'source': source_identity(capture), 'context': adapter.data_context(receipt.get('context'))})
        for row in adapter.entities(capture):
            if row.get('key') and row.get('label'):
                labels.setdefault((origin, row.get('modelCode'), row['key']), set()).add(row['label'])
                label_refs.setdefault((origin, row.get('modelCode'), row['key']), set()).add(capture['captureRef'])
    scanned = 0
    for capture in captures:
        adapter = sources.for_capture(capture)
        receipt = adapter.receipt_info(capture)
        origin = digest({'source': source_identity(capture), 'context': adapter.data_context(receipt.get('context'))})
        for item in sources.catalog(capture, member_dimension):
            scanned += 1
            if scanned > store.limits.max_facts:
                raise AnalysisError('INVENTORY_LIMIT', '会话单元格超过索引容量，请用 captures/capture 定位。')
            dimensions = {**item['effectiveDimensions'], **item['dimensions']}
            member = dimensions.get(member_dimension) if member_dimension else None
            model = item['dimensionModels'].get(member_dimension)
            names = labels.get((origin, model, member), set())
            item = {**item, 'objectId': member, 'objectLabel': next(iter(names)) if len(names) == 1 else '',
                    'objectModelCode': model, 'objectLabelState': 'RESOLVED' if len(names) == 1 else 'CONFLICT' if names else 'MISSING',
                    'objectLabelCaptureRefs': sorted(label_refs.get((origin, model, member), [])),
                    'resolvedDimensions': dimensions,
                    'sourceGroup': origin, 'capturedAt': capture.get('capturedAt')}
            contexts.add(origin)
            if any(expected is not None and item.get(key) != expected for key, expected in filters.items()): continue
            if search and search not in json.dumps(item, ensure_ascii=False): continue
            rows.append(item)
    from .inspect_pages import character_page
    def pack(selected):
        if detail == "full": return {"items": selected, "contexts": {}}
        contexts, compact = {}, []
        context_keys = ('context', 'querySessionId', 'taskName', 'formName', 'executionId', 'observationId',
                        'bindingRef', 'effectiveDimensions', 'dimensionModels', 'sourceGroup', 'capturedAt')
        for row in selected:
            context = {key: row.get(key) for key in context_keys}
            context_ref = 'context_' + digest(context)[:24]
            contexts[context_ref] = context
            compact.append({key: value for key, value in row.items() if key not in context_keys and key not in
                            ('rawValueJson', 'displayValue', 'row', 'column', 'cellAddress')})
            compact[-1].update(contextRef=context_ref, bindingRef=row['bindingRef'])
        return {'items': compact, 'contexts': contexts}
    metadata = {**counts, 'detail': detail,
            'diagnosticsSection': 'inventoryIssues' if issues else None,
            'sourceGroupCount': len({row['sourceGroup'] for row in rows}), 'scannedCells': scanned, 'scope': 'current_session',
            'meaning': 'discovery_only_not_merged_or_aggregated', 'nextSection': 'capture_or_build'}
    return character_page(rows, offset, limit, store.limits.page_size, pack, metadata, fields_supported=False)


def entities(captures, offset, limit, max_page, max_items, object_id=None, search=None, sources=None):
    sources = sources or default_sources()
    items, scanned = [], 0
    for capture in captures:
        adapter = sources.for_capture(capture)
        receipt = adapter.receipt_info(capture)
        seen = set()
        for row in adapter.entities(capture):
            if not row.get('key') or not row.get('label'): continue
            scanned += 1
            if scanned > max_items:
                raise AnalysisError('INVENTORY_LIMIT', '实体索引超过容量，请按 captureRef 定位。')
            item = {k: row.get(k) for k in ('key', 'label', 'resourceIri', 'modelCode')}
            identity = digest(item)
            if identity in seen: continue
            seen.add(identity)
            if object_id is not None and item['key'] != object_id: continue
            if search and search not in json.dumps(item, ensure_ascii=False): continue
            items.append({**item, 'captureRef': capture['captureRef'], 'context': receipt.get('context'),
                          'querySessionId': receipt.get('querySessionId')})
    return bounded_page(items, offset, limit, max_page)
