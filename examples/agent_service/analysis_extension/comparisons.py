"""Arithmetic directions with explicit operands; no business interpretation."""
from .models import decimal_value, decimal_text, precise_sum


def comparison(left, right, left_role, right_role):
    if left is None or right is None: return None
    delta = precise_sum([decimal_value(left), decimal_value(right).copy_negate()])
    return {'leftRole': left_role, 'rightRole': right_role, 'leftValue': left, 'rightValue': right,
            'differenceConvention': 'left_minus_right', 'difference': decimal_text(delta),
            'absoluteDifference': decimal_text(delta.copy_abs()),
            'leftComparedToRight': 'GREATER' if delta > 0 else 'LESS' if delta < 0 else 'EQUAL'}
