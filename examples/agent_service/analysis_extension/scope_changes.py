"""Derive follow-up changes from explicit intent and concrete selectors, not duplicate flags."""
from copy import deepcopy

from .dimension_scopes import STANDARD_FIELDS, _scope_aliases, concept
from .models import AnalysisError, digest


PRESENTATION_FIELDS = ('grouping', 'order')


def normalize_intent(intent):
    """A patch has three states: omitted=KEEP, value=SET, null=CLEAR.

    New-turn presentation defaults belong to merge_intent, not normalization.
    """
    if intent is None:
        return None
    from .followup_scope import INTENT_FIELDS
    if (not isinstance(intent, dict) or set(intent) - set(INTENT_FIELDS)
            or any(value is not None
                   and (not isinstance(value, str) or not value.strip() or len(value) > 1024)
                   for key, value in intent.items())):
        raise AnalysisError('INVALID_QUERY_INTENT', 'intent 省略字段表示保留，非空业务原文表示设置，null 表示明确清除；不使用空字符串。')
    return {key: value.strip() if isinstance(value, str) else None for key, value in intent.items()}


def merge_intent(state, context, intent, *, context_supplied=False):
    """One canonical intent feeds queries, anchors, reviews and conversation snapshots."""
    previous = state.get('previousQueryIntent', {})
    changes = set(context.get('changedFields', []))
    current_context = state.get('followupContext', {})
    same_base = (context['mode'] == current_context.get('mode')
                 and context.get('contextRefs', []) == current_context.get('contextRefs', []))
    current = 'queryIntent' in state and (context['mode'] == 'CONTINUE' or same_base)
    if current:
        effective = deepcopy(state['queryIntent'])
        decisions = deepcopy(state.get('intentDecisions', {}))
    else:
        # Invalidate history only when establishing the base, never the already
        # confirmed patches from the current user turn.
        effective = ({key: deepcopy(value) for key, value in previous.items()
                      if key not in changes and key not in PRESENTATION_FIELDS}
                     if context['mode'] == 'CONTINUE' else {})
        decisions = {field: {'operation': 'CLEAR', 'value': None} for field in PRESENTATION_FIELDS}
    for key, value in (intent or {}).items():
        if value is None:
            effective.pop(key, None)
        else:
            effective[key] = value
        decisions[key] = {'operation': ('INHERIT' if value == previous.get(key) else 'SET')
                          if value is not None else 'CLEAR', 'value': value}
    if effective != state.get('queryIntent') or decisions != state.get('intentDecisions'):
        state['intentRevision'] = state.get('intentRevision', 0) + 1
    state['intentDecisions'] = decisions
    return effective


def presentation_summary(state):
    """Short business explanation, backed by the same canonical intent as execution."""
    intent = state.get('queryIntent', {})
    labels = {'grouping': '分组', 'order': '排序'}
    actions = {'INHERIT': '沿用', 'SET': '采用', 'CLEAR': '取消'}
    parts = []
    for field, label in labels.items():
        decision = state.get('intentDecisions', {}).get(field)
        if decision:
            value = intent.get(field)
            parts.append('默认' + label + '：' + (actions[decision['operation']] + '“' + value + '”' if value else '本轮不' + label))
    return '；'.join(parts) + ('。机构、时期等范围见本轮有效意图及范围回执。' if parts else '')


def intent_context(state, context, intent):
    result = deepcopy(context)
    if result.get('mode', 'CONTINUE') != 'CONTINUE':
        return result
    previous = state.get('previousQueryIntent', {})
    changes = set(result.get('changedFields', []))
    for field, value in (intent or {}).items():
        if field not in previous:
            continue
        if value == previous[field]:
            changes.discard(field)
        else:
            changes.add(field)
    result['changedFields'] = sorted(changes)
    if changes:
        result['userQuote'] = state.get('request', '')
    return result


