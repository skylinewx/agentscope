"""Budget row identity, independent of values, observations and analysis roles."""
from copy import deepcopy
import re

from .models import digest


_TABLE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\[')


def record_identity(fact):
    """Only verified, resolved addresses identify rows; aggregate results are not rows."""
    if fact.get('aggregation') or fact.get('calcType'):
        return None
    expression = fact.get('expression') or fact.get('cellExp')
    match = _TABLE.match(expression) if isinstance(expression, str) else None
    dimensions = {**(fact.get('effectiveDimensions') or {}), **(fact.get('dimensions') or {})}
    if not match or not dimensions:
        return None
    # These dimensions come from the validated execution, not the question or
    # display names. Do not coerce string codes, nulls or leading zeroes.
    table = match.group(1)
    return {'recordKey': 'record_' + digest([table, dimensions])[:32],
            'table': table, 'dimensions': deepcopy(dimensions)}


def annotate_records(facts):
    for fact in facts:
        identity = record_identity(fact)
        if identity:
            fact['recordKey'] = identity['recordKey']
            fact['recordTable'] = identity['table']


def record_catalog(facts):
    """Share row addresses while retaining every field, observation and input use."""
    records, unresolved = {}, []
    for fact in facts:
        identity = record_identity(fact)
        ref = fact.get('factId', fact.get('ref'))
        if identity is None:
            unresolved.append(ref)
            continue
        # Data sources are separate namespaces. A key does not assert that two
        # environments with identically named tables are the same environment.
        namespace = fact.get('sourceContextId', '')
        index = (namespace, identity['recordKey'])
        record = records.setdefault(index, {**identity, 'sourceContextId': namespace, 'observations': []})
        record['observations'].append({key: deepcopy(value) for key, value in {
            'factRef': ref, 'field': fact.get('sourceMeasureCode') or fact.get('measureCode'),
            'value': fact.get('value'), 'valueState': fact.get('valueState'),
            'inputRole': fact.get('inputRole'), 'inputMemberships': fact.get('inputMemberships'),
            'captureRefs': fact.get('captureRefs'), 'observationId': fact.get('observationId')
        }.items() if value is not None or key == 'value' and 'value' in fact})
    return {'records': list(records.values()), 'unresolvedFactRefs': unresolved,
            'recordCount': len(records), 'factCount': len(facts)}
