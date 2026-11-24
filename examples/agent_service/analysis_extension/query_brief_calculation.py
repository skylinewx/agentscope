"""A direct-query writer can calculate against only the tables delivered in that reply."""
from .models import AnalysisError


ARGUMENT = {'type': 'object', 'properties': {
    'table': {'type': 'string'}, 'column': {'type': 'string'},
    'operation': {'enum': ['MIN', 'MAX', 'COUNT', 'SUM']},
    'filters': {'type': 'object', 'additionalProperties': {'type': 'string'}}},
    'required': ['table', 'column', 'operation'], 'additionalProperties': False}
CALCULATION = {'type': 'object', 'properties': {
    'expression': {'type': 'string', 'description': '按顺序用 v0、v1 等引用下面的范围值，例如 v0 - v1。'},
    'inputs': {'type': 'array', 'items': ARGUMENT, 'minItems': 1, 'maxItems': 8}},
    'required': ['expression', 'inputs'], 'additionalProperties': False}


def request_schema(tables):
    from copy import deepcopy
    schema = deepcopy(CALCULATION)
    fields = schema['properties']['inputs']['items']['properties']
    fields['table']['enum'] = list(tables)
    fields['column']['enum'] = sorted({key for _, _, _, columns, _ in tables.values() for key, _ in columns})
    dimensions = {key for _, _, _, _, dims in tables.values() for key, _ in dims}
    fields['filters'] = {'type': 'object', 'properties': {key: {'type': 'string'} for key in sorted(dimensions)},
                         'additionalProperties': False, 'description': '用下方维度标识筛选。值用该维度的原始编码或表中成员名称。'}
    return schema


async def compute(turn, request, tables):
    from .value_references import calculate_references, reference_context
    from numeric_tools import calculate
    if not isinstance(request, dict) or not isinstance(request.get('expression'), str):
        raise AnalysisError('INVALID_VALUE_REFERENCE', '简述计算需要表达式和所选范围。')
    inputs = request.get('inputs')
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 8:
        raise AnalysisError('INVALID_VALUE_REFERENCE', '简述计算可选择 1 至 8 个已交付的范围。')
    refs, labels = {}, []
    for i, item in enumerate(inputs):
        if not isinstance(item, dict) or not isinstance(item.get('table'), str) or item['table'] not in tables:
            raise AnalysisError('TABLE_NOT_FOUND', '简述计算引用的表不在本次查询内。')
        capture_ref, table_ref, name, columns, _ = tables[item['table']]
        filters = item.get('filters', {})
        if not isinstance(filters, dict) or any(not isinstance(v, str) for v in filters.values()):
            raise AnalysisError('INVALID_TABLE_FILTER', '筛选需要维度和成员。')
        cols = dict(columns)
        column = item.get('column')
        if not isinstance(column, str) or column not in cols:
            raise AnalysisError('INVALID_TABLE_COLUMNS', '简述计算引用的列不在该表内。')
        refs['v' + str(i)] = {'captureRef': capture_ref, 'tableRef': table_ref, 'columnRef': column,
                              'aggregation': item.get('operation'), 'filters': [
                                  {'column': k, 'operator': 'EQ', 'value': v} for k, v in filters.items()]}
        labels.append(f"v{i} {name} {cols[column]} {filters} {item.get('operation')}")
    with reference_context(turn.service, None):
        result = await calculate_references({'expression': request['expression'], 'valueRefs': refs}, calculate)
    details = result['sourceReferences'][0]['inputs']
    return '\n'.join(f"{label} = {item['value']} {item.get('unit') or ''} {item.get('extrema', '')}"
                     for label, item in zip(labels, details)) + '\n' + request['expression'] + ' = ' + result['value']
