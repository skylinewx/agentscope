"""Conversation turns and native condition references for budget queries."""
from copy import deepcopy

from .models import AnalysisError, digest
from .budget_dimensions import SYSTEM_DIMENSIONS


def system_scope(record):
    codes = {item.get('dimCode', item.get('dimensionCode')) for item in record.get('bindings', [])} - {None}
    return bool(codes) and codes <= SYSTEM_DIMENSIONS


def review_system_scope(state):
    """Keep the protected baseline when reviewing model-declared scope changes.

    Historical business relaxation is a deterministic execution decision. A
    model's explicit system-scope edit still needs semantic review against the
    user request, so neither its old range nor its edit can be projected away.
    """
    from .dimension_scopes import _scope_aliases, concept
    def project(records):
        return {digest([concept(record['draft']['dimensionHint']), record['draft']['selector']]): {
            'dimensionHint': record['draft']['dimensionHint'], 'selector': deepcopy(record['draft']['selector'])}
            for record in records if system_scope(record)}

    records = [record for record in state.get('inheritedDimensionScopes', []) if system_scope(record)]
    conditions = project(records)
    recent = {}
    active = state.get('activeBusinessContextRef')
    if active and active not in state.get('selectedContextRefs', []):
        from .conversation_context import _source_scopes
        anchor = next((c for c in reversed(state.get('businessContexts', [])) if c.get('contextRef') == active), None)
        if anchor:
            changed = [item for key, item in project(_source_scopes(anchor)).items() if key not in conditions]
            if changed:
                recent = {'latestSourceScope': {'contextRef': active, 'request': anchor.get('request', ''), 'conditions': changed}}
    aliases = set().union(*(_scope_aliases(record) for record in records)) if records else set()
    edits = [deepcopy(edit) for edit in state.get('scopeEdits', [])
             if concept(edit['dimensionHint']) in aliases]
    return {**recent, **({'inheritedSystemConditions': list(conditions.values())} if conditions else {}),
            **({'scopeEdits': edits} if edits else {})}


def advance(saved, turn_key):
    ledger = deepcopy((saved or {}).get('conditionTurns', {}))
    # Persist message identities, not wall time or tool-call counts. Replaying a
    # previous message cannot age the conversation a second time.
    if turn_key not in ledger:
        ledger[turn_key] = max(ledger.values(), default=0) + 1
    return {'conditionTurns': ledger, 'conditionTurn': ledger[turn_key],
            'conditionConversationId': (saved or {}).get('conditionConversationId') or next(iter(ledger))}


def history_sources(state, selected):
    """Index native proofs. Picking an older fact source does not rewind the conversation clock."""
    from .dimension_scopes import _scope_aliases, concept, STANDARD_FIELDS
    clears = {concept(edit['dimensionHint']) for edit in state.get('scopeEdits', []) if edit.get('selector') is None}
    clears.update(dim for dim, field in STANDARD_FIELDS.items() if (state.get('intentDecisions', {}).get(field) or {}).get('operation') == 'CLEAR')
    records = {}
    for item in [*state.get('conversationConditions', []), *selected]:
        key = item.get('conditionId')
        if key and not clears & _scope_aliases(item) and (key not in records or
                item.get('confirmedTurn', 0) >= records[key].get('confirmedTurn', 0)):
            records[key] = item
    return records


def source_references(state, selected):
    history = history_sources(state, selected)
    current = {row.get('conditionId') for row in state.get('conversationConditions', [])}
    refs = {}
    for key, record in history.items():
        ref = {'id': key, 'source': deepcopy(record['confirmedSource'])}
        # Selecting a fact from before an explicit replacement or clear cannot
        # restore that old condition by itself. Native recognition can renew it.
        if ('conversationConditions' in state and key not in current
                and record.get('receiptTurn', record['confirmedTurn']) < state['conditionTurn']):
            ref['sourceOnly'] = True
        refs[key] = ref
    return history, refs


