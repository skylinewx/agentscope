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
            'capturedChainStatus': status, 'hasIncompleteLayers': incomplete,
            'matchedFactRefs': groups['MATCH'], 'mismatchedFactRefs': groups['MISMATCH'],
            'blockedFactRefs': groups['BLOCKED'],
            'externalFormulasVerified': False if trace.get('externalBoundaries') else None,
            'meaning': '本层核对与已捕获链路分别陈述；INCOMPLETE/MISMATCH 不能概括为整条路径复算一致。来源空值保留，不要求重复取数或补零。'}


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
