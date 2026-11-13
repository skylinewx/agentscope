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
    if result_set:
        sets = [item for item in sets if item.get('resultSetId') == result_set]
    elif selection and sets:
        origins = {item['originId'] for item in receipt.get('data', {}).get('origins', [])
                   if all(selection.get(key) is None or item.get(key) == selection[key]
                          for key in ('stepId', 'batchId'))
                   and (selection.get('columnNames') is None or item.get('sourceColumn') in selection['columnNames'])
                   and (selection.get('rowIndices') is None or item.get('rowIndex') in selection['rowIndices'])}
        sets = [item for item in sets if any(origins.intersection(row.get('originRefs', []))
                                           for row in item.get('rows', []))]
    return sets


def original_request(capture, receipt, selection=None, result_set=None):
    """A FACTS view retains the business request, never its transient value filter."""
    proof = receipt.get('data', {}).get('sourceBasis') or {}
    archived = proof.get('originalRequest') if proof.get('operation') == 'IMMUTABLE_FACT_VIEW' else None
    result = deepcopy(archived or capture.get('request') or {})
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
        for selection, capture, table, _ in _inherited_tables(turn, _selectors(turn)):
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


def restore_targets(request, previous, changed, released=()):
    """Fill omitted, confirmed target/condition structure; never copy value filters.

    The retained evidence is already role-filtered by followup_scope. This is a
    structural reuse, not an inference about the meaning of a new question.
    """
    prior = previous.get('intentDraft') or {}
    draft = request.setdefault('intentDraft', {})
    evidence = request.get('question', '') + ' ' + request.get('contextText', '')
    # Shared conditions are independent of whether the model repeats the target.
    # Retained evidence has already released changed dimensions/scenarios. An
    # explicit list (including []) wins, and value filters are never inherited.
    if 'sharedConditions' not in draft:
        draft['sharedConditions'] = [deepcopy(phrase) for phrase in prior.get('sharedConditions', [])
                                      if phrase.get('text') and phrase['text'] in evidence and phrase['text'] not in released]
    if 'metric' in changed or draft.get('targets'):
        return
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
