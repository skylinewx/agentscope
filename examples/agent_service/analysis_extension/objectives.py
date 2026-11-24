"""Business objectives for a real user turn. Native requests and proof are derived from these choices."""
from copy import deepcopy

from .material_plan import ROLES, normalize
from .models import AnalysisError, digest


def merge(state, additions):
    existing = deepcopy(state.get('objectives', []))
    if additions is None:
        return existing
    if not isinstance(additions, list) or not 1 <= len(additions) <= 20:
        raise AnalysisError('INVALID_OBJECTIVE', '每次声明 1 至 20 个取值或数据构成目标。')
    by_ref = {item['objectiveRef']: item for item in existing}
    pending = {}
    for item in existing:
        if not item.get('sourceRef'):
            pending[(item['role'], item['question'], item['capability'], item.get('membersFromObjective'))] = item['objectiveRef']
    declared = []
    for item in additions:
        if not isinstance(item, dict) or item.get('capability') not in ('VALUES', 'DATA_COMPOSITION'):
            raise AnalysisError('INVALID_OBJECTIVE', '目标能力使用 VALUES（取值）或 DATA_COMPOSITION（数据构成）。')
        old = by_ref.get(item.get('objectiveRef'), {})
        if item.get('objectiveRef') and not old:
            raise AnalysisError('UNKNOWN_OBJECTIVE', '请选择本轮声明已返回的 objectiveRef。')
        role = item.get('role', old.get('role', 'PRIMARY'))
        question = item.get('question', old.get('question', ''))
        if role not in ROLES or not isinstance(question, str) or len(question) > 500:
            raise AnalysisError('INVALID_OBJECTIVE', '用简短的业务问题和实际对象用途区分目标。')
        if not old and item.get('sourceRef') and not item.get('independent'):
            equivalent = [candidate for candidate in existing
                          if candidate['role'] == role and candidate['capability'] == item['capability']
                          and item['sourceRef'] in (candidate.get('sourceRef'), candidate.get('resolvedMemberSource'))
                          and sorted(candidate.get('factRefs', [])) == sorted(item.get('factRefs', []))
                          and ('membersFromObjective' not in item or item['membersFromObjective'] == candidate.get('membersFromObjective'))]
            exact = [c for c in equivalent if c['question'] == question]
            if len(exact) == 1:
                old = exact[0]
            elif equivalent:
                raise AnalysisError('OBJECTIVE_IDENTITY_REQUIRED', '这些已登记目标采用同一来源和用途。更新时带上对应目标引用，新增独立业务问题可指定 independent=true。',
                                    {'choices': [{k: c[k] for k in ('objectiveRef', 'role', 'question')} for c in equivalent]})
        dependency = item.get('membersFromObjective', old.get('membersFromObjective'))
        if type(dependency) is int and 1 <= dependency <= len(declared):
            dependency = declared[dependency - 1]
        if dependency is not None:
            parent = by_ref.get(dependency) if isinstance(dependency, str) else None
            if not parent or dependency == old.get('objectiveRef') or parent['capability'] != 'VALUES' or parent.get('membersFromObjective'):
                raise AnalysisError('INVALID_OBJECTIVE_DEPENDENCY', '成员来源使用前面取值目标的序号（从 1 开始）或已登记的取值目标引用。')
        identity = [state.get('turnKey'), role, question, item['capability'], item.get('sourceRef')]
        if dependency is not None:
            identity.append(dependency)
        ref = old.get('objectiveRef') or 'obj_' + digest(identity)[:16]
        if not old and item.get('sourceRef') and ref not in by_ref:
            matching = [i for i in by_ref.values() if i['role'] == role and i['question'] == question
                        and i['capability'] == item['capability'] and i.get('sourceRef') == item['sourceRef']
                        and i.get('membersFromObjective') == dependency]
            if matching:
                ref = matching[0]['objectiveRef']
            else:
                unbound = pending.pop((role, question, item['capability'], dependency), None)
                if unbound and not by_ref[unbound].get('sourceRef'):
                    ref = unbound
        previous = by_ref.get(ref, {})
        merged = {**previous, **item, 'role': role, 'objectiveRef': ref, 'question': question}
        merged.pop('independent', None)
        if merged.get('memberSourceRef') and dependency is None:
            raise AnalysisError('INVALID_OBJECTIVE_DEPENDENCY', 'memberSourceRef 需要同时指定成员来自哪个取值目标。')
        if dependency is not None:
            merged['membersFromObjective'] = dependency
            if 'sourceRef' in item and item['sourceRef'] != previous.get('sourceRef'):
                merged['compositionSource'] = item['sourceRef']
            if (previous.get('membersFromObjective'), previous.get('memberSourceRef')) != (dependency, merged.get('memberSourceRef')):
                merged.pop('resolvedMemberSource', None)
        declared.append(ref)
        if previous.get('sourceRef') != merged.get('sourceRef') and 'factRefs' not in item:
            merged.pop('factRefs', None)
        if 'factRefs' in merged and (not merged.get('sourceRef') or merged['capability'] != 'DATA_COMPOSITION'):
            raise AnalysisError('INVALID_OBJECTIVE', '按行分析数据构成时同时选择该行所在的 sourceRef。')
        by_ref[ref] = merged
    if len(by_ref) > 20:
        raise AnalysisError('INVALID_OBJECTIVE', '本轮最多保留 20 个独立目标。')
    return list(by_ref.values())


