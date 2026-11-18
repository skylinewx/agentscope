"""Selected result provenance, shared by request inheritance and evidence binding."""
from copy import deepcopy


def scope_phrase_terms(selector):
    """Business values whose dimension is established by a native scope binding."""
    from .dimension_scopes import selector_evidence
    def values(node):
        value = node.get('value')
        direct = [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
        return direct + [text for child in node.get('children', []) for text in values(child)]
    return selector_evidence(selector) + values(selector.get('where') or {})


def selected_result_sets(receipt, selection=None, result_set=None):
    """Resolve selected native identities once for targets and scope provenance."""
    sets = receipt.get('data', {}).get('resultSets', [])
    result_set = result_set or (selection or {}).get('resultSetId')
    if result_set:
        sets = [item for item in sets if item.get('resultSetId') == result_set]
    elif selection and sets:
        # Native exact-cell results live in cellResults, not the empty legacy
        # rows array. Their origin carries targetRef instead of rowIndex.
        from .adapters.budget_batch import tables
        exact_indices = {(step, table['batchId'], row.get('targetRef')): index
                         for step, table, _ in tables(receipt)
                         for index, row in enumerate(table.get('data', {}).get('cellResults', []))}
        def row_index(origin):
            return origin.get('rowIndex') if 'rowIndex' in origin else exact_indices.get(
                (origin.get('stepId'), origin.get('batchId'), origin.get('targetRef')))
        origins = {item['originId'] for item in receipt.get('data', {}).get('origins', [])
                   if all(selection.get(key) is None or item.get(key) == selection[key]
                          for key in ('stepId', 'batchId'))
                   and (selection.get('columnNames') is None or item.get('sourceColumn') in selection['columnNames'])
                   and (selection.get('rowIndices') is None or row_index(item) in selection['rowIndices'])}
        sets = [item for item in sets if any(origins.intersection(row.get('originRefs', []))
                                           for row in item.get('rows', []))]
    return sets


def original_request(capture, receipt, selection=None, result_set=None):
    """A FACTS view retains the business request, never its transient value filter."""
    proof = receipt.get('data', {}).get('sourceBasis') or {}
    archived = proof.get('originalRequest') if proof.get('operation') == 'IMMUTABLE_FACT_VIEW' else None
    from .condition_context import effective_request
    result = (deepcopy(archived) if archived else
              effective_request(capture.get('request') or {}, receipt.get('data') or {}))
    if archived:
        draft = result.get('intentDraft') or {}
        draft.pop('valueFilter', None)
        draft.pop('presentation', None)
    # A selected column/result set can represent only one original business target.
    # Resolve its identity through native origins, never labels or array positions.
    sets = selected_result_sets(receipt, selection, result_set)
    if archived or selection or result_set:
        ids = {str(item.get('targetId', '')).split(':', 1)[0] for item in sets}
        draft = result.get('intentDraft') or {}
        if draft.get('targets') and receipt.get('data', {}).get('resultSets'):
            draft['targets'] = [target for target in draft['targets'] if target['id'] in ids]
    return result


def scope_terms(turn, origin, request, source_ref=None):
    """Authorize only explicitly reused objects from the selected source(s).

    A returned canonical name is evidence for that object, not a new session
    default. Merely mentioning an object in an unrelated old answer grants none.
    """
    scope = request.get('scope') or {}
    if not any(scope.get(field) for field in ('taskNames', 'formNames', 'taskCodes', 'formCodes', 'bindings')):
        return []
    from .followup_scope import _inherited_tables, _selectors, _same_source
    contexts = []
    if source_ref:
        source = turn.service.store.get_source_view(turn.service.owner, source_ref)
        if not all(source['sourceIdentity'].get(key) == origin.get(key) for key in source['sourceIdentity']):
            from .models import AnalysisError
            raise AnalysisError('SOURCE_CONNECTION_MISMATCH', '来源引用不属于本次预算工具连接。')
        contexts = source.get('contexts', [])
    elif turn.state.get('followupContext', {}).get('mode', 'CONTINUE') == 'CONTINUE':
        selections = _selectors(turn)
        for selection in selections:
            if 'resultSetId' not in selection:
                continue
            capture = turn.service.store.get_capture(turn.service.owner, selection['captureRef'])
            if not _same_source(capture, origin):
                continue
            from .empty_sources import result
            _, item = result(capture, selection)
            contexts.append(item.get('context', {}))
        for selection, capture, table, _ in _inherited_tables(turn, selections):
            if not _same_source(capture, origin):
                continue
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            if receipt.get('data', {}).get('resultSets'):
                # Exact-cell tables need not repeat business names on their
                # technical metrics. Both normal continuation and sourceRef use
                # the same native result-set provenance, including selected rows.
                contexts.extend(item.get('context', {}) for item in selected_result_sets(receipt, selection))
                continue
            columns = (selection or {}).get('columnNames')
            contexts.extend(metric.get('context', metric.get('formContext', {}))
                            for metric in (table or {}).get('metrics', [])
                            if columns is None or metric.get('name') in columns)
    terms = []
    for field, context_field in (('taskNames', 'taskName'), ('formNames', 'formName')):
        known = {context.get(context_field) for context in contexts}
        terms.extend(name for name in scope.get(field, []) if name in known)
    for field, context_field in (('taskCodes', 'taskCode'), ('formCodes', 'formCode')):
        known = {context.get('target', {}).get(context_field) for context in contexts}
        terms.extend(code for code in scope.get(field, []) if code in known)
    from .scope_candidates import resolve
    for item in resolve(turn, origin, scope.get('bindings', [])):
        binding = item['binding']
        field = 'taskCode' if binding['modelCode'] == 'BUDGET_TASK' else 'formCode'
        if item['code'] in {context.get('target', {}).get(field) for context in contexts}:
            terms.append(binding['text'])
    return list(dict.fromkeys(terms))


def restore_shared_conditions(request, previous, released=(), scenario_terms=()):
    """Reuse only conditions common to every explicitly selected request.

    Different result targets do not imply different shared conditions. Preserve
    their intersection without choosing a target, form, or a latest source.
    """
    draft = request.setdefault('intentDraft', {})
    if not previous:
        return
    groups = []
    for prior in previous:
        old = prior.get('intentDraft') or {}
        phrases = {p['text']: p for p in old.get('sharedConditions', []) if p.get('text')}
        targets = old.get('targets', [])
        for scenario in scenario_terms:
            def contains(items):
                return any(p.get('text') == scenario or any(scenario in parts for parts in p.get('alternatives', [])) for p in items)
            if contains(old.get('sharedConditions', [])) or targets and all(contains(t.get('businessPhrases', [])) for t in targets):
                phrases.setdefault(scenario, {'text': scenario, 'sourceText': scenario})
        groups.append(phrases)
    common = set.intersection(*(set(group) for group in groups))
    evidence = request.get('question', '') + ' ' + request.get('contextText', '')
    current = draft.setdefault('sharedConditions', [])
    represented = {p.get('text') for p in current} | {
        p.get('text') for target in draft.get('targets', []) for p in target.get('businessPhrases', [])}
    for value, phrase in groups[0].items():
        if value in common and value in evidence and value not in released and value not in represented:
            current.append(deepcopy(phrase))
            represented.add(value)


def restore_targets(request, previous, changed, released=()):
    """Fill omitted, confirmed target/condition structure; never copy value filters.

    The retained evidence is already role-filtered by followup_scope. This is a
    structural reuse, not an inference about the meaning of a new question.
    """
    prior = previous.get('intentDraft') or {}
    draft = request.setdefault('intentDraft', {})
    # Shared conditions are independent of whether the model repeats the target.
    # Changed dimensions were released before this projection; filters never copy.
    restore_shared_conditions(request, [previous], released)
    if 'metric' in changed or draft.get('targets'):
        return
    evidence = request.get('question', '') + ' ' + request.get('contextText', '')
    targets = deepcopy(prior.get('targets') or [])
    if not targets:
        return
    for target in targets:
        target['businessPhrases'] = [phrase for phrase in target.get('businessPhrases', [])
                                     if phrase.get('text') and phrase['text'] in evidence and phrase['text'] not in released]
        target.pop('inheritFrom', None)
        if 'period' in changed:
            target.pop('periodText', None)
        if not target['businessPhrases']:
            return  # Leave ambiguous/new business targets to the model.
    draft['targets'] = targets


def fact_draft(request):
    """Discard a single empty target placeholder, not substantive conditions.

    A sourceRef already selects one actual metric. Its sole empty local alias
    can therefore be removed from the result predicate without choosing a target.
    """
    result = deepcopy(request)
    draft = result.get('intentDraft') or {}
    targets = draft.get('targets') or []
    if len(targets) != 1 or set(targets[0]) - {'id', 'businessPhrases'} or targets[0].get('businessPhrases'):
        return result
    alias = targets[0].get('id')
    def references(node):
        return ([node['targetId']] if node.get('targetId') else []) + [
            value for child in node.get('children', []) for value in references(child)]
    predicate = draft.get('valueFilter') or {}
    if any(value != alias for value in references(predicate)):
        return result
    def remove_alias(node):
        node.pop('targetId', None)
        for child in node.get('children', []):
            remove_alias(child)
    remove_alias(predicate)
    draft['targets'] = []
    return result