def prepare(turn, request, origin=None):
    """Only references to native bindings cross the boundary. The host owns user-turn identity."""
    request.pop('conditionContext', None)
    number = turn.state.get('conditionTurn')
    if number is None:
        return request
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    from .dimension_scopes import retained_scopes
    entries = []
    history, refs = source_references(turn.state, retained_scopes(turn.state))
    default_orgs = []
    if (request.get('basis') or {}).get('kind', 'INTENT') == 'INTENT' and request.get('intentDraft') is not None:
        from .conversation_context import _source_scopes
        active = turn.state.get('activeBusinessContextRef')
        anchor = next((c for c in reversed(turn.state.get('businessContexts', [])) if c.get('contextRef') == active), None)
        baseline = _source_scopes(anchor) if anchor else turn.state.get('inheritedDimensionScopes', [])
        for record in baseline:
            key = record.get('conditionId')
            codes = {b.get('dimCode') or b.get('dimensionCode') for b in record.get('bindings', [])}
            if key and codes == {'MD_ORG'} and record.get('appliesToAllTargets') and not record.get('ambiguousScope'):
                history.setdefault(key, record)
                refs.setdefault(key, {'id': key, 'source': deepcopy(record['confirmedSource'])})
                if key not in default_orgs:
                    default_orgs.append(key)
    for key, record in history.items():
        source = record['confirmedSource']
        item = refs[key]
        scope = next((s for s in scopes if (s.get('confirmedSource') or {}).get('scopeId') == key), None)
        if scope is not None:
            scope['confirmedSource'] = deepcopy(source)
            scope['sourceText'] = record['draft']['sourceText']
            scope['selector'] = deepcopy(record['draft']['selector'])
            item['scopeId'] = scope['id']
        entries.append(item)
    owner = getattr(getattr(turn, 'service', None), 'owner', None)
    conversation = getattr(owner, 'session_id', None) or turn.state.get('conditionConversationId')
    if conversation:
        request['conditionContext'] = {'conversationId': conversation, 'turn': number,
                                       'conditions': entries, 'omitted': []}
        if turn.state.get('comparisonPlan'):
            from .query_comparison import batch_coverage
            batch = batch_coverage(turn.state, request)
            if batch:
                request['conditionContext']['comparisonBatch'] = batch
        if default_orgs:
            request['conditionContext']['organizationDefaults'] = default_orgs
        if origin and (request.get('basis') or {}).get('kind') != 'FACTS':
            from .form_history import preferences
            preferred = preferences(turn.state, origin)
            if preferred:
                request['conditionContext']['formPreferences'] = preferred
    return request


def _metric_target_labels(data):
    """Identify lookup members that actually name a row or column of the returned metric."""
    targets = {t['id']: t['label'] for t in data.get('targets', [])
               if t.get('id') and t.get('label') and not t.get('titleTerms') and len(t.get('allOf', [])) == 2}
    labels = {}
    for result in data.get('resultSets', []):
        target = targets.get(result.get('targetId'))
        if not target:
            continue
        members = {(m.get('dimensionCode'), m.get('baseDataCode'), m.get('objectCode')): m['label']
                   for m in result.get('resolvedTerms', []) if m.get('label') and m.get('dimensionCode')
                   and m['dimensionCode'] not in SYSTEM_DIMENSIONS}
        for row in result.get('rows', []):
            if not members:
                break
            for proof in row.get('conditionEvidence', []):
                key = (proof.get('dimensionCode'), proof.get('baseDataCode'), proof.get('objectCode'))
                if key in members and members[key] in proof.get('runtimeHeaders', []):
                    labels.setdefault(key, set()).add(target)
                    del members[key]
    return labels


