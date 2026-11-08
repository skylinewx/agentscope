"""Bounded navigation of persisted Python records; execution inputs remain intact."""
from .models import AnalysisError
from .query_results import read_receipt, compact_receipt, COMPACT_AT, encode


def inspect_run(record, run_ref, pointer='', offset=0, limit=50, search=None, fields=None):
    def navigation(value):
        if isinstance(value, list):
            return [navigation(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {('pythonRunRef' if key == 'captureRef' else 'pythonPath' if key == 'pointer' else key):
                  item if key in ('items', 'fields', 'receipt', 'value', 'sourceState') else navigation(item)
                  for key, item in value.items()}
        if 'pythonRunRef' in result:
            result['section'] = 'python'
        return result
    try:
        if not pointer and not fields and not search and offset == 0:
            size = len(encode(record))
            if size <= 6000:
                return record
            result = navigation(compact_receipt(record, run_ref, max(COMPACT_AT, size)))
            result['pythonRunRef'] = run_ref
            result['summary'] = {'status': record.get('status'), 'inputCount': len(record.get('inputRecords', [])),
                                 'outputCount': len(record.get('outputs', [])), 'codeChars': len(record.get('code', ''))}
        else:
            result = navigation(read_receipt(record, run_ref, pointer, offset, limit, search, fields))
    except AnalysisError as error:
        raise AnalysisError(error.code, str(error), navigation(error.details)) from error
    result.update(readTool='AnalysisInspect',
                  note='完整代码、输入、输出已保存。按 readOptions/read/nextRead 的 pythonPath 分页读取；展示省略不需要重新计算或取数。')
    return result
