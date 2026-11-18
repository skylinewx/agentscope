"""Conversation turns and native condition references for budget queries."""
from copy import deepcopy

from .models import AnalysisError, digest


SYSTEM_DIMENSIONS = frozenset(('DATATIME', 'MD_ORG', 'MD_SCENE', 'MD_CURRENCY', 'MD_MGRVER'))


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
    records = [record for record in state.get('inheritedDimensionScopes', []) if system_scope(record)]
    conditions = {digest([record['draft']['dimensionHint'], record['draft']['selector']]): {
        'dimensionHint': record['draft']['dimensionHint'], 'selector': deepcopy(record['draft']['selector'])}
        for record in records}
    aliases = set().union(*(_scope_aliases(record) for record in records)) if records else set()
    edits = [deepcopy(edit) for edit in state.get('scopeEdits', [])
             if concept(edit['dimensionHint']) in aliases]
    return {**({'inheritedSystemConditions': list(conditions.values())} if conditions else {}),
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


def prepare(turn, request):
    """Only references to native bindings cross the boundary. The host owns user-turn identity."""
    request.pop('conditionContext', None)
    number = turn.state.get('conditionTurn')
    if number is None:
        return request
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    from .dimension_scopes import retained_scopes
    entries = []
    history, refs = source_references(turn.state, retained_scopes(turn.state))
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
    return request


def native_records(receipt, capture_ref):
    """Expose the native scope to existing follow-up callers; a committed receipt owns its confirmation."""
    state = (receipt.get('data') or {}).get('conditionState', {})
    records = []
    for entry in state.get('entries', []):
        key = entry['id']
        source = {'scopeId': key, 'executionId': receipt['executionId'], 'querySessionId': receipt['querySessionId'],
                  'contextId': receipt['context']['contextId']}
        records.append({'conditionId': key, 'confirmedTurn': entry['lastConfirmedTurn'], 'receiptTurn': state['turn'],
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
        refreshed.append((ref, renewed, [row for row in renewed if row['conditionId'] in actual]))
    for ref, history, actual in refreshed:
        native[ref] = history
        state.setdefault('dimensionScopeCaptures', {})[ref] = actual
    if refreshed:
        save_context(state, state['lastCheck']['status'])


def effective_request(request, data):
    """Read the executed scope and verify that only authorized omissions occurred."""
    actual = data.get('effectiveRequest')
    if actual is None:
        if (request.get('conditionContext') or {}).get('omitted'):
            raise AnalysisError('EFFECTIVE_SCOPE_MISSING', '已放宽的查询缺少实际执行条件，不能恢复原请求代替。')
        return deepcopy(request)
    expected = apply_omissions(request)
    # Transport IDs belong to the envelope; native projection contains business input.
    fields = ('purpose', 'selection', 'question', 'scope', 'groupBy', 'contextText', 'intentDraft', 'basis', 'conditionContext')
    if any(actual.get(key) != expected.get(key) for key in fields):
        raise AnalysisError('EFFECTIVE_SCOPE_MISMATCH', '实际执行条件与已冻结的条件处理结果不一致。')
    return {**deepcopy(request), **deepcopy(actual)}


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
