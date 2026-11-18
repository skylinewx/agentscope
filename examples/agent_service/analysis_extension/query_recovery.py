"""Actionable target-discovery recovery; never reinterpret valid empty results."""
from copy import deepcopy


def target_recovery(receipt, request):
    data = receipt.get('data') or {}
    if receipt.get('status') not in ('FAILED', 'PARTIAL'):
        return None
    issues = [receipt.get('error') or {}, *data.get('unresolvedGroups', [])]
    codes = {item.get('code') for item in issues}
    codes.update((data.get('failureSummary') or {}).get('errorCounts', {}))
    if not codes.intersection({'BUDGET_TARGET_UNRESOLVED', 'TARGET_DISCOVERY_UNRESOLVED'}):
        return None
    # Successful null/empty results need no candidate substitution. For a partial
    # query, keep all successful groups and repair only the unresolved target.
    from .condition_context import effective_request
    actual = effective_request(request, data)
    draft = actual.get('intentDraft') or {}
    targets = draft.get('targets', [])
    unresolved = {d['logicalTargetId']: d for issue in issues if issue.get('code') == 'TARGET_DISCOVERY_UNRESOLVED'
                  for d in [issue.get('diagnostics') or {}] if d.get('logicalTargetId')}
    groups = []
    for index, target in enumerate(targets):
        target_id = target.get('id')
        if target_id not in unresolved and (receipt['status'] == 'PARTIAL' or unresolved):
            continue
        diagnostic = unresolved.get(target_id, {})
        if 'searchTerms' in diagnostic:
            terms = diagnostic['searchTerms']
        else:
            # Older receipts lack the native list. Only trace explicit ownership in their executed draft.
            phrases = [*draft.get('sharedConditions', []), *target.get('businessPhrases', [])]
            for reference in target.get('inheritFrom', []):
                parent, _, word = reference.partition(':')
                phrases.extend(p for t in targets if t.get('id') == parent
                               for p in t.get('businessPhrases', []) if p.get('text') == word)
            words = [p['text'] for p in phrases if p.get('text')]
            terms = [t for t in diagnostic.get('unresolvedTitleTerms', []) if t and any(t in word for word in words)] or words
        if terms:
            groups.append({'logicalTargetId': target_id, 'targetIndex': index, 'searchTerms': list(dict.fromkeys(terms))})
    return {'action': 'SEARCH_CANDIDATES', 'targets': groups,
            'sourceTerms': list(dict.fromkeys(term for group in groups for term in group['searchTerms'])),
            'searchArguments': {key: deepcopy(request[key]) for key in ('contractVersion', 'contextId', 'querySessionId') if key in request},
            'next': '优先使用随本次回执返回的扩大检索候选。需继续检索时，用目标原词作 entityMention 调用 search_entities，省略未经元数据确认的 modelKinds。结合上下文选择候选，再用 scope.bindings 的 text+candidateRef 绑定。'
                    '保留原指标、成员、时期、情景和 selection；候选是表样不代表用户要求整表。alternatives 只接受原词的完整分词，不接受同义词替换。',
            'scenarioRule': '非预算情景先由本体尝试预算格定位后切换到原目标情景取数。若仍未定位，再搜索候选；成功返回空值则接受空值，不改用预算金额。'}


async def expand_targets(turn, references, runner):
    """Recall candidates for unresolved quotes on the same authorized connection and snapshot."""
    from .actions import source_identity
    from .business_tables import model_text
    from .models import AnalysisError
    from .scope_candidates import candidates
    sections, searches = [], {}
    store, owner = turn.service.store, turn.service.owner
    for ref in dict.fromkeys(references):
        capture = store.get_capture(owner, ref)
        receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        recovery = target_recovery(receipt, capture['request'])
        if not recovery:
            continue
        for group, term in ((group, term) for group in recovery['targets'] for term in group['searchTerms']):
            args = {**recovery['searchArguments'], 'entityMention': term}
            key = (source_identity(capture), args.get('contextId'), args.get('querySessionId'), term)
            if key not in searches:
                try:
                    searches[key] = await runner(turn, capture, 'search_entities', args)
                except AnalysisError as failure:
                    searches[key] = ([], failure.as_dict())
            refs, error = searches[key]
            if error or not refs:
                sections.append(f'“{term}”的扩大检索未完成（{(error or {}).get("code", "SOURCE_RECEIPT_MISSING")}）。可用同一原词继续检索。')
                continue
            result = store.get_capture(owner, refs[-1])
            search = turn.service.sources.for_capture(result).raw_receipt(result['response'])
            choices = candidates(search, result)
            items = search.get('data', {}).get('candidates', [])
            if choices:
                for choice in choices:
                    choice['candidateRef'] += f":{ref}:{group['targetIndex']}"
                sections.append(f"目标 {group['logicalTargetId']} 的“{term}”扩大检索候选（原查询 {ref}）\n" + model_text({
                    'viewType': 'scope_candidates', 'scopeCandidates': choices, 'captureRef': refs[-1],
                    'note': f'候选属于上述目标。选择后宿主复用原查询条件；已有结果可从原回执读取。'}))
            else:
                sections.append(f'“{term}”的扩大检索返回 {len(items)} 个候选，本页没有可绑定的任务或表样。'
                                f'可用 QueryResultRead 读取 {refs[-1]} 的 /data/candidates，结合业务含义继续定位。')
    return '\n\n'.join(sections)
