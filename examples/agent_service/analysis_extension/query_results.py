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


def read_options(value, pointer):
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
        if isinstance(child, (list, str)):
            option.update(offset=0, limit=10)
        if len(encode(options + [option])) <= 10000:
            options.append(option)
    return options


def step_reads(receipt):
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
        entry = {'pointer': pointer, 'readOptions': read_options(step, pointer)}
        if isinstance(step.get('stepId'), str) and len(step['stepId']) <= 200:
            entry['stepId'] = step['stepId']
        if len(encode(result + [entry])) > 6000:
            break
        result.append(entry)
    return result


def project(value, pointer, omitted, width=20, depth=0, defer=False):
    """Every omitted subtree has a recoverable pointer; source is untouched."""
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


def compact_receipt(receipt, capture_ref, source_chars):
    if source_chars < COMPACT_AT:
        return None
    for width in (20, 5, 1):
        omitted = []
        result = {'viewType': 'captured_query_result', 'captureRef': capture_ref,
                  'receipt': project(receipt, '', omitted, width, defer=True), 'omitted': omitted,
                  'stepReads': step_reads(receipt),
                  'sourceChars': source_chars, 'readTool': 'QueryResultRead',
                  'note': '完整原回执已保存。stepReads 提供实际步骤读取路径，不代表步骤全集；未列出的步骤从 /data/stepResults 分页读取。stepId 不是数组下标。omitted 仅为展示省略，不表示来源已完整；沿返回 pointer/nextOffset 读取，不要重新查询或为了读取而构建分析。'}
        if len(encode(result)) <= MODEL_CHARS:
            return result
    # Unusual shapes stay intact instead of concealing metadata or errors.
    return None


def read_receipt(receipt, capture_ref, pointer='', offset=0, limit=20, search=None, fields=None):
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

    def select_fields(item, path):
        if fields is None:
            return item
        if not isinstance(item, dict) or any(k not in item for k in fields):
            raise AnalysisError('QUERY_FIELD_NOT_FOUND', '所选直接字段不存在；按实际字段或子路径读取，不能当成空值。',
                                {'pointer': path, 'availableFields': available_fields(item),
                                 'readOptions': read_options(item, path)})
        return {k: item[k] for k in fields}

    if isinstance(value, list):
        matches = [(i, item) for i, item in enumerate(value) if search is None or search in encode(item)]
        page = matches[offset:offset + limit]
        result.update(sourceCount=len(value), matchingTotal=len(matches), requestedLimit=limit,
                      items=[select_fields(item, child_pointer(pointer, i)) for i, item in page],
                      itemPointers=[child_pointer(pointer, i) for i, _ in page])
        if offset + len(page) < len(matches):
            result['nextOffset'] = offset + len(page)
        # Keep whole items, reduce only this read page, and expose the exact continuation.
        if len(encode(result)) > MODEL_CHARS and len(page) > 1:
            items, pointers = result['items'], result['itemPointers']
            low, high, fit = 1, len(page) - 1, 0
            while low <= high:
                count = (low + high) // 2
                candidate = {**result, 'items': items[:count], 'itemPointers': pointers[:count],
                             'nextOffset': offset + count, 'pageLimited': True}
                if len(encode(candidate)) <= MODEL_CHARS:
                    fit, low = count, count + 1
                else:
                    high = count - 1
            if fit:
                result.update(items=items[:fit], itemPointers=pointers[:fit], nextOffset=offset + fit, pageLimited=True)
    elif isinstance(value, str):
        if search is not None or fields is not None:
            raise AnalysisError('INVALID_QUERY_SELECTION', '文本分页不接受 search 或 fields。')
        result.update(value=value[offset:offset + limit * 100], sourceCount=len(value))
        if offset + len(result['value']) < len(value):
            result['nextOffset'] = offset + len(result['value'])
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
