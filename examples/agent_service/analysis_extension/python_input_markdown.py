"""Render the AI's pre-calculation material from the exact, owner-bound Python inputs."""
from collections import defaultdict
from decimal import Decimal
import json
import re

from .business_tables import (
    BUDGET_TOTAL_MEMBER, BusinessTable, DEFAULT_LABELS, STATES, actual_periods, escape,
)
from .formula_arithmetic import BUDGET_FORMULA


def _row(cells):
    return '| ' + ' | '.join(escape(cell) for cell in cells) + ' |'


def _header(cells):
    return _row(cells) + '\n' + _row(['---'] * len(cells))


def _cell(fact):
    if fact is None:
        return '未返回'
    if fact.get('valueState') != 'PRESENT':
        return STATES.get(fact.get('valueState'), fact.get('valueState') or '')
    value = fact.get('value')
    if fact.get('valueType') == 'DECIMAL_STRING' and isinstance(value, str) and re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        return format(Decimal(value), ',f')
    return value


def _data_rows(table, rows, dimensions):
    headers = [table.labels.get(dim, dim) for dim in dimensions]
    yield _header(headers + [table.column_label(col) for col in table.columns])
    for row in rows:
        names = row.get('displayLabels', {})
        values = [names.get(dim, row['labels'][dim]) for dim in dimensions]
        yield _row(values + [_cell(row['cells'].get(col['id'])) for col in table.columns])


def _member_examples(table, members, totals):
    # A display name is paired with its own code in the actual inputs. Never
    # infer a dimension's name from a member or from another dimension's dictionary.
    dimensions = sorted(table.dimensions, key=lambda dim: (dim not in totals, dim == 'DATATIME', dim))
    for dim in dict.fromkeys([*dimensions, *sorted(totals)]):
        values = members[dim]
        keys = []
        if dim in totals:
            keys.append(BUDGET_TOTAL_MEMBER)
        detail = next((code for code in sorted(values) if code != BUDGET_TOTAL_MEMBER), None)
        if detail is not None:
            keys.append(detail)
        for code in keys:
            yield _row([table.labels.get(dim, dim), dim, code, values[code]])


def _table_material(table, role, preferred_groups, single_input_table):
    members = defaultdict(dict)
    properties = set()
    for fact in table.facts:
        properties.update(fact.get('propertyDimensions') or [])
        for dim, code in fact.get('inputMemberCodes', {}).items():
            if isinstance(code, str):
                name = (fact.get('dimensionNames') or {}).get(dim)
                if name or code not in members[dim]:
                    members[dim][code] = name or ('合计' if code == BUDGET_TOTAL_MEMBER else '')
    totals = {dim for dim, values in members.items() if dim not in properties and BUDGET_TOTAL_MEMBER in values}
    # Partition along one evidenced dimension only. With multiple subtotal axes
    # leave the full coordinates visible; one aggregate flag cannot describe them.
    mixed = [dim for dim in totals if dim in table.dimensions and len(members[dim]) > 1]
    axis = mixed[0] if len(mixed) == 1 and all(
        isinstance(f['inputMemberCodes'].get(mixed[0]), str) and f['inputMemberCodes'][mixed[0]]
        for f in table.facts) else None
    dimensions = sorted(table.dimensions, key=lambda dim: (dim != 'DATATIME', dim not in preferred_groups, dim))
    rows = sorted(table.rows, key=lambda row: tuple(str(row['dimensions'].get(dim) or '') for dim in dimensions))
    yield '## 查询结果'
    yield ''
    yield table.scope()
    if role:
        yield '资料用途为' + escape(role) + '。'
    periods = actual_periods(table.facts)
    yield ('实际数据时期为' + escape(periods) + '。' if periods else '') + f'本表共 {len(rows)} 行、{len(table.facts)} 个来源值。'
    yield ''
    yield '### Python 输入'
    yield ''
    yield '`dimensions` 保存维度标识与成员编码，用于筛选和分组。`dimensionNames` 保存同一标识对应的成员名称，用于展示。'
    examples = list(_member_examples(table, members, totals))
    if examples:
        yield ''
        yield _header(['维度名称', '维度标识', '成员编码', '成员名称'])
        yield from examples
    yield ''
    yield '按来源指标编码读取 `value`。数值字符串可用 `Decimal` 计算，来源空值保留为空。'
    yield ''
    yield _header(['指标', '输入字段', '来源指标编码'])
    for col in table.columns:
        yield _row([col['label'], col['inputField'], col['measureCode']])
    yield ''
    if totals:
        yield '预算维度的合计成员编码是字符串 `00000000`。同一口径的合计与所属明细分别统计。'
        yield ''
    if axis:
        label = escape(table.labels.get(axis, axis))
        subtotal = [r for r in rows if r['dimensions'].get(axis) == BUDGET_TOTAL_MEMBER]
        detail = [r for r in rows if r['dimensions'].get(axis) != BUDGET_TOTAL_MEMBER]
        count = sum(len(r['cells']) for r in subtotal)
        yield f'本表按{label}合计筛选可取得 {len(subtotal)} 行、{count} 个指标值。'
        if single_input_table:
            yield ''
            key = json.dumps(axis, ensure_ascii=False)
            yield (f'```python\ntotal_rows = [row for row in inputs '
                   f'if (row.get("dimensions") or {{}}).get({key}) == "{BUDGET_TOTAL_MEMBER}"]\n```')
        yield ''
        yield f'### {label}合计'
        yield ''
        yield from _data_rows(table, subtotal, [dim for dim in dimensions if dim != axis])
        yield ''
        yield f'### {label}明细（{len(detail)} 行）'
        yield ''
        yield from _data_rows(table, detail, dimensions)
    else:
        yield '### 查询明细'
        yield ''
        yield from _data_rows(table, rows, dimensions)
    if table.duplicates:
        yield ''
        yield '相同维度下的多个来源值已分别保留。'
    yield ''


