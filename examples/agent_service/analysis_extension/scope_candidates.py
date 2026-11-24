"""Select captured task/form candidates; models never manufacture execution proofs."""
from copy import deepcopy
import re
import unicodedata

from .models import AnalysisError

MODELS = {'BUDGET_TASK': '任务', 'BUDGET_FORM_TEMPLATE': '表样'}


def candidates(receipt, capture):
    if capture.get('originalToolName') != 'search_entities' or receipt.get('status') != 'SUCCEEDED':
        return []
    result = []
    for index, item in enumerate(receipt.get('data', {}).get('candidates', [])):
        if (item.get('modelCode') not in MODELS or item.get('kind') != 'RESOURCE'
                or 'RESOURCE_RDF_VERIFIED' not in item.get('matchedBy', [])):
            continue
        result.append({'candidateRef': f"scand:{capture['captureRef']}:{index}",
                       'kind': MODELS[item['modelCode']], 'name': item.get('label'),
                       'key': item.get('key'), 'matchedBy': item.get('matchedBy', []),
                       'searchText': capture.get('request', {}).get('entityMention')})
    return result


def bind(turn, origin, request):
    bindings = request.get('scope', {}).get('bindings')
    if not bindings:
        return request
    result = deepcopy(request)
    selected = resolve(turn, origin, bindings)
    recoveries = {item['recoveryRef'] for item in selected if 'recoveryRef' in item}
    if recoveries:
        if len(recoveries) != 1:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '一次重试请选择来自同一查询回执的候选。')
        capture = turn.service.store.get_capture(turn.service.owner, next(iter(recoveries)))
        receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        from .condition_context import effective_request
        frozen = effective_request(capture['request'], receipt['data'])
        if (frozen.get('question') != turn.state.get('request')
                or (frozen.get('conditionContext') or {}).get('turn') != turn.state.get('conditionTurn')):
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '该目标候选属于另一次查询，请使用本轮的扩大检索回执。')
        aliases = _check_recovery_target_phrases(frozen, request)
        # Start from the branch actually executed, including any local relaxation.
        for field in ('purpose', 'selection', 'question', 'scope', 'groupBy', 'basis', 'contextText', 'intentDraft'):
            result.pop(field, None)
            if field in frozen:
                result[field] = deepcopy(frozen[field])
        prior = result.setdefault('scope', {}).get('bindings', [])
        chosen = [item['binding'] for item in selected]
        result['scope']['bindings'] = [old for old in prior if not any(
            (old.get('targetId'), old.get('modelCode'), old.get('text')) ==
            (new.get('targetId'), new['modelCode'], new['text']) for new in chosen)] + chosen
        _correct_recovery_organization(result, request)
        _apply_recovery_conditions(result, request, aliases, capture['request'])
        _withdraw_unverified_role(turn.state, result, chosen, receipt['data'])
    else:
        result['scope']['bindings'] = [item['binding'] for item in selected]
    return result


def _check_recovery_target_phrases(frozen, proposed):
    """A recovery reference authorizes its executed target. Do not silently discard a new metric."""
    proposed_targets = (proposed.get('intentDraft') or {}).get('targets') or []

    def phrases(items):
        return {unicodedata.normalize('NFKC', phrase['text']).strip()
                for phrase in items
                if isinstance(phrase, dict) and isinstance(phrase.get('text'), str) and phrase['text'].strip()}

    draft = frozen.get('intentDraft') or {}
    targets = draft.get('targets', [])
    shared = phrases(draft.get('sharedConditions', []))
    prior = {target['id']: phrases(target.get('businessPhrases', [])) for target in targets}
    effective = {}
    for target in targets:
        words = prior[target['id']] | shared
        for ref in target.get('inheritFrom', []):
            parent, _, word = ref.partition(':')
            words.update(prior.get(parent, set()) & {unicodedata.normalize('NFKC', word).strip()})
        effective[target['id']] = words
    aliases = {key: key for key in prior}
    for target in proposed_targets:
        target_id = target.get('id')
        specified = phrases(target.get('businessPhrases', []))
        original = effective.get(target_id)
        if original is None:
            matches = [key for key, terms in effective.items() if specified <= terms]
            if matches:
                if len(matches) == 1:
                    aliases[target_id] = matches[0]
                continue
        original = original or set()
        if specified - original:
            raise AnalysisError('RECOVERY_TARGET_CHANGED',
                '这次选择的候选绑定到上次执行的取数目标，同时提交的新目标词无法随该候选生效。'
                '要改目标，请移除旧的 scope.bindings，按本轮原话提交修正后的 intentDraft；'
                '如要沿用原目标，请仅提交候选绑定。',
                {'path': '/intentDraft/targets', 'targetId': target_id,
                 'executedPhrases': sorted(original)[:6], 'proposedPhrases': sorted(specified)[:6]})
    return aliases


