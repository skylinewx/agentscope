"""从已捕获的批量选择器确定成员，并保留原查询与分析选择之间的差异。"""
from copy import deepcopy

from .models import AnalysisError, digest


def selected_tables(captures, selections):
    """只接受真实批量身份；不按显示名称或数组顺序猜表。"""
    from .adapters.budget_batch import tables, validate, selection_scope
    from .adapters.budget_v2 import receipt_from_capture
    # One invocation reads immutable captures. A selected cell can have its
    # own selector; decoding and validating the entire receipt per selector
    # makes follow-up checks quadratic in the size of a large result.
    indexed, prepared = {}, {}
    for selection in selections:
        if 'batchId' not in selection:
            continue
        ref = selection.get('captureRef')
        if ref not in captures:
            raise AnalysisError('UNKNOWN_CAPTURE', '选择器引用的回执未纳入本次分析。')
        capture = captures[ref]
        if ref not in indexed:
            receipt = receipt_from_capture(capture)
            by_identity = {}
            for step, table, path in tables(receipt):
                from .budget_dimensions import dimension_labels, source_models
                models = source_models([*table.get('metrics', []), *table.get('data', {}).get('cellResults', [])])
                if models:
                    table = {**table, 'dimensionLabels': dimension_labels(receipt.get('data', {}).get('dimensionCatalog', []), models=models)}
                by_identity.setdefault((step, table.get('batchId')), []).append((step, table, path))
            indexed[ref] = receipt, by_identity
        receipt, by_identity = indexed[ref]
        identity = selection.get('stepId'), selection['batchId']
        matches = by_identity.get(identity, [])
        if len(matches) != 1:
            raise AnalysisError('UNKNOWN_BATCH', '请使用 selectors 返回的 stepId 与 batchId。')
        step, table, path = matches[0]
        key = ref, *identity
        if key not in prepared:
            validate(capture, receipt, step, table)
            from .adapters.budget_exact import view
            prepared[key] = view(table)
        table = prepared[key]
        columns, rows = selection_scope(selection, table)
        yield selection, table, path, columns, rows


def resolve_members(registry, captures, selections, scope, max_members):
    """省略 members 时按实际所选行生成有来源的成员引用，空属性也保留。"""
    if 'members' in scope:
        return scope
    if not selections or any('batchId' not in selection for selection in selections):
        raise AnalysisError('INVALID_SCOPE', '仅批量 TABLE 支持省略 members；其他来源使用 inventory 的成员身份。')
    scope = deepcopy(scope)
    tables = list(selected_tables(captures, selections))
    dimension = scope.get('memberDimension')
    if not dimension:
        grains = [{name for name, role in table['columnRoles'].items()
                   if role['kind'] in ('DIMENSION', 'PROPERTY')} for _, table, _, _, _ in tables]
        common = set.intersection(*grains) if grains else set()
        if len(common) != 1:
            raise AnalysisError('BATCH_GRAIN_MISMATCH', '存在多个结果维度，请从实际列中选择分析主体维度。',
                                {'path': '/scope/memberDimension', 'availableDimensions': sorted(common)})
        dimension = common.pop()
        scope['memberDimension'] = dimension
    from .member_scopes import member_ref
    members = {}
    references = set(scope.get('referenceMembers', []))
    unmatched = []
    for selection, table, _, selected_columns, rows in tables:
        if selection.get('rowIndices') and rows == set() and 'exactRowMetrics' in table:
            unmatched.append((selection, table, sorted(selected_columns)))
            continue
        columns = table['data']['columns']
        index = next((i for i, column in enumerate(columns) if column['name'] == dimension
                      and table['columnRoles'][dimension]['kind'] in ('DIMENSION', 'PROPERTY')), None)
        capture = captures[selection['captureRef']]
        namespace = registry.for_capture(capture).member_namespace(capture, dimension)
        if index is None and dimension not in table['data'].get('fixedDimValMap', {}):
            # A budget metric column can carry its own real dimension (e.g.
            # account or channel). Read the validated cell coordinates, not
            # just the matrix row headers, and retain the selection boundary.
            from .adapters.budget_batch import catalog
            cells = catalog(capture, [selection], max_members)
            available = set.intersection(*(set(item['dimensions']) for item in cells)) if cells else set()
            if dimension not in available:
                raise AnalysisError('BATCH_GRAIN_MISMATCH', '分析主体必须来自所选数据的真实行维度、列固定维度或表外维度。',
                                    {'path': '/scope/memberDimension', 'memberDimension': dimension,
                                     'availableDimensions': sorted(available)})
            raw_members = [item['dimensions'][dimension] for item in cells]
        else:
            raw_members = [row[index] if index is not None else table['data']['fixedDimValMap'][dimension]
                           for row_index, row in enumerate(table['data']['rows'])
                           if rows is None or row_index in rows]
        for raw in raw_members:
            ref = member_ref(namespace, raw)
            if ref not in references and raw not in references:
                members[ref] = None
            if len(members) > max_members:
                raise AnalysisError('FACT_LIMIT', '批量成员数量超过分析容量，原始查询结果仍可读取。')
    if not members:
        if unmatched:
            from .adapters.budget_batch import selectors
            selection, table, names = unmatched[0]
            capture = captures[selection['captureRef']]
            details = {'path': '/selections/rowIndices', 'metricColumns': names,
                       'submittedRowCount': len(selection['rowIndices']),
                       'nativeRowsForColumns': sum(name in names for name in table['exactRowMetrics']),
                       'selectionRead': {'section': 'selectors', 'captureRef': selection['captureRef']}}
            returned = [entry for entry in selectors(capture) if entry['stepId'] == selection['stepId']
                        and entry['batchId'] == selection['batchId'] and set(names) <= set(entry['columnNames'])]
            if len(returned) == 1 and returned[0].get('rowIndicesOptional') and not references:
                details['ifUsingAllReturnedRows'] = {key: selection[key] for key in (
                    'captureRef', 'stepId', 'batchId', 'columnNames', 'role', 'grain') if key in selection}
            raise AnalysisError('PREPARATION_NEEDS_SELECTION',
                '所填原始行号没有命中所选指标。业务表合并展示的行号不能用作原始行号。'
                '若目标是这些指标的全部返回行，可采用提供的完整选区；只取部分成员请沿用已筛选来源或核对原始选择器。无需重新取数。', details)
        raise AnalysisError('EMPTY_SELECTION', '所选批量结果没有主体成员。')
    scope['members'] = list(members)
    return scope


