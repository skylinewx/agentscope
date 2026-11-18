"""预算批量 TABLE 回执：保留矩阵、查询项身份和真实分组粒度，不构造网格单元格。"""
from ..models import AnalysisError, Limits, decimal_text, decimal_value, digest
from ..sources import combined_context


def tables(receipt):
    for step_index, step in enumerate(receipt.get('data', {}).get('stepResults', [])):
        for table_index, table in enumerate(step.get('batchResults', [])):
            yield step.get('stepId'), table, f'/data/stepResults/{step_index}/batchResults/{table_index}'


def validate(capture, receipt, step_id, table):
    """核对原请求、固定上下文及完整表形状；失败或未执行的批量范围不可用于分析。"""
    from .budget_v2 import _step_request, _bindings, CONTEXT_FIELDS
    step, steps = _step_request(capture, step_id)
    if step.get('operation') != 'BATCH_DATA_QUERY' or table.get('resultType') != 'BATCH_RESULT':
        raise AnalysisError('REQUEST_MISMATCH', '批量结果不属于原批量查询步骤。')
    if table.get('disposition') != 'SUCCEEDED':
        raise AnalysisError('BATCH_INCOMPLETE', table.get('message') or '批量范围尚未执行完整。')
    query, request = step.get('batchQuery', {}), table.get('request', {})
    metrics, origins = request.get('metrics', []), table.get('metrics', [])
    if not metrics or len(metrics) != len(origins) or len(metrics) != len(query.get('metrics', [])):
        raise AnalysisError('REQUEST_MISMATCH', '批量查询项与原请求不一致。')
    bindings = _bindings(step, steps)
    for previous in receipt.get('data', {}).get('stepResults', []):
        if previous.get('stepId') in step.get('inputs', []):
            bindings.update(row['resourceIri'] for row in previous.get('rows', []) if row.get('resourceIri'))
    categories = set()
    for index, (metric, origin, requested) in enumerate(zip(metrics, origins, query['metrics'])):
        context, selection = origin.get('context', {}), requested.get('selection', {})
        target, base = context.get('target', {}), context.get('baseContext', {})
        if any(not receipt.get('context', {}).get(key) or base.get(key) != receipt['context'][key] for key in CONTEXT_FIELDS):
            raise AnalysisError('CONTEXT_CONFLICT', '批量定位与回执的固定上下文不一致。')
        name = 'metric' + str(index + 1)
        binding = target.get('formBindingKey')
        if (metric.get('name') != name or origin.get('name') != name or binding not in bindings
                or requested.get('bindingIri', binding) != binding or metric.get('viewId') != target.get('viewId')
                or request.get('budTaskId') != target.get('mainTaskId')
                or metric.get('cellExp') != origin.get('cellExp')
                or metric.get('dimValMap') != context.get('effectiveDimensions')):
            raise AnalysisError('REQUEST_MISMATCH', '批量表达式、查询项或任务绑定与原请求不一致。')
        if selection.get('periodCode', context.get('period')) != context.get('period'):
            raise AnalysisError('REQUEST_MISMATCH', '批量定位时期与原请求不一致。')
        for key, value in selection.get('dimensions', {}).items():
            if context.get('effectiveDimensions', {}).get(key) != value:
                raise AnalysisError('REQUEST_MISMATCH', '批量定位表外条件与原请求不一致。')
        selected = {**context.get('effectiveDimensions', {}), **origin.get('selectedDimensions', {})}
        if any(selected.get(key) != value for key, value in selection.get('targetDimensions', {}).items()):
            raise AnalysisError('REQUEST_MISMATCH', '批量目标维度与原请求不一致。')
        if (selection.get('measureCode', origin.get('measureCode')) != origin.get('measureCode')
                or selection.get('cellKey', origin.get('selectionCellKey')) != origin.get('selectionCellKey')
                or selection.get('observationId', origin.get('selectionObservationId')) != origin.get('selectionObservationId')
                or selection.get('expression') != origin.get('requestedExpression')
                or selection.get('headerText') and selection['headerText'] not in origin.get('headers', [])):
            raise AnalysisError('REQUEST_MISMATCH', '批量定位目标与原请求不一致。')
        categories.add(target.get('organizationCategory'))
    for key in ('resultWhere', 'orderBy', 'aggregation', 'having'):
        if query.get(key) != request.get(key):
            raise AnalysisError('REQUEST_MISMATCH', '批量筛选、聚合或排序与原请求不一致。')
    dimensions = []
    if len(categories) != 1 or None in categories:
        raise AnalysisError('REQUEST_MISMATCH', '批量任务机构类型不完整。')
    for condition in query.get('batchDimensions', []):
        resolved = dict(condition)
        dimensions.append(resolved)
    if dimensions != request.get('batchDimensions'):
        raise AnalysisError('REQUEST_MISMATCH', '实际批量范围与原请求不一致。')
    data = table.get('data', {})
    columns, rows = data.get('columns'), data.get('rows')
    if (not isinstance(columns, list) or not isinstance(rows, list) or data.get('rowCount') != len(rows)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
        raise AnalysisError('INVALID_BATCH_TABLE', '批量行列不完整或数量不一致。')
    names = [column.get('name') for column in columns]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise AnalysisError('INVALID_BATCH_TABLE', '批量列名缺失或重复。')
    roles = table.get('columnRoles', {})
    if set(roles) != set(names):
        raise AnalysisError('INVALID_BATCH_TABLE', '批量列角色缺失。')
    expected = {measure['metric'] for measure in query['aggregation']['measures']} if query.get('aggregation') else {metric['name'] for metric in metrics}
    if {name for name, role in roles.items() if role.get('kind') == 'METRIC'} != expected:
        raise AnalysisError('INVALID_BATCH_TABLE', '批量指标列与查询项不一致。')
    for row in rows:
        for index, column in enumerate(columns):
            value = row[index]
            expected_type = bool if column.get('type') == 'BOOLEAN' else str
            if value is not None and type(value) is not expected_type:
                raise AnalysisError('INVALID_BATCH_TABLE', '批量值必须保留来源声明类型，金额为十进制字符串。')
            if roles[column['name']].get('kind') == 'METRIC':
                if column.get('type') != 'DECIMAL_STRING':
                    raise AnalysisError('INVALID_BATCH_TABLE', '金额必须保留十进制字符串或 null。')
                if value is not None:
                    decimal_value(value)
    return data


def selection_scope(selection, table):
    """先校验选择器，再构造集合；避免逐行线性扫描大批行下标。"""
    names = selection.get('columnNames')
    available = [name for name, role in table['columnRoles'].items() if role.get('kind') == 'METRIC']
    # 单指标回执的选择是确定的，调用方可以省略内部列名；多指标仍须明确选择。
    if names is None and len(available) == 1:
        names = available
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
            or any(table['columnRoles'].get(name, {}).get('kind') != 'METRIC' for name in names)):
        raise AnalysisError('INVALID_SELECTOR', 'columnNames 必须引用实际指标列名。',
                            {'availableMetricColumns': available,
                             'hint': '维度列已随指标自动保留；使用 grain 指定分析粒度。'})
    indices = selection.get('rowIndices')
    if 'rowIndices' in selection and (not isinstance(indices, list)
            or any(type(index) is not int or not 0 <= index < table['data']['rowCount'] for index in indices)
            or len(set(indices)) != len(indices)):
        raise AnalysisError('INVALID_SELECTOR', 'rowIndices 必须使用真实、无重复的表行下标。')
    return set(names), None if indices is None else set(indices)


