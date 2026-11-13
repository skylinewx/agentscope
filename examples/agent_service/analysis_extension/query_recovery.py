"""Actionable target-discovery recovery; never reinterpret valid empty results."""
from copy import deepcopy


def target_recovery(receipt, request):
    data = receipt.get('data') or {}
    issues = [receipt.get('error') or {}, *data.get('unresolvedGroups', [])]
    codes = {item.get('code') for item in issues}
    codes.update((data.get('failureSummary') or {}).get('errorCounts', {}))
    if not codes.intersection({'BUDGET_TARGET_UNRESOLVED', 'TARGET_DISCOVERY_UNRESOLVED'}):
        return None
    # Successful null/empty results need no candidate substitution. For a partial
    # query, keep all successful groups and repair only the unresolved target.
    terms = list(dict.fromkeys(phrase['text'] for target in (request.get('intentDraft') or {}).get('targets', [])
                              for phrase in target.get('businessPhrases', []) if phrase.get('text')))
    return {'action': 'SEARCH_CANDIDATES', 'sourceTerms': terms,
            'searchArguments': {key: deepcopy(request[key]) for key in ('contractVersion', 'contextId', 'querySessionId') if key in request},
            'next': '从未定位的目标原话选择 entityMention 调用 search_entities；结合上下文从候选选取，再用 scope.bindings 的 text+candidateRef 绑定。'
                    '保留原指标、成员、时期、情景和 selection；候选是表样不代表用户要求整表。alternatives 只接受原词的完整分词，不接受同义词替换。',
            'scenarioRule': '非预算情景先由本体尝试预算格定位后切换到原目标情景取数。若仍未定位，再搜索候选；成功返回空值则接受空值，不改用预算金额。'}
