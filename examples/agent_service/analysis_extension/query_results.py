"""Bounded model views of immutable receipts; never a replacement source."""

from __future__ import annotations

import json
import re
from itertools import islice

from .models import AnalysisError, json_values

MODEL_CHARS = 24000
COMPACT_AT = 16000
STATE_KEYS = ('contractVersion', 'status', 'completeness', 'resumeRef', 'error', 'errors', 'executionId', 'context', 'querySessionId')


def encode(value):
    return json.dumps(json_values(value), ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def child_pointer(parent, key):
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def at_pointer(value, pointer):
    if not isinstance(pointer, str) or len(pointer) > 2048 or (pointer and not pointer.startswith('/')):
        raise AnalysisError('INVALID_QUERY_POINTER', 'pointer 使用回执中的 JSON Pointer，根为字符串空值。')
    resolved = ''
    for part in pointer.split('/')[1:]:
        if re.search(r'~(?![01])', part):
            raise AnalysisError('INVALID_QUERY_POINTER', 'JSON Pointer 的 ~ 必须转义为 ~0，/ 转义为 ~1。')
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) < len(value):
            value = value[int(key)]
        else:
            raise AnalysisError('QUERY_POINTER_NOT_FOUND', '该路径不存在；按实际子路径继续读取，不按步骤编号猜数组下标。',
                                {'pointer': pointer, 'parentPointer': resolved, 'readOptions': read_options(value, resolved)})
        resolved += '/' + part
    return value


def available_fields(value):
    fields = []
    if isinstance(value, dict):
        for key in islice(value, 30):
            if len(encode(fields + [key])) <= 4000:
                fields.append(key)
    return fields


def read_arguments(value, pointer, capture_ref):
    """从实际数据生成有界读取参数；仅提供导航投影，不把它当作完整业务数据。"""
    if capture_ref is None:
        return None
    request = {'captureRef': capture_ref, 'pointer': pointer}
    samples = value[:10] if isinstance(value, list) else [value]
    if isinstance(value, (list, str)):
        request.update(offset=0, limit=10)
    if samples and all(isinstance(item, dict) for item in samples):
        # 共同的直接标量字段来自当前页，不把 raw key 与分析事实 cellKey 混为一谈。
        fields = [key for key in available_fields(samples[0]) if all(
            key in item and not isinstance(item[key], (dict, list))
            and (not isinstance(item[key], str) or len(item[key]) <= 1200) for item in samples)]
        if not fields:
            return None
        projected = [{key: item[key] for key in fields} for item in samples]
        if len(encode(projected)) > 4000:
            return None
        request['fields'] = fields
    elif isinstance(value, str):
        pass
    elif len(encode(samples)) > 4000:
        return None
    return request


def read_options(value, pointer, capture_ref=None):
    """Small, exact child locations; suggestions never invent receipt fields."""
    children = islice(value.items(), 20) if isinstance(value, dict) else enumerate(value[:5]) if isinstance(value, list) else []
    options = []
    for key, child in children:
        path = child_pointer(pointer, key)
        if len(path) > 2048:
            continue
        option = {'pointer': path, 'kind': 'object' if isinstance(child, dict) else 'array' if isinstance(child, list) else 'text' if isinstance(child, str) else 'scalar'}
        if isinstance(child, (dict, list, str)):
            option['size'] = len(child)
        if isinstance(child, dict):
            option['availableFields'] = available_fields(child)
            if isinstance(child.get('stepId'), str) and len(child['stepId']) <= 200:
                option['stepId'] = child['stepId']
        if isinstance(child, list) and child and isinstance(child[0], dict):
            option['availableFields'] = [key for key in available_fields(child[0])
                                         if all(isinstance(item, dict) and key in item for item in child[:10])]
            option['fieldSampleCount'] = min(10, len(child))
        if isinstance(child, (list, str)):
            option.update(offset=0, limit=10)
        request = read_arguments(child, path, capture_ref)
        if request is not None:
            option['read'] = request
        if len(encode(options + [option])) <= 10000:
            options.append(option)
    return options


