"""各入口共用的运算参数检查与可执行诊断，不自动改写表达式含义。"""
import ast

from .models import AnalysisError
from .arithmetic_contract import ARITHMETIC_FUNCTIONS, MAX_ARITHMETIC_INPUTS, parse_arithmetic
from .scalar_references import normalize_reference, reference_key


def validate_operation(operation):
    allowed = {'id', 'operator', 'select', 'inputRefs', 'ruleRef', 'comparabilityRef', 'relationRef',
               'aggregation', 'expression', 'wholePopulation', 'direction', 'decimalPlaces', 'top', 'checkScope'}
    unknown = sorted(set(operation) - allowed)
    operator = operation.get('operator')
    references = operation.get('inputRefs')
    details = {'operationId': operation.get('id'), 'requiresSourceQuery': False}
    if 'select' in operation and not isinstance(operation['select'], dict):
        raise AnalysisError('INVALID_SELECTOR', 'select 必须为对象。', details)
    if 'wholePopulation' in operation and type(operation['wholePopulation']) is not bool:
        raise AnalysisError('INVALID_OPERATION_ARGUMENT', 'wholePopulation 必须为布尔值。', details)
    if operator == 'ARITHMETIC':
        supplied = references if isinstance(references, list) else []
        details.update(variableMap={f'v{i}': ref for i, ref in enumerate(supplied[:8])},
                       expressionExample='v0 + v1' if len(supplied) > 1 else 'v0 * 100',
                       hint='inputRefs 按顺序对应 v0、v1；字段可用 {valueRef, field}。不传 variables，不抄金额替代引用。')
    if unknown:
        raise AnalysisError('INVALID_OPERATION_ARGUMENT', '运算包含不支持的字段：' + '、'.join(unknown),
                            {**details, 'fields': unknown})
    if references is not None:
        maximum = MAX_ARITHMETIC_INPUTS if operator == 'ARITHMETIC' else 1000
        if not isinstance(references, list) or not references or len(references) > maximum:
            raise AnalysisError('INVALID_OPERATION_INPUTS', f'inputRefs 须为 1 至 {maximum} 个引用；批量统计优先使用 SUM/DESCRIBE，按 select 选择时省略。',
                                {**details, 'maximumInputs': maximum})
        normalized = [normalize_reference(ref) for ref in references]
        if len({reference_key(ref) for ref in normalized}) != len(normalized):
            raise AnalysisError('DUPLICATE_INPUT', '同一数值字段不能重复列入 inputRefs；重复使用请在表达式中再次引用变量。', details)
        if any(isinstance(ref, dict) for ref in references) and operator != 'ARITHMETIC':
            raise AnalysisError('INVALID_OPERATION_INPUTS', '字段标量算术使用 ARITHMETIC；该算子需要完整事实或结果引用。', details)
    if operator != 'ARITHMETIC':
        return
    if not references:
        raise AnalysisError('ARITHMETIC_INPUTS', '通用算术须显式列出输入引用，以 v0、v1 等按顺序引用。', details)
    expression = operation.get('expression')
    try:
        tree = parse_arithmetic(expression)
    except AnalysisError as exc:
        raise AnalysisError(exc.code, str(exc), details) from exc
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - set(ARITHMETIC_FUNCTIONS)
    expected = {f'v{i}' for i in range(len(references))}
    if names - expected:
        raise AnalysisError('UNKNOWN_ARITHMETIC_VARIABLE', '表达式变量未与 inputRefs 对应。',
                            {**details, 'unknownVariables': sorted(names - expected)})
    if names != expected:
        raise AnalysisError('UNUSED_ARITHMETIC_INPUT', '每个输入引用都须实际参与表达式；纯常量运算请使用 Calculate。',
                            {**details, 'unusedVariables': sorted(expected - names)})
