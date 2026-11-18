"""Small, attributable observations for public progress, never raw prompts."""
from __future__ import annotations

import json
import math
import re

MAX_FACTS = 4
_SECRET = re.compile(r"password|secret|token|credential|authorization|email|phone|身份证|密码|手机号", re.I)
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return None
    if any(c in value for c in "\n\r<>\ufffd") or _SECRET.search(value):
        return None
    return value.strip()


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (str, int, float))
            and len(str(value)) < 40 and bool(_NUMBER.fullmatch(str(value)))
            and math.isfinite(float(value)))


def result_facts(receipt: dict, source_id: str) -> list[dict]:
    """Read only known data containers. Do not traverse arbitrary instructions.

    Row counts describe returned rows, never an entire population. Amounts
    require an explicit column unit. Keep the source's decimal string intact.
    Budget batch receipts and generic tabular tool responses share this shape.
    """
    facts: list[dict] = []

    def add(text, pointer, value=None, unit=None, scope=None):
        if len(facts) < MAX_FACTS and text not in [f['text'] for f in facts]:
            facts.append({'text': text, 'sourceId': source_id, 'pointer': pointer,
                          'value': value, 'unit': unit, 'scope': scope,
                          'state': 'observed'})

    def table(data, pointer):
        rows, columns = data.get('rows'), data.get('columns')
        count = data.get('rowCount')
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            add(f'本次返回 {count} 条结果', pointer + '/rowCount', count, '条')
        elif isinstance(rows, list):
            add(f'当前已读取 {len(rows)} 条结果', pointer + '/rows', len(rows), '条')
        detail = data.get('aggregationSummary', {}).get('detailRowCount') if isinstance(data.get('aggregationSummary'), dict) else None
        if isinstance(detail, int) and not isinstance(detail, bool) and detail >= 0:
            add(f'这批汇总结果的计算范围包含 {detail} 条明细', pointer + '/aggregationSummary/detailRowCount', detail, '条')
        if not isinstance(rows, list) or not isinstance(columns, list):
            return
        columns = columns[:80]
        metrics = [(i, c) for i, c in enumerate(columns) if isinstance(c, dict)
                   and _label(c.get('unit')) and _label(c.get('label') or c.get('name'))
                   and not _SECRET.search(str(c.get('name', '')))]
        dimensions = [(i, c) for i, c in enumerate(columns) if isinstance(c, dict)
                      and c.get('type') in ('STRING', 'string')
                      and not _SECRET.search(str(c.get('name', '')))]
        # Prefer display names over IDs/codes; no arbitrary free-text columns.
        named = [(i, c) for i, c in dimensions if str(c.get('name', '')).endswith('_NAME')]
        dimensions = (named or [(i, c) for i, c in dimensions if c.get('name') in ('label', 'name', 'category', 'period')])[:3]
        # A missing grouping label is a useful observation even when that row
        # comes last. Keep its exact amount and scope, never call it a total.
        def missing_group(row):
            return any(not _label(row.get(c.get('name')) if isinstance(row, dict)
                                  else row[i] if isinstance(row, list) and i < len(row) else None)
                       for i, c in dimensions)
        indices = list(range(min(len(rows), 256)))
        indices.sort(key=lambda index: not missing_group(rows[index]))
        for row_index in indices[:3]:
            row = rows[row_index]
            if not isinstance(row, (list, dict)):
                continue
            def cell(i, column):
                if isinstance(row, dict):
                    return row.get(column['name']) if isinstance(column.get('name'), str) else None
                return row[i] if i < len(row) else None
            labels = [_label(cell(i, c)) or f'未标注“{_label(c.get("label")) or "分组名称"}”'
                      for i, c in dimensions]
            scope = '、'.join(labels) or '未标注分组的记录'
            if not dimensions:
                scope = f'返回顺序第 {row_index + 1} 条记录'
            for i, column in metrics[:1]:
                value = cell(i, column)
                if _number(value):
                    unit = column['unit']
                    metric = column.get('label') or column['name']
                    fixed = data.get('fixedDimNameMap')
                    if len(metrics) == 1 and isinstance(fixed, dict) and _label(fixed.get('MD_BUDACCOUNT')):
                        metric = re.sub(r'^[一二三四五六七八九十]+[、.．]', '', fixed['MD_BUDACCOUNT'])
                    add(f'已返回的记录中，{scope}的“{metric}”为 {value} {unit}',
                        f'{pointer}/rows/{row_index}/{i if isinstance(row, list) else column["name"]}',
                        value, unit, scope)

    def visit(value, pointer='', depth=0):
        if not isinstance(value, dict) or depth > 6 or len(facts) >= MAX_FACTS:
            return
        if value.get('status') in ('FAILED', 'ERROR') or value.get('disposition') in ('FAILED', 'ERROR'):
            return
        # Batch envelopes may contain placeholder rows=[] while the actual
        # records are nested below. That is not a zero-row query result.
        envelope = any(isinstance(value.get(key), list) and value[key]
                       for key in ('stepResults', 'batchResults'))
        if not envelope and ('rows' in value or 'rowCount' in value):
            table(value, pointer)
        # Only navigate documented result containers, not requests or evidence.
        for key in ('data', 'receipt', 'result', 'tableContext'):
            if isinstance(value.get(key), dict):
                visit(value[key], pointer + '/' + key, depth + 1)
        for key in ('stepResults', 'batchResults'):
            if isinstance(value.get(key), list):
                for index, child in enumerate(value[key][:12]):
                    # Entity-set discovery is not a business result table.
                    if isinstance(child, dict) and str(child.get('resultType', '')).startswith('ENTITY_SET'):
                        continue
                    visit(child, f'{pointer}/{key}/{index}', depth + 1)

    visit(receipt)
    return facts


def facts_from_output(text: str, metadata: dict, tool_call_id: str) -> list[dict]:
    """Prefer bounded host observations, collected before result offloading."""
    public = metadata.get('public_progress')
    if isinstance(public, dict) and public.get('version') == 1:
        values = public.get('facts', [])
        if isinstance(values, list):
            return [dict(item, toolCallId=tool_call_id) for item in values[:MAX_FACTS]
                    if isinstance(item, dict) and isinstance(item.get('text'), str)
                    and len(item['text']) <= 400 and isinstance(item.get('sourceId'), str)
                    and item.get('state') in ('observed', 'calculated', 'verified')]
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, dict):
        return []
    source_id = metadata.get('analysisCaptureRef') or tool_call_id
    return [dict(fact, toolCallId=tool_call_id) for fact in result_facts(value, source_id)]