def catalog(capture, selections=None, max_facts=Limits().max_facts):
    from .budget_v2 import receipt_from_capture, budget_source_context, data_context
    receipt = receipt_from_capture(capture)
    source = budget_source_context(capture, receipt)
    result = []
    for step_id, table, path in tables(receipt):
        # 部分/失败批量结果可读取诊断，但不能作为数值事实。
        if table.get('disposition') != 'SUCCEEDED':
            continue
        data = validate(capture, receipt, step_id, table)
        selected = None if selections is None else [selection for selection in selections
                                                    if selection.get('stepId') == step_id and selection.get('batchId') == table['batchId']]
        if selected == []:
            continue
        selected_ranges = None if selected is None else [selection_scope(selection, table) for selection in selected]
        roles, columns = table['columnRoles'], data['columns']
        table_digest = digest(table)
        origins = {origin['name']: origin for origin in table['metrics']}
        evidence_ids = [e['evidenceId'] for e in receipt.get('evidence', []) if e.get('kind') == 'BUDGET_BATCH'
                        and e.get('fact', {}).get('batchId') == table.get('batchId')]
        for row_index, row in enumerate(data['rows']):
            row_selections = None if selected_ranges is None else [names for names, indices in selected_ranges
                                                                    if indices is None or row_index in indices]
            if row_selections == []:
                continue
            dimensions = {column['name']: row[index] for index, column in enumerate(columns)
                          if roles[column['name']]['kind'] in ('DIMENSION', 'PROPERTY')}
            dimension_names = {roles[column['name']]['valueColumn']: row[index] for index, column in enumerate(columns)
                               if roles[column['name']]['kind'] == 'NAME'}
            property_dimensions = [name for name, role in roles.items() if role['kind'] == 'PROPERTY']
            for dimension in property_dimensions:
                # 空属性和布尔属性保留原值，仅提供显示文本；成员引用由宿主按原类型签发。
                if dimensions[dimension] is None:
                    dimension_names[dimension] = '（属性为空）'
                elif type(dimensions[dimension]) is bool:
                    dimension_names[dimension] = str(dimensions[dimension]).lower()
            for column_index, column in enumerate(columns):
                name = column['name']
                if roles[name]['kind'] != 'METRIC':
                    continue
                if row_selections is not None and not any(name in names for names in row_selections):
                    continue
                origin = origins[name]
                effective = {**data.get('fixedDimValMap', {}), **column.get('fixedDimValMap', {}), **dimensions}
                names = {**data.get('fixedDimNameMap', {}), **column.get('fixedDimNameMap', {}), **dimension_names}
                raw = row[column_index]
                if raw is not None and (not isinstance(raw, str) or column.get('type') != 'DECIMAL_STRING'):
                    raise AnalysisError('INVALID_BATCH_TABLE', '金额必须保留十进制字符串或 null。')
                value = None if raw is None else decimal_text(decimal_value(raw))
                result.append({'factId': 'fact_' + digest([source['sourceContextId'], table['batchId'], row_index, name])[:24],
                               'sourceContextId': source['sourceContextId'], 'captureRef': capture['captureRef'],
                               'executionId': receipt.get('executionId'), 'context': receipt['context'],
                               'dataContext': data_context(receipt['context']), 'querySessionId': receipt['querySessionId'],
                               'stepId': step_id, 'batchId': table['batchId'], 'rowIndex': row_index, 'columnName': name,
                               'sourcePointer': f'{path}/data/rows/{row_index}/{column_index}',
                               'bindingRef': origin['context']['target']['formBindingKey'],
                               'formContext': origin['context'], 'expression': origin['cellExp'],
                               'selectionObservationId': origin.get('selectionObservationId'),
                               'formulaStatus': 'NOT_CAPTURED',
                               'measureCode': name, 'sourceMeasureCode': origin.get('measureCode'),
                               'queryMetricName': name, 'metricKey': digest([origin['cellExp'], origin['context']['target']['viewId']]),
                               'headers': [column.get('label', name)], 'label': column.get('label', name),
                               'formName': origin.get('formName'), 'taskName': origin.get('taskName'),
                               'dimensionModels': origin.get('dimensionModels', {}), 'dimensions': effective,
                               'propertyDimensions': property_dimensions,
                               'dimensionNames': names, 'effectiveDimensions': {},
                               'period': effective.get('DATATIME'), 'executionPeriod': effective.get('DATATIME'),
                               'value': value, 'valueState': 'NO_VALUE' if raw is None else 'PRESENT',
                               'precision': 'decimal_string', 'unit': column.get('unit'),
                               'calcType': column.get('calcType'), 'aggregation': table['request'].get('aggregation'),
                               'readAt': table.get('readAt'), 'evidenceIds': evidence_ids,
                               'tableDigest': table_digest})
                if len(result) > max_facts:
                    raise AnalysisError('FACT_LIMIT', '所选批量事实超过分析容量；普通查询仍可从 QueryResultRead 读取完整原回执。')
    return result


