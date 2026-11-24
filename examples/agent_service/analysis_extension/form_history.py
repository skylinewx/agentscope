"""Remember only the native task/form paths actually adopted for delivery."""
from copy import deepcopy
from types import SimpleNamespace

from .models import AnalysisError, digest
from .query_lineage import selected_result_sets


def connection(source):
    return {k: source.get(k) for k in ('resourceId', 'serverName', 'connectionFingerprint')}


def remember(turn):
    state = turn.state
    ordinal = state.get('conditionTurn')
    if not ordinal:
        return
    # Keep the previous user-turn baseline so a second delivery in this turn
    # can replace its selection without renewing a discarded probe.
    records = deepcopy(state.get('priorFormHistory', {}))
    if any(item['lastUsedTurn'] > ordinal for item in records.values()):
        return
    selections = state.get('resolvedSubjectScope', {}).get('selections', [])
    by_capture = {}
    for selection in selections:
        by_capture.setdefault(selection['captureRef'], []).append(selection)
    for capture_ref, selected in by_capture.items():
        try:
            capture = turn.service.store.get_capture(turn.service.owner, capture_ref)
            if (capture.get('originalToolName') != 'execute_budget_query'
                    or state.get('turnKey') and capture.get('turnKey') != state['turnKey']):
                continue
            receipt = turn.service.sources.for_capture(capture).receipt(capture)
        except AnalysisError as error:
            if error.code != 'NOT_FOUND':
                raise
            continue
        if receipt.get('status') not in ('SUCCEEDED', 'PARTIAL'):
            continue
        native = receipt.get('context', {})
        proof = {'executionId': receipt.get('executionId'), 'querySessionId': receipt.get('querySessionId'),
                 'contextId': native.get('contextId')}
        if not all(proof.values()):
            continue
        for selection in selected:
            for result in selected_result_sets(receipt, selection, successful_only=True):
                ctx = result.get('context', {})
                binding = ctx.get('target', {}).get('formBindingKey')
                if not binding or not result.get('resultSetId'):
                    continue
                rows = result.get('rows', [])
                if not any(r.get('valueState') in ('PRESENT', 'NULL', 'NO_DATA') for r in rows):
                    from .empty_sources import is_empty_result
                    if not is_empty_result(receipt, result):
                        continue
                source = connection(capture)
                key = digest([source, native.get('sourceId'), native.get('snapshotId'), binding])
                records[key] = {'connection': source, 'binding': binding, 'lastUsedTurn': ordinal,
                                'source': {**proof, 'resultSetId': result['resultSetId']}}
    state['formHistory'] = records


def restore_prior(service, saved, contexts):
    """Recover earlier conversations from their delivered selections and actual message ordinals."""
    history = {}
    turns = saved.get('conditionTurns', {})
    delivered = [r for r in contexts if r.get('kind') == 'DELIVERED' and r.get('turnKey') in turns]
    for record in sorted(delivered, key=lambda r: turns[r['turnKey']]):
        state = {'priorFormHistory': history, 'turnKey': record['turnKey'], 'conditionTurn': turns[record['turnKey']],
                 'resolvedSubjectScope': record.get('subjectScope') or {}}
        remember(SimpleNamespace(service=service, state=state))
        history = state['formHistory']
    return history


def preferences(state, origin):
    return [{k: deepcopy(item[k]) for k in ('lastUsedTurn', 'source')}
            for item in state.get('formHistory', {}).values() if item['connection'] == connection(origin)
            and item['lastUsedTurn'] <= state['conditionTurn']]