def step_reads(receipt, capture_ref=None):
    """Exact receipt locations, including steps omitted from the bounded model view."""
    data = receipt.get('data')
    steps = data.get('stepResults') if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    result = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        pointer = child_pointer('/data/stepResults', index)
        entry = {'pointer': pointer, 'readOptions': read_options(step, pointer, capture_ref)}
        if isinstance(step.get('stepId'), str) and len(step['stepId']) <= 200:
            entry['stepId'] = step['stepId']
        if isinstance(step.get('periodExecution'), dict):
            progress = step['periodExecution']
            entry['periodExecution'] = {key: progress.get(key) for key in
                                       ('executionPeriod', 'periodIndex', 'periodCount', 'workIndex', 'workCount', 'periodFinished', 'planFinished')}
            entry['periodResolutionReadAt'] = child_pointer(pointer, 'periodExecution')
        if len(encode(result + [entry])) > 6000:
            break
        result.append(entry)
    return result


def project(value, pointer, omitted, width=20, depth=0, defer=False):
    """Every omitted subtree has a recoverable pointer; source is untouched."""
    # TABLE 的列定义和一行的单元格必须整体读取，不能按通用 width 截成错位矩阵。
    if re.search(r'/batchResults/[0-9]+/data/(columns|rows/[0-9]+)$', pointer):
        if len(encode(value)) <= 10000:
            return value
        omitted.append({'pointer': pointer, 'kind': 'tableSchemaOrRow', 'returned': 0})
        return {'readAt': pointer}
    if defer and pointer in ('/evidence', '/data/normalizedPlan'):
        omitted.append({'pointer': pointer, 'kind': 'deferred', 'returned': 0})
        return {'readAt': pointer}
    if depth >= 12 and isinstance(value, (dict, list)):
        omitted.append({'pointer': pointer, 'kind': 'subtree', 'returned': 0})
        return {'readAt': pointer}
    if isinstance(value, list):
        result = [project(v, child_pointer(pointer, i), omitted, width, depth + 1, defer) for i, v in enumerate(value[:width])]
        if len(value) > width:
            omitted.append({'pointer': pointer, 'kind': 'array', 'total': len(value), 'returned': width, 'nextOffset': width})
        return result
    if isinstance(value, dict):
        keys = [k for k in STATE_KEYS if k in value] + [k for k in value if k not in STATE_KEYS]
        selected = keys[:max(width, len(STATE_KEYS))]
        result = {k: project(value[k], child_pointer(pointer, k), omitted, width, depth + 1, defer) for k in selected}
        if len(keys) > len(selected):
            omitted.append({'pointer': pointer, 'kind': 'object', 'total': len(keys), 'returned': len(selected), 'readFields': keys[len(selected):]})
        return result
    if isinstance(value, str) and len(value) > 1200:
        omitted.append({'pointer': pointer, 'kind': 'text', 'total': len(value), 'returned': 1200, 'nextOffset': 1200})
        return value[:1200]
    return value


def compact_receipt(receipt, capture_ref, source_chars, max_chars=MODEL_CHARS):
    if source_chars < COMPACT_AT:
        return None
    for width in (20, 5, 1):
        omitted = []
        result = {'viewType': 'captured_query_result', 'captureRef': capture_ref,
                  'receipt': project(receipt, '', omitted, width, defer=True), 'omitted': omitted,
                  'stepReads': step_reads(receipt, capture_ref),
                  'analysisRead': {'section': 'inventory', 'captureRef': capture_ref},
                  'sourceChars': source_chars, 'readTool': 'QueryResultRead',
                  'note': '完整原回执已保存。分析所需的值、公式与选择器可用 analysisRead 调用 AnalysisInspect 定向读取，不必先通读原报文。原始字段按 readOptions.read 调用 QueryResultRead；fields 是导航投影，不能当作已读取全部字段。stepReads 不代表步骤全集，未列步骤从 /data/stepResults 分页读取；stepId 不是下标。展示省略不代表来源完整，不要因此重新查询。'}
        if len(encode(result)) <= max_chars:
            return result
    # Unusual shapes stay intact instead of concealing metadata or errors.
    return None