def reconcile_selectors(turn, request):
    """A concrete unchanged selector retains its proof; a replacement releases only that dimension.

    This does not decide the meaning of the user question. New names still require
    language evidence, and the existing semantic review checks whether a change
    actually answers the user. No selector is added, broadened or rewritten here.
    """
    state = turn.state
    context = deepcopy(state.get('followupContext', {}))
    if context.get('mode', 'CONTINUE') != 'CONTINUE':
        return
    changes = set(context.get('changedFields', []))
    dimensions = list(context.get('changedDimensions', []))
    fields_pending = set(state.get('basePendingChanges', []))
    dimensions_pending = list(state.get('basePendingDimensions', []))
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    records = state.get('inheritedDimensionScopes', [])
    for record in records:
        aliases = _scope_aliases(record)
        matches = [scope for scope in scopes if concept(scope.get('dimensionHint')) in aliases]
        # Ambiguous or duplicated ranges remain the responsibility of scope validation.
        if len(matches) != 1 or record.get('ambiguousScope'):
            continue
        current, old = matches[0], record['draft']
        field = next((field for dimension, field in STANDARD_FIELDS.items() if dimension in aliases), None)
        if current.get('selector') == old.get('selector'):
            changes.discard(field)
            fields_pending.discard(field)
            dimensions = [name for name in dimensions if concept(name) not in aliases]
            dimensions_pending = [name for name in dimensions_pending if concept(name) not in aliases]
        else:
            if field:
                changes.add(field)
            if not any(concept(name) in aliases for name in dimensions):
                dimensions.append(old['dimensionHint'])
            # A proof of the old selector cannot authorize its replacement.
            if current.get('confirmedSource') == record.get('confirmedSource'):
                current.pop('confirmedSource', None)
    context.update(changedFields=sorted(changes), changedDimensions=dimensions)
    if changes or dimensions:
        context['userQuote'] = state.get('request', '')
    state.update(followupContext=context, basePendingChanges=sorted(fields_pending),
                 basePendingDimensions=dimensions_pending)


def retained_selector_terms(state, request):
    """Reuse only proposed names also present in the selected, confirmed old selector.

    A hierarchy may change level while keeping its root. The old whole sentence
    must not restore a replaced period, metric, scenario or another organization.
    """
    if state.get('followupContext', {}).get('mode', 'CONTINUE') != 'CONTINUE':
        return []
    from .dimension_scopes import selector_evidence
    terms = []
    for scope in (request.get('intentDraft') or {}).get('dimensionScopes', []):
        records = [record for record in state.get('inheritedDimensionScopes', [])
                   if concept(scope.get('dimensionHint')) in _scope_aliases(record)]
        if len(records) != 1 or records[0].get('ambiguousScope') or not records[0].get('confirmedSource'):
            continue
        old = set(selector_evidence(records[0]['draft']['selector']))
        terms.extend(text for text in selector_evidence(scope.get('selector') or {}) if text in old)
    return list(dict.fromkeys(terms))


def apply_scope_edits(turn, edits):
    """One dimension selector definition serves declaration and query execution."""
    if edits is None:
        return
    if not isinstance(edits, list) or len(edits) > 32:
        raise AnalysisError('INVALID_SCOPE_EDIT', 'scopeEdits 最多包含 32 个明确的业务维度修改。')
    current = {concept(item['dimensionHint']): deepcopy(item) for item in turn.state.get('scopeEdits', [])}
    seen = set()
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {'dimensionHint', 'selector'}
                or not isinstance(edit['dimensionHint'], str) or not edit['dimensionHint'].strip()
                or edit['selector'] is not None and not isinstance(edit['selector'], dict)):
            raise AnalysisError('INVALID_SCOPE_EDIT', '维度修改使用 dimensionHint 与 selector；null 解除旧限制。')
        key = concept(edit['dimensionHint'])
        if key in seen:
            raise AnalysisError('INVALID_SCOPE_EDIT', '同一次声明不能重复修改同一维度。')
        if edit['selector'] is not None:
            _validate_selector(edit['selector'])
        seen.add(key)
        current[key] = deepcopy(edit)
    context = deepcopy(turn.state.get('followupContext', {}))
    context['changedDimensions'] = sorted(set(context.get('changedDimensions', [])) | {item['dimensionHint'] for item in edits})
    context['userQuote'] = turn.state.get('request', '')
    intent = deepcopy(turn.state.get('queryIntent', {}))
    decisions = deepcopy(turn.state.get('intentDecisions', {}))
    for edit in edits:
        field = STANDARD_FIELDS.get(concept(edit['dimensionHint']))
        if field:
            # The structured selector is authoritative. An obsolete readable
            # population/scenario/period must not survive in review or recovery.
            intent.pop(field, None)
            decisions[field] = {'operation': 'CLEAR', 'value': None}
    if edits:
        turn.state['intentRevision'] = turn.state.get('intentRevision', 0) + 1
    turn.state.update(queryIntent=intent, intentDecisions=decisions)
    turn.state.update(scopeEdits=list(current.values()), followupContext=context)


