"""Source-qualified analysis members; original source IDs remain unchanged."""
from copy import deepcopy

from .models import AnalysisError, digest
from .sources import combined_context


def member_ref(namespace, object_id):
    return 'member_' + digest([namespace, object_id])[:24]


def catalog(registry, capture, dimension=None):
    adapter = registry.for_capture(capture)
    dimension = dimension or adapter.options.get('memberDimension', 'recordId')
    namespace = adapter.member_namespace(capture, dimension)
    result = adapter.catalog(capture)
    for fact in result:
        dimensions = {**fact.get('effectiveDimensions', {}), **fact.get('dimensions', {})}
        raw = fact.get('objectId', dimensions.get(dimension))
        if namespace is not None and isinstance(raw, str) and raw:
            fact.update(sourceObjectId=raw, memberRef=member_ref(namespace, raw))
    return result


def adapt(registry, captures, selections, scope, max_facts):
    dimension = scope.get('memberDimension', 'recordId')
    groups, namespaces, candidates = {}, {}, {}
    for ref, capture in captures.items():
        adapter = registry.for_capture(capture)
        namespace = adapter.member_namespace(capture, dimension)
        if namespace is None:
            continue  # Verification still receives all captures through the registry.
        ns = digest(namespace)
        namespaces[ns] = namespace
        key = digest([adapter.snapshot(), namespace])
        groups.setdefault(key, {'adapter': adapter, 'namespace': ns, 'captures': {}})['captures'][ref] = capture
        rows = catalog(registry, capture, dimension)
        ids = {f['sourceObjectId'] for f in rows if f.get('sourceObjectId')}
        ids.update(row['key'] for row in adapter.entities(capture) if isinstance(row.get('key'), str) and row['key'])
        for raw in ids:
            mid = member_ref(namespace, raw)
            candidates[mid] = {'memberRef': mid, 'sourceObjectId': raw, 'namespace': namespace}
    members, references = scope.get('members', []), scope.get('referenceMembers', [])
    if (not isinstance(members, list) or not members or not isinstance(references, list)
            or any(not isinstance(v, str) or not v for v in members + references)
            or len(set(members + references)) != len(members + references)):
        raise AnalysisError('INVALID_SCOPE', '主体与比较对象须为互不重叠、无重复的成员标识。')
    qualified = len(namespaces) > 1 or any(value in candidates for value in members + references)
    if not namespaces:
        raise AnalysisError('EMPTY_SELECTION', '需要选择实际捕获的成员数据，核验回执不提供成员。')
    if not qualified:
        member_captures = {ref: capture for group in groups.values() for ref, capture in group['captures'].items()}
        result = registry._adapt_unscoped(member_captures, selections, scope, max_facts)
        namespace = next(iter(namespaces.values()))
        for fact in result['facts']:
            fact.update(sourceObjectId=fact['objectId'], memberRef=member_ref(namespace, fact['objectId']))
        return result
    if len(groups) > 1 and scope.get('contextMode') != 'COMPOSITE':
        raise AnalysisError('CONTEXT_CONFLICT', '跨来源成员汇编须显式使用 scope.contextMode=COMPOSITE。')
    if not selections or any(item.get('captureRef') not in captures for item in selections):
        raise AnalysisError('UNKNOWN_CAPTURE', '选择器必须引用本次捕获。')
    resolved = {}
    for value in members + references:
        matches = [candidates[value]] if value in candidates else [c for c in candidates.values() if c['sourceObjectId'] == value]
        if not matches and len(namespaces) == 1:
            namespace = next(iter(namespaces.values()))
            matches = [{'memberRef': member_ref(namespace, value), 'sourceObjectId': value, 'namespace': namespace}]
        if len(matches) != 1:
            raise AnalysisError('AMBIGUOUS_MEMBER' if matches else 'UNKNOWN_MEMBER',
                                '跨来源成员须使用 inventory 返回的 memberRef；原始 ID 仅在来源唯一时可使用。',
                                {'objectId': value, 'candidates': matches[:100], 'path': '/scope/members'})
        resolved[value] = matches[0]
    if len({item['memberRef'] for item in resolved.values()}) != len(resolved):
        raise AnalysisError('INVALID_SCOPE', '同一成员不能同时用原始 ID 和 memberRef 重复声明。')
    excluded_members = []
    selected_ids = {item['memberRef'] for item in resolved.values()}
    for excluded in scope.get('excludedMembers', []):
        ref, value = excluded.get('captureRef'), excluded.get('objectId')
        if ref not in captures or not isinstance(value, str) or not value or not excluded.get('reason'):
            raise AnalysisError('INVALID_EXCLUSION', '排除对象需要原始 ID 或 memberRef、理由和已纳入的来源引用。')
        namespace = registry.for_capture(captures[ref]).member_namespace(captures[ref], dimension)
        if namespace is None:
            raise AnalysisError('INVALID_EXCLUSION', '排除对象须引用成员来源，不能引用核验回执。')
        candidate = candidates.get(value)
        if candidate and candidate['namespace'] != namespace:
            raise AnalysisError('INVALID_EXCLUSION', '排除对象的 memberRef 与 captureRef 来源不一致。')
        raw = candidate['sourceObjectId'] if candidate else value
        mid = member_ref(namespace, raw)
        if mid in selected_ids:
            raise AnalysisError('INVALID_EXCLUSION', '排除对象不能同时属于主体或比较对象。')
        excluded_members.append({**excluded, 'objectId': mid, 'sourceObjectId': raw})
    normalized = {**scope, 'members': [resolved[v]['memberRef'] for v in members],
                  'referenceMembers': [resolved[v]['memberRef'] for v in references],
                  'excludedMembers': excluded_members,
                  'memberBindings': list(resolved.values())}
    results, facts = [], []
    population = scope.get('population')
    if population and population.get('captureRef') not in captures:
        raise AnalysisError('UNKNOWN_POPULATION', '范围来源未纳入本次捕获。')
    primary_namespaces = {digest(resolved[v]['namespace']) for v in members}
    population_complete = False
    for group in groups.values():
        adapter, selected, ns = group['adapter'], group['captures'], group['namespace']
        own = [s for s in selections if s['captureRef'] in selected]
        if not own:
            continue
        primary = [resolved[v]['sourceObjectId'] for v in members if digest(resolved[v]['namespace']) == ns]
        comparison = [resolved[v]['sourceObjectId'] for v in references if digest(resolved[v]['namespace']) == ns]
        if not primary and not comparison:
            raise AnalysisError('SCOPE_MISMATCH', '所选来源没有本次声明的成员。')
        local = deepcopy(scope)
        # An adapter validates its own raw source IDs. Reference-only groups are
        # local subjects solely for extraction, then restored to the global role.
        local.update(members=primary or comparison, referenceMembers=comparison if primary else [])
        local.pop('reportTopics', None)
        if not population or population['captureRef'] not in selected or not primary:
            local.pop('population', None)
        local['excludedMembers'] = [{**e, 'objectId': e['sourceObjectId']} for e in excluded_members if e['captureRef'] in selected]
        result = adapter.adapt(selected, own, local, max_facts)
        results.append(result)
        if primary_namespaces == {ns} and local.get('population'):
            population_complete = result['scope'].get('populationComplete', False)
        for fact in result['facts']:
            if not fact.get('captureRefs'):
                continue
            raw = fact['objectId']
            mid = member_ref(namespaces[ns], raw)
            fact.update(objectId=mid, memberRef=mid, sourceObjectId=raw)
            facts.append(fact)
    if len({f['factId'] for f in facts}) != len(facts):
        raise AnalysisError('SOURCE_IDENTITY_CONFLICT', '来源适配器产生了重复事实标识。')
    observed = {f['objectId'] for f in facts}
    primary, reference = set(normalized['members']), set(normalized['referenceMembers'])
    for item in resolved.values():
        mid = item['memberRef']
        if mid not in observed:
            facts.append({'factId': 'missing_' + digest(mid)[:24], 'objectId': mid, 'memberRef': mid,
                          'sourceObjectId': item['sourceObjectId'], 'captureRefs': [],
                          'value': None, 'valueState': 'NOT_QUERIED', 'unit': None, 'scale': None})
    if len(facts) > max_facts:
        raise AnalysisError('FACT_LIMIT', '汇编事实超过上限。')
    pages_complete = bool(results) and all(r['scope'].get('sourcePagesComplete', False) for r in results)
    selected_complete = pages_complete and observed == primary | reference
    contexts = {c['sourceContextId']: c for r in results for c in r['sourceContexts']}
    coverage = 'COMPLETE' if population_complete and selected_complete else 'UNKNOWN' if selected_complete else 'PARTIAL'
    return {'facts': facts, 'sourceContexts': list(contexts.values()), 'context': combined_context(list(contexts.values())),
            'scope': {**normalized, 'scopeId': 'scope_' + digest(scope)[:24], 'coverage': coverage,
                      'populationComplete': population_complete,
                      'selectedDataComplete': selected_complete, 'sourcePagesComplete': pages_complete,
                      'observedMembers': sorted(observed & primary), 'pendingMembers': sorted(primary - observed),
                      'observedReferenceMembers': sorted(observed & reference), 'pendingReferenceMembers': sorted(reference - observed),
                      'semanticCompleteness': 'UNVERIFIED'}}
