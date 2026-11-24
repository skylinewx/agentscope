"""Repair the reviewer's own output against the same frozen material."""
from contextvars import ContextVar
from copy import deepcopy
import json

from agentscope.exception import StructuredOutputError
from jsonschema import ValidationError

from .models import AnalysisError


output_correction = ContextVar('review_output_correction', default=None)


class ReviewOutputUnavailable(AnalysisError):
    def __init__(self, limit):
        super().__init__('REVIEW_UNAVAILABLE', f'审查意见经过 {limit} 次格式修正仍未通过校验，当前材料与已完成阶段已保留。',
                         {'failureKind': 'REVIEW_OUTPUT_INVALID', 'requiresBuild': False, 'outputRepairLimit': limit})


def correction_for(error, diagnostic, previous):
    if isinstance(error, AnalysisError) and error.code == 'REVIEW_COVERAGE_INVALID':
        details = error.details or {}
        feedback = str(error)
    elif isinstance(error, ValidationError):
        details = {}
        feedback = f'字段 {error.json_path} 未满足输出约定 {error.validator}。'
    elif diagnostic.get('failureKind') == 'structured_output' or isinstance(error, StructuredOutputError):
        details = {}
        feedback = '模型回复未生成符合约定的结构化审查意见。'
        cause = error.__cause__
        if isinstance(cause, ValidationError):
            feedback = f'字段 {cause.json_path} 未满足 {cause.validator} 约束。请按该条可用位置和剩余额度填写。'
    else:
        return None
    correction = dict(previous or {})
    if details.get('coverageIndexes') and hasattr(error, 'wire_verdict'):
        correction.update(verdict=error.wire_verdict, indexes=details['coverageIndexes'],
                          invalidPositions=error.invalid_positions)
    elif getattr(error, 'requires_full_review', False):
        correction = {}
    correction['message'] = feedback
    return correction


def retained_refs(correction, index):
    invalid = set(correction['invalidPositions'][index])
    refs = correction['verdict']['coverage'][index]['evidenceRefs']
    return list(dict.fromkeys(ref for pos, ref in enumerate(refs) if pos not in invalid))


def coverage_overflow(verdict, schema):
    """Only isolate a length error when the rest of the complete verdict is valid."""
    from jsonschema import validate
    if ('coverage' not in schema.get('properties', {}) or not isinstance(verdict, dict)
            or not isinstance(verdict.get('coverage'), list)):
        return None
    limit = schema['properties']['coverage']['items']['properties']['evidenceRefs']['maxItems']
    check = deepcopy(verdict)
    positions = {}
    for index, row in enumerate(check.get('coverage') or []):
        refs = row.get('evidenceRefs') if isinstance(row, dict) else None
        if isinstance(refs, list) and len(refs) > limit and all(isinstance(ref, str) for ref in refs):
            positions[index] = list(range(len(refs)))
            row['evidenceRefs'] = []
    if not positions:
        return None
    try:
        validate(check, schema)
    except ValidationError:
        return None
    error = AnalysisError('REVIEW_COVERAGE_INVALID', f'每项最多引用 {limit} 个位置；请选择足以支持原判断的代表位置。',
                          {'coverageIndexes': list(positions)})
    error.wire_verdict, error.invalid_positions = verdict, positions
    return error


def initial_review_schema(schema):
    """Keep a parsed verdict available when only its citation count needs correction.

    The SDK otherwise discards the parsed object on maxItems and repeats the
    entire model request. The host immediately validates the canonical contract.
    """
    transport = deepcopy(schema)
    refs = transport['properties']['coverage']['items']['properties']['evidenceRefs']
    limit = refs.pop('maxItems')
    refs['description'] = f'每项最多 {limit} 个实际位置，通常一两个即可；仅依据正文时填空数组。'
    return transport


def correction_request(request, schema):
    correction = output_correction.get()
    if not correction:
        return request.markdown, schema
    note = '\n\n## 纠正本次审查的输出\n' + correction['message']
    if 'indexes' not in correction:
        return request.markdown + note + ' 请依据同一份材料，按原约定返回完整审查意见。', schema
    from .review_reference_index import reference_index, numbered_locations
    locations = numbered_locations(request)
    limit = schema['properties']['coverage']['items']['properties']['evidenceRefs']['maxItems']
    props = {}
    rows = []
    for index in correction['indexes']:
        item = correction['verdict']['coverage'][index]
        kept = retained_refs(correction, index)
        remaining = max(0, limit - len(kept))
        position = {'type': 'integer', 'minimum': 1, 'maximum': len(locations)} if locations else {'type': 'string'}
        props[str(index)] = {'type': 'array', 'items': position, 'uniqueItems': True, 'maxItems': remaining}
        rows.append({'条目': index, '要求': item['requirement'], '判断': item['status'], '位置': item['location'],
                     '已保留': kept, '待纠正': [item['evidenceRefs'][i] for i in correction['invalidPositions'][index]],
                     '最多可补': remaining})
    coordinate = '在数组中只填“引用号”对应的整数。' if locations else '按本份历史材料里的实际引用填写。'
    note += ('\n仅核对列出的条目，原判断和有效引用已保留。' + coordinate
             + '每条通常只需一两个直接依据，仅依据正文或已有引用足够时填 []。\n'
             + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    patch_schema = {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}
    return reference_index(request, locations) + note, {'type': 'object', 'properties': {'repairs': patch_schema},
                                             'required': ['repairs'], 'additionalProperties': False}


def corrected_verdict(response, request):
    correction = output_correction.get()
    if not correction or 'indexes' not in correction:
        return response
    verdict = deepcopy(correction['verdict'])
    from .review_reference_index import numbered_locations
    locations = numbered_locations(request)
    for index in correction['indexes']:
        refs = response['repairs'][str(index)]
        if locations:
            refs = [locations[number - 1] for number in refs]
        verdict['coverage'][index]['evidenceRefs'] = list(dict.fromkeys(retained_refs(correction, index) + refs))
    return verdict
