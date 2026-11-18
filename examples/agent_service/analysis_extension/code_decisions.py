"""记录模型选择自定义代码的功能缺口，不记录或要求内部推理。"""
import logging

from .models import AnalysisError, digest
from .native_analysis import CAPABILITY_VERSION


def decision(reason, purpose, code):
    """公共工具要求声明原因；内部直接调用未声明时如实标记，不臆测缺口。"""
    if not isinstance(code, str):
        raise AnalysisError('INVALID_PYTHON_CODE', 'code 必须是 Python 源代码文本。')
    if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
        raise AnalysisError('INVALID_FALLBACK_REASON', 'fallbackReason 使用简要文字说明原生能力不能完成的必要处理。')
    return {'mode': 'CUSTOM_CODE', 'capabilityVersion': CAPABILITY_VERSION,
            'declaration': 'DECLARED' if reason is not None else 'UNDECLARED',
            'reason': reason.strip() if reason is not None else None,
            'processing': purpose, 'codeDigest': digest(code), 'codeChars': len(code)}


def log_decision(record):
    """日志便于定位，完整代码、输入和状态仍以运行记录为准。"""
    item = record['capabilityDecision']
    logging.getLogger(__name__).info(
        '自定义分析代码 运行=%s 状态=%s 能力版本=%s 原因=%s 处理内容=%s 代码字符=%s 代码摘要=%s 耗时毫秒=%s',
        record['runRef'], record['status'], item['capabilityVersion'], item['reason'], item['processing'],
        item['codeChars'], item['codeDigest'], record.get('elapsedMs', 0))
