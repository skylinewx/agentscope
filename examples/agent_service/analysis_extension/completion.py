"""Check model-declared work against real references, without choosing a workflow."""
from .models import AnalysisError
from copy import deepcopy
from .scalar_references import normalize_reference, reference_id, reference_key, scalar_coverage

PROTECTED_OPERATORS = {'FORMULA_CHECK', 'RECONCILE', 'SUM', 'LINEAR', 'CONVERT'}


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
    updates = {u['taskId']: u for u in base.get('workRefs', [])}
    for task in prior:
        if task['taskId'] in updates:
            task['refs'] = deepcopy(updates[task['taskId']]['refs'])
            if 'operator' in updates[task['taskId']]:
                task['operator'] = updates[task['taskId']]['operator']
    positions = {t['taskId']: i for i, t in enumerate(prior)}
    seen = set()
    for task in incoming:
        if not isinstance(task, dict) or not isinstance(task.get('taskId'), str) or task['taskId'] in seen:
            raise AnalysisError('INVALID_WORK_PLAN', 'workPlan 须有不重复的 taskId。')
        key = task['taskId']
        seen.add(key)
        if key in positions:
            previous_task = prior[positions[key]]
            if (previous_task.get('operator') in PROTECTED_OPERATORS
                    and task.get('operator', previous_task['operator']) != previous_task['operator']):
                raise AnalysisError('INVALID_WORK_PLAN', '已声明的来源核对或业务可比运算不能替换为普通统计。', {'taskId': key})
            if task.get('kind') != previous_task['kind']:
                raise AnalysisError('INVALID_WORK_PLAN', '重建不能改变已有工作项类型；可更新结果引用或新增工作项。', {'taskId': key})
            if previous_task.get('requiredEvidence') == 'FORMULAS' and task.get('requiredEvidence', 'FORMULAS') != 'FORMULAS':
                raise AnalysisError('INVALID_WORK_PLAN', '重建须保留已有构成分析目标，不能改成仅取金额宣布完成。', {'taskId': key})
            for field in ('operator', 'targetRefs', 'requiredEvidence'):
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
    from .identity import same_source_position
    old_facts = {f['factId']: f for f in base['facts']}
    new_facts = {f['factId']: f for f in artifact['facts']}
    tasks = {t['taskId']: t for t in artifact.get('plan', {}).get('workPlan', [])}

    def same_position(old, new):
        return bool(old.get('sourceOrigins')) and same_source_position(old, new, same_snapshot=False)

    for previous in base.get('plan', {}).get('workPlan', []):
        targets = tasks[previous['taskId']].get('targetRefs', [])
        for ref in previous.get('targetRefs', []):
            if ref in targets: continue
            matches = [candidate for candidate in targets if same_position(old_facts[ref], new_facts[candidate])]
            if len(matches) != 1:
                raise AnalysisError('INVALID_WORK_PLAN', '重建须保留已有业务目标；新的观察引用只能替换同源、同对象、同指标、同时期的原目标。',
                                    {'taskId': previous['taskId'], 'missingTargetRef': ref, 'path': '/workPlan',
                                     'hint': '重新取数后显式更新 targetRefs 与运算 inputRefs；不能把缺失对象替换成另一个对象来宣布完成。'})


