"""算术 Schema、前置检查和执行器共享的容量与安全语法。"""
import ast

from .models import AnalysisError

MAX_ARITHMETIC_INPUTS = 200
MAX_EXPRESSION_LENGTH = 4096
MAX_EXPRESSION_NODES = 1200
MAX_EXPRESSION_DEPTH = 40
ARITHMETIC_FUNCTIONS = ('sum', 'min', 'max', 'abs')
ARITHMETIC_SYNTAX = '支持四则运算、abs(x)、sum/min/max(x,y,...) 或 sum/min/max([x,y,...])；函数列表须非空，不支持推导式、属性访问或任意函数。'


def function_arguments(node):
    """只在聚合函数的唯一位置展开列表字面量，不开放通用容器运算。"""
    if (not isinstance(node.func, ast.Name) or node.func.id not in ARITHMETIC_FUNCTIONS
            or node.keywords):
        raise AnalysisError('INVALID_EXPRESSION', ARITHMETIC_SYNTAX)
    arguments = node.args
    if node.func.id != 'abs' and len(arguments) == 1 and isinstance(arguments[0], ast.List):
        arguments = arguments[0].elts
    if not 1 <= len(arguments) <= MAX_ARITHMETIC_INPUTS or node.func.id == 'abs' and len(arguments) != 1:
        raise AnalysisError('INVALID_EXPRESSION', f'abs 接受一个数值，sum/min/max 接受 1 至 {MAX_ARITHMETIC_INPUTS} 个数值。')
    return arguments


def parse_arithmetic(expression):
    """解析受限表达式；预检与执行共用，绝不调用 eval。"""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > MAX_EXPRESSION_LENGTH:
        raise AnalysisError('INVALID_EXPRESSION', f'计算表达式须为 1 至 {MAX_EXPRESSION_LENGTH} 个字符。')
    try:
        tree = ast.parse(expression.strip(), mode='eval')
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise AnalysisError('INVALID_EXPRESSION', '表达式语法不正确。') from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_EXPRESSION_NODES:
        raise AnalysisError('CALCULATION_LIMIT', '表达式过于复杂，请分步计算。')

    def validate(node, depth=0):
        if depth > MAX_EXPRESSION_DEPTH:
            raise AnalysisError('CALCULATION_LIMIT', '表达式嵌套过深。')
        if isinstance(node, ast.Name) or isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            children = [node.operand]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            children = [node.left, node.right]
        elif isinstance(node, ast.Call):
            children = function_arguments(node)
        else:
            raise AnalysisError('INVALID_EXPRESSION', ARITHMETIC_SYNTAX)
        for child in children:
            validate(child, depth + 1)

    validate(tree.body)
    return tree