def render_input_material(service, prepared, artifact, input_ref, max_chars):
    originals = {fact['factId']: fact for fact in artifact.get('facts', [])}
    receipts = {}
    groups = defaultdict(list)
    for record in prepared['records']:
        fact = originals.get(record['ref'], {})
        capture_ref = fact.get('captureRef') or next(iter(fact.get('captureRefs', [])), None)
        code_field = 'sourceMeasureCode' if record.get('sourceMeasureCode') else 'measureCode'
        if (fact.get('formulaArithmetic') != BUDGET_FORMULA or not capture_ref
                or not record.get(code_field) or not record.get('recordTable') or not record.get('dimensions')):
            continue
        if capture_ref not in receipts:
            capture = service.store.get_capture(service.owner, capture_ref)
            adapter = service.sources.for_capture(capture)
            receipts[capture_ref] = adapter.raw_receipt(capture['response']) if adapter.adapter_id == 'budget-v2' else {}
        if not receipts[capture_ref]:
            continue
        memberships = {(m.get('inputRole', ''), m.get('inputPurpose', '')) for m in record.get('inputMemberships', [])}
        roles = tuple(sorted(memberships or {(record.get('inputRole', ''), '')}))
        key = (capture_ref, record.get('bindingRef'), record['recordTable'], record.get('taskName'),
               record.get('formName'), record.get('executionPeriod'), roles)
        dims = {**(record.get('effectiveDimensions') or {}), **record['dimensions']}
        groups[key].append({**record, 'factId': record['ref'], 'dimensions': dims,
                            'inputMemberCodes': record['dimensions'], 'measureCode': record[code_field],
                            'inputField': code_field, 'cellExp': fact.get('expression') or fact.get('cellExp')})
    if not groups:
        return ''
    turn = getattr(service, 'turn', None)
    questions = list(dict.fromkeys(r['data']['question'] for r in receipts.values() if (r.get('data') or {}).get('question')))
    question = (turn.state.get('request') if turn else '') or ('\n'.join(questions))
    lines = ['# 预算分析输入', '', '## 用户要求', '']
    if question:
        lines.extend('> ' + escape(line) for line in str(question).splitlines())
    lines.extend(['', f'本次完整输入的 `inputSetRef` 为 `{input_ref}`。'])
    covered = sum(len(items) for items in groups.values())
    other = len(prepared['records']) + len(prepared['files']) - covered
    if other:
        lines.append(f'以下业务表覆盖 {covered} 条预算来源值。另有 {other} 项输入，可用 AnalysisInspect 的 inputFields 查看。')
    lines.append('')
    used = sum(len(line) + 1 for line in lines)
    more = f'正文仅展示部分输入。Python 使用完整冻结集合。可用 AnalysisInspect(section="inputFields", inputSetRef="{input_ref}", offset=…, limit=…) 分页读取。'
    for (capture_ref, _, _, task, form, period, roles), entries in groups.items():
        data = receipts[capture_ref].get('data') or {}
        labels = {**DEFAULT_LABELS, **{d['dimCode']: d['label'] for d in data.get('dimensionCatalog', [])
                                       if d.get('dimCode') and d.get('label')}}
        # Metric field aliases may differ between batches. Use their verified
        # source codes as columns; remove only source-bound header decorations.
        table = BusinessTable('', {'taskName': task, 'formName': form, 'period': period}, '', '', labels, False, entries)
        fields = defaultdict(dict)
        for row in table.rows:
            for column_id, cell in row['cells'].items():
                fields[column_id][cell['inputField']] = None
        for col in table.columns:
            col['inputField'] = ' / '.join(fields[col['id']])
        preferred = {c['dimCode'] for g in data.get('grouping', []) for c in g.get('candidates', [])
                     if c.get('supported') and c.get('kind') == 'DIMENSION' and c.get('dimCode')}
        role_names = {'PRIMARY': '主体', 'REFERENCE': '对照', 'STRUCTURE': '结构'}
        role = '、'.join(dict.fromkeys(role_names.get(r, r) + ('（公式构成）' if purpose == 'FORMULA' else '')
                                     for r, purpose in roles if r))
        for line in _table_material(table, role, preferred, len(groups) == 1 and not other):
            if used + len(line) + len(more) + 3 > max_chars:
                return '\n'.join([*lines, '', more])
            lines.append(line)
            used += len(line) + 1
    return '\n'.join(lines).rstrip()


def frozen_input_markdown(service, value, artifact=None):
    ref = value.get('inputSetRef')
    if not ref:
        return ''
    prepared = service.store.get_input_set(service.owner, ref)
    if not prepared['records'] or not prepared.get('analysisRef'):
        return ''
    if artifact is None:
        artifact = service.store.get_analysis(service.owner, prepared['analysisRef'])
    budget = service.review_budget
    return render_input_material(service, prepared, artifact, ref, budget.target_chars(budget.tool_result_target_tokens))