def previous_target_scopes(receipt, source, request, source_request=None):
    """Release only complete historical conditions proven to locate the selected, replaced metric."""
    data = receipt.get('data') or {}
    selected = next((item for item in data.get('resultSets', [])
                     if item.get('resultSetId') == source.get('resultSetId')), {})
    target = next((item for item in data.get('targets', []) if item.get('id') == selected.get('targetId')), {})
    current = (request.get('intentDraft') or {}).get('targets', [])
    # A declared new target may use retrieval wording. Its identity does not
    # depend on quoting the user's entire sentence again.
    current_terms = [{p['text'] for p in t.get('businessPhrases', []) if p.get('text')}
                     | {ref.partition(':')[2] for ref in t.get('inheritFrom', [])} for t in current]
    if not current_terms or not all(current_terms):
        return []
    old_draft = (data.get('effectiveRequest') or source_request or {}).get('intentDraft') or {}
    shared = [p['text'] for p in old_draft.get('sharedConditions', []) if p.get('text')]
    logical_id = target.get('logicalTargetId') or str(selected.get('targetId', '')).split(':', 1)[0]
    old_target = next((t for t in old_draft.get('targets', []) if t['id'] == logical_id), None)
    owned = [p['text'] for p in (old_target or {}).get('businessPhrases', [])]
    owned.extend(ref.partition(':')[2] for ref in (old_target or {}).get('inheritFrom', []))
    if owned and all(words == set(owned) for words in current_terms):
        return []
    explicit_roles = {(scope.get('dimCode'), scope.get('valueDomain')) for scope in data.get('scopeResolution', [])
                      if scope.get('status') == 'RESOLVED'
                      and scope.get('targetId') in (None, '', logical_id, selected.get('targetId'))}
    facts = set(source.get('factIds', []))
    rows = [row for row in selected.get('rows', []) if row.get('factId') in facts]
    if not facts or {row['factId'] for row in rows} != facts:
        return []
    def coordinate(member):
        return member.get('dimensionCode'), member.get('baseDataCode'), member.get('objectCode')

    def metric_member(member):
        key, term = coordinate(member), member.get('term')
        return (term and all(key) and key[0] not in SYSTEM_DIMENSIONS and key[:2] not in explicit_roles
                and not member.get('titleFallback') and not any(term in phrase for phrase in shared)
                and any(term in phrase for phrase in (owned if old_target is not None else [target.get('label', '')]))
                and not all(words & {term, target.get('label')} for words in current_terms))

    members = {coordinate(m): (m['term'], m.get('label')) for m in selected.get('resolvedTerms', []) if metric_member(m)}
    # A runtime dimension alone may be an ordinary filter (for example the
    # product of a sales query). A source row/column label ties it to the metric.
    confirmed = set(members)
    for row in rows:
        row_keys = {coordinate(p) for p in row.get('conditionEvidence', []) if coordinate(p) in members
                    and members[coordinate(p)][1] in p.get('runtimeHeaders', [])}
        confirmed.intersection_update(row_keys)
    # The native ledger can store one VALUES condition spanning alternate forms of
    # the same target, even when a sibling form returned no matching rows.
    alternatives = {}
    for sibling in data.get('resultSets', []):
        if sibling.get('resultSetId') == selected.get('resultSetId') or sibling.get('targetId') != selected.get('targetId'):
            continue
        for m in sibling.get('resolvedTerms', []):
            key = coordinate(m)
            if key not in members and metric_member(m):
                alternatives[key] = m['term']
    terms = {**alternatives, **{key: members[key][0] for key in confirmed}}
    result = []
    for entry in data.get('conditionState', {}).get('entries', []):
        selector = entry.get('draft', {}).get('selector', {})
        bindings = entry.get('bindings', [])
        if not bindings or any(not b.get('condition', {}).get('values') for b in bindings):
            continue
        keys = {(b.get('dimCode'), b.get('valueDomain'), value) for b in bindings for value in b['condition']['values']}
        if (keys & confirmed and keys <= terms.keys() and selector.get('type') == 'VALUES' and not selector.get('where')
                and set(selector.get('members', [])) == {terms[key] for key in keys}):
            result.append(entry['id'])
    return result


def native_records(receipt, capture_ref):
    """Expose the native scope to existing follow-up callers; a committed receipt owns its confirmation."""
    data = receipt.get('data') or {}
    state = data.get('conditionState', {})
    metric_labels = _metric_target_labels(data)
    records = []
    for entry in state.get('entries', []):
        key = entry['id']
        source = {'scopeId': key, 'executionId': receipt['executionId'], 'querySessionId': receipt['querySessionId'],
                  'contextId': receipt['context']['contextId']}
        labels = {label for binding in entry['bindings'] for member in binding.get('condition', {}).get('values', [])
                  for label in metric_labels.get((binding.get('dimCode'), binding.get('valueDomain'), member), ())}
        records.append({'conditionId': key, 'confirmedTurn': entry['lastConfirmedTurn'], 'receiptTurn': state['turn'],
                        'metricTargetLabels': sorted(labels),
                        'captureRef': capture_ref, 'confirmedSource': source,
                        **{k: deepcopy(entry[k]) for k in ('scopeField', 'systemRequest') if k in entry},
                        **{field: deepcopy(entry[field]) for field in
                           ('bindings', 'draft', 'appliesToAllTargets', 'dimensionAliases')}})
    return records


