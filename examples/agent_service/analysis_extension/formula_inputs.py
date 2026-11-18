"""Expand explicitly selected targets from captured structure, without source queries."""
from copy import deepcopy

from .models import AnalysisError


def expand_selections(sources, captures, selections):
    """目标由调用者确定；程序只复用同一观察的已捕获依赖，不选择业务范围。"""
    expanded, targets, catalogs = [], set(), {}
    # prepared 目录和普通适配共享续查身份校验，不能把续页当成独立计划。
    effective_captures = dict(captures)
    from .adapters.budget_v2 import resolve_form_requests
    for adapter, group in sources.groups(captures):
        if adapter.adapter_id == 'budget-v2' and any(item.get('preparedTargets') and item.get('captureRef') in group
                                                    for item in selections):
            effective_captures.update(resolve_form_requests(group))
    for selection in selections:
        current = deepcopy(selection)
        if current.get('preparedTargets'):
            from .prepared_inputs import resolve
            if current.get('captureRef') not in captures:
                raise AnalysisError('UNKNOWN_CAPTURE', '目标输入必须引用本次捕获。')
            current = resolve(effective_captures[current['captureRef']], current)
        if not current.pop('includeFormulaDependencies', False):
            expanded.append(current)
            continue
        reference = current['captureRef']
        if reference not in captures:
            raise AnalysisError('UNKNOWN_CAPTURE', '公式输入必须引用本次捕获。')
        catalog_key = (reference, current['stepId'], current.get('observationId'), tuple(current['cellKeys']))
        if catalog_key not in catalogs:
            adapter = sources.for_capture(captures[reference])
            catalogs[catalog_key] = (adapter.catalog(captures[reference], grid_selection={**current, 'includeFormulaDependencies': True})
                                    if adapter.adapter_id == 'budget-v2' else adapter.catalog(captures[reference]))
        cells = [item for item in catalogs[catalog_key] if item.get('stepId') == current['stepId']
                 and all(current.get(key) is None or item.get(key) == current[key]
                         for key in ('observationId', 'bindingRef', 'executionPeriod'))]
        observations = {item.get('observationId') for item in cells}
        if len(observations) != 1:
            raise AnalysisError('FORMULA_OBSERVATION_AMBIGUOUS', '公式选择器须定位一个已捕获的观察，不能跨对象或时期拼接依赖。')
        by_key = {item['cellKey']: item for item in cells}
        by_address = {}
        for item in cells:
            if item.get('cellAddress'):
                by_address.setdefault(item['cellAddress'], []).append(item)
        selected = {}

        def visit(item, depth=0):
            if item['cellKey'] in selected:
                return
            if depth > 64 or len(selected) >= 1000:
                raise AnalysisError('FORMULA_LIMIT', '目标依赖超过本次分析容量，不能静默截断。')
            selected[item['cellKey']] = item
            structure = item.get('formulaStructure')
            if not isinstance(structure, dict):
                raise AnalysisError('FORMULA_STRUCTURE_NOT_CAPTURED', '当前观察没有标准公式结构，请通过 FORM_VIEW_QUERY 重新取得目标依赖。')
            for address in structure.get('references', []):
                candidates = by_address.get(address, [])
                # 不猜缺失/歧义地址；核对结果会保留具体断点。
                if len(candidates) == 1:
                    visit(candidates[0], depth + 1)

        for key in current['cellKeys']:
            if key not in by_key:
                raise AnalysisError('CELL_NOT_CAPTURED', '公式目标不在已捕获的观察中。', {'cellKey': key})
            if current.get('role') != 'STRUCTURE':
                targets.add(by_key[key]['factId'])
            visit(by_key[key])
        current['cellKeys'] = list(selected)
        expanded.append(current)
    return expanded, targets


def add_checks(operations, target_refs, facts, scope):
    """使用稳定事实引用生成检查，已有同目标检查不重复执行。"""
    result = deepcopy(operations)
    from .engine import _select
    known_facts = {item['factId']: item for item in facts}
    checked = set()
    for operation in operations:
        if operation.get('operator') != 'FORMULA_CHECK':
            continue
        pool = facts
        if scope.get('referenceMembers') and not operation.get('select', {}).get('objectId'):
            pool = [fact for fact in facts if fact['objectId'] in scope['members']]
        try:
            selected = _select(pool, operation, known_facts)
        except AnalysisError:
            # 无效显式运算由原计算器保留诊断；不能据此跳过有效目标。
            continue
        if len(selected) == 1 and selected[0].get('factId'):
            checked.add(selected[0]['factId'])
    known = {item['factId'] for item in facts}
    from .models import digest
    for reference in sorted(target_refs & known - checked):
        result.append({'id': 'formula_' + digest(reference)[:20], 'operator': 'FORMULA_CHECK', 'inputRefs': [reference]})
    return result
