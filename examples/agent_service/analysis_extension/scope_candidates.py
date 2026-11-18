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
        # Preserve transport parameters from this call and all business choices from the
        # executed branch. In particular a previous relaxation stays local to this retry.
        for field in ('purpose', 'selection', 'question', 'scope', 'groupBy', 'basis', 'contextText', 'intentDraft'):
            result.pop(field, None)
            if field in frozen:
                result[field] = deepcopy(frozen[field])
        prior = result.setdefault('scope', {}).get('bindings', [])
        chosen = [item['binding'] for item in selected]
        result['scope']['bindings'] = [old for old in prior if not any(
            (old.get('targetId'), old.get('modelCode'), old.get('text')) ==
            (new.get('targetId'), new['modelCode'], new['text']) for new in chosen)] + chosen
        _withdraw_unverified_role(turn.state, result, chosen, receipt['data'])
    else:
        result['scope']['bindings'] = [item['binding'] for item in selected]
    return result


def _withdraw_unverified_role(state, request, choices, data):
    """A selected term still has its business meaning when its provisional dimension role is withdrawn."""
    from .dimension_scopes import concept, STANDARD_FIELDS
    from .condition_context import SYSTEM_DIMENSIONS
    from .business_tables import DEFAULT_LABELS
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
    protected = set(STANDARD_FIELDS) | {concept(name) for code in SYSTEM_DIMENSIONS for name in (code, DEFAULT_LABELS[code])}
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
        if not isinstance(binding, dict) or set(binding) != {'text', 'candidateRef'}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '范围绑定只填写 text 原话和候选返回的 candidateRef。')
        match = re.fullmatch(r'scand:(cap_[0-9a-f]+):([0-9]+)(?::(cap_[0-9a-f]+):([0-9]+))?', str(binding.get('candidateRef', '')))
        if match is None:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '候选引用格式无效，先使用 search_entities 搜索范围原话。')
        capture = turn.service.store.get_capture(turn.service.owner, match[1])
        if capture.get('originalToolName') != 'search_entities' or any(
                capture.get(key) != origin.get(key) for key in ('resourceId', 'serverName', 'connectionFingerprint')):
            raise AnalysisError('SCOPE_CANDIDATE_SOURCE_MISMATCH', '候选不属于当前预算工具连接。')
        normalize = lambda value: unicodedata.normalize('NFKC', str(value or '')).strip()
        if normalize(binding.get('text')) != normalize(capture.get('request', {}).get('entityMention')):
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '绑定原话须与候选搜索原话一致；使用用户范围原话搜索后选择。')
        adapter = turn.service.sources.for_capture(capture)
        receipt = adapter.raw_receipt(capture['response'])
        available = candidates(receipt, capture)
        if f'scand:{match[1]}:{match[2]}' not in {item['candidateRef'] for item in available}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '所选引用不是已核验的任务或表样候选。')
        candidate = receipt['data']['candidates'][int(match[2])]
        resolved.append({'binding': {'text': binding['text'], 'modelCode': candidate['modelCode'], 'executionId': receipt['executionId'],
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
                      if g['targetIndex'] == int(match[4]) and binding['text'] in g['searchTerms']]
            if len(owners) != 1 or failed.get('context', {}).get('contextId') != receipt['context']['contextId'] or failed.get('querySessionId') != receipt['querySessionId']:
                raise AnalysisError('INVALID_SCOPE_CANDIDATE', '该原词不属于引用回执中同一上下文的未定位目标。')
            resolved[-1]['binding'].update(targetId=owners[0], recoveryExecutionId=failed['executionId'])
            resolved[-1]['recoveryRef'] = match[3]
    return resolved


def inherit(scope, previous, changed):
    """A selected task/form and its original quotation survive or clear together."""
    for model, role, fields in (('BUDGET_TASK', 'task', ('taskCodes', 'taskNames')),
                               ('BUDGET_FORM_TEMPLATE', 'metric', ('formCodes', 'formNames'))):
        prior = [item for item in previous.get('bindings', []) if item.get('modelCode') == model and not item.get('targetId')]
        if not prior or role in changed:
            continue
        current = [item for item in scope.get('bindings', []) if item.get('modelCode') == model]
        if current:
            if role == 'task' and current != prior:
                raise AnalysisError('FOLLOWUP_SCOPE_CONFLICT', '关联追问改变了未声明变化的任务绑定。')
            continue
        if any(scope.get(field) and scope.get(field) != previous.get(field) for field in fields):
            continue
        scope.setdefault('bindings', []).extend(deepcopy(prior))