async def confirm_reused(turn, runner):
    """Commit only the native conditions of actually selected, older budget results."""
    from .conversation_context import save_context
    state = turn.state
    scope = state.get('resolvedSubjectScope', {})
    refs = list(dict.fromkeys([s['captureRef'] for s in scope.get('selections', [])] or scope.get('captureRefs', [])))
    ordinal = state.get('conditionTurn')
    if not refs or not ordinal:
        return
    native = state.setdefault('nativeConditionHistory', {})
    refreshed = []
    for ref in refs:
        capture = turn.service.store.get_capture(turn.service.owner, ref)
        if capture.get('originalToolName') != 'execute_budget_query':
            continue
        adapter = turn.service.sources.for_capture(capture)
        receipt = adapter.raw_receipt(capture['response'])
        old = receipt.get('data', {}).get('conditionState') or {}
        if not old or old['turn'] == ordinal or old['conversationId'] != turn.service.owner.session_id:
            continue
        records = native_records(receipt, ref)
        active = set(old['active'])
        history, proofs = source_references(state, [r for r in records if r['conditionId'] in active])
        active &= history.keys()
        if not active:
            continue
        args = {'contractVersion': '2.0', 'purpose': 'QUERY', 'question': state['request'],
                'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId'],
                'conditionContext': {'conversationId': old['conversationId'], 'turn': ordinal, 'omitted': [],
                    'selected': sorted(active), 'conditions': list(proofs.values())}}
        captures, error = await runner(turn, capture, 'execute_budget_query', args)
        if error and error.get('code') == 'QUERY_CONTEXT_EXPIRED':
            opened, failure = await runner(turn, capture, 'get_context', {'contractVersion': '2.0', 'action': 'open',
                'sourceId': receipt['context']['sourceId'], 'snapshotId': receipt['context']['snapshotId']})
            if opened and not failure:
                opening = turn.service.store.get_capture(turn.service.owner, opened[-1])
                session = turn.service.sources.for_capture(opening).raw_receipt(opening['response'])['session']
                args.update(contextId=session['context']['contextId'], querySessionId=session['querySessionId'])
                captures, error = await runner(turn, capture, 'execute_budget_query', args)
        if error or not captures:
            raise AnalysisError('CONDITION_SELECTION_UNAVAILABLE', '未能保存本轮采用结果的维度绑定。', error or {})
        evidence = turn.service.store.get_capture(turn.service.owner, captures[-1])
        updated = turn.service.sources.for_capture(evidence).raw_receipt(evidence['response'])
        new_state = updated.get('data', {}).get('conditionState') or {}
        actual = set(new_state.get('active', []))
        baseline = {key for key in active if not proofs[key].get('sourceOnly')}
        if new_state.get('turn') != ordinal or new_state.get('conversationId') != old['conversationId'] or not baseline <= actual <= active:
            raise AnalysisError('CONDITION_SELECTION_MISMATCH', '采用结果的维度回执与本轮选择不一致。')
        renewed = native_records(updated, ref)
        original_labels = {r['conditionId']: r['metricTargetLabels'] for r in records}
        for row in renewed:
            # This receipt only confirms condition ownership. Its facts still belong to the selected query.
            row['metricTargetLabels'] = original_labels.get(row['conditionId'], [])
        refreshed.append((ref, renewed, [row for row in renewed if row['conditionId'] in actual]))
    for ref, history, actual in refreshed:
        native[ref] = history
        state.setdefault('dimensionScopeCaptures', {})[ref] = actual
    if refreshed:
        save_context(state, state['lastCheck']['status'])


