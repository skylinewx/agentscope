"""Ordinary Python language with explicit numeric/output contracts; not a security sandbox."""
import ast
import builtins
import csv
import io
import json
from decimal import Decimal, localcontext

if __package__:
    from .numeric_policy import configure, DecimalDivision, decimal_divide, analysis_method, normalize_output
else:
    from numeric_policy import configure, DecimalDivision, decimal_divide, analysis_method, normalize_output


def validate_code(code):
    if not isinstance(code, str) or not code.strip() or len(code) > 24000:
        raise ValueError('Python 代码需要 1—24000 个字符')
    tree = ast.parse(code)
    if sum(1 for _ in ast.walk(tree)) > 8000:
        raise ValueError('代码结构超过容量上限')
    return tree


def json_write(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
        default=lambda value: format(value, 'f') if isinstance(value, Decimal) else _unsupported(value))


def _unsupported(value):
    raise ValueError('JSON 不支持该输出类型：' + type(value).__name__)


def csv_write(rows, delimiter=','):
    output = io.StringIO(newline='')
    csv.writer(output, delimiter=delimiter).writerows(rows)
    return output.getvalue()


def evaluate(code, inputs, grid_rows):
    tree = validate_code(code)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        final = tree.body[-1]
        tree.body[-1] = ast.copy_location(ast.Assign(
            targets=[ast.Name(id='_analysis_final_value', ctx=ast.Store())], value=final.value), final)
    tree = ast.fix_missing_locations(DecimalDivision().visit(tree))
    namespace = {'__builtins__': builtins, '__name__': '__analysis__', 'inputs': inputs,
        'D': Decimal, 'Decimal': Decimal, 'grid_rows': grid_rows,
        '_decimal_divide': decimal_divide, '_analysis_method': analysis_method,
        'json_read': lambda text: json.loads(text, parse_float=str), 'json_write': json_write,
        'csv_read': lambda text, delimiter=',': list(csv.reader(io.StringIO(text), delimiter=delimiter)),
        'csv_write': csv_write}
    with localcontext() as context:
        configure(context)
        exec(compile(tree, '<analysis-python>', 'exec'), namespace, namespace)
    result = namespace.get('result') if 'result' in namespace else namespace.get('_analysis_final_value')
    if not isinstance(result, list) or not 1 <= len(result) <= 1000:
        raise ValueError('将产物列表赋给 result，或将该列表作为代码最后一个表达式；必须是 1—1000 项数组。')
    return normalize_output(result)
