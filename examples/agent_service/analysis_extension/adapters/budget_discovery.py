"""Exact, bounded selectors for the current v2 receipt shapes."""
from .budget_v2 import receipt_from_capture
from ..models import AnalysisError


def row_sets(receipt):
    data = receipt.get('data') or {}
    if isinstance(data.get('rows'), list):
        yield None, data
    for step in data.get('stepResults', []):
        if isinstance(step.get('rows'), list):
            yield step.get('stepId'), step


def entity_rows(receipt):
    """Actual identities from row results and RDF-verified search candidates.

    Search candidates are labels, never proof of a complete population.
    Similarity scores and matching phrases are not identity mappings.
    """
    for _, output in row_sets(receipt):
        yield from output['rows']
    for candidate in (receipt.get('data') or {}).get('candidates', []):
        if (candidate.get('kind') == 'RESOURCE' and candidate.get('resourceIri')
                and 'RESOURCE_RDF_VERIFIED' in candidate.get('matchedBy', [])):
            yield candidate


def selected_rows(receipt, step_id=None):
    matches = [output for sid, output in row_sets(receipt) if sid == step_id]
    if len(matches) != 1:
        raise AnalysisError('UNKNOWN_STEP', '未找到唯一的行集合；先读取 selectors，原样使用返回的 stepId。',
                            {'field': 'stepId', 'availableStepIds': [sid for sid, _ in row_sets(receipt)],
                             'nextSection': 'selectors'})
    return matches[0]


def rows_complete(receipt, step_id, output):
    if receipt.get('completeness', {}).get('enumerationExhausted') is not True:
        return False
    if step_id is not None:
        return output.get('exactness') == 'EXACT'
    page = output.get('page') or {}
    return page.get('enumerationExhausted') is True and page.get('nextCursor') is None and not page.get('stopReasons')


def selectors(capture):
    receipt = receipt_from_capture(capture)
    result = []
    for sid, output in row_sets(receipt):
        rows = output['rows']
        fields = sorted(set.intersection(*(set(r) for r in rows))) if rows else []
        pointers = ['/'+k.replace('~','~0').replace('/','~1') for k in fields
                    if all(isinstance(r[k], str) and r[k] for r in rows)
                    and len({r[k] for r in rows}) == len(rows)]
        selector = {'captureRef': capture['captureRef'], **({'stepId': sid} if sid is not None else {})}
        result.append({**selector, 'rowCount': len(rows), 'memberPointers': pointers,
                       'populationSelector': {**selector, 'memberPointer': '/key'} if '/key' in pointers else None,
                       'enumerationComplete': rows_complete(receipt, sid, output),
                       'nextSection': 'rows'})
    return result