def computation_coverage(value, known, expected_operator, reference=None):
    """核对真实计算链；计数等结果可以继续参与比例运算，不要求最后一步同名。"""
    if isinstance(reference, dict):
        from .validation import executable_field, selected_value
        field, node_ref = reference.get('field', 'value'), reference.get('nodeRef')
        matches = not expected_operator or value.get('operator') == expected_operator
        # 先验证字段确实存在；完成核对不能使拼错的字段或节点成为有效引用。
        selected_value(value, field, node_ref)
        # 字段选择不替代必须逐目标执行的来源公式/关系核对。
        if expected_operator in ('FORMULA_CHECK', 'RECONCILE'):
            return computation_coverage(value, known, expected_operator)
        available = executable_field(value, field, node_ref)
        ready = available and matches
        return ready, matches, scalar_coverage(value, reference, known) if ready else set(), []
    if not expected_operator or expected_operator in ('FORMULA_CHECK', 'RECONCILE'):
        # 公式/勾稽目标必须由对应检查直接处理，操作数的检查不能代替目标检查。
        matches = not expected_operator or value.get('operator') == expected_operator
        null_check_completed = (value.get('operator') == 'FORMULA_CHECK'
                                and value.get('executionStatus') == 'COMPLETED'
                                and value.get('completionReason') == 'SOURCE_VALUE_MISSING')
        ready = (value.get('calculationValidation') == 'PASS' or null_check_completed) and matches
        return ready, matches, set(value.get('inputRefs', [])) if ready else set(), []

    # 迭代遍历有向依赖，避免深链递归；只有实际 inputRefs 表示运算使用关系。
    # sourceFactIds 是追溯信息，不能单靠它证明执行过所需运算。
    pending = [(value['derivedId'], False)]
    active, results = set(), {}
    while pending:
        ref, exiting = pending.pop()
        if ref in results:
            continue
        current = known.get(ref)
        if current is None:
            return False, False, set(), []
        if 'factId' in current:
            results[ref] = ({ref}, set(), set())
            continue
        if current.get('calculationValidation') != 'PASS':
            return False, False, set(), []
        dependencies = current.get('inputRefs', [])
        if not exiting:
            if ref in active:
                return False, False, set(), []
            active.add(ref)
            pending.append((ref, True))
            pending.extend((dependency, False) for dependency in dependencies)
            continue
        active.remove(ref)
        facts, covered, matched = set(), set(), set()
        for dependency in dependencies:
            dependency_facts, dependency_coverage, dependency_matches = results[dependency]
            facts.update(dependency_facts)
            covered.update(dependency_coverage)
            matched.update(dependency_matches)
        if current.get('operator') == expected_operator:
            matched.add(ref)
            covered.update(current.get('selectedCoverageRefs', facts))
        results[ref] = facts, covered, matched
    _, covered, matched = results[value['derivedId']]
    return bool(matched), bool(matched), covered, sorted(matched)