def _apply_recovery_conditions(branch, proposed, aliases, prior):
    """Reuse a located resource while executing the explicitly amended query against it."""
    from .value_predicates import validate

    patch = deepcopy(proposed.get('intentDraft') or {})
    draft = branch['intentDraft']
    targets = {t['id']: t for t in draft['targets']}
    if 'groupBy' in proposed:
        branch['groupBy'] = deepcopy(proposed['groupBy'])
    if patch.get('valueFilter') is not None:
        draft['valueFilter'] = validate(patch['valueFilter'], set(targets), aliases=aliases)
    if patch.get('presentation'):
        draft.setdefault('presentation', {}).update(patch['presentation'])
    _apply_recovery_periods(targets, aliases, branch['scope'], proposed.get('scope') or {}, patch.get('targets', []))
    if patch.get('sharedConditions') or patch.get('dimensionScopes'):
        _apply_recovery_scopes(branch, patch, aliases, targets, prior)


def _apply_recovery_periods(targets, aliases, scope, supplied, proposals):
    from .budget_period_selection import annual_caption_year

    periods = {key: supplied[key] for key in ('periodCode', 'periodRequest') if supplied.get(key)}
    changed_scope = bool(periods) and periods != {key: scope[key] for key in ('periodCode', 'periodRequest') if scope.get(key)}
    captions = {}
    for target in proposals:
        if target.get('periodText'):
            actual = aliases.get(target['id'])
            if actual not in targets:
                raise AnalysisError('RECOVERY_TARGET_AMBIGUOUS', '时期须注明原查询中的目标。', {'targetIds': list(targets)})
            old_year = annual_caption_year(targets[actual].get('periodText'))
            if not changed_scope and old_year and annual_caption_year(target['periodText']) == old_year:
                # Keep the confirmed source wording when both spellings denote exactly the same year.
                continue
            captions[actual] = target['periodText']
    if changed_scope:
        for key in ('periodCode', 'periodRequest'):
            scope.pop(key, None)
        scope.update(periods)
        for target in targets.values():
            target.pop('periodText', None)
    elif not periods and any(targets[key].get('periodText') != text for key, text in captions.items()):
        if any(scope.get(key) for key in ('periodCode','periodRequest')):
            if any(not t.get('periodText') and key not in captions for key, t in targets.items()):
                raise AnalysisError('RECOVERY_PERIOD_AMBIGUOUS', '原查询有多个目标共用时期。请提交本次共用的 scope.periodRequest，或分别填写各目标的 periodText。')
            for key in ('periodCode', 'periodRequest'):
                scope.pop(key, None)
    for key, text in captions.items():
        targets[key]['periodText'] = text


def _apply_recovery_scopes(branch, patch, aliases, targets, prior):
    from .dimension_scopes import concept
    from .language_evidence import bind as bind_evidence
    from .query_lineage import scope_phrase_terms

    draft = branch['intentDraft']
    confirmed_text = (branch.get('question', ''), branch.get('contextText', ''))
    def terms(query):
        original = query.get('intentDraft') or {}
        words = {p['text'] for p in original.get('sharedConditions', [])}
        words.update(p['text'] for t in original.get('targets', []) for p in t.get('businessPhrases', []))
        words.update(word for s in original.get('dimensionScopes', []) for word in scope_phrase_terms(s['selector']))
        return words
    removed = terms(prior) - terms(branch)
    requested = {p['text'] for p in patch.get('sharedConditions', [])}
    requested.update(word for s in patch.get('dimensionScopes', []) for word in scope_phrase_terms(s['selector']))
    obsolete = sorted(word for word in requested & removed if word not in confirmed_text[0])
    if obsolete:
        raise AnalysisError('RECOVERY_CONDITION_UNCONFIRMED', '所选候选来自已放宽的查询。请保留本轮条件，移除回填的历史条件。', {'terms': obsolete})
    shared = {p['text']: p for p in draft.get('sharedConditions', [])}
    additions = []
    for phrase in patch.get('sharedConditions', []):
        if phrase['text'] in shared:
            if 'alternatives' in phrase:
                shared[phrase['text']]['alternatives'] = phrase['alternatives']
            continue
        additions.append(phrase)
        shared[phrase['text']] = phrase
    if additions:
        draft['sharedConditions'] = list(shared.values())
    scopes = draft.get('dimensionScopes', [])
    changed = []
    for item in patch.get('dimensionScopes', []):
        hint = concept(item['dimensionHint'])
        if hint == '组织机构':
            continue  # The named-organization correction preserves the verified target scope.
        owners = sorted({aliases.get(key, key) for key in item.get('targetIds', [])})
        if not set(owners) <= targets.keys():
            raise AnalysisError('RECOVERY_TARGET_AMBIGUOUS', '维度条件须引用原查询中的目标。', {'targetIds': list(targets)})
        matches = [s for s in scopes if concept(s['dimensionHint']) == hint and sorted(s.get('targetIds') or []) == owners]
        if any(s['selector'] == item['selector'] for s in matches):
            continue
        if len(matches) > 1:
            raise AnalysisError('RECOVERY_SCOPE_AMBIGUOUS', '存在多个同维度范围，请明确要修正的目标。', {'dimensionHint': hint})
        if matches:
            item['id'] = matches[0]['id']
            scopes.remove(matches[0])
        if any(s['id'] == item['id'] for s in scopes):
            raise AnalysisError('DIMENSION_SCOPE_CONFLICT', '不同的维度条件须使用不同的范围标识。')
        if owners:
            item['targetIds'] = owners
        scopes.append(item)
        changed.append(item)
    if changed:
        draft['dimensionScopes'] = scopes
    if additions or changed:
        bind_evidence(None, {'question': confirmed_text[0], 'contextText': confirmed_text[1],
                              'intentDraft': {'sharedConditions': additions, 'dimensionScopes': changed}})


