"""Track frozen numeric identities across presentation changes, not numbers of bars."""
import json

from .models import AnalysisError


def reference(document, binding):
    if not isinstance(binding, dict):
        raise ValueError('binding must be an object')
    if set(binding) == {'valueKey'}:
        value = next(item for item in document['bindings'] if item['key'] == binding['valueKey'])
        identity = [value['valueRef'], value.get('field', 'value'), value.get('nodeRef')]
    elif set(binding) == {'setKey', 'factRef'}:
        dataset = document['datasets'][binding['setKey']]
        if dataset['kind'] == 'TABULAR':
            index = [column['name'] for column in dataset['columns']].index('factRef')
            matches = [row for row in dataset['rows'] if row[index] == binding['factRef']]
        else:
            matches = [row for row in dataset['rows'] if row['factRef'] == binding['factRef']]
        if len(matches) != 1:
            raise ValueError('row is not unique')
        identity = [binding['factRef'], 'value', None]
    elif set(binding) == {'setKey', 'nodeRef', 'stepIndex'}:
        dataset = document['datasets'][binding['setKey']]
        node = next(node for node in dataset['nodes'] if node['factRef'] == binding['nodeRef'])
        index = binding['stepIndex']
        if type(index) is not int or not 0 <= index < len(node['waterfall']['steps']):
            raise ValueError('unknown waterfall step')
        identity = [dataset['valueRef'], 'waterfall', binding['nodeRef'], index]
    else:
        raise ValueError('unknown binding shape')
    return 'bound:' + json.dumps(identity, ensure_ascii=False, separators=(',', ':'))


def evaluate(document, observation):
    if (not isinstance(observation, dict) or set(observation) != {'charted', 'presented', 'complete'}
            or type(observation['complete']) is not bool):
        raise AnalysisError('INVALID_PREVIEW_CHECK', '数值覆盖需要实际图形、数值展示引用及完整性。')
    result = {'complete': observation['complete']}
    for key in ('charted', 'presented'):
        items = observation[key]
        if not isinstance(items, list) or len(items) > 1200:
            raise AnalysisError('INVALID_PREVIEW_CHECK', '数值覆盖引用超过容量。')
        try:
            result[key] = sorted({reference(document, item) for item in items})
        except (KeyError, ValueError, TypeError, StopIteration, IndexError):
            raise AnalysisError('INVALID_PREVIEW_CHECK', '数值覆盖引用不属于当前冻结文档。') from None
    # Seed old count-only previews from their current frozen numeric inputs once.
    # Subsequent v4 revisions retain exact identities, not chart counts.
    source = set()
    for key in document.get('bindings', []):
        if key['key'] in document.get('numericBindingKeys', []) or ('numericBindingKeys' not in document and key.get('field') != 'unit'):
            source.add(reference(document, {'valueKey': key['key']}))
    for key, dataset in document.get('datasets', {}).items():
        if dataset.get('kind') == 'TABULAR':
            names = [column['name'] for column in dataset['columns']]
            if 'factRef' not in names or 'value' not in names:
                continue
            refs = [row[names.index('factRef')] for row in dataset['rows'] if row[names.index('value')] is not None]
        elif dataset.get('kind') == 'ORDERED':
            refs = [row['factRef'] for row in dataset['rows'] if row.get('value') is not None]
        else:
            continue
        source.update(reference(document, {'setKey': key, 'factRef': ref}) for ref in refs)
    result['sourceRefs'] = sorted(source)
    return result
