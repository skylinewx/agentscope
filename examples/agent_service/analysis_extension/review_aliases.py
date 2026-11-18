"""Short, reversible names for repeated opaque evidence IDs in model views."""
from collections import Counter
from copy import deepcopy
import json

IDENTITIES = {'factId', 'derivedId', 'runRef', 'captureRef', 'sourceContextId', 'bindingRef',
              'executionId', 'querySessionId', 'observationId', 'sourceRef', 'memberRef', 'objectId',
              'batchId', 'metricKey', 'tableDigest', 'evidenceIds'}


def replace_references(value, names):
    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, list):
        return [replace_references(child, names) for child in value]
    if isinstance(value, dict):
        return {names.get(key, key): deepcopy(child) if key == 'code' else replace_references(child, names)
                for key, child in value.items()}
    return value


def compact_references(material, *, all_identities=False):
    if 'referenceAliases' in material:
        return deepcopy(material), {}
    counts, candidates = Counter(), set()
    def collect(value, key=None):
        if isinstance(value, str):
            counts[value] += 1
            if key in IDENTITIES and len(value) >= 20:
                candidates.add(value)
        elif isinstance(value, dict):
            for field, child in value.items():
                counts[field] += 1
                if field != 'code': collect(child, field)
        elif isinstance(value, list):
            for child in value: collect(child, key)
    collect(material)
    chosen = sorted(value for value in candidates if all_identities or counts[value] >= 3)
    names, aliases = {}, {}
    for value in chosen:
        alias = '@e' + str(len(names) + 1)
        while alias in counts: alias = '@' + alias
        # The original identity is included once; do not inflate small materials.
        if not all_identities and (len(value) - len(alias)) * (counts[value] - 1) < len(alias) + 30: continue
        names[value], aliases[alias] = alias, value
    if not aliases: return deepcopy(material), {}
    return replace_references(material, names), aliases