def _correct_recovery_organization(branch, proposed):
    """Apply one current, shared organization change to the actual failed branch."""
    from .dimension_scopes import concept, current_organization
    if (branch.get('basis') or {}).get('kind', 'INTENT') != 'INTENT':
        return
    question = branch.get('question', '')
    scoped = deepcopy((proposed.get('intentDraft') or {}).get('dimensionScopes', []))
    changed = [s for s in scoped if current_organization(s, question)]
    draft = branch.get('intentDraft') or {}
    previous = [s for s in draft.get('dimensionScopes', []) if concept(s.get('dimensionHint')) == '组织机构']
    if len(changed) != 1 or len(previous) > 1 or any(s.get('targetIds') or s['selector'].get('where') for s in previous):
        return
    current = changed[0]
    current['sourceText'] = question
    if previous:
        current['id'] = previous[0]['id']
    others = [s for s in draft.get('dimensionScopes', []) if s not in previous]
    if any(s['id'] == current['id'] for s in others):
        raise AnalysisError('DIMENSION_SCOPE_CONFLICT', '本轮机构范围与另一维度使用了相同的范围标识。')
    draft['dimensionScopes'] = [*others, current]


def _withdraw_unverified_role(state, request, choices, data):
    """A selected term still has its business meaning when its provisional dimension role is withdrawn."""
    from .dimension_scopes import concept, STANDARD_FIELDS
    from .budget_dimensions import SYSTEM_DIMENSION_LABELS
    cleared = {concept(edit['dimensionHint']) for edit in state.get('scopeEdits', []) if edit.get('selector') is None}
    draft = request.get('intentDraft') or {}
    if not cleared or not draft.get('dimensionScopes'):
        return
    owners = {}
    for choice in choices:
        if choice.get('targetId'):
            owners.setdefault(choice['text'], set()).add(choice['targetId'])
    targets = {target['id'] for target in draft.get('targets', [])}
    verified = {row.get('id', row.get('scopeId')) for row in data.get('scopeResolution', []) if row.get('status') == 'RESOLVED'}
    protected = set(STANDARD_FIELDS) | {concept(name) for pair in SYSTEM_DIMENSION_LABELS.items() for name in pair}
    evidence = unicodedata.normalize('NFKC', request['question'] + ' ' + (request.get('contextText') or '')).casefold()
    remaining = []
    for scope in draft['dimensionScopes']:
        hint = concept(scope['dimensionHint'])
        selector = scope.get('selector') or {}
        members = selector.get('members') or []
        applies_to = set(scope.get('targetIds') or targets)
        # Only a withdrawn, unverified singleton can be reinterpreted. A common
        # filter still belongs to every sibling not covered by the same choice.
        retract = (hint in cleared and hint not in protected and hint not in evidence and not scope.get('confirmedSource')
                   and scope.get('id') not in verified and len(members) == 1
                   and selector == {'type': 'VALUES', 'members': members}
                   and applies_to and applies_to <= owners.get(members[0], set()))
        if not retract:
            remaining.append(scope)
    draft['dimensionScopes'] = remaining


