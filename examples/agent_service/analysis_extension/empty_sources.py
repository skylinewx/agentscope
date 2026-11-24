"""A committed empty result is a selected input, not an absent selection."""
from copy import deepcopy
from .models import AnalysisError, digest


def is_empty_result(receipt, item):
    from .query_empty_result import completed_result_set
    return item.get('rows') == [] and completed_result_set(receipt, item)


def result(capture, selection):
    from .adapters.budget_v2 import receipt_from_capture
    receipt = receipt_from_capture(capture)
    data = receipt.get('data', {})
    matches = [item for item in data.get('resultSets', [])
               if item.get('resultSetId') == selection.get('resultSetId')]
    if len(matches) != 1 or not is_empty_result(receipt, matches[0]):
        raise AnalysisError('EMPTY_SOURCE_INVALID', '该结果集须有已执行完整且返回零行的来源回执。')
    return receipt, matches[0]


def candidates(capture, receipt):
    return [{'captureRef': capture['captureRef'], 'resultSetId': item['resultSetId'],
             'candidateRef': 'subject_' + digest([capture['captureRef'], item['resultSetId']])[:24],
             'rowCount': 0, 'grain': item.get('grain', [])}
             for item in receipt.get('data', {}).get('resultSets', []) if is_empty_result(receipt, item)]


def adapt(capture, selection, scope):
    from .adapters.budget_v2 import budget_source_context
    from .budget_dimensions import dimension_labels, loading_names_for_empty_results
    from .sources import combined_context
    receipt, item = result(capture, selection)
    context = budget_source_context(capture, receipt)
    selected = receipt['data']['resultSets']
    empty = loading_names_for_empty_results(selected)[selected.index(item)]
    labels = dimension_labels(receipt['data'].get('dimensionCatalog') or [], models=[empty['modelCode']] if empty.get('modelCode') else [], context=empty)
    display = {key: deepcopy(empty[key]) for key in ('taskName', 'formName', 'period', 'executionPeriod', 'effectiveDimensions', 'dimensionNames')
               if key in empty}
    display['dimensionLabels'] = {key: labels[key] for key in display.get('effectiveDimensions', {}) if key in labels}
    return {'facts': [], 'sourceContexts': [context], 'context': combined_context([context]),
            'scope': {**deepcopy(scope), 'members': [], 'referenceMembers': [],
                'sourceScopes': [{'captureRef': capture['captureRef'], 'resultSetId': item['resultSetId'],
                                  'returnedRowCount': 0, 'selectedRowCount': 0, 'inputRole': selection.get('role'),
                                  'emptyContext': display, 'label': item.get('label')}],
                'selectedDataComplete': True, 'sourcePagesComplete': receipt.get('status') == 'SUCCEEDED' and receipt['data'].get('complete') is True,
                'populationComplete': True, 'coverage': 'COMPLETE', 'periodCoverage': [],
                'pendingMembers': [], 'pendingReferenceMembers': [], 'observedMembers': [],
                'observedReferenceMembers': [], 'semanticCompleteness': 'UNVERIFIED',
                'scopeId': 'scope_' + digest(selection)[:24]}}
