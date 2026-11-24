"""Business coordinates for correcting citations in an already completed review."""
import re

from .business_tables import escape, measure_label, member_label
from .review_business_evidence import review_runs, source_facts


def numbered_locations(request):
    def key(label):
        match = re.fullmatch(r'表(\d+)第(\d+)行', label)
        if match:
            return (0, int(match[1]), int(match[2]))
        match = re.fullmatch(r'(结果|Python脚本)(\d+)', label)
        return (1 if match and match[1] == '结果' else 2, int(match[2]) if match else 0, label)
    return sorted(request.wire_references, key=key)


def reference_index(request, locations):
    """Use the exact identities advertised by the frozen review, including older packets."""
    material = request.material
    evidence = material.get('evidence') or {}
    facts = list(material.get('facts') or [])
    for source in evidence.get('sources', []):
        facts.extend(source_facts(source))
    runs = review_runs(material)
    for run in runs:
        facts.extend(run.get('inputRecords') or [])
    rows = {ref: fact for fact in reversed(facts) if isinstance(fact, dict)
            for ref in [fact.get('factId') or fact.get('ref') or fact.get('key')] if ref}
    outputs = material.get('derived') or evidence.get('finalResults') or [
        out for run in runs for out in run.get('outputs', [])]
    descriptions = {entry[key]: entry.get('label') or entry.get('purpose') or entry.get('operator') or entry.get('title')
                    for key, entries in [('derivedId', outputs if isinstance(outputs, list) else [outputs]), ('runRef', runs)]
                    for entry in entries if isinstance(entry, dict) and key in entry}
    tables, lines = {}, ['## 可引用的位置']
    for number, location in enumerate(locations, 1):
        ref = request.wire_references[location]
        match = re.fullmatch(r'(表\d+)第(\d+)行', location)
        if match and ref in rows:
            tables.setdefault(match[1], []).append((number, int(match[2]), rows[ref]))
        elif not match and descriptions.get(ref):
            lines.append(f'- 引用号 {number}　{location}　{escape(descriptions[ref])}')
        else:
            # Older saved material can lack structured rows or a script title.
            # In that case keep the original business context available.
            return request.markdown + '\n\n## 可选引用号\n' + '\n'.join(
                f'{i}　{label}' for i, label in enumerate(locations, 1))
    for name, records in tables.items():
        first = records[0][2]
        lines.extend(['', '### ' + name + '　' + escape(first.get('formName') or first.get('recordTable') or '')])
        keys = list(dict.fromkeys(key for _, _, row in records for key in (row.get('dimensions') or {})))
        def dimension(row, key):
            code = (row.get('dimensions') or {}).get(key)
            label = member_label(row, key)
            return f'{label}（{code}）' if code is not None and str(code) != label else label
        fixed = {key for key in keys if all(dimension(row, key) == dimension(first, key) for _, _, row in records)}
        if fixed:
            lines.append('共同条件　' + '、'.join(key + '＝' + escape(dimension(first, key)) for key in keys if key in fixed))
        changing = [key for key in keys if key not in fixed]
        # A coordinate can have several observations, even for the same measure.
        # Every cell retains its own original row number and amount.
        columns, grouped = {}, {}
        for number, local_row, row in records:
            headers = row.get('headers') or []
            title = headers[-1] if headers else measure_label(row) or row.get('label') or row.get('measureCode') or ''
            column = (title, row.get('measureCode'), row.get('sourceMeasureCode'), row.get('unit'))
            columns[column] = None
            coordinate = tuple(dimension(row, key) for key in changing)
            values = grouped.setdefault(coordinate, {}).setdefault(column, [])
            amount = row.get('value')
            values.append(f'#{number} ' + escape(amount if amount is not None else '空值'))
        if 1 < len(columns) <= 12 and len(grouped) * 2 <= len(records):
            def column_title(column):
                title, metric, source_metric, unit = column
                if sum(other[0] == title for other in columns) > 1:
                    title += ' ' + '/'.join(str(code) for code in (metric, source_metric) if code)
                return escape(title) + ('（' + escape(unit) + '）' if unit else '')
            lines += ['每格列出可填写的引用号（#）和来源值。', '',
                      '| ' + ' | '.join([*changing, *map(column_title, columns)]) + ' |',
                      '| ' + ' | '.join(['---'] * (len(changing) + len(columns))) + ' |']
            for coords, cells in grouped.items():
                lines.append('| ' + ' | '.join([*map(escape, coords),
                                                *['、'.join(cells.get(key, [])) for key in columns]]) + ' |')
            continue
        lines += ['', '| ' + ' | '.join(['引用号', '原表行号', *changing, '指标或科目', '来源值', '单位']) + ' |',
                  '| ' + ' | '.join(['---'] * (len(changing) + 5)) + ' |']
        for number, local_row, row in records:
            title = measure_label(row) or row.get('label') or row.get('measureCode') or ''
            value = row.get('value')
            fields = [number, local_row, *[dimension(row, key) for key in changing], title,
                      value if value is not None else '空值', row.get('unit') or '']
            lines.append('| ' + ' | '.join(escape(v) for v in fields) + ' |')
    return '\n'.join(lines) if request.wire_references else request.markdown
