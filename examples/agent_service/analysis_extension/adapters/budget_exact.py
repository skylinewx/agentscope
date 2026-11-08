"""Native exact-cell receipts: target identity is the join key, never a Cartesian table."""
from collections import Counter
import re

from ..models import AnalysisError, digest, decimal_text, decimal_value


def exact(table):
    return 'cellTargets' in table.get('request', {})


def require(condition, message):
    if not condition:
        raise AnalysisError('INVALID_EXACT_CELLS', message)


def validate(table, query):
    request, data = table['request'], table.get('data', {})
    targets, origins, results = request.get('cellTargets'), table.get('cellOrigins'), data.get('cellResults')
    require(isinstance(targets, list) and targets and isinstance(origins, list) and isinstance(results, list),
            '精确取数缺少目标、运行格来源或 cellResults。')
    require(query.get('cellTargets') == targets and query.get('cellSelections') == origins,
            '精确取数与服务端记录的运行格选择不一致。')
    expected = {item.get('targetRef'): item for item in targets}
    source = {item.get('targetRef'): item for item in origins}
    require(None not in expected and len(expected) == len(targets) == len(origins) == len(source) == len(results)
            and set(expected) == set(source), '精确目标与来源没有一一对应。')
    metrics = {item['name']: item for item in request['metrics']}
    metric_origins = {item['name']: item for item in table['metrics']}
    seen, counts, unique = set(), Counter(), set()
    for row in results:
        ref = row.get('targetRef')
        require(ref in expected and ref not in seen, '精确结果目标重复或超出请求。')
        seen.add(ref)
        target, origin = expected[ref], source[ref]
        from .budget_scenario import validate_origin
        scenario = validate_origin(origin)
        name = target.get('metric')
        require(name in metrics and row.get('metric') == name and origin.get('name') == name,
                '精确结果指标与目标不一致。')
        require(origin.get('context') == metric_origins[name].get('context')
                and origin.get('selectionObservationId') == metric_origins[name].get('selectionObservationId')
                and origin.get('selectionCellKey') and origin.get('selectedDimensions') == target.get('dimensions'),
                '精确目标来源与已确认的运行上下文不一致。')
        dimensions, names = row.get('dimensions'), row.get('dimensionNames')
        if scenario:
            require(isinstance(dimensions, dict) and dimensions.get('MD_SCENE') == scenario['toValue'],
                    '情景替换取数没有返回所要求的情景。')
        require(isinstance(dimensions, dict) and isinstance(names, dict)
                and {'MD_ORG', 'DATATIME'} <= dimensions.keys()
                and all(isinstance(value, str) and target['dimensions'].get(key) == value for key, value in dimensions.items())
                and all(key in dimensions and (value is None or isinstance(value, str)) for key, value in names.items()),
                '精确结果改变或扩大了目标坐标。')
        expression = metrics[name]['cellExp']
        physical = expression.split('[', 1)[0]
        logical = re.sub(r'_[NHJYXZRW]$', '', physical)
        require(row.get('modelCode') in (physical, logical) and row.get('measureCode') == origin.get('measureCode'),
                '精确结果模型或指标字段与来源不一致。')
        state, kind, value = row.get('valueState'), row.get('valueType'), row.get('value')
        require(state in ('PRESENT', 'NULL_VALUE', 'NO_DATA')
                and kind in ('DECIMAL_STRING', 'STRING', 'BOOLEAN', 'DATE', 'DATETIME'), '精确结果类型或状态未知。')
        require((type(value) is (bool if kind == 'BOOLEAN' else str)) if state == 'PRESENT' else value is None,
                '精确结果值与类型或空值状态不一致。')
        if state == 'PRESENT' and kind == 'DECIMAL_STRING':
            decimal_value(value)
        unique.add(digest([name, dimensions])); counts[state] += 1
    summary = data.get('cellSummary') or {}
    require(summary.get('requestedCount') == len(targets) and summary.get('uniqueCellCount') == len(unique)
            and summary.get('presentCount') == counts['PRESENT'] and summary.get('nullValueCount') == counts['NULL_VALUE']
            and summary.get('missingCount') == counts['NO_DATA'], '精确结果统计与明细不一致。')
    return data