def dependency_sources(turn, target):
    """An explicitly selected VALUES source is already a frozen query result."""
    from types import SimpleNamespace
    from .material_plan import request_key
    from .query_subject import candidates
    from .source_views import register_selections
    producer = target['membersFromObjective']
    parent = next(item for item in turn.state['objectives'] if item['objectiveRef'] == producer)
    source_ref = parent.get('sourceRef')
    source = turn.service.store.get_source_view(turn.service.owner, source_ref) if source_ref else {}
    capture_ref = source.get('selection', {}).get('captureRef')
    contexts = {c['contextRef']: c for c in turn.state.get('businessContexts', [])}
    requery = False
    for ref in turn.state.get('historyReferences', []):
        if ref.get('use') == 'REQUERY' and ref.get('role', 'PRIMARY') == parent['role']:
            context = contexts.get(ref.get('contextRef'), {})
            if (ref.get('sourceRef') == source_ref or capture_ref and
                    capture_ref in [*context.get('captureRefs', []),
                                    *(s.get('captureRef') for s in context.get('subjectScope', {}).get('selections', []))]):
                requery = True
    if source_ref and not requery:
        adopted = [item['sourceRef'] for item in turn.state.get('adoptedInputs', [])
                   if item.get('role') == parent['role'] and item.get('baseSourceRef') == source_ref]
        return [turn.service.store.get_source_view(turn.service.owner, ref) for ref in dict.fromkeys(adopted)] if adopted else [source]
    refs = []
    for ref in turn.state.get('queryCaptureRefs', []):
        capture = turn.service.store.get_capture(turn.service.owner, ref)
        if (capture.get('originalToolName') == 'execute_budget_query' and
                turn.state.get('materialQueryObjectives', {}).get(request_key(capture['request'])) == producer):
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            if receipt.get('status') == 'SUCCEEDED' and receipt.get('data', {}).get('complete') is True:
                refs.append(ref)
    selected = candidates(SimpleNamespace(service=turn.service, state={'queryCaptureRefs': refs}))
    return register_selections(turn, selected) if selected else []


def resolve_dependencies(turn):
    from . import source_views
    for item in turn.state.get('objectives', []):
        if not item.get('membersFromObjective'):
            continue
        choices = {entry['sourceRef']: entry for entry in dependency_sources(turn, item)}
        if not choices:
            item.pop('resolvedMemberSource', None)
            continue
        chosen = item.get('memberSourceRef')
        if chosen is None and len(choices) == 1:
            chosen = next(iter(choices))
        if chosen not in choices:
            raise AnalysisError('OBJECTIVE_MEMBER_SELECTION', '取值目标产生多个可用范围，请为后续目标选择 memberSourceRef。', {
                'objectiveRef': item['objectiveRef'], 'choices': [
                    {k: v[k] for k in ('sourceRef', 'label', 'rowCount')} for v in choices.values()]})
        item['resolvedMemberSource'] = chosen
        if item['capability'] == 'DATA_COMPOSITION':
            base = item.get('compositionSource') or chosen
            if base == chosen or choices[chosen]['rowCount'] == 0:
                item['sourceRef'] = chosen
            else:
                source = turn.service.store.get_source_view(turn.service.owner, base)
                subset = source_views.match_members(turn, source, choices[chosen])
                item['sourceRef'] = source_views.register(turn, subset)['sourceRef']