def effective_request(request, data):
    """Read the executed scope and verify that only authorized omissions occurred."""
    actual = data.get('effectiveRequest')
    selected = data.get('appliedHistoricalConditions')
    if selected is not None:
        context = request.get('conditionContext') or {}
        if (not actual or not context or context.get('omitted') or not isinstance(selected, list)
                or not selected or any(not isinstance(key, str) for key in selected) or len(set(selected)) != len(selected)):
            raise AnalysisError('INVALID_CONDITION_OMISSION', '本体返回的历史条件选择与本次请求不一致。')
        request = {**request, 'conditionContext': {**context, 'omitted': selected}}
    if actual is None:
        if (request.get('conditionContext') or {}).get('omitted'):
            raise AnalysisError('EFFECTIVE_SCOPE_MISSING', '已放宽的查询缺少实际执行条件，不能恢复原请求代替。')
        return deepcopy(request)
    expected = apply_omissions(request)
    # Transport IDs belong to the envelope; native projection contains business input.
    fields = ('purpose', 'selection', 'question', 'scope', 'groupBy', 'contextText', 'intentDraft', 'basis', 'conditionContext')
    if actual.get('intentDraft') != expected.get('intentDraft') or actual.get('conditionContext') != expected.get('conditionContext'):
        _adopt_native_organization(expected, actual)
    if any(actual.get(key) != expected.get(key) for key in fields):
        raise AnalysisError('EFFECTIVE_SCOPE_MISMATCH', '实际执行条件与已冻结的条件处理结果不一致。')
    return {**deepcopy(request), **deepcopy(actual)}


def _adopt_native_organization(expected, actual):
    """Accept only the native organization projection. All other fields still compare with the frozen request."""
    from unicodedata import normalize
    from .dimension_scopes import concept, selector_evidence
    context = expected.get('conditionContext') or {}
    defaults = set(context.get('organizationDefaults', []))
    if not context or not expected.get('intentDraft') or (expected.get('basis') or {}).get('kind', 'INTENT') != 'INTENT':
        return
    original = expected['intentDraft'].get('dimensionScopes', [])
    resolved = (actual.get('intentDraft') or {}).get('dimensionScopes', [])
    def is_org(scope):
        return concept(scope.get('dimensionHint', '')) == '组织机构'
    if [s for s in original if not is_org(s)] != [s for s in resolved if not is_org(s)]:
        return
    scopes = [s for s in resolved if is_org(s)]
    if not scopes or any(s.get('targetIds') for s in scopes):
        return
    peers = (context.get('comparisonBatch') or {}).get('peers', [])
    if peers and any(is_org(scope) and scope.get('targetIds') for scope in original):
        return  # A comparison can own an organization even when this batch executes only one target.
    peer_terms = {normalize('NFKC', term).strip() for peer in peers for term in peer.get('terms', [])}
    current = expected.get('question', '')
    entries = {r['id']: r for r in context['conditions']}
    actual_ids = {s['id'] for s in scopes}
    inherited = {}
    if len(actual_ids) != len(scopes):
        return
    for scope in scopes:
        if scope.get('confirmedSource'):
            key = scope['confirmedSource'].get('scopeId')
            if key not in defaults or key in inherited or scope['confirmedSource'] != entries.get(key, {}).get('source'):
                return
            inherited[key] = scope['id']
        else:
            selector = scope.get('selector') or {}
            source = scope.get('sourceText')
            if not isinstance(source, str) or not source or source not in current:
                return
            if selector.get('type') == 'VALUES':
                members = selector.get('members') or []
                if (set(selector) != {'type', 'members'} or not members
                        or not all(isinstance(member, str) and member and member in current for member in members)):
                    return
            elif selector.get('type') == 'HIERARCHY':
                if (set(selector) - {'type', 'rootText', 'level'} or not (selector.get('rootText') or selector.get('level'))
                        or selector.get('rootText', '') not in current):
                    return
            elif selector.get('type') == 'ALL':
                if (set(selector) - {'type', 'rootText'} or 'rootText' in selector and (
                        not isinstance(selector['rootText'], str) or not selector['rootText'] or selector['rootText'] not in current)):
                    return
            else:
                return
            if peer_terms and any(normalize('NFKC', term).strip() in peer_terms for term in selector_evidence(selector)):
                return
    if inherited and (set(inherited) != defaults or len(inherited) != len(scopes)):
        return
    original_ids = {s['id'] for s in original if is_org(s)}
    actual_entries = (actual.get('conditionContext') or {}).get('conditions', [])
    if [r['id'] for r in actual_entries] != list(entries):
        return
    for r in actual_entries:
        old = entries[r['id']]
        if old.get('scopeId') == r.get('scopeId'):
            continue
        if r.get('scopeId') is None and (r['id'] in defaults or old.get('scopeId') in original_ids):
            old.pop('scopeId', None)
        elif r['id'] in inherited and r.get('scopeId') == inherited[r['id']]:
            old['scopeId'] = r['scopeId']
        else:
            return
    previous_relations = expected['intentDraft'].get('relations', [])
    actual_relations = actual['intentDraft'].get('relations', [])
    if previous_relations != actual_relations:
        targets = {target['id'] for target in expected['intentDraft'].get('targets', [])}
        hierarchies = [r for r in previous_relations if r.get('type') == 'HIERARCHY']
        if (not hierarchies or len(scopes) != 1 or scopes[0].get('confirmedSource')
                or (scopes[0].get('selector') or {}).get('type') != 'ALL'
                or any(not targets <= set(r.get('targetIds', [])) for r in hierarchies)
                or [r for r in previous_relations if r.get('type') != 'HIERARCHY'] != actual_relations):
            return
        expected['intentDraft']['relations'] = deepcopy(actual_relations)
    expected['intentDraft']['dimensionScopes'] = deepcopy(resolved)


