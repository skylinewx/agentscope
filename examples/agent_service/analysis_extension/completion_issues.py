"""Bind completion claims to executed work or the user's frozen requirement.

Source consistency is an audit outcome, independent of execution completion.
This module never computes new formulas or infers a business cause.
"""
from copy import deepcopy

from .models import AnalysisError


ISSUE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'requirement': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'taskId': {'type': 'string', 'minLength': 1},
        'basis': {'oneOf': [
            {'type': 'object', 'additionalProperties': False,
             'properties': {'kind': {'const': 'WORK_ITEM'}, 'taskId': {'type': 'string', 'minLength': 1}},
             'required': ['kind', 'taskId']},
            {'type': 'object', 'additionalProperties': False,
             'properties': {'kind': {'const': 'AUDIT_RESULT'}, 'resultRefs': {
                 'type': 'array', 'minItems': 1, 'maxItems': 100, 'uniqueItems': True,
                 'items': {'type': 'string', 'minLength': 1}}}, 'required': ['kind', 'resultRefs']},
            {'type': 'object', 'additionalProperties': False,
             'properties': {'kind': {'const': 'INVESTIGATION'}, 'requirementRef': {'type': 'string', 'minLength': 1}},
             'required': ['kind', 'requirementRef']},
            {'type': 'object', 'additionalProperties': False,
             'properties': {'kind': {'const': 'USER_REQUIREMENT'}, 'userQuote': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
             'required': ['kind', 'userQuote']},
        ]},
    }, 'required': ['requirement', 'reason', 'basis'],
}


def context_for(turn, base=None):
    if turn is None:
        return deepcopy((base or {}).get('completionContext'))
    from .conversation_context import selected_contexts
    return {'userRequest': turn.state.get('request', ''),
            'selectedContextRequests': [{'contextRef': item['contextRef'], 'request': item.get('request', '')}
                                        for item in selected_contexts(turn.state)],
            'investigations': deepcopy(turn.state.get('analysisRequirement', {}).get('investigations', []))}


def audit_results(artifact):
    from .formula_check_summary import summarize
    results = []
    for value in artifact.get('derivedValues', []):
        if value.get('operator') not in ('FORMULA_CHECK', 'RECONCILE'):
            continue
        summary = summarize(value['formulaTrace']) if value.get('formulaTrace') else {}
        completed = (value.get('calculationValidation') == 'PASS'
                     or value.get('executionStatus') == 'COMPLETED'
                     and value.get('completionReason') == 'SOURCE_VALUE_MISSING')
        completed = completed and not summary.get('hasIncompleteLayers', False)
        results.append({'resultRef': value['derivedId'], 'executionCompleted': completed,
                        'sourceConsistency': value.get('sourceConsistency', 'NOT_CHECKED'),
                        'hasIncompleteLayers': summary.get('hasIncompleteLayers', False),
                        'mismatchedFactRefs': summary.get('mismatchedFactRefs', []),
                        'blockedFactRefs': summary.get('blockedFactRefs', []),
                        'meaning': '核对完成与来源一致分别判断；差额披露为审核发现，来源原值保持不变。'})
    return results


def normalize_items(artifact, items):
    """Validate new writes only. Frozen historical artifacts are never rewritten."""
    from jsonschema import Draft202012Validator
    if not isinstance(items, list) or len(items) > 100:
        raise AnalysisError('INVALID_COMPLETION_ISSUES', 'unresolvedItems 最多 100 项。')
    result = []
    context = artifact.get('completionContext')
    tasks = {t['taskId'] for t in artifact.get('plan', {}).get('workPlan', [])}
    audits = {item['resultRef']: item for item in audit_results(artifact)}
    investigations = {item['requirementRef']: item for item in (context or {}).get('investigations', [])}
    for index, original in enumerate(items):
        item = deepcopy(original)
        path = f'/unresolvedItems/{index}'
        if (isinstance(item, dict) and item.get('basis') == {'kind': 'HOST_DELIVERY'}
                and artifact.get('hostSummary')):
            result.append(item)
            continue
        if next(Draft202012Validator(ISSUE_SCHEMA).iter_errors(item), None):
            raise AnalysisError('INVALID_COMPLETION_ISSUES',
                '未完成项须提供真实依据 basis：WORK_ITEM/taskId、AUDIT_RESULT/resultRefs、INVESTIGATION/requirementRef，或遗漏用户要求的 USER_REQUIREMENT/userQuote。',
                {'path': path, 'investigations': list(investigations.values()),
                 'auditResults': list(audits.values())})
        basis = item['basis']
        kind = basis['kind']
        if kind == 'WORK_ITEM':
            if basis['taskId'] not in tasks or item.get('taskId', basis['taskId']) != basis['taskId']:
                raise AnalysisError('INVALID_COMPLETION_ISSUES', '未完成依据必须引用同一实际工作项。', {'path': path})
            item['taskId'] = basis['taskId']
        elif kind == 'AUDIT_RESULT':
            if any(ref not in audits for ref in basis['resultRefs']):
                raise AnalysisError('INVALID_COMPLETION_ISSUES', '审核依据必须引用实际 FORMULA_CHECK/RECONCILE 结果。', {'path': path})
            if all(audits[ref]['executionCompleted'] for ref in basis['resultRefs']):
                # The completed checks remain in auditResults/derivedValues and
                # semantic review still checks their disclosure in the document.
                continue
        elif kind == 'INVESTIGATION':
            requirement = investigations.get(basis['requirementRef'])
            if requirement is None:
                raise AnalysisError('UNCONFIRMED_INVESTIGATION',
                    '本轮没有确认这项差额原因调查要求；已完成核对的差额作为审核发现。',
                    {'path': path, 'investigations': list(investigations.values())})
            item['requirement'] = requirement['requirement']
        elif context is not None:
            quotes = [context.get('userRequest', ''),
                      *(entry.get('request', '') for entry in context.get('selectedContextRequests', []))]
            if not any(basis['userQuote'].strip() and basis['userQuote'] in text for text in quotes):
                raise AnalysisError('UNCONFIRMED_COMPLETION_REQUIREMENT',
                    '遗漏要求必须引用本轮或明确选用上下文的用户原话，不能引用模型自拟计划。', {'path': path})
        result.append(item)
    return result