def selection_audit(captures, selections, scope):
    """按原始表合并重复选择器；记录遗漏行，禁止未声明的整组排除。"""
    records = {}
    dimension = scope.get('memberDimension')
    for selection, table, path, columns, rows in selected_tables(captures, selections):
        identity = (selection['captureRef'], selection['stepId'], selection['batchId'])
        record = records.setdefault(identity, {'table': table, 'path': path, 'columns': set(), 'rows': set(), 'columnRows': {},
                                               'selectedRange': selection.get('role') == 'REFERENCE' or bool(selection.get('selectionReason'))})
        record['columns'].update(columns)
        selected = set(range(table['data']['rowCount'])) if rows is None else rows
        record['rows'].update(selected)
        for column in columns:
            record['columnRows'].setdefault(column, set()).update(selected)
    audits = []
    for (ref, step, batch), record in records.items():
        table, data = record['table'], record['table']['data']
        columns = data['columns']
        index = next((i for i, column in enumerate(columns) if column['name'] == dimension), None)
        excluded = [item for item in scope.get('excludedMembers', []) if item.get('captureRef') == ref]
        # 此函数由适配器校验成员身份后执行，scope 中的成员仍为来源原值。
        declared = {digest(value) for value in scope.get('members', []) + scope.get('referenceMembers', [])}
        excluded_ids = {digest(item.get('objectId')) for item in excluded if item.get('reason')}
        omitted, unacknowledged, missing_selected_cells = [], [], 0
        value_columns = [i for i, column in enumerate(columns)
                         if table['columnRoles'][column['name']]['kind'] != 'METRIC'
                         or column['name'] in record['columns']]
        for row_index, row in enumerate(data['rows']):
            missing_columns = sorted(name for name, selected in record['columnRows'].items() if row_index not in selected)
            if 'exactRowMetrics' in table:
                missing_columns = [name for name in missing_columns if name == table['exactRowMetrics'][row_index]]
            if not missing_columns:
                continue
            raw = row[index] if index is not None else data.get('fixedDimValMap', {}).get(dimension)
            omitted.append({'rowIndex': row_index, 'missingColumns': missing_columns,
                            'values': {columns[i]['name']: row[i] for i in value_columns}})
            if digest(raw) in declared and not record['selectedRange']:
                missing_selected_cells += len(missing_columns)
            if digest(raw) not in declared | excluded_ids and not record['selectedRange']:
                unacknowledged.append(row_index)
        if unacknowledged:
            raise AnalysisError('UNDECLARED_SCOPE_EXCLUSION', '所选行排除了原查询返回的成员；保留全量，或显式声明排除对象和原因。',
                                {'path': '/scope/excludedMembers', 'captureRef': ref, 'stepId': step, 'batchId': batch,
                                 'omittedRowCount': len(unacknowledged), 'rowIndices': unacknowledged[:20],
                                 'nextRead': {'captureRef': ref, 'pointer': record['path'] + '/data/rows'}})
        audits.append({'captureRef': ref, 'stepId': step, 'batchId': batch,
                       'memberDimension': dimension, 'returnedRowCount': data['rowCount'],
                       'selectedRowCount': len(record['rows']), 'omittedRows': omitted,
                       'missingSelectedCellCount': missing_selected_cells,
                       'columnSelection': [{'columnName': name, 'selectedRowCount': len(selected)}
                                           for name, selected in sorted(record['columnRows'].items())],
                       'columns': columns, 'columnRoles': table['columnRoles'], 'dimensionLabels': table.get('dimensionLabels', {}),
                       'request': {key: table['request'][key] for key in
                                   ('budTaskId', 'batchDimensions', 'resultWhere', 'aggregation', 'having', 'orderBy')
                                   if key in table['request']},
                       'fixedDimValMap': data.get('fixedDimValMap', {}),
                       'rangeSummary': data.get('rangeSummary', {}),
                       'aggregationSummary': data.get('aggregationSummary'),
                       'excludedMembers': excluded})
    from .aggregation_explanation import annotate
    return annotate(audits)
