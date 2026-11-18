"""按仍有效的工作项延续计算，并按真实依赖排序；不复用旧计算值。"""
from copy import deepcopy

from .models import AnalysisError
from .scalar_references import reference_id


def merge_operations(base, supplied, work_plan, fact_refs=()):
    names = [operation['id'] for operation in supplied]
    if len(set(names)) != len(names):
        raise AnalysisError('INVALID_OPERATION_ID', '计算标识必须唯一，不能用重复标识覆盖另一项计算。')
    fact_refs = set(fact_refs)
    previous_results = {value['derivedId']: value for value in base['derivedValues']}
    operations = {}
    for operation in base['plan'].get('operations', []):
        preserved = deepcopy(operation)
        result = previous_results.get('derived_' + operation['id'], {})
        # 旧选择器固定为实际输入，避免补入参考对象后扩大原计算范围。
        if not preserved.get('inputRefs') and result.get('inputRefs'):
            preserved['inputRefs'] = result['inputRefs']
            preserved.pop('select', None)
        operations[operation['id']] = preserved
    operations.update({operation['id']: deepcopy(operation) for operation in supplied})

    def operation_id(ref):
        ref = reference_id(ref)
        if ref in fact_refs:
            return None
        if isinstance(ref, str) and ref.startswith('derived_') and ref[8:] in operations:
            return ref[8:]
        if ref in operations:
            return ref
        return None

    # 新计划、工作项及成功的旧计算都保留。失败且已被工作项替换的旧尝试
    # 不再自动带入；仍被工作项或其他计算依赖的失败操作必须保留。
    roots = [operation['id'] for operation in supplied]
    roots += [operation_id(ref) for task in work_plan if task.get('kind') == 'COMPUTATION'
              for ref in task.get('refs', [])]
    roots += [name for name in operations if previous_results.get('derived_' + name, {}).get('calculationValidation') == 'PASS']
    ordered, complete, visiting = [], set(), []

    def visit(name):
        if name is None or name in complete:
            return
        if name in visiting:
            raise AnalysisError('CYCLIC_OPERATIONS', '计算计划存在循环依赖，请修正计算引用，无需重新取数。',
                                {'operationIds': visiting[visiting.index(name):] + [name],
                                 'requiresSourceQuery': False})
        visiting.append(name)
        for ref in operations[name].get('inputRefs', []):
            visit(operation_id(ref))
        visiting.pop()
        complete.add(name)
        ordered.append(operations[name])

    for name in roots:
        visit(name)
    return ordered
