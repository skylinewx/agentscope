"""Budget numeric semantics, independent of language permissions and execution backend."""
import ast
import sys
from decimal import Decimal, ROUND_HALF_UP, FloatOperation

NUMERIC_POLICY = 'DECIMAL_128_HALF_UP_V2'
ROUNDING = ROUND_HALF_UP


def configure(context):
    context.prec = 128
    context.rounding = ROUNDING
    context.traps[FloatOperation] = True


def decimal_value(value):
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Decimal 输入只接受十进制字符串或整数')
    number = Decimal(value)
    if not number.is_finite() or len(number.as_tuple().digits) > 128 or abs(number.as_tuple().exponent) > 128:
        raise ValueError('数值超出精度上限')
    return number


def decimal_divide(left, right):
    scalar = (str, int, Decimal)
    if isinstance(left, scalar) and isinstance(right, scalar):
        return decimal_value(left) / decimal_value(right)
    # Path composition and user-defined numeric/array operators retain normal
    # dispatch. Object arrays of Decimal already provide exact division.
    return left / right


def analysis_method(receiver, method, *args, **kwargs):
    # These defaults preserve the existing supported call forms. They do not
    # claim to rewrite arbitrary agg functions or custom statistical algorithms.
    if type(receiver).__module__.split('.')[0] == 'pandas':
        if method == 'sum':
            kwargs.setdefault('min_count', 1)
        elif method == 'groupby':
            kwargs.setdefault('dropna', False)
    return getattr(receiver, method)(*args, **kwargs)


class DecimalDivision(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Div):
            return ast.copy_location(ast.Call(func=ast.Name(id='_decimal_divide', ctx=ast.Load()),
                args=[node.left, node.right], keywords=[]), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ('sum', 'groupby'):
            return ast.copy_location(ast.Call(func=ast.Name(id='_analysis_method', ctx=ast.Load()),
                args=[node.func.value, ast.Constant(node.func.attr), *node.args], keywords=node.keywords), node)
        return node


def normalize_output(value, libraries_enabled=True):
    if isinstance(value, Decimal):
        return format(decimal_value(value), 'f')
    # Do not import every scientific library for an ordinary Decimal job.
    np, pd = sys.modules.get('numpy'), sys.modules.get('pandas')
    if pd is not None:
        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.DataFrame):
            return normalize_output(value.to_numpy(dtype=object).tolist())
        if isinstance(value, (pd.Series, pd.Index)):
            return normalize_output(value.tolist())
    if np is not None and isinstance(value, (np.generic, np.ndarray)):
        return normalize_output(value.tolist())
    if isinstance(value, float):
        if value != value:
            return None
        raise ValueError('输出含二进制浮点值；金额使用 Decimal/object，统计浮点产物不能冒充精确值。')
    if isinstance(value, dict):
        return {key: normalize_output(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_output(item) for item in value]
    return value
