"""Business conditions and source values for a point query."""
from .business_tables import cell_text, data_periods, escape, measure_label, result_metric


def single_value_targets(data, *, separate_targets=False):
    """Recognize the full executed query before any table selection or pagination."""
    results = data.get('resultSets', [])
    if (data.get('purpose') != 'QUERY' or data.get('selection', 'CELLS') != 'CELLS'
            or data.get('valueFilter') or data.get('grouping') or (data.get('presentation') or {}).get('valueOrder')
            or not results or any(len(result.get('rows', [])) > 1 for result in results)):
        return None
    draft = (data.get('effectiveRequest') or {}).get('intentDraft') or {}
    one_member_scopes = {scope['id'] for scope in draft.get('dimensionScopes') or []
                         if scope.get('id') and (scope.get('selector') or {}).get('type') == 'VALUES'
                         and len(scope['selector'].get('members') or []) == 1}
    for scope in data.get('scopeResolution', []):
        condition = scope.get('condition') or {}
        if condition.get('all') or condition.get('hierarchy') or condition.get('where'):
            return None
        values = condition.get('values') or []
        if len(values) > 1:
            # One requested member can resolve to several source readings. Show
            # them individually only when every resolved code has its own fact.
            observed = {row.get('dimensions', {}).get(condition.get('dimCode')) for r in results for row in r.get('rows', [])}
            if scope.get('id') not in one_member_scopes or observed != set(values):
                return None
    targets = {target.get('id'): target for target in data.get('targets', [])}
    target_ids = set(targets) | {result['targetId'] for result in results if result.get('targetId')}
    if not separate_targets and len({targets.get(key, {}).get('logicalTargetId') or key for key in target_ids}) > 1:
        return None
    only_target = next(iter(targets.values())) if len(targets) == 1 else {}
    return {result.get('resultSetId', ''): result_metric(result, targets.get(result.get('targetId'), only_target))
            for result in results}


def source_name(table):
    return table.context.get('formName') or '业务表名称待确认'


def _conditions(table, target_label):
    from .display_tables import field_name, member_name
    fact = table.facts[0] if table.facts else {}
    context = table.context
    conditions = {}
    if target_label:
        conditions['target'] = ('指标', target_label, target_label)
    task_code = (context.get('target') or {}).get('taskCode')
    task = context.get('taskName') or task_code
    if task:
        conditions['task'] = ('任务', task_code, task)
    effective = context.get('effectiveDimensions') or {}
    dimensions = fact.get('dimensions') or {}
    period = data_periods(table)
    if period:
        conditions['period'] = ('数据时期', dimensions.get('DATATIME') or fact.get('dataPeriod') or period, period)
    for code, member in dimensions.items():
        if code != 'DATATIME':
            conditions[('dimension', code)] = (field_name(table.labels, code),
                                               member, member_name(fact, code))
    loading_fields = {field['code']: field for field in (context.get('dimensions') or [])
                      if isinstance(field, dict) and field.get('code')}
    for code, member in effective.items():
        if code in dimensions or code == 'DATATIME' or member is None:
            continue
        field = loading_fields.get(code, {})
        label = field_name(table.labels, code)
        name = field.get('valueLabel') if field.get('value') == member else None
        value = (context.get('dimensionNames') or {}).get(code) or name or '名称待确认'
        conditions[('loadingDimension', code)] = (f'数据加载条件（{label}）', member, value)
    if fact.get('sourceTitle'):
        conditions['title'] = ('来源标题', fact['sourceTitle'], fact['sourceTitle'])
    return conditions


def prepare_single_values(data, tables, *, separate_targets=False):
    targets = single_value_targets(data, separate_targets=separate_targets)
    if targets is None:
        return
    for table in tables:
        table.single_value_target = targets.get(table.set_id) or ''
        column = table.columns[0]
        fact = table.facts[0] if table.facts else {}
        if not table.scenario and column['label'] == column.get('measureCode') and not fact.get('measureTitle'):
            # Keep the source header selectable for saved reads and calculations.
            column['sourceLabel'] = column['label']
            column['label'] = (fact.get('dimensionNames') or {}).get('MD_SCENE') or table.single_value_target or '查询值'


def _row(values):
    return '| ' + ' | '.join(escape(value) for value in values) + ' |'


def single_value_markdown(tables):
    conditions = [_conditions(table, table.single_value_target) for table in tables]
    common = {key: field for key, field in conditions[0].items()
              if all(other.get(key) == field for other in conditions[1:])}
    lines = ['**查询条件**', '', _row(['项目', '内容']), _row(['---', '---'])]
    lines.extend(_row([label, value]) for label, _, value in common.values())
    if not common:
        lines.append(_row(['范围', '各来源条件分别列于下表']))
    differing = any(set(fields) - set(common) - {'target'} for fields in conditions)
    headers = ['业务表', '指标'] + (['本表条件'] if differing else []) + ['查询值', '单位']
    lines.extend(['', '**查询结果**', '', _row(headers), _row(['---'] * len(headers))])
    for table, fields in zip(tables, conditions):
        col = table.columns[0]
        source_metric = measure_label(table.facts[0]) if table.facts else None
        if source_metric == col.get('measureCode'):
            source_metric = None
        metric = table.single_value_target or source_metric or '查询指标'
        if col.get('valueType') in ('STRING', 'BOOLEAN', 'DATE', 'DATETIME'):
            metric = table.column_label(col, metric)
        detail = '；'.join(f'{label}：{value}' for key, (label, _, value) in fields.items() if key not in common and key != 'target')
        lines.append(_row([source_name(table), metric] + ([detail] if differing else []) +
                          [cell_text(table.facts[0], grouped=True) if table.facts else '无数据', col.get('unit') or '']))
    return '\n'.join(lines)
