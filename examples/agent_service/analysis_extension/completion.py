"""Check model-declared work against real references, without choosing a workflow."""
from .models import AnalysisError
from copy import deepcopy


def continue_plan(base, scope, work_plan):
    """Carry declared obligations across explicit revisions, never across unrelated tasks."""
    scope = deepcopy(scope)
    incoming = deepcopy([] if work_plan is None else work_plan)
    if not isinstance(incoming, list):
        raise AnalysisError('INVALID_WORK_PLAN', 'workPlan 必须是工作项数组。')
    previous = base.get('plan', {})
    prior_scope = previous.get('scope', {})
    if prior_scope.get('population') and not scope.get('population'):
        if (set(prior_scope.get('members', [])) != set(scope.get('members', []))
                or prior_scope.get('memberDimension') != scope.get('memberDimension')):
            raise AnalysisError('POPULATION_REQUIRED', '重建改变了主体范围，须提供对应的 population；不能丢掉已有范围来源。',
                                {'path': '/scope/population', 'baseMembers': prior_scope.get('members', [])})
        scope['population'] = deepcopy(prior_scope['population'])
    if 'referenceMembers' not in scope and prior_scope.get('referenceMembers'):
        scope['referenceMembers'] = deepcopy(prior_scope['referenceMembers'])
    prior = deepcopy(previous.get('workPlan', []))
    updates = {u['taskId']: u['refs'] for u in base.get('workRefs', [])}
    for task in prior:
        if task['taskId'] in updates:
            task['refs'] = deepcopy(updates[task['taskId']])
    positions = {t['taskId']: i for i, t in enumerate(prior)}
    seen = set()
    for task in incoming:
        if not isinstance(task, dict) or not isinstance(task.get('taskId'), str) or task['taskId'] in seen:
            raise AnalysisError('INVALID_WORK_PLAN', 'workPlan 须有不重复的 taskId。')
        key = task['taskId']
        seen.add(key)
        if key in positions:
            previous_task = prior[positions[key]]
            if task.get('kind') != previous_task['kind']:
                raise AnalysisError('INVALID_WORK_PLAN', '重建不能改变已有工作项类型；可更新结果引用或新增工作项。', {'taskId': key})
            for field in ('operator', 'targetRefs'):
                if field not in task and field in previous_task:
                    task[field] = deepcopy(previous_task[field])
            targets = task.get('targetRefs', [])
            if (not isinstance(targets, list) or any(not isinstance(ref, str) for ref in targets)
                    or len(set(targets)) < len(previous_task.get('targetRefs', []))):
                raise AnalysisError('INVALID_WORK_PLAN', '重建须保留已声明的目标，不能缩小目标范围宣布完成。', {'taskId': key})
            prior[positions[key]] = task
        else:
            prior.append(task)
    return scope, prior


def validate_target_continuity(base, artifact):
    """Explicitly rebind fresh observations only at the same business positions."""
    from .findings import signature
    old_facts = {f['factId']: f for f in base['facts']}
    new_facts = {f['factId']: f for f in artifact['facts']}
    tasks = {t['taskId']: t for t in artifact.get('plan', {}).get('workPlan', [])}

    def same_position(old, new):
        return (signature(old) == signature(new)
                and old.get('objectModelCode') == new.get('objectModelCode')
                and bool(old.get('sourceOrigins'))
                and set(old['sourceOrigins']) == set(new.get('sourceOrigins', []))
                and all(old.get('context', {}).get(k) == new.get('context', {}).get(k)
                        for k in ('sourceId', 'schemaHash')))

    for previous in base.get('plan', {}).get('workPlan', []):
        targets = tasks[previous['taskId']].get('targetRefs', [])
        for ref in previous.get('targetRefs', []):
            if ref in targets: continue
            matches = [candidate for candidate in targets if same_position(old_facts[ref], new_facts[candidate])]
            if len(matches) != 1:
                raise AnalysisError('INVALID_WORK_PLAN', '重建须保留已有业务目标；新的观察引用只能替换同源、同对象、同指标、同时期的原目标。',
                                    {'taskId': previous['taskId'], 'missingTargetRef': ref, 'path': '/workPlan',
                                     'hint': '重新取数后显式更新 targetRefs 与运算 inputRefs；不能把缺失对象替换成另一个对象来宣布完成。'})