def _validate_selector(selector):
    kind = selector.get('type')
    allowed = {'VALUES': {'type', 'members'}, 'ALL': {'type'},
               'HIERARCHY': {'type', 'rootText', 'level', 'where'}, 'FILTER': {'type', 'where'}}
    if kind not in allowed or set(selector) - allowed[kind]:
        raise AnalysisError('INVALID_SCOPE_EDIT', '选择器字段与 VALUES/ALL/HIERARCHY/FILTER 类型不一致。')
    if kind == 'VALUES':
        members = selector.get('members')
        if not isinstance(members, list) or not members or any(not isinstance(v, str) or not v.strip() for v in members):
            raise AnalysisError('INVALID_SCOPE_EDIT', 'VALUES 需要非空成员原文。')
    if kind == 'HIERARCHY':
        if 'rootText' in selector and (not isinstance(selector['rootText'], str) or not selector['rootText'].strip()):
            raise AnalysisError('INVALID_SCOPE_EDIT', '层级根使用非空业务原文。')
        if not selector.get('rootText') and selector.get('level') is None:
            raise AnalysisError('INVALID_SCOPE_EDIT', '层级需要根或绝对层级。')
        if 'level' in selector and (type(selector['level']) is not int or not 1 <= selector['level'] <= 64):
            raise AnalysisError('INVALID_SCOPE_EDIT', '层级需要 1 至 64 的整数。')
    if kind == 'FILTER' and not isinstance(selector.get('where'), dict):
        raise AnalysisError('INVALID_SCOPE_EDIT', 'FILTER 需要属性筛选条件。')
    if 'where' in selector:
        _validate_where(selector['where'])


def _validate_where(condition, depth=0):
    if not isinstance(condition, dict) or depth > 8 or set(condition) - {'fieldHint', 'op', 'value', 'children'}:
        raise AnalysisError('INVALID_SCOPE_EDIT', '属性条件使用 fieldHint/op/value 或逻辑条件 children。')
    op = condition.get('op')
    if op in ('AND', 'OR', 'NOT'):
        children = condition.get('children')
        if (not isinstance(children, list) or not children or len(children) > 32
                or op == 'NOT' and len(children) != 1 or 'fieldHint' in condition or 'value' in condition):
            raise AnalysisError('INVALID_SCOPE_EDIT', '逻辑条件需要非空子条件，NOT 仅有一个子条件。')
        for child in children:
            _validate_where(child, depth+1)
    elif (op not in ('EQ', 'NE', 'GT', 'GE', 'LT', 'LE', 'IN', 'NOT_IN', 'IS_NULL', 'IS_NOT_NULL')
          or not isinstance(condition.get('fieldHint'), str) or not condition['fieldHint'].strip()
          or condition.get('children') or op in ('IN', 'NOT_IN') and (not isinstance(condition.get('value'), list) or not condition['value'])
          or op not in ('IS_NULL', 'IS_NOT_NULL') and condition.get('value') is None):
        raise AnalysisError('INVALID_SCOPE_EDIT', '属性比较条件缺少合法的属性、运算符或值。')


def apply_query_scopes(state, request):
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    explicit = {concept(item['dimensionHint']) for item in scopes}
    for edit in state.get('scopeEdits', []):
        if edit['selector'] is not None and concept(edit['dimensionHint']) not in explicit:
            scopes.append({'id': 'declared-' + digest(edit['dimensionHint'])[:12],
                           'dimensionHint': edit['dimensionHint'], 'selector': deepcopy(edit['selector'])})
    if scopes:
        request.setdefault('intentDraft', {})['dimensionScopes'] = scopes


def reconcile_period(state, request):
    """An explicit period quoted in this question must not be overwritten by history.

    Period-code guesses or text absent from the current question do not release
    the old period. Resolution of the actual date remains with the budget service.
    """
    context = state.get('followupContext', {})
    if context.get('mode', 'CONTINUE') != 'CONTINUE':
        return
    previous = state.get('previousQueryIntent', {}).get('period')
    question = state.get('request', '')
    proposed = {target.get('periodText') for target in (request.get('intentDraft') or {}).get('targets', [])}
    if previous and any(isinstance(period, str) and period and period in question and period != previous for period in proposed):
        context['changedFields'] = sorted(set(context.get('changedFields', [])) | {'period'})
        context['userQuote'] = question
        state['followupContext'] = context
