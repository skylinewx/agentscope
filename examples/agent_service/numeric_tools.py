"""Default, read-only arithmetic and sorting tools, independent of analysis."""
from __future__ import annotations

import ast
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import cmp_to_key
import json
import re

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from analysis_extension.models import AnalysisError, canonical, decimal_text, decimal_value
from analysis_extension.tools import array, obj

TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
DECIMAL = {"type": "string", "minLength": 1, "maxLength": 512}


def _number(value):
    try:
        return decimal_value(value)
    except AnalysisError as exc:
        raise AnalysisError(exc.code, "数值须为有限、无千分位的十进制字符串，且不超过支持的精度。") from exc


def calculate(expression, variables=None, decimalPlaces=None):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 4096:
        raise AnalysisError("INVALID_EXPRESSION", "计算表达式须为 1 至 4096 个字符。")
    variables = {} if variables is None else variables
    if (not isinstance(variables, dict) or len(variables) > 200 or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', k)
            or k in ('sum', 'min', 'max', 'abs') for k in variables)):
        raise AnalysisError("INVALID_VARIABLES", "变量须使用互不重复的名称，最多 200 个。")
    if decimalPlaces is not None and (type(decimalPlaces) is not int or not 0 <= decimalPlaces <= 16):
        raise AnalysisError("INVALID_PRECISION", "小数位数须在 0 至 16 之间。")
    values = {key: Fraction(_number(value)) for key, value in variables.items()}
    try:
        tree = ast.parse(expression.strip(), mode='eval')
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise AnalysisError("INVALID_EXPRESSION", "表达式语法不正确。") from exc
    if sum(1 for _ in ast.walk(tree)) > 400:
        raise AnalysisError("CALCULATION_LIMIT", "表达式过于复杂，请分步计算。")

    def evaluate(node, depth=0):
        if depth > 40:
            raise AnalysisError("CALCULATION_LIMIT", "表达式嵌套过深。")
        child = lambda part: evaluate(part, depth + 1)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = Fraction(_number(ast.get_source_segment(expression.strip(), node)))
        elif isinstance(node, ast.Name) and node.id in values:
            result = values[node.id]
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = child(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = child(node.left), child(node.right)
            if isinstance(node.op, ast.Add): result = left + right
            elif isinstance(node.op, ast.Sub): result = left - right
            elif isinstance(node.op, ast.Mult): result = left * right
            elif not right: raise AnalysisError("ZERO_DENOMINATOR", "分母不能为零。")
            else: result = left / right
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ('sum', 'min', 'max', 'abs') and not node.keywords
              and 1 <= len(node.args) <= 200):
            items = [child(arg) for arg in node.args]
            if node.func.id == 'abs' and len(items) != 1:
                raise AnalysisError("INVALID_EXPRESSION", "abs 只接受一个数值。")
            result = {'sum': lambda: sum(items, Fraction(0)), 'min': lambda: min(items),
                      'max': lambda: max(items), 'abs': lambda: abs(items[0])}[node.func.id]()
        else:
            raise AnalysisError("INVALID_EXPRESSION", "仅支持数值、已声明变量、四则运算以及 sum/min/max/abs。")
        if max(result.numerator.bit_length(), result.denominator.bit_length()) > 4096:
            raise AnalysisError("CALCULATION_LIMIT", "计算超出数值容量，请缩小范围。")
        return result

    value = evaluate(tree.body)
    if decimalPlaces is None:
        denominator, powers = value.denominator, []
        for factor in (2, 5):
            count = 0
            while denominator % factor == 0:
                denominator //= factor
                count += 1
            powers.append(count)
        if denominator != 1:
            raise AnalysisError("PRECISION_REQUIRED", "结果为无限小数，请指定 decimalPlaces 后重试。")
        places = max(powers)
        if places > 128:
            raise AnalysisError("CALCULATION_LIMIT", "精确结果小数位过多，请指定 decimalPlaces。")
    else:
        places = decimalPlaces
    scaled, remainder = divmod(abs(value.numerator) * 10 ** places, value.denominator)
    if remainder * 2 >= value.denominator:
        scaled += 1
    if value < 0:
        scaled = -scaled
    with localcontext() as context:
        context.prec = 1500
        number = _number(Decimal(scaled).scaleb(-places))
    return {"value": decimal_text(number), "rounded": Fraction(number) != value,
            "decimalPlaces": decimalPlaces, "rounding": "HALF_UP" if decimalPlaces is not None else None}


