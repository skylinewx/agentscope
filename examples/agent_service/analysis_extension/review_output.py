"""Repair the reviewer's own output against the same frozen material."""
from contextvars import ContextVar
from copy import deepcopy
import json
import re

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
    else:
        return None
    correction = dict(previous or {})
    correction['message'] = feedback
    if details.get('coverageIndexes') and hasattr(error, 'wire_verdict'):
        correction.update(verdict=error.wire_verdict, indexes=details['coverageIndexes'])
        correction['invalidPositions'] = error.invalid_positions
    return correction


def correction_request(request, schema):
    markdown = request.markdown
    correction = output_correction.get()
    if not correction:
        return markdown, schema
    note = '\n\n## 纠正本次审查的输出\n' + correction['message']
    if 'indexes' not in correction:
        return markdown + note + ' 请依据同一份材料，按原约定返回完整审查意见。', schema
    indexes = correction['indexes']
    rows = []
    for index in indexes:
        row = correction['verdict']['coverage'][index]
        rows.append({'index': index, 'requirement': row['requirement'], 'location': row['location'],
                     '待纠正的引用': [row['evidenceRefs'][i] for i in correction['invalidPositions'][index]]})
    note += (' 仅为下列条目选择能支持原判断的数据位置。原有业务判断和有效引用由宿主保留。\n'
             + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    groups = {}
    for label in request.wire_references:
        match = re.fullmatch(r'(表\d+)第(\d+)行|(结果|Python脚本)(\d+)', label)
        if match:
            group, number = (match[1], match[2]) if match[1] else (match[3], match[4])
            groups.setdefault(group, {})[int(number)] = label
    properties = {'index': {'type': 'integer', 'enum': indexes}}
    if groups:
        # Coordinates come from rendered tables. Range bounds stay small even for a large grid.
        sources, descriptions = {}, []
        for group, locations in groups.items():
            numbers = sorted(locations)
            if len(numbers) == numbers[-1] - numbers[0] + 1:
                allowed = {'minimum': numbers[0], 'maximum': numbers[-1]}
                description = f'{numbers[0]} 至 {numbers[-1]}'
            else:
                allowed = {'enum': numbers}
                description = '、'.join(map(str, numbers))
            sources[group] = {'type': 'array', 'items': {'type': 'integer', **allowed}, 'uniqueItems': True}
            descriptions.append(f'{group}的编号 {description}')
        properties['sources'] = {'type': 'object', 'properties': sources, 'additionalProperties': False}
        note += ('\n可选位置为' + '，'.join(descriptions) + '。在 sources 中以实际表名、结果或脚本为键，只填写整数编号。'
                 '章节标题已由 location 表达。如该条仅依据正文或没有额外数据位置，填空对象 {}。')
    else:
        properties['evidenceRefs'] = schema['properties']['coverage']['items']['properties']['evidenceRefs']
        note += '\n按原文选择可以核对的 evidenceRefs。仅依据正文时填空数组。'
    item = {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    return markdown + note, {'type': 'object', 'properties': {'repairs': {'type': 'array',
            'minItems': len(indexes), 'maxItems': len(indexes), 'items': item}},
            'required': ['repairs'], 'additionalProperties': False}


def corrected_verdict(response, request):
    correction = output_correction.get()
    if not correction or 'indexes' not in correction:
        return response
    rows = response['repairs']
    if sorted(row['index'] for row in rows) != correction['indexes']:
        raise AnalysisError('REVIEW_COVERAGE_INVALID', '请为每个指定的 index 各返回一次引用修正。')
    verdict = deepcopy(correction['verdict'])
    for row in rows:
        index = row['index']
        item = verdict['coverage'][index]
        refs = [ref for pos, ref in enumerate(item['evidenceRefs']) if pos not in correction['invalidPositions'][index]]
        if 'sources' in row:
            for group, numbers in row['sources'].items():
                for number in numbers:
                    location = f'{group}第{number}行' if group.startswith('表') else f'{group}{number}'
                    if location not in request.wire_references:
                        raise AnalysisError('REVIEW_COVERAGE_INVALID', '请选择本次材料实际列出的数据编号。')
                    refs.append(location)
        else:
            refs.extend(row['evidenceRefs'])
        item['evidenceRefs'] = list(dict.fromkeys(refs))
    return verdict
