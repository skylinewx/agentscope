"""Decode host storage before presenting business evidence to a reviewer."""
from copy import deepcopy

from .models import AnalysisError


WIRE_POLICY = 'BUSINESS_REVIEW_FOUR_SECTIONS_V8'


def source_facts(source):
    """Both persisted row encodings carry the same complete business facts."""
    if 'facts' in source:
        return source['facts']
    if 'factRows' not in source:
        return []
    columns = source['factColumns']
    result = []
    for index, values in enumerate(source['factRows']):
        if len(values) != len(columns):
            raise AnalysisError('REVIEW_MATERIAL_CHANGED', '来源数据列数与完整记录不一致。')
        fact = {**source.get('fixedFactFields', {}), **dict(zip(columns, values))}
        for key in source.get('missingFactFields', {}).get(str(index), []):
            fact.pop(key, None)
        result.append(fact)
    return result


def source_entities(source, evidence):
    """Resolve dictionary references here, never ask the model to decode them."""
    if 'entities' in source:
        return source['entities']
    result = []
    for row in source.get('entityRows', []):
        entity = deepcopy(evidence['entityDefinitions'][row[0]])
        for key in ('resourceRef', 'rootResourceRef'):
            if isinstance(entity.get(key), list):
                prefix, suffix = entity[key]
                entity[key] = evidence['identityPrefixes'][prefix] + suffix
        if len(row) > 1:
            fields = []
            for field in row[1]:
                definition = deepcopy(evidence['fieldDefinitions'][field[0]])
                if len(field) > 1:
                    definition['values'] = deepcopy(field[1])
                fields.append(definition)
            entity['fields'] = fields
        result.append(entity)
    return result


def review_runs(material):
    """Analysis and query review use different envelopes for the same execution."""
    evidence = material.get('evidence') or {}
    runs = deepcopy(material.get('pythonRuns') or evidence.get('pythonRuns') or [])
    shared = evidence.get('sharedSourceData') or material.get('sharedSourceData') or {}
    for run in runs:
        if 'inputRecords' not in run:
            run['inputRecords'] = run.get('inputs', [])
        sources = []
        for source in run.get('sourceData', []):
            if 'sourceDataRef' in source:
                source = shared[source['sourceDataRef']]
            sources.append(deepcopy(source))
        run['sourceData'] = sources
    return runs


def output_business_fields(output):
    # Values/rows/data may contain arbitrary user-computed business fields.
    # Remove only known outer execution identities; never whitelist payload keys.
    host = {'derivedId', 'pythonRunRef', 'inputRefs', 'sourceFactIds', 'reviewInputRefs',
            'runtime', 'inputDigest', 'outputDigest', 'evidenceIds', 'sourceContextIds',
            'sourceContexts', 'createdAt', 'owner', 'artifactRef'}
    result = {key: deepcopy(value) for key, value in output.items() if key not in host}
    if result.get('outputKind') in ('TABLE', 'CHART_DATA', 'FILE', 'DIAGNOSTIC'):
        for key in ('value', 'unit'):
            if result.get(key) is None:
                result.pop(key, None)
    return result