def completion_summary(artifact):
    plan = artifact.get('plan', {}).get('workPlan', [])
    if not isinstance(plan, list) or len(plan) > 100:
        raise AnalysisError('INVALID_WORK_PLAN', 'workPlan 最多 100 项。')
    known = {f['factId']: f for f in artifact['facts']}
    known.update({v['derivedId']: v for v in artifact['derivedValues']})
    relations = {r['relationRef']: r for r in artifact['relations']}
    updates, updated = artifact.get('workRefs', []), {}
    if not isinstance(updates, list) or len(updates) > 100:
        raise AnalysisError('INVALID_WORK_PLAN', 'workRefs 最多 100 项。')
    for index, update in enumerate(updates):
        if (not isinstance(update, dict) or set(update) != {'taskId', 'refs'}
                or not isinstance(update['taskId'], str) or update['taskId'] in updated
                or update['taskId'] not in {t.get('taskId') for t in plan if isinstance(t, dict)}
                or not isinstance(update['refs'], list) or len(update['refs']) > 1000
                or any(not isinstance(ref, str) for ref in update['refs']) or len(set(update['refs'])) != len(update['refs'])):
            raise AnalysisError('INVALID_WORK_PLAN', 'workRefs 只能为已有工作项补充真实引用，不能删除或替换工作项。', {'path': f'/workRefs/{index}'})
        updated[update['taskId']] = update['refs']
    result, seen = [], set()
    for task in plan:
        if (not isinstance(task, dict) or not {'taskId', 'title', 'kind', 'refs'} <= set(task)
                or set(task) - {'taskId', 'title', 'kind', 'refs', 'operator', 'targetRefs'}
                or any(not isinstance(task.get(k), str) or not task[k].strip() or len(task[k]) > 1024 for k in ('taskId', 'title'))
                or task['taskId'] in seen or task['kind'] not in ('VALUES', 'COMPUTATION', 'RELATION')
                or not isinstance(task['refs'], list) or len(task['refs']) > 1000
                or any(not isinstance(ref, str) for ref in task['refs']) or len(set(task['refs'])) != len(task['refs'])):
            raise AnalysisError('INVALID_WORK_PLAN', '工作项须有唯一标识、业务标题、类型和真实引用；尚未取得引用时填空列表，不提交完成状态。')
        from .engine import OPERATORS
        targets = task.get('targetRefs', [])
        if ('operator' in task or 'targetRefs' in task) and task['kind'] != 'COMPUTATION':
            raise AnalysisError('INVALID_WORK_PLAN', 'operator/targetRefs 仅用于 COMPUTATION 工作项。', {'taskId': task['taskId']})
        if ('operator' in task and (not isinstance(task['operator'], str) or task['operator'] not in OPERATORS)
                or not isinstance(targets, list) or len(targets) > 1000
                or any(not isinstance(ref, str) or ref not in known or 'factId' not in known[ref] for ref in targets)
                or len(set(targets)) != len(targets)
                or 'targetRefs' in task and (not targets or 'operator' not in task)):
            raise AnalysisError('INVALID_WORK_PLAN', 'targetRefs 须列出实际目标 factId，并声明 operator；不能用说明文字代替目标范围。', {'taskId': task['taskId']})
        seen.add(task['taskId'])
        items, covered = [], set()
        for ref in updated.get(task['taskId'], task['refs']):
            value = relations.get(ref) if task['kind'] == 'RELATION' else known.get(ref)
            if value is None or (task['kind'] == 'VALUES' and 'factId' not in value) or (task['kind'] == 'COMPUTATION' and 'derivedId' not in value):
                allowed = list(relations) if task['kind'] == 'RELATION' else [ref for ref, value in known.items() if ('factId' if task['kind'] == 'VALUES' else 'derivedId') in value]
                raise AnalysisError('INVALID_WORK_REF', '工作项引用不存在或与类型不符。', {'taskId': task['taskId'], 'ref': ref,
                    'expectedKind': task['kind'], 'allowedRefs': allowed[:100], 'allowedRefCount': len(allowed),
                    'actualKind': ('RELATION' if ref in relations else 'VALUES' if 'factId' in known.get(ref, {})
                                   else 'COMPUTATION' if ref in known else 'UNKNOWN'),
                    'hint': 'RELATION 只记录关系枚举，用 relationRef；数值汇总核对单独声明 COMPUTATION/RECONCILE，用实际 derivedId。不能用枚举完成代替数值核对。',
                    'nextSection': {'VALUES': 'facts', 'COMPUTATION': 'derived', 'RELATION': 'relations'}[task['kind']]})
            if task['kind'] == 'RELATION':
                ready = value['complete']
                detail = {'enumerationComplete': value['complete'], 'childCount': len(value['childIds'])}
            elif task['kind'] == 'COMPUTATION':
                operator_matches = not task.get('operator') or value.get('operator') == task['operator']
                ready = value.get('calculationValidation') == 'PASS' and operator_matches
                detail = {'executionStatus': value['executionStatus'], 'sourceConsistency': value.get('sourceConsistency', 'NOT_CHECKED'),
                          'diagnostics': value.get('diagnostics', [])}
                if task.get('operator'):
                    detail['operatorMatches'] = operator_matches
                if ready:
                    # A formula operand is not a completed analysis of that operand's own formula.
                    covered.update(value.get('inputRefs', []))
            else:
                ready = value.get('valueState') in ('PRESENT', 'NO_VALUE', 'NOT_APPLICABLE')
                detail = {'valueState': value.get('valueState')}
            items.append({'ref': ref, 'ready': ready, **detail})
        state = 'PENDING' if not items else 'READY' if all(v['ready'] for v in items) else 'BLOCKED'
        missing = [ref for ref in targets if ref not in covered]
        if missing and state == 'READY': state = 'PENDING'
        result.append({'taskId': task['taskId'], 'title': task['title'], 'kind': task['kind'], 'state': state, 'items': items,
                       **({'operator': task['operator']} if 'operator' in task else {}),
                       **({'targetRefs': targets, 'missingTargetRefs': missing} if targets else {})})
    declared_state = 'UNDECLARED' if not result else 'READY' if all(t['state'] == 'READY' for t in result) else 'INCOMPLETE'
    partial_reason = artifact.get('partialReason')
    return {'state': 'INCOMPLETE' if partial_reason else declared_state,
            'declaredWorkState': declared_state,
            **({'partialReason': partial_reason} if partial_reason else {}),
            'items': result, 'pendingTaskIds': [t['taskId'] for t in result if t['state'] != 'READY'],
            'boundary': '仅核对模型已声明工作项的引用与执行状态；partialReason 表示仍有未完成范围，pendingTaskIds 为空不能抵消该说明。READY 不表示来源一致或用户问题已完整回答。'}
