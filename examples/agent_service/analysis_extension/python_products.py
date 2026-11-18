"""冻结授权数据文件及通用代码产物；不把脚本结果升级成来源业务定义。"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import csv
import io
import json
from pathlib import Path
import re

from .models import AnalysisError, decimal_text, decimal_value, digest
from .python_worker import MAX_BYTES


def product_number(raw, decimal_places=None):
    """仅在输出明确声明精度时舍入；不改源值、运算中间精度或全局数值上限。"""
    try:
        if decimal_places is None:
            return decimal_text(decimal_value(raw))
        if type(decimal_places) is not int or not 0 <= decimal_places <= 16:
            raise AnalysisError('PYTHON_RESULT_INVALID', 'decimalPlaces 使用 0—16 的整数。')
        if isinstance(raw, (bool, float)) or not isinstance(raw, (str, int, Decimal)) or len(str(raw)) > 512:
            raise AnalysisError('PYTHON_RESULT_INVALID', '数值输出须使用有界十进制字符串。')
        number = Decimal(raw)
        if not number.is_finite() or len(number.as_tuple().digits) > 128 or abs(number.adjusted()) > 128:
            raise AnalysisError('NUMBER_LIMIT', '数值超出支持的精度或数量级。')
        with localcontext() as context:
            context.prec = 512
            rounded = number.quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_EVEN)
        return decimal_text(decimal_value(rounded))
    except InvalidOperation as error:
        raise AnalysisError('PYTHON_RESULT_INVALID', '输出不是有效的十进制数值。') from error
    except AnalysisError as error:
        if error.code == 'NUMBER_LIMIT':
            raise AnalysisError(error.code, str(error), {
                'requiresSourceQuery': False, 'nextAction': 'CHECK_OUTPUT_PRECISION',
                'hint': '比例等除法输出可能含大量小数；在该标量或 DECIMAL_STRING 列声明 decimalPlaces（0—16），只舍入输出，不重新查询数据。'}) from error
        raise


def load_files(service, specifications):
    """只读当前智能体工作区内显式指定的数据文件，内容随执行冻结。"""
    if not isinstance(specifications, list) or len(specifications) > 20:
        raise AnalysisError('PYTHON_FILE_LIMIT', '最多指定 20 个工作区数据文件。')
    result = []
    total = 0
    for spec in specifications:
        raw = spec.get('path') if isinstance(spec, dict) else None
        if (not isinstance(raw, str) or len(raw) > 1024 or '\\' in raw or ':' in raw or raw.startswith('/')
                or any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in raw.removeprefix('./').split('/'))):
            raise AnalysisError('PYTHON_FILE_PATH', '使用当前工作区内的数据文件相对路径，不允许上级目录或链接。')
        if service.delivery is None:
            raise AnalysisError('PYTHON_FILE_PATH', '当前会话未提供授权工作区。')
        root = Path(service.delivery.root).resolve()
        path = root
        for part in raw.removeprefix('./').split('/'):
            path /= part
            if path.is_symlink() or path.is_junction():
                raise AnalysisError('PYTHON_FILE_PATH', '数据文件路径不能经过链接。')
        suffix = path.suffix.lower()
        if not path.resolve().is_relative_to(root) or suffix not in ('.json', '.csv', '.tsv', '.txt'):
            raise AnalysisError('PYTHON_FILE_PATH', '只支持当前工作区内 UTF-8 JSON、CSV、TSV、TXT 数据。')
        try:
            with path.open('rb') as stream:
                content = stream.read(MAX_BYTES + 1)
            total += len(content)
            if total > MAX_BYTES:
                raise AnalysisError('PYTHON_INPUT_LIMIT', '数据文件总量超过单次 4 MiB 上限。')
            text = content.decode('utf-8-sig')
            data = (json.loads(text, parse_float=str) if suffix == '.json' else
                    list(csv.reader(io.StringIO(text), delimiter='\t' if suffix == '.tsv' else ','))
                    if suffix in ('.csv', '.tsv') else text)
        except (OSError, ValueError, UnicodeError) as error:
            raise AnalysisError('PYTHON_FILE_READ', '无法读取指定的 UTF-8 数据文件。', {'path': raw}) from error
        result.append({'ref': 'file_' + digest([raw, text])[:24], 'kind': 'FILE', 'path': raw,
                       'data': data, 'contentDigest': digest(text), 'provenance': 'WORKSPACE_FILE',
                       'sourceFactIds': [], 'valueState': 'NOT_APPLICABLE'})
    if len({item['ref'] for item in result}) != len(result):
        raise AnalysisError('PYTHON_FILE_LIMIT', '同一输入文件不能重复声明。')
    return result


def bind_single_grid(outputs, inputs, file_reads):
    """只有一个授权网格且宿主实测读取完整时，补记产物省略的文件来源。"""
    if len(inputs) != 1 or inputs[0].get('kind') != 'GRID_FILE' or not file_reads:
        return []
    grid = inputs[0]
    if any(read.get('ref') != grid['ref'] or read.get('sha256') != grid['sha256']
           or read.get('exhausted') is not True or read.get('scannedCount') != grid['cellCount'] for read in file_reads):
        return []
    if not any(read.get('matchedCount', 0) > 0 for read in file_reads):
        return []
    positions = []
    for index, output in enumerate(outputs):
        if isinstance(output, dict) and 'inputRefs' not in output:
            output['inputRefs'] = [grid['ref']]
            positions.append(index)
    return positions


def products(outputs, known, input_refs, scope_id, run_ref):
    """输出按实际类型校验，统一保留依赖，可用于下一次处理或报告数据绑定。"""
    names, values = set(), []
    for output_index, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise AnalysisError('PYTHON_RESULT_INVALID', '输出必须是带 name、label、inputRefs 的对象。')
        name, refs, kind = output.get('name'), output.get('inputRefs'), output.get('kind', 'SCALAR')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name) or name in names:
            raise AnalysisError('PYTHON_RESULT_INVALID', '输出 name 必须为唯一的字母开头标识。')
        if (not isinstance(output.get('label'), str) or not 1 <= len(output['label']) <= 300
                or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs) or not set(refs) <= set(input_refs)):
            raise AnalysisError('PYTHON_RESULT_INVALID', '输出须有简短名称及来自本次输入的真实依赖。',
                                {'path': f'/result/{output_index}', 'requiresSourceQuery': False,
                                 'requiredFields': ['label', 'inputRefs'],
                                 'hint': '每项输出的 inputRefs 从该项实际使用的 inputs[*].ref 收集；不是 captureRef、列名或自造标识。',
                                 'availableInputRefs': input_refs[:20], 'availableInputCount': len(input_refs)})
        common = {'name', 'label', 'inputRefs', 'kind'}
        payload, value, unit, state = {}, None, None, 'NOT_APPLICABLE'
        if kind == 'SCALAR':
            if set(output) - common - {'decimalPlaces'} != {'value', 'unit'}:
                raise AnalysisError('PYTHON_RESULT_INVALID', 'SCALAR 输出需要 value、unit。')
            if 'decimalPlaces' in output:
                product_number('0', output['decimalPlaces'])
            unit, raw = output['unit'], output['value']
            if unit is not None and (not isinstance(unit, str) or len(unit) > 100):
                raise AnalysisError('PYTHON_RESULT_INVALID', 'unit 使用短字符串或 null。')
            value = None if raw is None else product_number(raw, output.get('decimalPlaces'))
            if 'decimalPlaces' in output:
                payload['rounding'] = {'mode': 'ROUND_HALF_EVEN', 'decimalPlaces': output['decimalPlaces']}
            state = 'NO_VALUE' if value is None else 'PRESENT'
        elif kind in ('TABLE', 'CHART_DATA'):
            if set(output) - common != {'columns', 'rows'}:
                raise AnalysisError('PYTHON_RESULT_INVALID', 'TABLE/CHART_DATA 需要 columns 和 rows。')
            columns, rows = output['columns'], output['rows']
            if (not isinstance(columns, list) or not 1 <= len(columns) <= 100
                    or any(not isinstance(col, dict) or set(col) - {'name', 'label', 'type', 'unit', 'decimalPlaces'}
                           or not isinstance(col.get('name'), str) or not col['name']
                           or not isinstance(col.get('label'), str)
                           or col.get('type') not in ('STRING', 'DECIMAL_STRING', 'BOOLEAN') for col in columns)
                    or len({col['name'] for col in columns}) != len(columns)
                    or not isinstance(rows, list) or len(rows) > 20000):
                raise AnalysisError('PYTHON_RESULT_INVALID', '表产物需要唯一列名、标签、明确类型，最多 100 列、20000 行。')
            rows = deepcopy(rows)
            for column in columns:
                if 'decimalPlaces' in column:
                    if column['type'] != 'DECIMAL_STRING':
                        raise AnalysisError('PYTHON_RESULT_INVALID', 'decimalPlaces 仅用于 DECIMAL_STRING 列。')
                    product_number('0', column['decimalPlaces'])
            for row_index, row in enumerate(rows):
                if not isinstance(row, list) or len(row) != len(columns):
                    raise AnalysisError('PYTHON_RESULT_INVALID', 'rows 必须是二维数组；每行按 columns 顺序提供同样数量的值，不能传字典行。',
                                        {'path': f'/result/{output_index}/rows/{row_index}',
                                         'requiresSourceQuery': False, 'nextAction': 'FIX_OUTPUT_ROWS',
                                         'columnNames': [column['name'] for column in columns],
                                         'expectedColumnCount': len(columns), 'actualRowType': type(row).__name__,
                                         'actualColumnCount': len(row) if isinstance(row, (list, dict)) else None,
                                         'hint': '保持原数据与口径，仅调整输出结构。若已有字典行且字段齐全，使用 [[record[column["name"]] for column in columns] for record in records]；缺字段应明确处理，不用 0 补齐。'})
                for index, column in enumerate(columns):
                    cell = row[index]
                    if cell is None:
                        continue
                    if column['type'] == 'DECIMAL_STRING':
                        row[index] = product_number(cell, column.get('decimalPlaces'))
                    elif not isinstance(cell, str if column['type'] == 'STRING' else bool):
                        raise AnalysisError('PYTHON_RESULT_INVALID', '表产物单元格与声明类型不一致。')
            payload = {'columns': columns, 'rows': rows, 'rowCount': len(rows)}
        elif kind == 'DIAGNOSTIC':
            if set(output) - common != {'data'} or not isinstance(output['data'], (dict, list)):
                raise AnalysisError('PYTHON_RESULT_INVALID', 'DIAGNOSTIC 需要结构化 data。')
            payload = {'data': output['data']}
        elif kind == 'FILE':
            if (set(output) - common != {'filename', 'text'} or not isinstance(output['text'], str)
                    or not isinstance(output['filename'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}\.(csv|tsv|json|txt)', output['filename'])):
                raise AnalysisError('PYTHON_RESULT_INVALID', 'FILE 需要简单 filename（csv/tsv/json/txt）和 UTF-8 text。')
            if output['filename'].endswith('.json'):
                try:
                    json.loads(output['text'])
                except ValueError as error:
                    raise AnalysisError('PYTHON_RESULT_INVALID', 'JSON 文件内容无效。') from error
            payload = {'filename': output['filename'], 'text': output['text'], 'contentDigest': digest(output['text'])}
        else:
            raise AnalysisError('PYTHON_RESULT_INVALID', 'kind 使用 SCALAR/TABLE/CHART_DATA/DIAGNOSTIC/FILE。')
        names.add(name)
        sources = sorted({source for ref in refs for source in known[ref].get('sourceFactIds', [ref])})
        values.append({'derivedId': 'derived_' + run_ref + '_' + name, 'operator': 'PYTHON',
                       'label': output['label'], 'scopeId': scope_id, 'inputRefs': refs, 'sourceFactIds': sources,
                       'value': value, 'unit': unit, 'valueState': state, 'outputKind': kind, **payload,
                       'pythonRunRef': run_ref, 'calculationValidation': 'PASS', 'executionStatus': 'COMPLETED',
                       'sourceConsistency': 'NOT_CHECKED', 'businessValidation': 'NOT_REVIEWED', 'diagnostics': []})
    return values


def deliver_files(service, values):
    """文件仅落入宿主分配的分析目录，代码不能自行选择写入位置。"""
    for value in values:
        if value.get('outputKind') == 'FILE' and service.delivery is not None:
            relative = 'analysis/python/' + value['pythonRunRef'] + '/' + value['derivedId'] + '/' + value['filename']
            service.delivery._write(relative, value['text'].encode('utf-8'))
            value['path'] = './' + relative