def sort_records(records, keys, offset=0, limit=100):
    if not isinstance(records, list) or not 1 <= len(records) <= 5000 or len(canonical(records)) > 1024 * 1024:
        raise AnalysisError("RECORD_LIMIT", "排序需要 1 至 5000 条记录，总大小不超过 1 MiB。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise AnalysisError("INVALID_PAGE", "分页位置须非负，每页 1 至 1000 条。")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 10:
        raise AnalysisError("INVALID_SORT", "需要 1 至 10 个明确的排序键。")
    fields = []
    for key in keys:
        if (not isinstance(key, dict) or set(key) != {'field', 'type', 'direction', 'nulls'}
                or not isinstance(key['field'], str) or not key['field'] or len(key['field']) > 1024
                or key['type'] not in ('number', 'text') or key['direction'] not in ('asc', 'desc')
                or key['nulls'] not in ('first', 'last') or key['field'] in fields):
            raise AnalysisError("INVALID_SORT", "排序键须明确字段、数值或文本类型、方向和空值位置，且字段不重复。")
        fields.append(key['field'])
    ids, normalized = set(), []
    for row in records:
        if (not isinstance(row, dict) or set(row) != {'id', 'values'} or not isinstance(row['id'], str)
                or not 1 <= len(row['id']) <= 128 or row['id'] in ids or not isinstance(row['values'], dict)
                or len(row['values']) > 100 or any(not isinstance(k, str) or not k or len(k) > 1024
                    or (v is not None and (not isinstance(v, str) or len(v) > 4096)) for k, v in row['values'].items())):
            raise AnalysisError("INVALID_RECORD", "记录须具有唯一 id 和字符串或空值组成的 values。")
        ids.add(row['id'])
        values = []
        for key in keys:
            if key['field'] not in row['values']:
                raise AnalysisError("MISSING_SORT_FIELD", "记录缺少排序字段；未知值须显式传 null。")
            value = row['values'][key['field']]
            values.append(_number(value) if value is not None and key['type'] == 'number' else value)
        normalized.append((row, values))

    def compare(left, right):
        for index, key in enumerate(keys):
            a, b = left[1][index], right[1][index]
            if a is None or b is None:
                if a is b: continue
                return (-1 if a is None else 1) * (1 if key['nulls'] == 'first' else -1)
            order = (a > b) - (a < b)
            if order: return order * (1 if key['direction'] == 'asc' else -1)
        return 0

    ordered = sorted(normalized, key=cmp_to_key(compare))
    end = min(len(ordered), offset + limit)
    return {"records": [row for row, _ in ordered[offset:end]], "total": len(records),
            "offset": offset, "nextOffset": end if end < len(records) else None,
            "keys": keys, "ties": "input_order"}


class _NumericTool(ToolBase):
    is_concurrency_safe = True
    is_read_only = True

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="仅计算或排序传入数据，不访问网络或修改数据。")

    async def call(self, **kwargs):
        try:
            return ToolChunk(content=[TextBlock(text=json.dumps(self.operation(**kwargs), ensure_ascii=False))])
        except AnalysisError as exc:
            return ToolChunk(content=[TextBlock(text=json.dumps({"error": exc.as_dict()}, ensure_ascii=False))], state=ToolResultState.ERROR)


class Calculate(_NumericTool):
    name = "Calculate"
    description = "通用精确十进制计算，无须分析 Skill 或 AnalysisBuild。expression 使用四则运算、变量及 sum/min/max/abs；variables 的数值使用原始十进制字符串。有限小数默认精确输出；无限小数需指定 decimalPlaces，按 HALF_UP 舍入并返回 rounded。仅执行算术，不确认业务可加性、单位或汇总口径；业务合计按适用规则或用户明确采用的分析预期判断。"
    input_schema = obj({"expression": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "variables": {"type": "object", "additionalProperties": DECIMAL, "maxProperties": 200},
                        "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}}, ['expression'])
    operation = staticmethod(calculate)


class SortRecords(_NumericTool):
    name = "SortRecords"
    description = "通用稳定排序，无须分析 Skill 或 AnalysisBuild。records 每条含唯一 id、values；数值保留原始十进制字符串，未知值显式 null。keys 逐一声明 field、type=number/text、direction=asc/desc、nulls=first/last。先对全部传入记录排序再分页；并列保持输入次序，原记录完整返回。text 使用 Unicode 顺序，不代表拼音或业务顺序。总范围由模型保证完整；不得把分页子集当作全体排名。"
    input_schema = obj({"records": array(obj({"id": {"type": "string", "minLength": 1, "maxLength": 128},
        "values": {"type": "object", "additionalProperties": {"anyOf": [{"type": "string", "maxLength": 4096}, {"type": "null"}]}, "maxProperties": 100}}, ['id', 'values']), 5000, 1),
        "keys": array(obj({"field": TEXT, "type": {"enum": ['number', 'text']}, "direction": {"enum": ['asc', 'desc']},
            "nulls": {"enum": ['first', 'last']}}, ['field', 'type', 'direction', 'nulls']), 10, 1),
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ['records', 'keys'])
    operation = staticmethod(sort_records)


class NumericToolsMiddleware(MiddlewareBase):
    async def list_tools(self):
        return [Calculate(), SortRecords()]

    async def on_system_prompt(self, agent, current_prompt):
        return current_prompt + "\n\n通用数值工具：查询中的排序使用 SortRecords，计算使用 Calculate；不因调用它们而启动分析。数值按来源原始精度传入，按工具结果组织回答。排序前获取本轮所需完整范围；纯算术结果不等于业务汇总成立。"