def view(table):
    """Derived sparse view for existing scope selectors. It is never persisted as source data."""
    if not exact(table) or 'exactRowMetrics' in table:
        return table
    data = table['data']; results = data.get('cellResults', [])
    dimensions = sorted({key for row in results for key in row['dimensions']})
    metrics = [metric['name'] for metric in table['request']['metrics']]
    types = {row['metric']: row['valueType'] for row in results}
    columns = [{'name': code, 'type': 'STRING'} for code in dimensions]
    columns += [{'name': name, 'type': types.get(name, 'DECIMAL_STRING')} for name in metrics]
    roles = {code: {'kind': 'DIMENSION', 'dimCode': code, 'valueColumn': code} for code in dimensions}
    roles.update({name: {'kind': 'METRIC', 'metricName': name} for name in metrics})
    rows = [[row['dimensions'].get(code) for code in dimensions]
            + [row['value'] if name == row['metric'] else None for name in metrics] for row in results]
    return {**table, 'columnRoles': roles, 'exactRowMetrics': [row['metric'] for row in results],
            'data': {**data, 'columns': columns, 'rows': rows, 'rowCount': len(rows)}}


def catalog(capture, receipt, step_id, table, path, selections, max_facts):
    from .budget_v2 import budget_source_context, data_context
    from .budget_batch import selection_scope
    chosen = None if selections is None else [s for s in selections if s.get('stepId') == step_id and s.get('batchId') == table['batchId']]
    if chosen == []:
        return []
    ranges = None if chosen is None else [selection_scope(s, table) for s in chosen]
    source = budget_source_context(capture, receipt)
    origins = {item['targetRef']: item for item in table['cellOrigins']}
    evidence_ids = [e['evidenceId'] for e in receipt.get('evidence', []) if e.get('kind') == 'BUDGET_BATCH'
                    and e.get('fact', {}).get('batchId') == table['batchId']]
    facts = []
    table_digest = digest(table)
    for index, row in enumerate(table['data']['cellResults']):
        name = row['metric']
        if ranges is not None and not any(name in names and (indices is None or index in indices) for names, indices in ranges):
            continue
        origin = origins[row['targetRef']]; context = origin['context']
        from .budget_scenario import headers as scenario_headers
        headers = scenario_headers(origin, row['dimensionNames'])
        raw, kind = row['value'], row['valueType']
        value = decimal_text(decimal_value(raw)) if raw is not None and kind == 'DECIMAL_STRING' else raw
        facts.append({'factId': 'fact_' + digest([source['sourceContextId'], table['batchId'], row['targetRef']])[:24],
            'sourceContextId': source['sourceContextId'], 'captureRef': capture['captureRef'], 'executionId': receipt.get('executionId'),
            'context': receipt['context'], 'dataContext': data_context(receipt['context']), 'querySessionId': receipt['querySessionId'],
            'stepId': step_id, 'batchId': table['batchId'], 'rowIndex': index, 'columnName': name, 'targetRef': row['targetRef'],
            'sourcePointer': f'{path}/data/cellResults/{index}/value', 'bindingRef': context['target']['formBindingKey'],
            'formContext': context, 'expression': (origin.get('scenarioFallback') or {}).get('queryExpression', origin['runtimeCellExp']),
            'selectionObservationId': origin['selectionObservationId'],
            'selectionCellKey': origin['selectionCellKey'], 'formulaStatus': 'NO_TARGET_CELL' if origin.get('scenarioFallback') else 'NOT_CAPTURED',
            'scenarioFallback': origin.get('scenarioFallback'), 'measureCode': name,
            'sourceMeasureCode': row['measureCode'], 'queryMetricName': name,
            'metricKey': digest([origin['cellExp'], context['target']['viewId']]), 'headers': headers,
            'label': '_'.join(headers), 'formName': origin.get('formName'), 'taskName': origin.get('taskName'),
            'dimensionModels': origin.get('dimensionModels', {}), 'dimensions': row['dimensions'], 'propertyDimensions': [],
            'dimensionNames': row['dimensionNames'], 'effectiveDimensions': {}, 'period': row['dimensions'].get('DATATIME'),
            'executionPeriod': context['period'], 'selectionExecutionPeriod': context['period'],
            'selectionDataPeriod': row['dimensions'].get('DATATIME'), 'value': value,
            'valueState': 'NO_VALUE' if row['valueState'] == 'NULL_VALUE' else row['valueState'], 'sourceValueState': row['valueState'],
            'valueType': kind, 'precision': 'decimal_string' if kind == 'DECIMAL_STRING' else 'source_typed',
            'unit': row.get('unit') if kind == 'DECIMAL_STRING' else None, 'calcType': None, 'aggregation': None,
            'readAt': table.get('readAt'), 'evidenceIds': evidence_ids, 'tableDigest': table_digest})
        if len(facts) > max_facts:
            raise AnalysisError('FACT_LIMIT', '精确结果超过本次分析容量，完整回执仍可按引用读取。')
    return facts