def selectors(capture):
    from .budget_v2 import receipt_from_capture
    result = []
    for step_id, table, path in tables(receipt_from_capture(capture)):
        data = table.get('data', {})
        result.append({'captureRef': capture['captureRef'], 'stepId': step_id, 'batchId': table.get('batchId'),
                       'disposition': table.get('disposition'), 'rowCount': data.get('rowCount'),
                       'columnNames': [name for name, role in table.get('columnRoles', {}).items() if role.get('kind') == 'METRIC'],
                       'memberDimensions': [name for name, role in table.get('columnRoles', {}).items() if role.get('kind') in ('DIMENSION', 'PROPERTY')],
                       'rowIndicesOptional': True, 'rowsPointer': path + '/data/rows',
                       'columnsPointer': path + '/data/columns'})
    return result


def adapt(captures, selections, scope, max_facts):
    from .budget_v2 import receipt_from_capture, budget_source_context, data_context
    from ..actions import source_identity
    receipts = {ref: receipt_from_capture(capture) for ref, capture in captures.items()}
    data_refs = {selection.get('captureRef') for selection in selections}
    if not data_refs or not data_refs <= captures.keys():
        raise AnalysisError('UNKNOWN_CAPTURE', '批量选择器引用未纳入的回执。')
    contexts = {ref: budget_source_context(captures[ref], receipts[ref]) for ref in data_refs}
    boundaries = {digest([source_identity(captures[ref]), data_context(receipts[ref]['context'])]) for ref in data_refs}
    if len(boundaries) != 1 and scope.get('contextMode', 'SINGLE_SNAPSHOT') != 'COMPOSITE':
        raise AnalysisError('CONTEXT_CONFLICT', '批量分析跨来源或快照时必须声明 COMPOSITE。')
    members, references = scope.get('members', []), scope.get('referenceMembers', [])
    if (not isinstance(members, list) or not members or not isinstance(references, list)
            or any(not (isinstance(member, str) and member or member is None or type(member) is bool) for member in members + references)
            or len(set(members + references)) != len(members + references)):
        raise AnalysisError('INVALID_SCOPE', '主体与比较对象必须是不重复的原始维值。')
    dimension = scope.get('memberDimension')
    selected_members = set(members + references)
    catalogs = {ref: catalog(captures[ref], [selection for selection in selections if selection.get('captureRef') == ref], max_facts)
                for ref in data_refs}
    facts = {}
    complete = True
    populations = []
    for selection in selections:
        ref = selection['captureRef']
        matches = [(sid, table) for sid, table, _ in tables(receipts[ref])
                   if sid == selection.get('stepId') and table.get('batchId') == selection.get('batchId')]
        if len(matches) != 1:
            raise AnalysisError('UNKNOWN_BATCH', '请使用 selectors 返回的 stepId 与 batchId。')
        sid, table = matches[0]
        data = validate(captures[ref], receipts[ref], sid, table)
        role = table['columnRoles'].get(dimension, {})
        if role.get('kind') not in ('DIMENSION', 'PROPERTY') and dimension not in data.get('fixedDimValMap', {}):
            raise AnalysisError('BATCH_GRAIN_MISMATCH', 'memberDimension 必须是实际结果粒度，不能用原机构维度替代属性分组列。',
                                {'path': '/scope/memberDimension', 'memberDimension': dimension,
                                 'availableDimensions': [name for name, role in table['columnRoles'].items()
                                                         if role['kind'] in ('DIMENSION', 'PROPERTY')],
                                 'captureRef': ref, 'stepId': sid, 'batchId': table['batchId']})
        column_names, row_set = selection_scope(selection, table)
        own = [item for item in catalogs[ref] if item['batchId'] == table['batchId']]
        member_index = next((index for index, column in enumerate(data['columns']) if column['name'] == dimension), None)
        populations.append({row[member_index] if member_index is not None else data.get('fixedDimValMap', {}).get(dimension)
                            for row in data['rows']})
        complete &= receipts[ref].get('completeness', {}).get('enumerationExhausted') is True
        for item in own:
            if row_set is not None and item['rowIndex'] not in row_set or item['columnName'] not in column_names:
                continue
            member = item['dimensions'].get(dimension)
            if dimension not in item['dimensions'] or member not in selected_members:
                raise AnalysisError('SCOPE_MISMATCH', '批量结果成员未包含在声明的分析范围内。',
                                    {'path': '/scope/members', 'memberDimension': dimension, 'sourceObjectId': member,
                                     'captureRef': ref, 'stepId': sid, 'batchId': table['batchId'],
                                     'rowIndex': item['rowIndex'], 'columnName': item['columnName']})
            if any(item['dimensions'].get(key) != value for key, value in scope.get('expectedDimensions', {}).items()):
                raise AnalysisError('SCOPE_MISMATCH', '批量事实不满足声明的共同固定条件。',
                                    {'path': '/scope/expectedDimensions', 'captureRef': ref, 'rowIndex': item['rowIndex'],
                                     'conflicts': {key: {'expected': value, 'actual': item['dimensions'].get(key)}
                                                   for key, value in scope['expectedDimensions'].items()
                                                   if item['dimensions'].get(key) != value}})
            label = item['dimensionNames'].get(dimension)
            fact = {**item, 'captureRefs': [ref], 'objectId': member,
                    'objectLabel': label or '', 'objectLabelState': 'RESOLVED' if label else 'MISSING',
                    'objectModelCode': item['dimensionModels'].get(dimension),
                    'scale': '1' if item.get('unit') else None,
                    'scaleBasis': 'source_unit_label' if item.get('unit') else None,
                    'cellKey': None, 'cellAddress': None, 'observationId': None,
                    'rawValueJson': None if item['value'] is None else '"' + item['value'] + '"'}
            previous = facts.get(fact['factId'])
            if previous:
                if previous['tableDigest'] != fact['tableDigest']:
                    previous['valueState'] = 'CONFLICT'
                previous['captureRefs'] = sorted(set(previous['captureRefs'] + [ref]))
            else:
                facts[fact['factId']] = fact
            if len(facts) > max_facts:
                raise AnalysisError('FACT_LIMIT', '批量分析事实数量超过上限。')
    observed = {fact['objectId'] for fact in facts.values()}
    population_complete = complete and bool(populations) and set().union(*populations) == selected_members
    # 结果筛选后的全集可用于“当前结果”分析，不能推定为未筛选的全部机构/全部板块。
    if scope.get('population'):
        from .budget_v2 import BudgetV2Adapter
        population = scope['population']
        if population.get('captureRef') not in captures:
            raise AnalysisError('UNKNOWN_POPULATION', '范围回执未纳入分析。')
        population_complete = BudgetV2Adapter().population(captures[population['captureRef']], population, members)
    result_facts = list(facts.values())
    for member in sorted(selected_members - observed, key=str):
        result_facts.append({'factId': 'missing_' + digest(member)[:24], 'objectId': member,
                             'value': None, 'valueState': 'NOT_QUERIED', 'captureRefs': [], 'unit': None, 'scale': None})
    if len(result_facts) > max_facts:
        raise AnalysisError('FACT_LIMIT', '包含待查询对象后的事实数量超过上限。')
    selected_complete = complete and observed == selected_members
    from ..batch_scope import selection_audit
    source_scopes = selection_audit(captures, selections, scope)
    selected_complete &= not any(item['missingSelectedCellCount'] for item in source_scopes)
    return {'facts': result_facts, 'context': combined_context(list(contexts.values())), 'sourceContexts': list(contexts.values()),
            'scope': {**scope, 'scopeId': 'scope_' + digest(scope)[:24], 'semanticCompleteness': 'UNVERIFIED',
                      'coverage': 'COMPLETE' if population_complete and selected_complete else 'PARTIAL' if not selected_complete else 'UNKNOWN',
                      'populationBasis': 'BUDGET_QUERY_RESULT', 'populationComplete': population_complete,
                      'sourceScopes': source_scopes,
                      'sourcePagesComplete': complete, 'selectedDataComplete': selected_complete,
                      'observedMembers': sorted(set(members) & observed, key=str), 'pendingMembers': sorted(set(members) - observed, key=str),
                      'observedReferenceMembers': sorted(set(references) & observed, key=str), 'pendingReferenceMembers': sorted(set(references) - observed, key=str),
                      'periodCoverage': [], 'excludedMembers': scope.get('excludedMembers', [])}}
