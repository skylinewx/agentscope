"""Revise an implementation using frozen inputs and actual replacement outputs."""
from copy import deepcopy

from .models import AnalysisError, digest
from .scalar_references import reference_id

# These express source verification or a conversion obligation, not a statistical method.
SOURCE_OPERATORS = {'FORMULA_CHECK', 'RECONCILE', 'CONVERT'}
RULE_OPERATORS = {'SUM', 'LINEAR'}


def effective_tasks(artifact):
    tasks = deepcopy(artifact.get('plan', {}).get('workPlan', []))
    updates = {item['taskId']: item for item in artifact.get('workRefs', [])}
    for task in tasks:
        update = updates.get(task['taskId'], {})
        for key in ('refs', 'operator', 'revisionReason'):
            if key in update:
                task[key] = deepcopy(update[key])
    return tasks


def revision_basis(base, previous, incoming):
    """Use only the frozen execution, never a model-declared failure or success flag."""
    old, new = previous.get('operator'), incoming.get('operator', previous.get('operator'))
    if old == new or old not in SOURCE_OPERATORS | RULE_OPERATORS:
        return None

    def reject(message):
        raise AnalysisError('INVALID_WORK_PLAN', message, {
            'taskId': previous['taskId'], 'requiresSourceQuery': False,
            'hint': '保留业务目标、范围和规则。方法误选须填写 revisionReason，并绑定已成功执行且覆盖原目标的结果；来源公式、勾稽和单位换算要求不能降级。'})

    if (old in SOURCE_OPERATORS or previous.get('requiredEvidence') == 'FORMULAS') and new != 'PYTHON':
        reject('来源核对、构成分析或单位换算要求不能替换成普通统计。')
    reason = incoming.get('revisionReason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        reject('修正已声明的业务运算需要 revisionReason，说明方法误选及新方法如何回答原目标。')
    known = {value['derivedId']: value for value in base.get('derivedValues', [])}
    operations = {'derived_' + value['id']: value for value in base.get('plan', {}).get('operations', [])}
    refs = [reference_id(ref) for ref in previous.get('refs', [])]
    attempts = [known[ref] for ref in refs if ref in known and known[ref].get('operator') == old]
    # A planner may correct an operation before execution. Completion still
    # requires actual replacement outputs and exactly the original targets.
    if any(operations.get(ref, {}).get('ruleRef') or operations.get(ref, {}).get('comparabilityRef') for ref in refs) or any(value.get('ruleRef') or value.get('ruleSource') or value.get('comparabilityRef')
           or operations.get(value['derivedId'], {}).get('ruleRef')
           or operations.get(value['derivedId'], {}).get('comparabilityRef') for value in attempts):
        reject('计算已经绑定业务规则，须修复原规则下的运算，不能改用普通统计绕过。')
    facts = {fact['factId'] for fact in base.get('facts', [])}
    # Rule-based methods can consume derived inputs. Only actual, fully resolvable
    # input chains establish the original population; a prose label never does.
    pending = [ref for value in attempts for ref in value.get('inputRefs', [])]
    if not attempts:
        pending.extend(ref for ref in refs if ref in facts)
        pending.extend(ref for key in refs for ref in operations.get(key, {}).get('inputRefs', []))
    targets, visited = set(previous.get('targetRefs', [])), set()
    while pending:
        ref = reference_id(pending.pop())
        if ref in visited:
            continue
        visited.add(ref)
        if ref in facts:
            targets.add(ref)
        elif ref in known and known[ref].get('inputRefs'):
            pending.extend(known[ref]['inputRefs'])
        else:
            reject('原计算输入尚不明确，不能证明替代方法覆盖同一业务范围。')
    if not targets:
        reject('原计算没有可验证的实际输入，不能仅凭说明修改已声明方法。')
    if old == 'SUM':
        from .engine import source_aggregation_rule
        by_ref = {fact['factId']: fact for fact in base.get('facts', [])}
        if source_aggregation_rule([by_ref[ref] for ref in targets]):
            reject('来源已提供原生聚合规则，须保留该业务规则，不能以描述统计替代。')
    return {'taskId': previous['taskId'], 'title': previous['title'],
            'previousOperator': old, 'operator': new, 'previousRefs': deepcopy(previous['refs']),
            'targetRefs': sorted(targets), 'reason': reason.strip(),
            'failureCodes': sorted({item['code'] for value in attempts for item in value['diagnostics'] if item.get('code')}),
            'baseAnalysisRef': base.get('analysisRef')}


def record_revisions(base, artifact):
    """Validate replacements after execution; all entry points commit atomically."""
    from .completion import computation_coverage
    previous = {task['taskId']: task for task in effective_tasks(base)}
    known = {item.get('factId', item.get('derivedId')): item
             for item in artifact['facts'] + artifact['derivedValues']}
    history = deepcopy(base.get('methodRevisions', []))
    for task in effective_tasks(artifact):
        old = previous.get(task['taskId'])
        if old is None:
            continue
        basis = revision_basis(base, old, task)
        if basis is None:
            continue
        covered = set()
        for ref in task.get('refs', []):
            value = known.get(reference_id(ref), {})
            if 'derivedId' not in value:
                continue
            ready, matches, targets, _ = computation_coverage(value, known, task.get('operator'), ref)
            if ready and matches:
                covered.update(targets)
        expected = set(basis['targetRefs'])
        if covered != expected:
            raise AnalysisError('INVALID_WORK_PLAN', '替代方法须已成功执行，并精确覆盖原计算目标；不能缩小范围或混入对照。', {
                'taskId': task['taskId'], 'missingTargetRefs': sorted(expected - covered),
                'unexpectedTargetRefs': sorted(covered - expected), 'requiresSourceQuery': False})
        basis['refs'] = deepcopy(task['refs'])
        basis['revisionId'] = digest(basis)
        if not any(item['revisionId'] == basis['revisionId'] for item in history):
            history.append(basis)
    if history:
        artifact['methodRevisions'] = history
    return history


def prune_replaced_failures(artifact):
    """Remove only inactive failed attempts, leaving referenced failures blocking."""
    revisions = artifact.get('methodRevisions', [])
    if not revisions:
        return
    known = {value['derivedId']: value for value in artifact['derivedValues']}
    operations = {'derived_' + op['id']: op for op in artifact['plan'].get('operations', [])}
    def dependencies(ref):
        return {reference_id(item) for item in known.get(ref, {}).get('inputRefs', [])} | {
            (reference_id(item) if reference_id(item) in known else 'derived_' + reference_id(item))
            for item in operations.get(ref, {}).get('inputRefs', [])}
    candidates = {reference_id(ref) for item in revisions for ref in item['previousRefs']}
    while True:
        more = {ref for ref, value in known.items() if value.get('calculationValidation') != 'PASS'
                and dependencies(ref) & candidates}
        if more <= candidates:
            break
        candidates.update(more)
    active, pending = set(), [reference_id(ref) for task in effective_tasks(artifact) for ref in task.get('refs', [])]
    pending += [ref for ref, value in known.items() if value.get('calculationValidation') == 'PASS']
    while pending:
        ref = pending.pop()
        if ref in active:
            continue
        active.add(ref)
        pending.extend(dependencies(ref) - active)
    removed = {ref for ref in candidates - active if ref in known and known[ref].get('calculationValidation') != 'PASS'}
    if removed:
        artifact['derivedValues'] = [value for value in artifact['derivedValues'] if value['derivedId'] not in removed]
        artifact['plan']['operations'] = [op for op in artifact['plan'].get('operations', []) if 'derived_' + op['id'] not in removed]
        artifact['planDigest'] = digest(artifact['plan'])
        artifact['calculationValidation'] = ('BLOCKED' if any(value.get('calculationValidation') != 'PASS'
                                                            for value in artifact['derivedValues']) else 'PASS')
