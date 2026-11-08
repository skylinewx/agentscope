"""Owner-scoped saved-result inputs for the existing exact arithmetic tool."""
from contextlib import contextmanager
from contextvars import ContextVar

from .models import AnalysisError, digest

_CURRENT = ContextVar('business_value_reference_service', default=None)


@contextmanager
def reference_context(service, turn):
    token = _CURRENT.set((service, turn))
    try:
        yield
    finally:
        _CURRENT.reset(token)


async def calculate_references(kwargs, operation):
    bound = _CURRENT.get()
    if bound is None:
        raise AnalysisError('RESULT_REFERENCE_UNAVAILABLE', '当前会话未启用保存结果读取，请使用明确的原始数值。')
    service, turn = bound
    await service.authorize()
    if kwargs.get('requests') is not None:
        requests = kwargs['requests']
        if set(kwargs) != {'requests'} or not isinstance(requests, list) or not 1 <= len(requests) <= 32:
            raise AnalysisError('INVALID_CALCULATION_BATCH', '批量计算仅使用1—32项 requests。')
        ids, results = set(), []
        for index, item in enumerate(requests):
            key = item.get('id', str(index)) if isinstance(item, dict) else None
            if not isinstance(key, str) or not key or len(key) > 128 or key in ids:
                raise AnalysisError('INVALID_CALCULATION_BATCH', '计算 id 须为唯一的非空文本。')
            ids.add(key)
            try:
                value = await calculate_references({k: v for k, v in item.items() if k != 'id'}, operation)
                results.append({'id': key, 'requestIndex': index, 'status': 'SUCCEEDED', **value})
            except AnalysisError as error:
                results.append({'id': key, 'requestIndex': index, 'status': 'FAILED', 'error': error.as_dict()})
        count = sum(r['status'] == 'SUCCEEDED' for r in results)
        return {'results': results, 'complete': count == len(results), 'succeeded': count,
                'status': 'SUCCEEDED' if count == len(results) else 'PARTIAL' if count else 'FAILED'}
    else:
        requests = [kwargs]
    from .business_tables import build_tables, resolve_cell
    from numeric_tools import calculate
    receipts, table_cache, prepared, proofs, capture_refs = {}, {}, [], [], set()
    for request in requests:
        if not isinstance(request, dict) or set(request) - {'expression', 'variables', 'decimalPlaces', 'valueRefs'}:
            raise AnalysisError('INVALID_VALUE_REFERENCE', '计算项包含不支持的字段。')
        if request.get('variables') is not None and not isinstance(request['variables'], dict):
            raise AnalysisError('INVALID_VALUE_REFERENCE', 'variables 必须是变量名到十进制字符串的映射。')
        variables = dict(request.get('variables') or {})
        references = request.get('valueRefs', {})
        if not isinstance(references, dict) or len(references) > 1000:
            raise AnalysisError('INVALID_VALUE_REFERENCE', 'valueRefs 是变量名到保存结果引用的映射。')
        inputs = []
        for name, ref in references.items():
            if name in variables or not isinstance(ref, dict) or set(ref) - {'captureRef', 'analysisRef', 'tableRef', 'rowRef', 'columnRef', 'rowRefs', 'filters', 'aggregation'}:
                raise AnalysisError('INVALID_VALUE_REFERENCE', '引用变量不能覆盖字面变量，且只能使用已声明的引用字段。')
            if ('captureRef' in ref) == ('analysisRef' in ref):
                raise AnalysisError('INVALID_VALUE_REFERENCE', 'captureRef 与 analysisRef 必须且只能选一个，沿用表格返回的所属引用。')
            reference_field = 'captureRef' if 'captureRef' in ref else 'analysisRef'
            source_ref = ref[reference_field]
            if any(not isinstance(ref.get(key), str) or not ref[key] for key in (reference_field, 'tableRef', 'columnRef')):
                raise AnalysisError('INVALID_VALUE_REFERENCE', '引用需要非空所属结果引用、tableRef、columnRef。')
            cache_key = (reference_field, source_ref)
            if cache_key not in receipts:
                if reference_field == 'captureRef':
                    saved = service.store.get_capture(service.owner, source_ref)
                    adapter = service.sources.for_capture(saved)
                    # Catalog validation proves the native request, rows and exact scope.
                    adapter.catalog(saved)
                    receipts[cache_key] = adapter.raw_receipt(saved['response'])
                    table_cache[cache_key] = build_tables(receipts[cache_key])
                    capture_refs.add(source_ref)
                else:
                    # The store validates the immutable artifact and its owner.
                    # Formula T/R/C ids must use exactly the same projection.
                    from .formula_tables import analysis_tables
                    artifact = service.store.get_analysis(service.owner, source_ref)
                    receipts[cache_key] = {}
                    table_cache[cache_key] = analysis_tables(artifact['facts'])
            receipt = receipts[cache_key]
            if 'rowRef' in ref:
                if any(k in ref for k in ('rowRefs', 'filters', 'aggregation')):
                    raise AnalysisError('INVALID_VALUE_REFERENCE', '单格引用与范围聚合分开。')
                if not isinstance(ref['rowRef'], str) or not ref['rowRef']:
                    raise AnalysisError('INVALID_VALUE_REFERENCE', 'rowRef 必须是返回的非空行标识。')
                facts = [resolve_cell(receipt, ref.get('tableRef'), ref['rowRef'], ref.get('columnRef'), table_cache[cache_key])]
                value = facts[0]['value']
                rows = [ref['rowRef']]
            else:
                if ref.get('aggregation') not in ('SUM', 'COUNT', 'MIN', 'MAX'):
                    raise AnalysisError('AGGREGATION_REQUIRED', '范围引用必须显式指定 SUM/COUNT/MIN/MAX；均值可用明确的SUM与COUNT变量计算。')
                table = next((t for t in table_cache[cache_key] if t.ref == ref.get('tableRef')), None)
                if table is None:
                    raise AnalysisError('TABLE_NOT_FOUND', '结果中不存在指定业务表。')
                candidates = table.select_rows(ref.get('filters'))
                if 'rowRefs' in ref:
                    ids = ref['rowRefs']
                    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                            or len(set(ids)) != len(ids) or set(ids) - {r['id'] for r in candidates}):
                        raise AnalysisError('INVALID_VALUE_REFERENCE', 'rowRefs 必须全部属于已选范围且无重复。')
                    candidates = [r for r in candidates if r['id'] in ids]
                rows = [r['id'] for r in candidates]
                if not rows:
                    raise AnalysisError('EMPTY_CALCULATION_RANGE', '计算范围没有来源事实。')
                if len(rows) > 1000:
                    raise AnalysisError('CALCULATION_RANGE_LIMIT', '范围超过现有精确计算容量，使用分析算子处理完整范围。')
                facts = [resolve_cell(receipt, table.ref, row, ref.get('columnRef'), table_cache[cache_key]) for row in rows]
                if len({f.get('unit') for f in facts}) > 1:
                    raise AnalysisError('UNIT_MISMATCH', '范围存在不同单位，不能自动聚合。')
                # An explicit row selection may legitimately compare totals and
                # details; ordinary implicit whole-table SUM must not double count.
                if ref['aggregation'] == 'SUM':
                    dims = {k for f in facts for k in f.get('dimensions', {})}
                    mixed = [k for k in dims if '00000000' in {f.get('dimensions', {}).get(k) for f in facts}
                             and len({f.get('dimensions', {}).get(k) for f in facts}) > 1]
                    if mixed:
                        raise AnalysisError('TOTAL_DETAIL_OVERLAP', '范围同时包含合计和明细，请用 filters 或 rowRefs 明确不重复的计算范围。', {'dimensions': mixed})
                agg = ref['aggregation']
                numbers = [f['value'] for f in facts]
                from .arithmetic_contract import MAX_ARITHMETIC_INPUTS
                if agg == 'COUNT':
                    value = str(len(facts))
                else:
                    while len(numbers) > 1:
                        reduced = []
                        for start in range(0, len(numbers), MAX_ARITHMETIC_INPUTS):
                            vals = {'v' + str(i): v for i, v in enumerate(numbers[start:start + MAX_ARITHMETIC_INPUTS])}
                            reduced.append(calculate(f"{agg.lower()}({','.join(vals)})", vals)['value'])
                        numbers = reduced
                    value = numbers[0]
            variables[name] = value
            capture_refs.update(f['captureRef'] for f in facts if isinstance(f.get('captureRef'), str))
            inputs.append({'variable': name, reference_field: source_ref, 'tableRef': ref.get('tableRef'),
                           'columnRef': ref.get('columnRef'), 'rowRefs': rows, 'aggregation': ref.get('aggregation'),
                           'factRefs': [f['factId'] for f in facts], 'value': value,
                           'unit': None if ref.get('aggregation') == 'COUNT' else facts[0].get('unit')})
        prepared.append({**{k: v for k, v in request.items() if k != 'valueRefs'}, 'variables': variables})
        proofs.append({'expression': request.get('expression'), 'inputs': inputs})
    result = operation(**({'requests': prepared} if 'requests' in kwargs else prepared[0]))
    evidence = {'request': prepared, 'references': proofs, 'result': result}
    if turn is not None:
        turn.state.setdefault('referencedCalculations', {})[digest(evidence)] = evidence
        captures = turn.state.setdefault('queryCaptureRefs', [])
        for ref in sorted(capture_refs):
            if ref not in captures:
                captures.append(ref)
    visible = [{'expression': p['expression'], 'inputs': [
        {**{k: v for k, v in i.items() if k not in ('factRefs', 'rowRefs')}, 'rowCount': len(i['rowRefs']),
         **({'rowRefs': i['rowRefs']} if len(i['rowRefs']) <= 4 else {})} for i in p['inputs']]} for p in proofs]
    return {**result, 'sourceReferences': visible}
