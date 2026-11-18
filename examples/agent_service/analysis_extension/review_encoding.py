"""核对模型专用的无损表格投影；原始回执和内部分析结构不变。"""
from copy import deepcopy
import json


ENCODING_GUIDE = ('仅当材料顶层 recordTableEncoding=1 时，_recordTable 表示原本有序的对象列表：每个 rows 行按 columns 还原字段，再合并 fixedFields；'
                  '若有 recordKeys，则按对应行的 key 还原为对象映射而非列表。valueSets 按列名存放重复值字典，该列行值为零起始索引，先展开字典再解释；'
                  'missingFields 按零基行号列出不存在的字段，必须删除，不能当成 null。'
                  '未列为缺失的 null 是显式空值，0/false 保持原类型；列表顺序、数值、引用和依赖均未删减。')


# 宿主公式核对结果须可直接阅读，不能再用字典索引掩盖本层/逐层差别。
DIRECT_FIELDS = {'factId', 'value', 'valueState', 'derivedId', 'formulaTrace'}

def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _contains_marker(value):
    if isinstance(value, dict):
        return '_recordTable' in value or any(_contains_marker(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_marker(child) for child in value)
    return False


def pack_review_records(material):
    """仅在结构明显变小时提取重复键；遇到来源同名字段保留原结构。"""
    if 'recordTableEncoding' in material or _contains_marker(material):
        return deepcopy(material)

    def pack_rows(rows, record_keys=None):
        if len(rows) < 4 or not all(isinstance(row, dict) for row in rows):
            return None
        columns = list(dict.fromkeys(key for row in rows for key in row))
        if len(columns) > 60:
            return None
        fixed = {key: rows[0][key] for key in columns if key in rows[0]
                 and key not in DIRECT_FIELDS
                 and all(key in row and _encode(row[key]) == _encode(rows[0][key]) for row in rows)}
        columns = [key for key in columns if key not in fixed]
        table = {'columns': columns, 'rows': [[row.get(key) for key in columns] for row in rows]}
        if record_keys is not None:
            table['recordKeys'] = record_keys
        if fixed:
            table['fixedFields'] = fixed
        missing = {str(index): [key for key in columns if key not in row]
                   for index, row in enumerate(rows) if any(key not in row for key in columns)}
        if missing:
            table['missingFields'] = missing
        # 仅对确有重复且明显缩短的背景/依赖列建立字典；事实身份与数值仍逐行直读。
        value_sets = {}
        for column_index, column in enumerate(columns):
            if column in DIRECT_FIELDS:
                continue
            values, indices, identities = [], [], {}
            original = [row[column_index] for row in table['rows']]
            for value in original:
                identity = _encode(value)
                if identity not in identities:
                    identities[identity] = len(values)
                    values.append(value)
                indices.append(identities[identity])
            if len(values) < len(original) and len(_encode({column: values})) + len(_encode(indices)) + 20 < len(_encode(original)) * .9:
                value_sets[column] = values
                for row, index in zip(table['rows'], indices):
                    row[column_index] = index
        if value_sets:
            table['valueSets'] = value_sets
        packed = {'_recordTable': table}
        original = rows if record_keys is None else dict(zip(record_keys, rows))
        return packed if len(_encode(packed)) < len(_encode(original)) * .9 else None

    def visit(value):
        if isinstance(value, dict):
            mapped = {key: deepcopy(child) if key == 'formulaTrace' else visit(child)
                      for key, child in value.items()}
            return pack_rows(list(mapped.values()), list(mapped)) or mapped
        if isinstance(value, list):
            rows = [visit(child) for child in value]
            return pack_rows(rows) or rows
        return value

    result = visit(material)
    if result != material:
        result['recordTableEncoding'] = 1
    return result


def unpack_review_records(material):
    """供同源审计逐字段还原投影，验证没有遗漏必要证据。"""
    if not isinstance(material, dict) or material.get('recordTableEncoding') != 1:
        return deepcopy(material)
    return _unpack({key: value for key, value in material.items() if key != 'recordTableEncoding'})


def _unpack(material):
    if isinstance(material, list):
        return [_unpack(child) for child in material]
    if not isinstance(material, dict):
        return material
    if set(material) == {'_recordTable'}:
        table = material['_recordTable']
        result = []
        for index, row in enumerate(table['rows']):
            fields = {key: table['valueSets'][key][value] if key in table.get('valueSets', {}) else value
                      for key, value in zip(table['columns'], row)}
            restored = {**table.get('fixedFields', {}), **fields}
            for key in table.get('missingFields', {}).get(str(index), []):
                restored.pop(key, None)
            result.append(_unpack(restored))
        return dict(zip(table['recordKeys'], result)) if 'recordKeys' in table else result
    return {key: _unpack(value) for key, value in material.items()}
