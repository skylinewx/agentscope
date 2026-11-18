"""有界分析执行；标准库使用开放目录，第三方库只能进入隔离容器。"""
import ast
import csv
import ctypes
import io
import json
import os
import platform
import re
import sys
from decimal import Decimal, localcontext
from pathlib import Path

if __package__:
    from .python_libraries import STANDARD, approved_module, library_imports, importer
else:
    # -I/-S 不加载脚本目录，仅由宿主加入这一份可信运行代码。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from python_libraries import STANDARD, approved_module, library_imports, importer


MAX_BYTES = 4 * 1024 * 1024
METHODS = {'get', 'items', 'keys', 'values', 'append', 'extend', 'count', 'index', 'quantize',
           'is_finite', 'copy', 'lower', 'upper', 'strip', 'startswith', 'endswith', 'split', 'join',
           'isdigit', 'isdecimal', 'isnumeric',
           'add', 'update', 'setdefault', 'pop', 'sort', 'reverse', 'replace', 'sqrt',
           'most_common', 'elements', 'isoformat', 'strftime', 'strptime', 'fromisoformat',
           'total_seconds', 'date', 'year', 'month', 'day', 'days', 'seconds', 'prec',
           'getvalue', 'read', 'write', 'writerow', 'writerows'}
STANDARD_ATTRIBUTES = {field for fields in STANDARD.values() for field in fields}
BLOCKED_ATTRIBUTES = {'eval', 'query', 'exec', 'system', 'popen', 'ctypes', 'os', 'sys', 'subprocess',
                      'load', 'loads', 'dump', 'dumps', 'save', 'savez', 'savez_compressed', 'memmap',
                      'to_pickle', 'read_pickle', 'read_sql', 'to_sql', 'connect', 'urlopen', 'open',
                      'read_csv', 'read_table', 'read_json', 'read_excel', 'to_csv', 'to_json', 'to_excel'}
BUILTINS = {'len': len, 'range': range, 'enumerate': enumerate, 'zip': zip, 'sum': sum,
            'min': min, 'max': max, 'sorted': sorted, 'abs': abs, 'all': all, 'any': any,
            'list': list, 'dict': dict, 'tuple': tuple, 'set': set, 'str': str, 'int': int,
            'bool': bool, 'round': round, 'object': object}
NODES = {ast.Module, ast.Assign, ast.AugAssign, ast.Expr, ast.For, ast.If, ast.While, ast.Break,
         ast.Continue, ast.Pass, ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple,
         ast.Dict, ast.Set, ast.Subscript, ast.Slice, ast.Call, ast.keyword, ast.Attribute,
         ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.ListComp, ast.DictComp,
         ast.SetComp, ast.GeneratorExp, ast.comprehension, ast.Lambda, ast.arguments, ast.arg,
         ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.UAdd, ast.USub, ast.Not,
         ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
         ast.Is, ast.IsNot, ast.FunctionDef, ast.Return, ast.Import, ast.ImportFrom, ast.alias,
         ast.With, ast.withitem, ast.BitAnd, ast.BitOr, ast.Invert, ast.JoinedStr, ast.FormattedValue}


def json_write(value):
    def encode(item):
        if isinstance(item, Decimal):
            return format(item, 'f')
        raise ValueError('JSON 不支持该输出类型：' + type(item).__name__)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=encode)


def csv_write(rows, delimiter=','):
    stream = io.StringIO(newline='')
    csv.writer(stream, delimiter=delimiter).writerows(rows)
    return stream.getvalue()


HELPERS = {'json_read': lambda text: json.loads(text, parse_float=str), 'json_write': json_write,
           'csv_read': lambda text, delimiter=',': list(csv.reader(io.StringIO(text), delimiter=delimiter)),
           'csv_write': csv_write, 'grid_rows': None}

GRID_WHERE_FIELDS = {'cellKeys', 'dimensions', 'measureCode', 'headerText', 'mapTypes',
                     'startRow', 'endRowExclusive', 'startColumn', 'endColumnExclusive'}