def pending_members(state):
    return [{key: item[key] for key in ('objectiveRef', 'question', 'membersFromObjective')}
            for item in state.get('objectives', [])
            if item.get('membersFromObjective') and not item.get('resolvedMemberSource')]


def material_plan_for(items):
    return normalize([{'role': role, 'kinds': ['FORMULAS'] if any(
        i['role'] == role and i['capability'] == 'DATA_COMPOSITION' for i in items) else ['VALUES']}
        for role in ROLES if any(i['role'] == role for i in items)])


def query_objective(state, request):
    items = state.get('objectives', [])
    reference = request.pop('objectiveRef', None)
    if reference and not items:
        raise AnalysisError('QUERY_OBJECTIVE_REQUIRED', '本轮没有已登记的分析目标。直接取数请省略 objectiveRef，指标编号填在 intentDraft.targets[].id。')
    if reference:
        matches = [item for item in items if item['objectiveRef'] == reference]
    else:
        bound = (request.get('basis') or {}).get('sourceRef')
        matches = [item for item in items if bound and item.get('sourceRef') == bound] or items
    if not matches and not reference:
        return None
    if len(matches) != 1:
        raise AnalysisError('QUERY_OBJECTIVE_REQUIRED', '请选择这次取数服务的目标。', {
            'objectives': [{k: i[k] for k in ('objectiveRef', 'question', 'role', 'capability')} for i in items]})
    return matches[0]


def adopt_bound_sources(turn, explicit, committed_objectives):
    """Bind local additions while retaining other adopted groups and their member semantics."""
    from . import source_views
    inputs = list(turn.state.get('adoptedInputs', []))
    additions = []
    conflicts = []
    committed_refs = {item['objectiveRef'] for item in committed_objectives}
    known = {(i['sourceRef'], i['role']) for i in inputs}
    for item in turn.state.get('objectives', []):
        if not item.get('sourceRef') or (item.get('membersFromObjective') and not item.get('resolvedMemberSource')):
            continue
        key = (item['sourceRef'], item['role'])
        if key in known:
            continue
        narrowed = [i['sourceRef'] for i in inputs if (i.get('baseSourceRef'), i['role']) == key]
        if len(narrowed) == 1:
            item['sourceRef'] = narrowed[0]
            continue
        if explicit:
            conflict = {field: item[field] for field in ('role', 'capability', 'sourceRef')}
            conflict['question'] = item.get('question', '')[:120]
            if item['objectiveRef'] in committed_refs:
                conflict['objectiveRef'] = item['objectiveRef']
            conflicts.append(conflict)
            continue
        additions.append({'sourceRef': key[0], 'role': key[1]})
        known.add(key)
    if conflicts:
        selected = []
        for item in inputs:
            source = {field: item[field] for field in ('role', 'sourceRef', 'rowCount') if field in item}
            if item.get('label'):
                source['label'] = item['label'][:120]
            selected.append(source)
        raise AnalysisError('OBJECTIVE_SOURCE_MISMATCH',
            '本次声明未保存。conflicts 列出目标所用来源，subjectSources 列出本次选用的来源和用途。'
            '已有目标用返回的 objectiveRef 更新，本次新增目标在原声明中修改。'
            '按用户意图为目标选择同用途的来源，或在 subjectSources 中补上该目标确需的来源及用途。',
            {'conflicts': conflicts, 'subjectSources': selected})
    if additions:
        inputs.extend(source_views.select(turn, additions, require_primary=not any(i['role'] == 'PRIMARY' for i in inputs)))
        source_views.install(turn, inputs)


def for_selection(state, selection):
    return [item for item in state.get('objectives', []) if item['capability'] == 'DATA_COMPOSITION'
            and (not item.get('membersFromObjective') or item.get('resolvedMemberSource'))
            and item['role'] == selection.get('role', 'PRIMARY')
            and (item['objectiveRef'] == selection['objectiveRef'] if selection.get('objectiveRef')
                 else not item.get('sourceRef') or item['sourceRef'] == selection.get('sourceRef'))]


