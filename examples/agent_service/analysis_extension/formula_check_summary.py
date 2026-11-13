"""Explicit check scope from host calculations, independent of narrative wording."""
from copy import deepcopy


def summarize(trace):
    groups = {'MATCH': [], 'MISMATCH': [], 'BLOCKED': []}
    for node in trace.get('nodes', []):
        check = node.get('localCheck') or {}
        if check.get('status') in groups:
            groups[check['status']].append(node['factRef'])
    incomplete = bool(groups['BLOCKED']) or trace.get('status', trace.get('recursiveStatus')) == 'BLOCKED'
    status = 'MISMATCH' if groups['MISMATCH'] else 'INCOMPLETE' if incomplete else 'MATCH' if groups['MATCH'] else 'NOT_CHECKED'
    return {'targetLocalCheck': deepcopy(trace.get('targetLocalCheck')),
            'arithmeticPolicy': trace.get('arithmeticPolicy'),
            'nullAsZeroRefs': deepcopy(trace.get('nullAsZeroRefs', [])),
            'localResults': local_results(trace),
            'capturedChainStatus': status, 'hasIncompleteLayers': incomplete,
            'matchedFactRefs': groups['MATCH'], 'mismatchedFactRefs': groups['MISMATCH'],
            'blockedFactRefs': groups['BLOCKED'],
            'externalFormulasVerified': False if trace.get('externalBoundaries') else None,
            'meaning': '本层核对与已捕获链路分别陈述；INCOMPLETE/MISMATCH 不能概括为整条路径复算一致。预算来源空值按0运算、除以0返回0，来源原值仍保留空；未读或错误不作为空值。'}


def local_results(trace):
    """Render only facts already computed by the formula engine, never causal/missing-item claims."""
    result = []
    for node in trace.get('nodes', []):
        check = node.get('localCheck') or {}
        status = check.get('status')
        if status not in ('MATCH', 'MISMATCH', 'BLOCKED'):
            continue
        item = {'factRef': node['factRef'], 'status': status, 'sourceValue': node.get('observed'),
                'calculatedValue': check.get('value'), 'difference': check.get('difference'),
                'differenceConvention': 'source_minus_immediate_inputs_calculation'}
        if status == 'BLOCKED':
            diagnostic = check.get('diagnostic') or next(iter(node.get('diagnostics', [])), {})
            item['diagnostic'] = deepcopy(diagnostic)
            item['statement'] = '本层复算受阻：' + (diagnostic.get('message') or '必要依赖或运算条件尚未满足。')
        elif status == 'MISMATCH':
            source_label = '空（运算按0）' if node.get('observed') is None and node.get('effectiveSourceValue') == '0' else item['sourceValue']
            item['statement'] = (f"来源值 {source_label}，本层复算值 {item['calculatedValue']}，"
                                 f"差额 {item['difference']}；两者不一致。")
        else:
            item['statement'] = '本层复算与来源值相符；这不代表整条依赖链已核对通过。'
        if node.get('arithmeticPolicy'):
            item.update(arithmeticPolicy=node['arithmeticPolicy'],
                        effectiveSourceValue=node.get('effectiveSourceValue'),
                        nullAsZeroRefs=deepcopy(check.get('nullAsZeroRefs', [])),
                        zeroDivisorCount=check.get('zeroDivisorCount', 0))
            if item['nullAsZeroRefs'] or (node.get('observed') is None and node.get('effectiveSourceValue') is not None):
                item['statement'] += ' 来源空值在运算及差额核对中按0处理，原始值仍为空。'
            if item['zeroDivisorCount']:
                item['statement'] += ' 除数为0的除法按预算规则返回0。'
        result.append(item)
    return result


def normalize_review_issue(item, material):
    """A specifically identified check-status contradiction is substantive, not optional polish."""
    if item.get('code') != 'CHECK_STATUS_MISMATCH' or not item.get('statement'):
        return item
    refs = set(item.get('evidenceRefs', []))
    for derived in material.get('derived', []):
        trace = derived.get('formulaTrace') or {}
        summary = summarize(trace)
        invalid = set(summary['blockedFactRefs'] + summary['mismatchedFactRefs'])
        if invalid and (refs & invalid or derived.get('derivedId') in refs):
            return {**item, 'severity': 'BLOCKING'}
    return item