def resolve(turn, origin, bindings):
    """Resolve public references once under the capture owner and connection boundary."""
    resolved = []
    for binding in bindings:
        if not isinstance(binding, dict) or 'candidateRef' not in binding or set(binding) - {'text', 'candidateRef'}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '选择返回的 candidateRef。需要指定不同的用户原词时再填 text。')
        match = re.fullmatch(r'scand:(cap_[0-9a-f]+):([0-9]+)(?::(cap_[0-9a-f]+):([0-9]+))?', str(binding.get('candidateRef', '')))
        if match is None:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '候选引用格式无效，先使用 search_entities 搜索范围原话。')
        capture = turn.service.store.get_capture(turn.service.owner, match[1])
        if capture.get('originalToolName') != 'search_entities' or any(
                capture.get(key) != origin.get(key) for key in ('resourceId', 'serverName', 'connectionFingerprint')):
            raise AnalysisError('SCOPE_CANDIDATE_SOURCE_MISMATCH', '候选不属于当前预算工具连接。')
        normalize = lambda value: unicodedata.normalize('NFKC', str(value or '')).strip()
        search_text = capture.get('request', {}).get('entityMention')
        original = binding.get('text', search_text)
        if not normalize(original):
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '候选缺少可核实的检索词，请按用户原词重新检索。')
        if normalize(original) != normalize(search_text) and original not in getattr(turn, 'state', {}).get('request', ''):
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', 'text 使用本轮原词，或省略以采用回执中的检索词。',
                                {'path': '/scope/bindings/text', 'expectedText': search_text})
        adapter = turn.service.sources.for_capture(capture)
        receipt = adapter.raw_receipt(capture['response'])
        available = candidates(receipt, capture)
        if f'scand:{match[1]}:{match[2]}' not in {item['candidateRef'] for item in available}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '所选引用不是已核验的任务或表样候选。')
        candidate = receipt['data']['candidates'][int(match[2])]
        resolved.append({'binding': {'text': original, 'modelCode': candidate['modelCode'], 'executionId': receipt['executionId'],
                                    'candidateId': candidate['candidateId'], 'contextId': receipt['context']['contextId'],
                                    'querySessionId': receipt['querySessionId']},
                         'code': candidate['key'], 'name': candidate['label']})
        if match[3]:
            source = turn.service.store.get_capture(turn.service.owner, match[3])
            if source.get('originalToolName') != 'execute_budget_query' or any(
                    source.get(k) != capture.get(k) for k in ('serverName', 'resourceId', 'connectionFingerprint')):
                raise AnalysisError('SCOPE_CANDIDATE_SOURCE_MISMATCH', '查询回执与候选来源不一致。')
            failed = turn.service.sources.for_capture(source).raw_receipt(source['response'])
            from .query_recovery import target_recovery
            recovery = target_recovery(failed, source['request']) or {}
            owners = [g['logicalTargetId'] for g in recovery.get('targets', [])
                      if g['targetIndex'] == int(match[4]) and original in g['searchTerms'] and search_text in g['searchTerms']]
            if len(owners) != 1 or failed.get('context', {}).get('contextId') != receipt['context']['contextId'] or failed.get('querySessionId') != receipt['querySessionId']:
                raise AnalysisError('INVALID_SCOPE_CANDIDATE', '该原词不属于引用回执中同一上下文的未定位目标。')
            resolved[-1]['binding'].update(targetId=owners[0], recoveryExecutionId=failed['executionId'])
            resolved[-1]['recoveryRef'] = match[3]
    return resolved


def inherit(scope, previous, changed):
    """Replay confirmed shared bindings only on an explicit same-conditions query."""
    for role, model in (('task', 'BUDGET_TASK'), ('form', 'BUDGET_FORM_TEMPLATE')):
        if role in changed or any(scope.get(role + suffix) and scope[role + suffix] != previous.get(role + suffix)
                                  for suffix in ('Codes', 'Names')):
            continue
        prior = [item for item in previous.get('bindings', [])
                 if item.get('modelCode') == model and not item.get('targetId')]
        current = [item for item in scope.get('bindings', []) if item.get('modelCode') == model]
        if prior and current and current != prior:
            raise AnalysisError('FOLLOWUP_SCOPE_CONFLICT', '重新查询使用的来源绑定与原条件不一致。若要更改任务或表样，请声明本轮变更。')
        if prior and not current:
            scope.setdefault('bindings', []).extend(deepcopy(prior))
