"""Shared, deterministic grid filtering and value projection; no execution authority."""
import json
import re

GRID_WHERE_FIELDS = {'cellKeys', 'dimensions', 'measureCode', 'headerText', 'mapTypes',
                    'startRow', 'endRowExclusive', 'startColumn', 'endColumnExclusive'}


def validate_where(where):
    if not isinstance(where, dict) or set(where) - GRID_WHERE_FIELDS:
        raise ValueError('grid_rows 筛选字段不合法；可用字段：' + ', '.join(sorted(GRID_WHERE_FIELDS)))
    for key, value in where.items():
        if key in ('cellKeys', 'mapTypes'):
            valid = isinstance(value, list) and len(value) <= 100000 and all(isinstance(v, str) for v in value)
        elif key == 'dimensions':
            valid = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        elif key in ('measureCode', 'headerText'):
            valid = isinstance(value, str)
        else:
            valid = type(value) is int and 0 <= value <= 2**31
        if not valid:
            raise ValueError('grid_rows 筛选字段类型不合法：' + key)
    return json.loads(json.dumps(where))


def grid_matches(cell, dimensions, where):
    return not ('cellKeys' in where and cell.get('key') not in where['cellKeys']
        or 'measureCode' in where and cell.get('measureCode') != where['measureCode']
        or 'mapTypes' in where and cell.get('mapType') not in where['mapTypes']
        or 'headerText' in where and where['headerText'] not in cell.get('headers', [])
        and where['headerText'] not in (cell.get('displayValue') or '')
        or any(dimensions.get(key) != value for key, value in where.get('dimensions', {}).items())
        or cell['row'] < where.get('startRow', 0) or cell['row'] >= where.get('endRowExclusive', 2**31)
        or cell['column'] < where.get('startColumn', 0) or cell['column'] >= where.get('endColumnExclusive', 2**31))


def grid_value(cell, number_format=None):
    raw = cell.get('rawValueJson')
    value = json.loads(raw, parse_float=str) if raw is not None else None
    if (cell.get('measureCode') and isinstance(value, str) and number_format == 'grouped-dot'
            and re.fullmatch(r'-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?', value)):
        return value.replace(',', '')
    return value
