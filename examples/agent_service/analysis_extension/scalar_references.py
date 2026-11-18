"""计算、工作项与页面共用的标量引用；只读取已冻结产物，不推测业务范围。"""
from copy import deepcopy

from .models import AnalysisError, decimal_value, digest


def reference_id(reference):
    return reference.get('valueRef') if isinstance(reference, dict) else reference


def normalize_reference(reference, aliases=None):
    """保留字段选择，统一解析同一计划中的运算别名。"""
    if isinstance(reference, str) and reference:
        return (aliases or {}).get(reference, reference)
    if (not isinstance(reference, dict) or set(reference) - {'valueRef', 'field', 'nodeRef'}
            or not isinstance(reference.get('valueRef'), str) or not reference['valueRef']
            or any(not isinstance(value, str) or not value for value in reference.values())):
        raise AnalysisError('INVALID_SCALAR_REFERENCE', '引用使用真实 ID，或 {valueRef, field?, nodeRef?}；不接受原始金额。',
                            {'requiresSourceQuery': False})
    result = deepcopy(reference)
    result['valueRef'] = (aliases or {}).get(result['valueRef'], result['valueRef'])
    return result


def reference_key(reference):
    normalized = normalize_reference(reference)
    binding = {'valueRef': normalized} if isinstance(normalized, str) else normalized
    return digest({**binding, 'field': binding.get('field', 'value')})


def statistical_members(value, field):
    """按冻结的统计行选择字段主体；比例的分母依赖另由来源链完整保留。"""
    rows = value.get('rows', [])
    valid = [row for row in rows if row[3] is not None]
    if field in ('value', 'sum', 'mean', 'observed'):
        return [row[0] for row in valid]
    if field == 'total':
        return [row[0] for row in rows]
    if field == 'unavailable':
        return [row[0] for row in rows if row[3] is None]
    if field in ('positive', 'positiveSum', 'negative', 'negativeSum', 'zero'):
        def matches(raw):
            number = decimal_value(raw)
            return number > 0 if field.startswith('positive') else number < 0 if field.startswith('negative') else number == 0
        return [row[0] for row in valid if matches(row[3])]
    if field in ('topSum', 'topShare'):
        return [row[0] for row in valid[:value.get('top') or 0]]
    if field in ('minimum', 'maximum'):
        target = value.get('statistics', {}).get(field)
        return [row[0] for row in valid if target is not None and decimal_value(row[3]) == decimal_value(target)]
    raise AnalysisError('INVALID_VALUE_REF', '统计结果不存在所选字段。', {'field': field})


def scalar_unit(value, field):
    if field in value.get('counts', {}):
        return '项'
    if value.get('operator') == 'DESCRIBE' and field == 'topShare':
        return '%'
    return value.get('unit')


def scalar_coverage(value, reference, known=None):
    """字段覆盖只证明该字段参与对象，不能用整个分布替代一个子集。"""
    field = reference.get('field', 'value') if isinstance(reference, dict) else 'value'
    if value.get('operator') == 'DESCRIBE':
        return set(statistical_members(value, field))
    if value.get('operator') == 'COUNT':
        # COUNT 的字段同样只覆盖参与该计数的对象，不能借全部输入扩大负值/缺值子集。
        if known is None:
            raise AnalysisError('COUNT_INPUTS_REQUIRED', '字段覆盖需要已冻结的计数输入。')
        items = [known[ref] for ref in value['inputRefs']]
        selected_field = 'observed' if field == 'value' else field
        rows = [[item['factId'], None, None, item.get('value') if item.get('valueState') == 'PRESENT' else None] for item in items]
        return set(statistical_members({'rows': rows}, selected_field))
    if isinstance(reference, dict) and reference.get('nodeRef'):
        return {reference['nodeRef']}
    if value.get('operator') == 'FORMULA_CHECK':
        return {value['formulaTrace']['targetFactRef']}
    if 'selectedCoverageRefs' in value:
        return set(value['selectedCoverageRefs'])
    return set(value.get('sourceFactIds', value.get('inputRefs', [value.get('factId')]))) - {None}


def scalar_record(value, reference, known=None):
    """使用与页面相同的字段解释，运算输入不回写原始产物。"""
    from .validation import executable_field, selected_value, value_reference_details
    binding = reference if isinstance(reference, dict) else {'valueRef': reference}
    field, node_ref = binding.get('field', 'value'), binding.get('nodeRef')
    if field == 'unit':
        raise AnalysisError('NON_NUMERIC_FIELD', '单位字段不能作为算术操作数。', {'reference': binding})
    if not executable_field(value, field, node_ref):
        raise AnalysisError('UPSTREAM_CALCULATION_BLOCKED', '所引用的结果字段尚不可计算；保留已成功结果，先处理上游诊断。',
                            {**value_reference_details(value, field, node_ref), 'requiresSourceQuery': False,
                             'upstreamDiagnostics': value.get('diagnostics', [])[:2]})
    raw = selected_value(value, field, node_ref)
    if raw is None:
        raise AnalysisError('SCALAR_VALUE_UNAVAILABLE', '该字段为 null，不能当成零计算；无需因真实空值重新取数。',
                            {**value_reference_details(value, field, node_ref), 'requiresSourceQuery': False})
    decimal_value(raw)
    return {**value, 'value': raw, 'unit': scalar_unit(value, field), 'valueState': 'PRESENT',
            'calculationValidation': 'PASS', 'selectedField': field,
            'selectedCoverageRefs': sorted(scalar_coverage(value, reference, known))}


def field_catalog(value):
    """短目录只返回字段含义与数量；完整成员留在宿主的结果行。"""
    if value.get('operator') != 'DESCRIBE':
        return []
    labels = {'total': '所选对象数', 'observed': '有数值对象数', 'unavailable': '缺值对象数',
              'positive': '正值对象数', 'negative': '负值对象数', 'zero': '零值对象数',
              'sum': '所选非空值有符号合计', 'positiveSum': '所选正值合计', 'negativeSum': '所选负值合计',
              'minimum': '最小值', 'maximum': '最大值', 'mean': '所选非空值均值',
              'topSum': '排序前项合计', 'topShare': '排序前项占所选非空值有符号合计比例'}
    result = []
    counts = value.get('counts', {})
    count_fields = {'sum': 'observed', 'mean': 'observed', 'positiveSum': 'positive', 'negativeSum': 'negative'}
    for field in [*value.get('counts', {}), *value.get('statistics', {})]:
        if field in ('topSum', 'topShare') and value.get('top') is None:
            continue
        if field in ('topSum', 'topShare'):
            member_count = min(value['top'], counts.get('observed', 0))
        elif field in counts or field in count_fields:
            member_count = counts.get(count_fields.get(field, field), 0)
        else:
            member_count = len(statistical_members(value, field))
        item = {'field': field, 'label': labels[field], 'unit': scalar_unit(value, field), 'memberCount': member_count}
        if field in ('topSum', 'topShare'):
            item.update(top=value['top'], direction=value['direction'])
        if field == 'topShare':
            item['denominatorField'] = 'sum'
        result.append(item)
    return result
