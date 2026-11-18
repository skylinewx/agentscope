"""分析代码的开放目录；选择由模型决定，安装与导入边界由宿主管理。"""
import importlib
from types import SimpleNamespace


STANDARD = {
    'decimal': ('Decimal', 'localcontext', 'ROUND_HALF_EVEN', 'ROUND_HALF_UP', 'ROUND_DOWN', 'ROUND_UP'),
    'collections': ('Counter', 'defaultdict', 'deque'),
    'itertools': ('chain', 'islice', 'groupby', 'product', 'combinations', 'accumulate', 'repeat'),
    'statistics': ('mean', 'median', 'median_low', 'median_high', 'mode', 'multimode', 'variance', 'pvariance', 'stdev', 'pstdev', 'quantiles'),
    'datetime': ('date', 'datetime', 'timedelta'),
    'json': ('loads', 'dumps'),
    'csv': ('reader', 'writer', 'DictReader', 'DictWriter'),
    'io': ('StringIO',),
    'math': ('ceil', 'floor', 'isfinite', 'isnan', 'isinf', 'comb', 'perm', 'factorial'),
}
ANALYSIS_MODULES = ('pandas', 'numpy', 'scipy', 'statsmodels')
NUMERIC_POLICY = 'DECIMAL_128_PLAIN_V1'


def approved_module(name):
    """相对导入、未开放库和安装器不进入执行阶段。"""
    return name in STANDARD or name.split('.')[0] in ANALYSIS_MODULES


def library_imports(tree):
    import ast
    return sorted({name.split('.')[0] for node in ast.walk(tree)
                   for name in ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                   if name.split('.')[0] in ANALYSIS_MODULES})


def importer(decimal_factory, libraries_enabled=False):
    """标准库只暴露计算接口；第三方库仅在独立容器中加载。"""
    modules = {}

    def load(name):
        if name in modules:
            return modules[name]
        if name in STANDARD:
            original = importlib.import_module(name)
            module = SimpleNamespace(**{field: getattr(original, field) for field in STANDARD[name]})
            if name == 'decimal':
                module.Decimal = decimal_factory
        elif approved_module(name) and libraries_enabled:
            module = importlib.import_module(name)
        else:
            raise ValueError('未开放的分析模块：' + name)
        modules[name] = module
        return module

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or not approved_module(name):
            raise ValueError('未开放的分析模块：' + name)
        module = load(name)
        if fromlist:
            for field in fromlist:
                if field.startswith('_') or field == '*':
                    raise ValueError('请显式导入开放的公共名称')
                if not hasattr(module, field) and name.split('.')[0] in ANALYSIS_MODULES:
                    load(name + '.' + field)
                if not hasattr(module, field):
                    raise ValueError('该模块未开放名称：' + name + '.' + field)
            return module
        return load(name.split('.')[0])
    return safe_import


def capability():
    return {'standardModules': list(STANDARD), 'analysisLibraries': list(ANALYSIS_MODULES),
            'installation': 'HOST_MANAGED_PINNED_IMAGE', 'numericPolicy': NUMERIC_POLICY,
            'pandas': '金额列使用 Decimal/object；全空求和使用 min_count=1，缺失分组使用 dropna=False。',
            'precision': '数值输出为普通十进制字符串；不接受浮点金额。SciPy/statsmodels 已备库，其浮点估计不属于当前精确金额计算能力。'}
