"""Associate outputs with the declared calculation, preserving explicit selections."""
from .models import AnalysisError


def bind_native(work, operations, results, requested_tasks=None, requested_operations=None):
    tasks = {t['taskId']: t for t in work if t.get('kind') == 'COMPUTATION'}
    by_operation = {}
    for task in tasks.values():
        by_operation.setdefault(task.get('operator'), []).append(task)
    selected = {}
    for op, result in zip(operations, results):
        explicit = op.get('taskId')
        if explicit:
            if explicit not in tasks or tasks[explicit].get('operator') != op['operator']:
                raise AnalysisError('INVALID_WORK_PLAN', '运算所选工作项不存在或算子不同。',
                                    {'operationId': op['id'], 'taskId': explicit, 'requiresSourceQuery': False})
            candidates = [tasks[explicit]]
        else:
            if requested_operations is not None and op['id'] not in requested_operations:
                continue
            candidates = [t for t in by_operation.get(op['operator'], [])
                          if requested_tasks is None or t['taskId'] in requested_tasks]
        if (len(candidates) != 1 or candidates[0].get('refs')
                or op['operator'] in ('FORMULA_CHECK', 'RECONCILE')):
            continue
        targets = set(candidates[0].get('targetRefs', []))
        coverage = set(result.get('sourceFactIds', []))
        if targets and (not coverage or not coverage <= targets):
            continue
        selected.setdefault(candidates[0]['taskId'], []).append(result['derivedId'])
    for key, refs in selected.items():
        tasks[key]['refs'] = refs


def python_bindings(work_plan, outputs):
    if not work_plan or not isinstance(outputs, list) or not outputs or any(not isinstance(o, str) for o in outputs):
        return work_plan
    from copy import deepcopy
    work = deepcopy(work_plan)
    declared = [t for t in work if t.get('kind') == 'COMPUTATION' and t.get('operator') == 'PYTHON']
    if len(declared) == 1 and not declared[0].get('refs'):
        declared[0]['refs'] = list(outputs)
    return work