def grid_matches(cell, dimensions, where):
    """执行器与只读证据恢复共用筛选规则，避免两处对读集的理解不一致。"""
    return not ('cellKeys' in where and cell.get('key') not in where['cellKeys']
        or 'measureCode' in where and cell.get('measureCode') != where['measureCode']
        or 'mapTypes' in where and cell.get('mapType') not in where['mapTypes']
        or 'headerText' in where and where['headerText'] not in cell.get('headers', [])
        and where['headerText'] not in (cell.get('displayValue') or '')
        or any(dimensions.get(key) != value for key, value in where.get('dimensions', {}).items())
        or cell['row'] < where.get('startRow', 0) or cell['row'] >= where.get('endRowExclusive', 2**31)
        or cell['column'] < where.get('startColumn', 0) or cell['column'] >= where.get('endColumnExclusive', 2**31))


def grid_value(cell, number_format=None):
    """保留原始 JSON；只按来源已配置的数值格式移除合法千分位，不猜量级。"""
    raw = cell.get('rawValueJson')
    value = json.loads(raw, parse_float=str) if raw is not None else None
    if (cell.get('measureCode') and isinstance(value, str) and number_format == 'grouped-dot'
            and re.fullmatch(r'-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?', value)):
        return value.replace(',', '')
    return value


def grid_reader(inputs, reads):
    """仅暴露宿主选定的文件引用，流式读取并由运行时记录实际扫描范围。"""
    files = {}
    for item in inputs:
        if item.get('kind') == 'GRID_FILE':
            # 输入向模型开放，授权路径及读取上下文由运行时持有独立副本。
            files[item['ref']] = json.loads(json.dumps(item))
            item.pop('_path')

    def grid_rows(ref, where=None):
        if ref not in files:
            raise ValueError('grid_rows 需要 inputs 中已授权的网格 ref')
        where = {} if where is None else where
        if not isinstance(where, dict) or set(where) - GRID_WHERE_FIELDS:
            raise ValueError('grid_rows 筛选字段不合法；可用字段：' + ', '.join(sorted(GRID_WHERE_FIELDS))
                             + '。mapTypes 为数组，不能写 mapType；指标优先使用实际 measureCode。')
        where = json.loads(json.dumps(where))
        source = files[ref]
        read = {'ref': ref, 'sha256': source['sha256'], 'where': where, 'scannedCount': 0,
                'matchedCount': 0, 'exhausted': False, 'selectedKeys': [], 'selectedKeysComplete': True}
        reads.append(read)
        if len(reads) > 100:
            raise ValueError('单次执行最多读取 100 个网格范围')
        with open(source['_path'], 'rb') as stream:
            while True:
                line = stream.readline(32 * 1024 * 1024 + 1)
                if not line:
                    read['exhausted'] = read['scannedCount'] == source['cellCount']
                    if not read['exhausted']:
                        raise ValueError('网格文件记录数量不完整')
                    return
                if len(line) > 32 * 1024 * 1024:
                    raise ValueError('单条网格记录超限')
                cell = json.loads(line, parse_float=str)
                read['scannedCount'] += 1
                dimensions = {**source.get('context', {}).get('effectiveDimensions', {}), **cell.get('dimensions', {})}
                if not grid_matches(cell, dimensions, where):
                    continue
                read['matchedCount'] += 1
                if len(read['selectedKeys']) < 5000:
                    read['selectedKeys'].append(cell['key'])
                else:
                    read['selectedKeysComplete'] = False
                cell['dimensions'] = dimensions
                cell['value'] = grid_value(cell, source.get('numberFormat'))
                cell['sourceRef'] = ref
                yield cell
    return grid_rows