def formula_target_refs(artifact):
    """从公式工作项及真实计算链确定分析目标，筛选背景不自动成为公式目标。"""
    tasks = artifact.get('plan', {}).get('workPlan', [])
    updates = {item['taskId']: item['refs'] for item in artifact.get('workRefs', [])}
    derived = {value['derivedId']: value for value in artifact.get('derivedValues', [])}
    targets, pending = set(), []
    for task in tasks:
        if task.get('kind') != 'COMPUTATION':
            continue
        # 显式目标包括尚未完成的对象，不能只留下已有计算结果的对象。
        if task.get('operator') == 'FORMULA_CHECK' or task.get('requiredEvidence') == 'FORMULAS':
            targets.update(task.get('targetRefs', []))
        pending.extend(reference_id(ref) for ref in updates.get(task['taskId'], task.get('refs', [])))
    visited = set()
    while pending:
        ref = pending.pop()
        if ref in visited or ref not in derived:
            continue
        visited.add(ref)
        value = derived[ref]
        if value.get('operator') == 'FORMULA_CHECK':
            # 只取本次检查的目标；不把公式的全部递归操作数扩成分析义务。
            targets.update(value.get('inputRefs', []))
        else:
            pending.extend(value.get('inputRefs', []))
    return targets


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
        if (not isinstance(update, dict) or not {'taskId', 'refs'} <= set(update) or set(update) - {'taskId', 'refs', 'operator'}
                or not isinstance(update['taskId'], str) or update['taskId'] in updated
                or update['taskId'] not in {t.get('taskId') for t in plan if isinstance(t, dict)}
                or not isinstance(update['refs'], list) or len(update['refs']) > 1000
                or len({reference_key(ref) for ref in update['refs']}) != len(update['refs'])):
            raise AnalysisError('INVALID_WORK_PLAN', 'workRefs 只能为已有工作项补充真实引用，不能删除或替换工作项。', {'path': f'/workRefs/{index}'})
        if 'operator' in update:
            from .engine import OPERATORS
            task = next(task for task in plan if task.get('taskId') == update['taskId'])
            if (task.get('kind') != 'COMPUTATION' or task.get('operator') in PROTECTED_OPERATORS
                    or not isinstance(update['operator'], str) or update['operator'] not in OPERATORS | {'PYTHON'}
                    or update['operator'] in PROTECTED_OPERATORS):
                raise AnalysisError('INVALID_WORK_PLAN', '只可显式修正普通计算的方法；来源公式/汇总核对要求保持。')
        updated[update['taskId']] = update['refs']
    result, seen = [], set()
    for task in plan:
        if (not isinstance(task, dict) or not {'taskId', 'title', 'kind', 'refs'} <= set(task)
                or set(task) - {'taskId', 'title', 'kind', 'refs', 'operator', 'targetRefs', 'requiredEvidence'}
                or any(not isinstance(task.get(k), str) or not task[k].strip() or len(task[k]) > 1024 for k in ('taskId', 'title'))
                or task['taskId'] in seen or task['kind'] not in ('VALUES', 'COMPUTATION', 'RELATION')
                or not isinstance(task['refs'], list) or len(task['refs']) > 1000
                or len({reference_key(ref) for ref in task['refs']}) != len(task['refs'])):
            raise AnalysisError('INVALID_WORK_PLAN', '工作项须有唯一标识、业务标题、类型和真实引用；尚未取得引用时填空列表，不提交完成状态。')
        from .engine import OPERATORS
        if task.get('requiredEvidence', 'VALUES') not in ('VALUES', 'FORMULAS'):
            raise AnalysisError('INVALID_WORK_PLAN', '工作项 requiredEvidence 使用 VALUES 或 FORMULAS。')
        if task.get('requiredEvidence') == 'FORMULAS' and (task['kind'] != 'COMPUTATION' or not task.get('targetRefs')):
            raise AnalysisError('INVALID_WORK_PLAN', '构成分析工作项需要 COMPUTATION 和明确的 targetRefs；概览对象不自动成为公式目标。')
        targets = task.get('targetRefs', [])
        if ('operator' in task or 'targetRefs' in task) and task['kind'] != 'COMPUTATION':
            raise AnalysisError('INVALID_WORK_PLAN', 'operator/targetRefs 仅用于 COMPUTATION 工作项。', {'taskId': task['taskId']})
        if ('operator' in task and (not isinstance(task['operator'], str) or task['operator'] not in OPERATORS | {'PYTHON'})
                or not isinstance(targets, list) or len(targets) > 1000
                or any(not isinstance(ref, str) or ref not in known or 'factId' not in known[ref] for ref in targets)
                or len(set(targets)) != len(targets)
                or 'targetRefs' in task and (not targets or 'operator' not in task)):
            raise AnalysisError('INVALID_WORK_PLAN', 'targetRefs 须列出实际目标 factId，并声明 operator；不能用说明文字代替目标范围。', {'taskId': task['taskId']})
        seen.add(task['taskId'])
        items, covered, statistical_subjects = [], set(), set()
        supporting_computations = set()
        effective_operator = next((update['operator'] for update in updates if update['taskId'] == task['taskId'] and 'operator' in update), task.get('operator'))
        for reference in updated.get(task['taskId'], task['refs']):
            ref = reference_id(reference)
            if isinstance(reference, dict) and task['kind'] != 'COMPUTATION':
                raise AnalysisError('INVALID_WORK_REF', '字段引用仅用于 COMPUTATION，取值与关系使用真实 ID。')
            value = relations.get(ref) if task['kind'] == 'RELATION' else known.get(ref)
            if value is None or (task['kind'] == 'VALUES' and 'factId' not in value) or (task['kind'] == 'COMPUTATION' and 'derivedId' not in value):
                allowed = list(relations) if task['kind'] == 'RELATION' else [ref for ref, value in known.items() if ('factId' if task['kind'] == 'VALUES' else 'derivedId') in value]
                raise AnalysisError('INVALID_WORK_REF', '工作项引用不存在或与类型不符。', {'taskId': task['taskId'], 'ref': ref,
                    'path': '/workPlan', 'requiresSourceQuery': False,
                    'expectedKind': task['kind'], 'allowedRefs': allowed[:100], 'allowedRefCount': len(allowed),
                    'actualKind': ('RELATION' if ref in relations else 'VALUES' if 'factId' in known.get(ref, {})
                                   else 'COMPUTATION' if ref in known else 'UNKNOWN'),
                    'hint': '先检查已捕获但未纳入本版本的选择器及已有派生结果，不重新查询来源。VALUES 用 factId，COMPUTATION 用 derivedId，RELATION 用 relationRef；续建保留原工作项需要的输入。',
                    'nextSection': {'VALUES': 'facts', 'COMPUTATION': 'derived', 'RELATION': 'relations'}[task['kind']]})
            if task['kind'] == 'RELATION':
                ready = value['complete']
                detail = {'enumerationComplete': value['complete'], 'childCount': len(value['childIds'])}
            elif task['kind'] == 'COMPUTATION':
                ready, operator_matches, calculation_targets, matched_refs = computation_coverage(
                    value, known, effective_operator, reference)
                detail = {'executionStatus': value['executionStatus'], 'sourceConsistency': value.get('sourceConsistency', 'NOT_CHECKED'),
                          'diagnostics': value.get('diagnostics', [])}
                if value.get('completionReason'):
                    detail['completionReason'] = value['completionReason']
                if 'applicability' in value:
                    detail['applicability'] = value['applicability']
                    detail['valueState'] = value['valueState']
                if effective_operator:
                    detail['operatorMatches'] = operator_matches
                    if matched_refs:
                        detail['operatorMatchRefs'] = matched_refs
                    # 一个工作项可以同时交付家数、合计和占比。额外的已完成计算
                    # 不必都使用同一算子；但必须存在真正执行声明算子的结果，且
                    # 目标覆盖仍仅由该算子的真实依赖链证明。公式/勾稽工作项
                    # 可附带差额、占比等计算，但这些附属结果不提供目标核对覆盖。
                    if (not ready and not operator_matches
                            and value.get('operator') not in ('FORMULA_CHECK', 'RECONCILE')):
                        own_ready, _, _, _ = computation_coverage(value, known, value.get('operator'), reference)
                        if own_ready:
                            supporting_computations.add(ref)
                if ready:
                    covered.update(calculation_targets)
                    if isinstance(reference, dict) and value.get('operator') in ('DESCRIBE', 'COUNT'):
                        statistical_subjects.update(calculation_targets)
            else:
                ready = value.get('valueState') in ('PRESENT', 'NO_VALUE', 'NOT_APPLICABLE')
                detail = {'valueState': value.get('valueState')}
            items.append({'ref': reference, 'ready': ready, **detail})
        if any(item['ready'] and item.get('operatorMatches') for item in items):
            for item in items:
                if reference_id(item['ref']) in supporting_computations:
                    item.update(ready=True, supportingComputation=True)
        state = 'PENDING' if not items else 'READY' if all(v['ready'] for v in items) else 'BLOCKED'
        missing = [ref for ref in targets if ref not in covered]
        if missing and state == 'READY': state = 'PENDING'
        # 直接选统计字段须与声明子集一致；算术中的合法分母/比较项不自动变成分析主体。
        extra = sorted(statistical_subjects - set(targets)) if targets else []
        if extra and state == 'READY':
            state = 'BLOCKED'
        result.append({'taskId': task['taskId'], 'title': task['title'], 'kind': task['kind'], 'state': state, 'items': items,
                       **({'operator': effective_operator} if effective_operator else {}),
                       **({'unexpectedTargetRefs': extra} if extra else {}),
                       **({'targetRefs': targets, 'missingTargetRefs': missing} if targets else {})})
    declared_state = 'UNDECLARED' if not result else 'READY' if all(t['state'] == 'READY' for t in result) else 'INCOMPLETE'
    partial_reason = artifact.get('partialReason')
    preparation = artifact.get('inputPreparation') or {}
    if not partial_reason and preparation.get('issues'):
        partial_reason = f"预算材料已准备 {preparation['ready']}/{preparation['expected']} 个目标，未取得的结构或构成见 inputPreparation.issues。"
    return {'state': 'INCOMPLETE' if partial_reason else declared_state,
            'declaredWorkState': declared_state,
            **({'partialReason': partial_reason} if partial_reason else {}),
            'items': result, 'pendingTaskIds': [t['taskId'] for t in result if t['state'] != 'READY'],
            'boundary': '仅核对模型已声明工作项的引用与执行状态；partialReason 表示仍有未完成范围，pendingTaskIds 为空不能抵消该说明。READY 不表示来源一致或用户问题已完整回答。'}
