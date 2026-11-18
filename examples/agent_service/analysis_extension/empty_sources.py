"""A committed empty result is a selected input, not an absent selection."""
from copy import deepcopy
from .models import AnalysisError, digest


def result(capture, selection):
    from .adapters.budget_v2 import receipt_from_capture
    receipt = receipt_from_capture(capture)
    data = receipt.get('data', {})
    matches = [item for item in data.get('resultSets', [])
               if item.get('resultSetId') == selection.get('resultSetId')]
    if (receipt.get('status') != 'SUCCEEDED' or data.get('complete') is not True
            or len(matches) != 1 or matches[0].get('rows') != []):
        raise AnalysisError('EMPTY_SOURCE_INVALID', '空来源必须对应本次完整成功回执中真实的空结果集。')
    return receipt, matches[0]


def candidates(capture, receipt):
    if receipt.get('status') != 'SUCCEEDED' or receipt.get('data', {}).get('complete') is not True:
        return []
    return [{'captureRef': capture['captureRef'], 'resultSetId': item['resultSetId'],
             'candidateRef': 'subject_' + digest([capture['captureRef'], item['resultSetId']])[:24],
             'rowCount': 0, 'grain': item.get('grain', [])}
            for item in receipt.get('data', {}).get('resultSets', []) if item.get('rows') == []]


def adapt(capture, selection, scope):
    from .adapters.budget_v2 import budget_source_context
    from .sources import combined_context
    receipt, item = result(capture, selection)
    context = budget_source_context(capture, receipt)
    return {'facts': [], 'sourceContexts': [context], 'context': combined_context([context]),
            'scope': {**deepcopy(scope), 'members': [], 'referenceMembers': [],
                'sourceScopes': [], 'selectedDataComplete': True, 'sourcePagesComplete': True,
                'populationComplete': True, 'coverage': 'COMPLETE', 'periodCoverage': [],
                'pendingMembers': [], 'pendingReferenceMembers': [], 'observedMembers': [],
                'observedReferenceMembers': [], 'semanticCompleteness': 'UNVERIFIED',
                'scopeId': 'scope_' + digest(selection)[:24]}}
