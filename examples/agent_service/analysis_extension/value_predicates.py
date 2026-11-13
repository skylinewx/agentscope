"""Business predicate contract and native request verification; no second amount-filter engine."""
import re
from .models import AnalysisError


def validate(condition, target_ids=None, depth=0):
    if not isinstance(condition, dict) or set(condition) - {'operator', 'value', 'targetId', 'children'} or depth > 4:
        raise AnalysisError('INVALID_VALUE_FILTER', '数值条件使用业务目标引用、比较或 AND/OR/NOT，不填写物理列编码。')
    op = condition.get('operator')
    children = condition.get('children') or []
    if op in ('AND', 'OR', 'NOT'):
        if (not isinstance(children, list) or not 1 <= len(children) <= 32
                or (op == 'NOT' and len(children) != 1) or condition.get('value') is not None or condition.get('targetId') is not None):
            raise AnalysisError('INVALID_VALUE_FILTER', '逻辑条件只包含 operator/children。')
        return {'operator': op, 'children': [validate(child, target_ids, depth + 1) for child in children]}
    if children or op not in ('LT', 'LE', 'GT', 'GE', 'EQ', 'NE', 'IS_NULL', 'IS_NOT_NULL'):
        raise AnalysisError('INVALID_VALUE_FILTER', '不支持的数值条件。')
    target = condition.get('targetId')
    if target_ids is not None and (target not in target_ids if target is not None else len(target_ids) != 1):
        raise AnalysisError('INVALID_VALUE_FILTER', '多个业务目标需要明确 targetId，不能按物理指标名称推测。')
    result = {'operator': op}
    if target is not None:
        if not isinstance(target, str) or not target.strip():
            raise AnalysisError('INVALID_VALUE_FILTER', 'targetId 必须是业务目标标识。')
        result['targetId'] = target
    value = condition.get('value')
    if op in ('IS_NULL', 'IS_NOT_NULL'):
        if value is not None:
            raise AnalysisError('INVALID_VALUE_FILTER', '空值判断不使用阈值。')
    else:
        if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value):
            raise AnalysisError('INVALID_VALUE_FILTER', '阈值必须是精确十进制字符串。')
        result['value'] = value
    return result


def compile_native(condition, aliases):
    condition = validate(condition, set(aliases))
    if condition.get('children'):
        return {'op': condition['operator'], 'children': [compile_native(child, aliases) for child in condition['children']]}
    target = condition.get('targetId', next(iter(aliases)))
    result = {'op': condition['operator'], 'field': aliases[target]}
    if 'value' in condition:
        result['value'] = condition['value']
    return result


from decimal import Decimal, InvalidOperation

def scalar_comparison(condition):
    """Validate the declared predicate without choosing a historical population."""
    operators = {'LT': lambda a, b: a < b, 'LE': lambda a, b: a <= b,
                 'GT': lambda a, b: a > b, 'GE': lambda a, b: a >= b,
                 'EQ': lambda a, b: a == b, 'NE': lambda a, b: a != b}
    if not isinstance(condition, dict) or set(condition) != {'operator', 'value'} or condition.get('operator') not in operators:
        raise AnalysisError('INVALID_VALUE_FILTER', 'valueFilter 使用 operator 和精确十进制 value。')
    try:
        threshold = Decimal(condition['value'])
        if not isinstance(condition['value'], str) or not threshold.is_finite():
            raise ValueError()
    except (InvalidOperation, ValueError, TypeError):
        raise AnalysisError('INVALID_VALUE_FILTER', '筛选阈值必须是有限十进制字符串。') from None
    import re
    if len(condition['value']) > 128 or not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', condition['value']):
        raise AnalysisError('INVALID_VALUE_FILTER', '筛选阈值须为精确十进制字符串，不使用科学记数法。')
    return operators[condition['operator']], threshold