def apply_omissions(request):
    """Deterministic mirror of the native projection; never chooses omissions."""
    result = deepcopy(request)
    context = result.get('conditionContext') or {}
    omitted = set(context.get('omitted', []))
    entries = context.get('conditions', [])
    chosen = [item for item in entries if item['id'] in omitted]
    if len(chosen) != len(omitted):
        raise AnalysisError('INVALID_CONDITION_OMISSION', '放宽条件不属于本次已确认条件。')
    if any(not item.get('scopeId') and not item.get('term') or set(item.get('dimensionCodes', [])) & SYSTEM_DIMENSIONS
           or item.get('confirmedTurn', 0) >= context['turn'] for item in chosen):
        raise AnalysisError('INVALID_CONDITION_OMISSION', '系统维度或本轮条件不能通过历史条件放宽删除。')
    draft = result.get('intentDraft') or {}
    scope_ids = {item['scopeId'] for item in chosen if item.get('scopeId')}
    scopes = draft.get('dimensionScopes', [])
    terms = {item['term'] for item in chosen if item.get('term')}
    for scope in scopes:
        if scope.get('id') in scope_ids:
            terms.update([scope.get('sourceText', ''), *(scope.get('selector') or {}).get('members', [])])
    if 'dimensionScopes' in draft:
        draft['dimensionScopes'] = [scope for scope in scopes if scope.get('id') not in scope_ids]
    for owner, field in [(draft, 'sharedConditions'), *((t, 'businessPhrases') for t in draft.get('targets', []))]:
        if field in owner:
            owner[field] = [p for p in owner[field] if p.get('text') not in terms
                            or p.get('sourceText') and p['sourceText'] in result.get('question', '')]
    text = result.get('contextText')
    if text is not None:
        protected = [p.get('sourceText', p.get('text', '')) for p in draft.get('sharedConditions', [])]
        for target in draft.get('targets', []):
            protected.extend(p.get('sourceText', p.get('text', '')) for p in target.get('businessPhrases', []))
            protected.append(target.get('periodText', ''))
        for scope in draft.get('dimensionScopes', []):
            protected.extend([scope.get('sourceText', ''), *(scope.get('selector') or {}).get('members', []),
                              (scope.get('selector') or {}).get('rootText', '')])
        # A dropped word may be a substring of another retained condition. Keep
        # that complete evidence atom (e.g. 水泥 vs 白水泥), never rewrite its meaning.
        for term in sorted(terms - {''}, key=len, reverse=True):
            positions = []
            for phrase in protected:
                start = text.find(phrase) if phrase else -1
                while start >= 0:
                    positions.append((start, start + len(phrase)))
                    start = text.find(phrase, start + 1)
            start, pieces, cursor = text.find(term), [], 0
            while start >= 0:
                if not any(left <= start and start + len(term) <= right for left, right in positions):
                    pieces.append(text[cursor:start])
                    cursor = start + len(term)
                start = text.find(term, start + len(term))
            text = ''.join([*pieces, text[cursor:]])
        result['contextText'] = ' '.join(text.split())
    context['conditions'] = [item for item in entries if item['id'] not in omitted]
    context['omitted'] = []
    context.pop('recoverySource', None)
    return result
