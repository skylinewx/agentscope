"""Named table rows shared by execution, model material and document consumers.

Positional rows are read only at existing native/storage boundaries. New Python
products use named values; neither row order nor display labels define a column.
"""
from copy import deepcopy

from .models import AnalysisError


CONTRACT = 'BUSINESS_DATASET_V1'


def names(columns):
    result = [column.get('name', column.get('key')) for column in columns]
    if (any(not isinstance(name, str) or not name for name in result)
            or len(set(result)) != len(result)):
        raise AnalysisError('INVALID_TABLE_FIELDS', '表格字段名必须非空且唯一，显示名称不作为字段键。')
    return result


def row_values(row, columns=()):
    """Read native or historical rows without changing their frozen originals."""
    if isinstance(row, dict):
        return row['values'] if wrapped_row(row, columns) else row
    if isinstance(row, list) and len(row) == len(columns):
        return dict(zip(names(columns), row))
    raise AnalysisError('INVALID_TABLE_ROW', '表格行与实际字段定义不一致。')


def wrapped_row(row, columns=()):
    return (isinstance(row, dict) and isinstance(row.get('values'), dict)
            and ('values' not in names(columns) or 'rowRef' in row and 'valueStates' in row))


def rows(columns, source, *, prefix='row', named_only=False, strict_fields=False):
    keys = names(columns)
    result, identities = [], set()
    for index, row in enumerate(source):
        if named_only and not isinstance(row, dict):
            raise AnalysisError('PYTHON_RESULT_INVALID',
                '表产物使用字段式行：{"values":{"实际列名":值}}，不使用列下标数组。',
                {'path': f'/rows/{index}', 'nextAction': 'FIX_OUTPUT_ROWS',
                 'columnNames': keys, 'requiresSourceQuery': False})
        try:
            values = row_values(row, columns)
        except AnalysisError as error:
            error.details = {'path': f'/rows/{index}', 'columnNames': keys,
                             'nextAction': 'FIX_OUTPUT_ROWS', 'requiresSourceQuery': False}
            raise
        wrapped = wrapped_row(row, columns)
        if (named_only or strict_fields) and set(values) - set(keys):
            raise AnalysisError('PYTHON_RESULT_INVALID', '表产物含未声明的字段，请同步 columns。',
                                {'path': f'/rows/{index}/values', 'unknownFields': sorted(set(values) - set(keys)),
                                 'columnNames': keys, 'nextAction': 'FIX_OUTPUT_ROWS', 'requiresSourceQuery': False})
        # Missing and null are different; never manufacture a value for a missing field.
        projected = {key: deepcopy(values[key]) for key in keys if key in values}
        states = {key: ('MISSING' if key not in values else 'NO_VALUE' if values[key] is None else 'PRESENT')
                  for key in keys}
        original_states = row.get('valueStates', {}) if wrapped else {}
        if not isinstance(original_states, dict) or set(original_states) - set(keys):
            raise AnalysisError('INVALID_TABLE_ROW', '取值状态必须对应本表实际字段。')
        for key, state in original_states.items():
            if state not in ('PRESENT', 'NO_VALUE', 'MISSING', 'FAILED', 'NOT_APPLICABLE'):
                raise AnalysisError('INVALID_TABLE_ROW', '表格取值状态无效。')
            if (state == 'PRESENT' and states[key] != 'PRESENT'
                    or state == 'MISSING' and states[key] != 'MISSING'
                    or state == 'NO_VALUE' and states[key] != 'NO_VALUE'
                    or state == 'FAILED' and states[key] == 'PRESENT'):
                raise AnalysisError('INVALID_TABLE_ROW', '表格取值状态与实际值矛盾。')
            states[key] = state
        identity = row.get('rowRef') if wrapped else None
        identity = identity or f'{prefix}:{index}'
        if not isinstance(identity, str) or not identity or identity in identities:
            raise AnalysisError('INVALID_TABLE_ROW', '表格行引用必须唯一。')
        identities.add(identity)
        item = {'rowRef': identity, 'values': projected, 'valueStates': states}
        if wrapped and 'metadata' in row:
            item['metadata'] = deepcopy(row['metadata'])
        elif isinstance(row, dict) and not wrapped:
            extra = {key: deepcopy(value) for key, value in row.items() if key not in keys}
            if extra: item['metadata'] = extra
        result.append(item)
    return result


def table(columns, source, *, ref='', kind='TABULAR', named_only=False):
    columns = deepcopy(columns)
    names(columns)
    converted = rows(columns, source, prefix=ref or 'row', named_only=named_only)
    return {'kind': kind, 'rowContract': CONTRACT, 'columns': columns,
            'rows': converted, 'rowCount': len(converted)}


def project(value):
    """Project table-valued native or stored results; non-table structures stay intact."""
    result = deepcopy(value)
    if result.get('outputKind') in ('TABLE', 'CHART_DATA') and 'columns' in result and 'rows' in result:
        data = table(result['columns'], result['rows'], ref=result.get('derivedId', result.get('ref', 'row')))
        result.update({key: data[key] for key in ('rowContract', 'columns', 'rows', 'rowCount')})
    return result


def model_tables(value):
    """Detach real Markdown from tool JSON; execution still receives named rows."""
    from .business_tables import encode, escape
    sections = []

    def visit(item, path='result'):
        if isinstance(item, list):
            return [visit(child, path + '/' + str(index)) for index, child in enumerate(item)]
        if not isinstance(item, dict):
            return item
        if item.get('rowContract') == CONTRACT and 'columns' in item and 'rows' in item:
            columns, source = item['columns'], item['rows']
            keys = names(columns)
            title = '业务表 ' + str(len(sections) + 1)
            lines = ['| 行引用 | ' + ' | '.join(escape(str(col.get('label', col['name'])) + ' [' + col['name'] + ']') for col in columns) + ' |',
                     '| --- | ' + ' | '.join('---' for _ in columns) + ' |']
            for row in source:
                cells = [escape(row['rowRef'])]
                for key in keys:
                    state = row['valueStates'].get(key)
                    raw = row['values'].get(key)
                    cells.append(escape('MISSING' if state == 'MISSING' else encode(raw)))
                lines.append('| ' + ' | '.join(cells) + ' |')
            sections.append(title + '（' + path + '）：\n' + '\n'.join(lines))
            result = {key: visit(child, path + '/' + key) for key, child in item.items() if key != 'rows'}
            result['rowsTable'] = title
            result['rowAnnotations'] = [{key: row[key] for key in ('rowRef', 'valueStates', 'metadata') if key in row}
                                        for row in source]
            return result
        return {key: visit(child, path + '/' + key) for key, child in item.items()}

    metadata = visit(value)
    return metadata, sections