def coverage(state, artifact):
    """Classify only checked roots of the selected input. Interpretation is the reviewer's job."""
    from .completion import formula_target_refs
    roots = formula_target_refs(artifact)
    checks = {v.get('formulaTrace', {}).get('targetFactRef'): v for v in artifact.get('derivedValues', [])
              if v.get('operator') == 'FORMULA_CHECK'}
    groups = state.get('adoptedInputs', [])
    rows = []
    for index, objective in enumerate(state.get('objectives', []), 1):
        row = {k: objective[k] for k in ('objectiveRef', 'question', 'role', 'capability', 'exploratory') if k in objective}
        row['number'] = index
        rows.append(row)
        if objective.get('membersFromObjective') and not objective.get('resolvedMemberSource'):
            row['missingTargets'] = 1
            row['membershipPending'] = True
            continue
        if objective['capability'] != 'DATA_COMPOSITION':
            continue
        selected = [g for g in groups if g['role'] == objective['role']
                    and (not objective.get('sourceRef') or g['sourceRef'] == objective['sourceRef'])]
        inputs = {g['inputRef'] for g in selected}
        def belongs(item):
            return (item.get('role', 'PRIMARY') == objective['role']
                    and (not inputs or item.get('inputRef') in inputs)
                    and (not objective.get('sourceRef') or item.get('sourceRef') == objective['sourceRef'] or item.get('inputRef') in inputs)
                    and (objective['objectiveRef'] in item.get('objectiveRefs', [])
                         or not item.get('objectiveRefs') and not objective.get('factRefs')))
        records = [r for r in state.get('budgetFormulaPreparation', []) if belongs(r)]
        selectors = [r['selection'] for r in records if r.get('selection')]
        form_facts = set(state.get('budgetFormObjectiveFacts', {}).get(objective['objectiveRef'], []))
        def matches(selection, fact):
            return (selection['captureRef'] in fact.get('captureRefs', []) and selection.get('stepId') == fact.get('stepId')
                    and selection.get('observationId') == fact.get('observationId')
                    and (not selection.get('cellKeys') or fact.get('cellKey') in selection['cellKeys']))
        def included(fact):
            if objective.get('sourceRef') and not selected:
                return False
            if fact['factId'] in form_facts:
                return True
            if selectors or records:
                return any(matches(s, fact) for s in selectors)
            memberships = fact.get('inputMemberships') or [fact]
            if objective.get('factRefs'):
                return False
            return any(m.get('inputRole', 'PRIMARY') == objective['role'] and (not inputs or m.get('inputRef') in inputs)
                       for m in memberships)
        row['formulaRefs'] = []
        row['boundaryRefs'] = []
        covered = []
        for fact in artifact.get('facts', []):
            ref = fact['factId']
            if (ref not in roots and ref not in form_facts) or not included(fact):
                continue
            check = checks.get(ref)
            if check and any(n.get('expression') for n in check['formulaTrace'].get('nodes', [])):
                row['formulaRefs'].append(check['derivedId'])
                covered.append(fact)
            elif fact.get('formulaStatus') == 'NOT_CONFIGURED':
                row['boundaryRefs'].append(ref)
                covered.append(fact)
        row['boundaryRefs'].extend(b['factRef'] for b in state.get('materialBoundaries', [])
                                   if b.get('role') == objective['role'] and b.get('status') == 'NO_TARGET_CELL'
                                   and (not inputs or b.get('inputRef') in inputs)
                                   and (not objective.get('factRefs') or objective['objectiveRef'] in b.get('objectiveRefs', [])))
        row['empty'] = bool(selected) and all(g.get('rowCount') == 0 for g in selected)
        row['failedTargets'] = sum(bool(r.get('error')) for r in records)
        expected = {t['key'] for t in state.get('budgetFormulaTargets', []) if belongs(t)}
        supplied = {r.get('targetKey') for r in records if r.get('selection') and not r.get('error') and any(
            matches(r['selection'], f) and all((f.get('dimensions') or {}).get(k) == v
                for k, v in (r.get('subject') or {}).items()) for f in covered)}
        missing = expected - supplied
        missing_forms = form_facts - {f['factId'] for f in covered}
        if missing or missing_forms:
            row['missingTargets'] = len(missing) + len(missing_forms)
    return rows