def validate_code(code):
    if not isinstance(code, str) or not code.strip() or len(code) > 24000:
        raise ValueError('代码长度必须在 1—24000 字符内')
    tree = ast.parse(code, mode='exec')
    nodes = list(ast.walk(tree))
    functions = {node.name for node in nodes if isinstance(node, ast.FunctionDef)}
    imports = set()
    json_aliases = set()
    scientific = bool(library_imports(tree))
    for node in nodes:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and (node.level or not approved_module(node.module or '')):
            raise ValueError('未开放的分析模块：' + (node.module or '相对导入'))
        for alias in node.names:
            module = node.module if isinstance(node, ast.ImportFrom) else alias.name
            if not approved_module(module) or alias.name == '*' or any(part.startswith('_') for part in alias.name.split('.')):
                raise ValueError('请显式导入开放目录中的分析库：' + alias.name)
            name = alias.asname or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.split('.')[0])
            if name.startswith('_') or name in {*BUILTINS, *HELPERS, 'D', 'Decimal', 'inputs'} and not (
                    isinstance(node, ast.ImportFrom) and module == 'decimal' and alias.name == 'Decimal' and name in ('D', 'Decimal')):
                raise ValueError('导入别名不能覆盖运行名称：' + name)
            if isinstance(node, ast.ImportFrom) and module in STANDARD and alias.name not in STANDARD[module]:
                raise ValueError('该标准库名称未开放：' + module + '.' + alias.name)
            imports.add(name)
            if isinstance(node, ast.Import) and module == 'json':
                json_aliases.add(name)
    protected = {*BUILTINS, *HELPERS, 'Decimal', 'D'}
    # 两个名称都由宿主绑定为同一十进制函数；正常别名声明不改变运行能力。
    # 只允许名称到名称的单一赋值，不放开函数、参数、循环或解构中的覆盖。
    decimal_alias_targets = {id(node.targets[0]) for node in nodes
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ('D', 'Decimal')
        and isinstance(node.value, ast.Name) and node.value.id in ('D', 'Decimal')}
    if len(nodes) > 8000:
        raise ValueError('代码结构超过容量上限')
    for node in nodes:
        if type(node) not in NODES:
            raise ValueError('不支持的 Python 语法：' + type(node).__name__)
        if isinstance(node, ast.FunctionDef) and (node.name.startswith('_') or node.name in protected or node.decorator_list):
            raise ValueError('函数名不能覆盖运行函数或使用装饰器')
        if ((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in protected
                and id(node) not in decimal_alias_targets)
                or isinstance(node, ast.arg) and node.arg in protected):
            raise ValueError('不能覆盖运行函数')
        if isinstance(node, (ast.Name, ast.arg)):
            name = getattr(node, 'id', getattr(node, 'arg', ''))
            # 单独的 _ 是常见的占位变量；私有名称和所有内部属性仍不可访问。
            if name.startswith('_') and name != '_':
                raise ValueError('不允许访问私有名称')
        if isinstance(node, ast.Attribute):
            if (node.attr.startswith('_') or not isinstance(node.ctx, ast.Load) and node.attr != 'prec'
                    or not scientific and node.attr not in METHODS | STANDARD_ATTRIBUTES
                    or scientific and node.attr in BLOCKED_ATTRIBUTES and not (
                        isinstance(node.value, ast.Name) and node.value.id in json_aliases and node.attr in ('loads', 'dumps'))):
                raise ValueError('不允许该属性访问：' + node.attr)
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in {*protected, *functions, *imports}
                    or isinstance(node.func, ast.Attribute) and (scientific or node.func.attr in METHODS | STANDARD_ATTRIBUTES)):
                name = getattr(node.func, 'attr', getattr(node.func, 'id', type(node.func).__name__))
                raise ValueError('只允许公开的计算函数与容器方法：' + name + '；代码行 ' + str(node.lineno))
        if isinstance(node, ast.Constant) and (isinstance(node.value, (float, complex, bytes))
                or isinstance(node.value, int) and node.value.bit_length() > 512):
            raise ValueError('金额使用 Decimal 字符串，禁止浮点字面量和超大整数')
    return tree


def decimal_value(value):
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Decimal 输入只接受十进制字符串或整数')
    number = Decimal(value)
    if not number.is_finite() or len(number.as_tuple().digits) > 128 or abs(number.as_tuple().exponent) > 128:
        raise ValueError('数值超出精度上限')
    return number


class DecimalDivision(ast.NodeTransformer):
    """Python 的整数除法也改为十进制，避免不经意引入二进制浮点。"""
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


def analysis_method(receiver, method, *args, **kwargs):
    """pandas 的默认值与预算缺数语义对齐；不重定义其他库的计算方法。"""
    if type(receiver).__module__.split('.')[0] == 'pandas':
        if method == 'sum':
            kwargs.setdefault('min_count', 1)
        elif method == 'groupby':
            kwargs.setdefault('dropna', False)
    return getattr(receiver, method)(*args, **kwargs)


