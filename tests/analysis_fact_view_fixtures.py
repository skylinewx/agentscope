"""Native FACTS receipt fixture, validated by the real host adapter before use."""
from copy import deepcopy
from analysis_extension.source_views import register, query_basis
from analysis_extension.adapters.budget_v2 import receipt_from_capture
from analysis_extension.adapters.budget_source_view import matches
from analysis_budget_batch_test import update


def fact_view(turn, source_ref, predicate=None, *, reopened=False):
    store, owner = turn.service.store, turn.service.owner
    source = store.get_source_view(owner, source_ref)
    old = store.get_capture(owner, source['selection']['captureRef'])
    original = receipt_from_capture(old)
    request = query_basis(turn, old, {'contractVersion': '2.0', 'requestId': 'fact-view',
        'contextId': original['context']['contextId'], 'querySessionId': original['querySessionId'],
        'purpose': 'QUERY', 'question': turn.state['request'], 'basis': {'kind': 'FACTS', 'sourceRef': source_ref},
        **({'intentDraft': {'valueFilter': predicate}} if predicate else {})})
    basis = request['basis']
    result = deepcopy(next(item for item in original['data']['resultSets'] if item['resultSetId'] == basis['source']['resultSetId']))
    selected = [row for row in result['rows'] if row['factId'] in basis['source']['factIds']]
    result['rows'] = [row for row in selected if not predicate or matches(predicate, row) is True]
    receipt = deepcopy(original)
    previous = original['data'].get('sourceBasis') or {}
    receipt.update(executionId=original['executionId']+'-view', status='SUCCEEDED')
    if reopened:
        request.update(contextId='reopened-context', querySessionId='reopened-session')
        receipt['context']['contextId'] = request['contextId']
        receipt['querySessionId'] = request['querySessionId']
    receipt['data'].update(question=request['question'], purpose='QUERY', resultSets=[result], complete=True,
        unresolvedGroups=[], sourceBasis={'basis': deepcopy(basis), 'selectedFactIds': basis['source']['factIds'],
            'selectedFacts': selected, 'sourceContext': previous.get('sourceContext', original['context']),
            'originalRequest': previous.get('originalRequest', old['request']),
            'operation': 'IMMUTABLE_FACT_VIEW', 'retainedFactIds': [row['factId'] for row in result['rows']],
            **({'valueFilter': predicate} if predicate else {})})
    capture = deepcopy(old)
    capture.pop('captureRef', None)
    capture['request'] = request
    update(capture, receipt)
    receipt_from_capture(capture)
    ref = store.capture(owner, capture)
    record = register(turn, {**source['selection'], 'captureRef': ref})
    return record, store.get_capture(owner, ref)
