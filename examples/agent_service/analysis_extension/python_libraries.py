"""Informational library catalogue. Imports execute normally inside the isolated runtime."""
from .numeric_policy import NUMERIC_POLICY


def capability():
    return {'standardModules': ['decimal', 'collections', 'itertools', 'statistics', 'datetime', 'json', 'csv', 'io', 'math'],
            'analysisLibraries': ['pandas', 'numpy', 'scipy', 'statsmodels'],
            'catalogueMode': 'EXAMPLES_NOT_ALLOWLIST', 'runtime': 'ISOLATED_PYTHON',
            'installation': 'HOST_MANAGED_PINNED_IMAGE', 'numericPolicy': NUMERIC_POLICY,
            'pandas': '金额列使用 Decimal/object；全空求和使用 min_count=1，缺失分组使用 dropna=False。',
            'precision': '四舍五入 ROUND_HALF_UP；数值输出为普通十进制字符串，不接受浮点金额或统计浮点产物。',
            'isolation': '每次独立容器；无网络、无宿主挂载；只有临时目录可写；授权数据由宿主提供。'}
