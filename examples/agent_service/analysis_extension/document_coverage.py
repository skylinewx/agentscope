"""Track frozen numeric identities across presentation changes, not numbers of bars."""
import json

from .models import AnalysisError
from .business_dataset import row_values

WATERFALL_BINDING_HELP = (
    '完整构成可直接在当前段落放 <div data-analysis-waterfalls="公式数据集key"></div>，宿主按本层来源绘制并标明差额。自定义显示时，'
    '在当前模板中按 node.waterfall.steps 顺序为每步创建轨道及其子条形。DOM 创建后调用 '
    'window.analysisCharts.bindHorizontalWaterfall(rows.map(({bar, track, labelElement}, stepIndex) => '
    '({element: bar, track, labelElement, stepIndex})), {setKey, nodeRef: node.factRef})。'
    '名称元素显示 step.label。图注如需表内地址用 node.cellAddress。setKey 是 document.datasets 中的绑定名。')


def reference(document, binding):
    if not isinstance(binding, dict):
        raise ValueError('binding must be an object')
    if set(binding) == {'valueKey'}:
        value = next(item for item in document['bindings'] if item['key'] == binding['valueKey'])
        identity = [value['valueRef'], value.get('field', 'value'), value.get('nodeRef')]
    elif set(binding) == {'setKey', 'factRef'}:
        dataset = document['datasets'][binding['setKey']]
        matches = [row for row in dataset['rows']
                   if row_values(row, dataset.get('columns', [])).get('factRef') == binding['factRef']]
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
    elif set(binding) == {'trendRef'} and binding['trendRef'] in document.get('periodTrendRefs', []):
        identity = ['periodTrend', binding['trendRef']]
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
            refs = [values['factRef'] for row in dataset['rows']
                    if (values := row_values(row, dataset['columns'])).get('value') is not None]
        elif dataset.get('kind') == 'ORDERED':
            refs = [values['factRef'] for row in dataset['rows']
                    if (values := row_values(row, dataset.get('columns', []))).get('value') is not None]
        else:
            continue
        source.update(reference(document, {'setKey': key, 'factRef': ref}) for ref in refs)
    result['sourceRefs'] = sorted(source)
    required = required_formula_steps(document)
    result['requiredFormulaSteps'] = sorted(required)
    result['missingFormulaSteps'] = sorted(required - set(result['charted']))
    return result


def formula_step_groups(document):
    """Group expected chart marks by their actual dataset and formula node."""
    for key, dataset in document.get('datasets', {}).items():
        if dataset.get('kind') != 'FORMULA': continue
        for node in dataset.get('nodes', []):
            steps = (node.get('waterfall') or {}).get('steps', [])
            if len(steps) < 3: continue  # A single-input transfer does not require a waterfall.
            refs = [reference(document, {'setKey': key, 'nodeRef': node['factRef'], 'stepIndex': i})
                    for i in range(len(steps))]
            yield key, node['factRef'], refs


def required_formula_steps(document):
    return {ref for _, _, refs in formula_step_groups(document) for ref in refs}
