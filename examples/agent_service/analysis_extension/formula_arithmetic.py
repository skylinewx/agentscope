"""Source-owned arithmetic rules; raw facts are never rewritten for calculation."""
from decimal import Decimal

from .models import AnalysisError, decimal_value

BUDGET_FORMULA = 'BUDGET_NULL_ZERO_V1'


def source_number(fact, target):
    """Resolve a captured operand, distinguishing source null from unavailable data."""
    source_null = (fact.get('valueState') == 'NO_VALUE' and fact.get('value') is None
                   and fact.get('precision') != 'display_only')
    budget = (target.get('formulaArithmetic') == BUDGET_FORMULA
              and fact.get('formulaArithmetic') == BUDGET_FORMULA)
    if source_null and not budget:
        raise AnalysisError('FORMULA_SOURCE_VALUE_MISSING',
                            '来源已读取为空，且该来源未声明空值运算规则。', {'factRef': fact['factId']})
    if not source_null and (fact.get('valueState') != 'PRESENT' or fact.get('precision') == 'display_only'):
        raise AnalysisError('FORMULA_VALUE_UNAVAILABLE', '公式引用包含未读、冲突或仅有显示精度的单元格。')
    if (fact.get('unit'), fact.get('scale')) != (target.get('unit'), target.get('scale')):
        raise AnalysisError('FORMULA_UNIT_CONFLICT', '表内公式项的单位或缩放标注不一致。')
    return Decimal(0) if source_null else decimal_value(fact['value'])


def local_input_value(node, fact):
    """Charts consume the exact operands recorded by the formula evaluator."""
    values = node.get('localCheck', {}).get('inputValues')
    return values.get(fact['factId']) if values is not None else (
        fact.get('value') if fact.get('valueState') == 'PRESENT' else None)