def decimal_divide(left, right):
    """标量除法固定十进制；Decimal/object 数组保留向量计算，不强转标量。"""
    scalar = (str, int, Decimal)
    if isinstance(left, scalar):
        left = decimal_value(left)
    if isinstance(right, scalar):
        right = decimal_value(right)
    return left / right


def normalize_output(value, libraries_enabled=False):
    """缺失值归一为 null；有限浮点值不冒充精确金额，不输出科学计数法。"""
    if isinstance(value, Decimal):
        return format(decimal_value(value), 'f')
    if libraries_enabled:
        import numpy as np
        import pandas as pd
        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, (np.generic, np.ndarray)):
            return normalize_output(value.tolist(), True)
        if isinstance(value, pd.DataFrame):
            return normalize_output(value.to_numpy(dtype=object).tolist(), True)
        if isinstance(value, (pd.Series, pd.Index)):
            return normalize_output(value.tolist(), True)
    if isinstance(value, float):
        if value != value:
            return None
        raise ValueError('输出含二进制浮点值；金额请使用 Decimal/object 计算，统计浮点结果当前不作为精确数值接收。')
    if isinstance(value, dict):
        return {key: normalize_output(item, libraries_enabled) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_output(item, libraries_enabled) for item in value]
    return value


def resource_limits(timeout_seconds=15):
    """失败时关闭执行；Windows Job 与 Linux rlimit 约束独立子进程。"""
    memory = (1024 if os.environ.get('ANALYSIS_LIBRARY_WORKER') == '1' else 256) * 1024 * 1024
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds))
        return None
    from ctypes import wintypes
    class BASIC(ctypes.Structure):
        _fields_ = [('processTime', ctypes.c_int64), ('jobTime', ctypes.c_int64), ('flags', wintypes.DWORD),
                    ('minWorkingSet', ctypes.c_size_t), ('maxWorkingSet', ctypes.c_size_t),
                    ('activeProcessLimit', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                    ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('readOps', 'writeOps', 'otherOps', 'readBytes', 'writeBytes', 'otherBytes')]
    class EXTENDED(ctypes.Structure):
        _fields_ = [('basic', BASIC), ('io', IO), ('processMemory', ctypes.c_size_t), ('jobMemory', ctypes.c_size_t),
                    ('peakProcessMemory', ctypes.c_size_t), ('peakJobMemory', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = EXTENDED()
    limits.basic.flags = 0x100 | 0x8  # PROCESS_MEMORY + ACTIVE_PROCESS
    limits.basic.activeProcessLimit = 1
    limits.processMemory = memory
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise RuntimeError('无法启用 Python 执行资源限制')
    return job  # 保持 Job 存活至进程结束


def main():
    payload = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('Python 输入超过容量上限')
    request = json.loads(payload)
    job = resource_limits(min(60, max(1, int(request.get('timeoutSeconds', 15)))))
    tree = validate_code(request['code'])
    tree = ast.fix_missing_locations(DecimalDivision().visit(tree))
    reads = []
    reader = grid_reader(request['inputs'], reads)
    libraries_enabled = os.environ.get('ANALYSIS_LIBRARY_WORKER') == '1'
    namespace = {'__builtins__': {'__import__': importer(decimal_value, libraries_enabled)},
                 **BUILTINS, **HELPERS, 'grid_rows': reader, 'Decimal': decimal_value, 'D': decimal_value,
                 '_decimal_divide': decimal_divide,
                 '_analysis_method': analysis_method,
                 'inputs': request['inputs']}
    with localcontext() as context:
        context.prec = 128
        exec(compile(tree, '<analysis-python>', 'exec'), namespace, namespace)
    result = namespace.get('result')
    if not isinstance(result, list) or not 1 <= len(result) <= 1000:
        raise ValueError('result 必须是 1—1000 项的数组')
    output = json.dumps({'result': normalize_output(result, libraries_enabled), 'fileReads': reads,
                        'interpreter': {'python': platform.python_version(), 'system': platform.system(),
                                        'architecture': platform.machine()}},
                        ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(output) > MAX_BYTES:
        raise ValueError('Python 输出超过容量上限')
    sys.stdout.buffer.write(output)


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        sys.stdout.buffer.write(json.dumps({'error': {'type': type(error).__name__, 'message': str(error)[:1200]}},
                                         ensure_ascii=False).encode('utf-8'))
        sys.exit(1)