def read_receipt(receipt, capture_ref, pointer='', offset=0, limit=20, search=None, fields=None):
    """原回执不变；错误恢复参数同样从该回执生成，不猜字段别名。"""
    try:
        return _read_receipt(receipt, capture_ref, pointer, offset, limit, search, fields)
    except AnalysisError as error:
        details = dict(error.details or {})
        details['captureRef'] = capture_ref
        for option in details.get('readOptions', []):
            child = at_pointer(receipt, option['pointer'])
            request = read_arguments(child, option['pointer'], capture_ref)
            if request is not None:
                option['read'] = request
                if len(encode(details)) > MODEL_CHARS - 2000:
                    # 错误恢复也须有界；保留原有路径和字段，不为示例撑大错误报文。
                    option.pop('read')
        raise AnalysisError(error.code, str(error), details) from error


def _read_receipt(receipt, capture_ref, pointer='', offset=0, limit=20, search=None, fields=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AnalysisError('INVALID_QUERY_PAGE', 'offset 须非负，limit 须在 1 至 100 之间。')
    if search is not None and (not isinstance(search, str) or not 1 <= len(search) <= 200):
        raise AnalysisError('INVALID_QUERY_SEARCH', 'search 为 1 至 200 字的原文片段。')
    if fields is not None and (not isinstance(fields, list) or not 1 <= len(fields) <= 30 or any(not isinstance(k, str) or not k for k in fields) or len(set(fields)) != len(fields)):
        raise AnalysisError('INVALID_QUERY_FIELDS', 'fields 为 1 至 30 个不重复的直接字段名。')
    value = at_pointer(receipt, pointer)
    state, state_omitted = {}, []
    for key in STATE_KEYS:
        if key not in receipt:
            continue
        path = child_pointer('', key)
        if len(encode(receipt[key])) <= 1600:
            state[key] = receipt[key]
        else:
            state[key] = {'readAt': path}
            state_omitted.append({'pointer': path, 'kind': 'sourceState', 'returned': 0})
    result = {'captureRef': capture_ref, 'pointer': pointer,
              'sourceState': state, 'stateOmitted': state_omitted,
              'offset': offset, 'nextOffset': None}
    if isinstance(value, dict):
        result['availableFields'] = available_fields(value)
        result['readOptions'] = read_options(value, pointer, capture_ref)

    def next_request(next_offset):
        return {'captureRef': capture_ref, 'pointer': pointer, 'offset': next_offset, 'limit': limit,
                **({'search': search} if search is not None else {}),
                **({'fields': fields} if fields is not None else {})}

    def continuation():
        if result['nextOffset'] is not None:
            result['nextRead'] = next_request(result['nextOffset'])

    table_match = re.match(r'(.*/batchResults/[0-9]+)/data/rows(?:/|$)', pointer)
    if table_match:
        table_path = table_match.group(1)
        table = at_pointer(receipt, table_path)
        if isinstance(table, dict) and table.get('resultType') == 'BATCH_RESULT':
            data = table.get('data', {})
            metadata = {'columns': data.get('columns', []), 'columnRoles': table.get('columnRoles', {}),
                        'fixedDimValMap': data.get('fixedDimValMap', {}), 'fixedDimNameMap': data.get('fixedDimNameMap', {}),
                        'rowCount': data.get('rowCount'), 'disposition': table.get('disposition')}
            result['tableContext'] = metadata if len(encode(metadata)) <= 8000 else {
                'columnsReadAt': table_path + '/data/columns', 'columnRolesReadAt': table_path + '/columnRoles',
                'fixedDimensionsReadAt': table_path + '/data/fixedDimValMap', 'rowCount': data.get('rowCount')}
            result['tableContext']['requestReadAt'] = table_path + '/request'

    def select_fields(item, path):
        if fields is None:
            return item
        if not isinstance(item, dict) or any(k not in item for k in fields):
            raise AnalysisError('QUERY_FIELD_NOT_FOUND', '所选直接字段不存在；按实际字段或子路径读取，不能当成空值。',
                                {'pointer': path, 'availableFields': available_fields(item),
                                 'readOptions': read_options(item, path)})
        return {k: item[k] for k in fields}

    if isinstance(value, list):
        if not value and pointer.endswith('/cells'):
            parent_pointer = pointer.rsplit('/', 1)[0]
            parent = at_pointer(receipt, parent_pointer)
            page = parent.get('page') if isinstance(parent, dict) else None
            if isinstance(page, dict) and isinstance(page.get('cells'), list) and page['cells']:
                actual = parent_pointer + '/page/cells'
                result['relatedRead'] = {'captureRef': capture_ref, 'pointer': actual, 'offset': 0, 'limit': limit}
                result['note'] = '当前 cells 确为空；同一表的实际分页单元格位于 relatedRead，不需要重新取数。'
        matches = [(i, item) for i, item in enumerate(value) if search is None or search in encode(item)]
        page = matches[offset:offset + limit]
        result.update(sourceCount=len(value), matchingTotal=len(matches), requestedLimit=limit,
                      items=[select_fields(item, child_pointer(pointer, i)) for i, item in page],
                      itemPointers=[child_pointer(pointer, i) for i, _ in page])
        if offset + len(page) < len(matches):
            result['nextOffset'] = offset + len(page)
        continuation()
        # Keep whole items, reduce only this read page, and expose the exact continuation.
        if len(encode(result)) > MODEL_CHARS and len(page) > 1:
            items, pointers = result['items'], result['itemPointers']
            low, high, fit = 1, len(page) - 1, 0
            while low <= high:
                count = (low + high) // 2
                candidate = {**result, 'items': items[:count], 'itemPointers': pointers[:count],
                             'nextOffset': offset + count, 'pageLimited': True,
                             'nextRead': next_request(offset + count)}
                if len(encode(candidate)) <= MODEL_CHARS:
                    fit, low = count, count + 1
                else:
                    high = count - 1
            if fit:
                result.update(items=items[:fit], itemPointers=pointers[:fit], nextOffset=offset + fit, pageLimited=True)
                continuation()
    elif isinstance(value, str):
        if search is not None or fields is not None:
            raise AnalysisError('INVALID_QUERY_SELECTION', '文本分页不接受 search 或 fields。')
        result.update(value=value[offset:offset + limit * 100], sourceCount=len(value))
        if offset + len(result['value']) < len(value):
            result['nextOffset'] = offset + len(result['value'])
        continuation()
    else:
        if search is not None or offset:
            raise AnalysisError('INVALID_QUERY_SELECTION', '对象或标量不接受 search 或非零 offset；请指定字段或下级 pointer。')
        result['value'] = select_fields(value, pointer)
    # A single object/item cannot be shortened without changing its meaning.
    if len(encode(result)) > MODEL_CHARS:
        item_path, item = (child_pointer(pointer, page[0][0]), page[0][1]) if isinstance(value, list) and page else (pointer, value)
        raise AnalysisError('QUERY_SELECTION_TOO_LARGE', '单个对象或数组项超过容量；limit 不缩小对象内部字段。请用 availableFields 选直接字段，或按 readOptions 的子路径读取。',
                            {'pointer': item_path, 'availableFields': available_fields(item),
                             'readOptions': read_options(item, item_path), 'limitAppliesTo': 'array_items_or_text_only'})
    return result
