"""Deterministic business tables over immutable facts; never an aggregation engine."""
from __future__ import annotations

import base64
import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from .models import AnalysisError, digest, json_values
from .budget_dimensions import dimension_labels, loading_names_for_empty_results, source_models

BUDGET_TOTAL_MEMBER = '00000000'
HIERARCHY_SCOPE_GUIDANCE = '明细范围按本次执行的层级和返回成员描述。层级起点用于定位，只有其维值出现在明细成员中时才计入。'
STATES = {'NULL': '空值', 'NO_VALUE': '空值', 'NO_DATA': '无数据', 'NOT_LOADED': '未取得',
          'NOT_QUERIED': '未查询', 'CONFLICT': '来源冲突', 'ERROR': '来源错误', 'NOT_APPLICABLE': '不适用'}


def encode(value):
    return json.dumps(json_values(value), ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def escape(value):
    return text(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\\', '\\\\').replace('|', '\\|').replace('\r\n', '<br>').replace('\n', '<br>').replace('\r', '<br>')


def cell_text(fact, *, grouped=False):
    if fact is None:
        return '未返回'
    if fact.get('valueState') == 'PRESENT':
        value = text(fact.get('value'))
        if grouped:
            from .formatting import NUMERIC_TYPES, grouped_value
            if fact.get('valueType', 'DECIMAL_STRING') in NUMERIC_TYPES:
                return grouped_value(value)
        return value
    return STATES.get(fact.get('valueState'), '状态未确认')


def period_label(code, supplied=None):
    match = re.fullmatch(r'(\d{4})([NJYH])(\d{4})', text(code))
    if match:
        year, kind, number = match.groups(); number = int(number)
        if kind == 'N' and number == 1:
            return year + '年'
        if kind == 'J' and 1 <= number <= 4:
            return f'{year}年第{number}季度'
        if kind == 'Y' and 1 <= number <= 12:
            return f'{year}年{number}月'
        if kind == 'H' and 1 <= number <= 2:
            return year + ('年上半年' if number == 1 else '年下半年')
    return text(supplied or code)


def member_label(fact, dimension):
    code = fact.get('dimensions', {}).get(dimension)
    label = fact.get('dimensionNames', {}).get(dimension)
    if dimension == 'DATATIME':
        return period_label(code, label)
    return text(label or ('合计' if code == BUDGET_TOTAL_MEMBER else code))


def actual_headers(cell, fallback_period=None):
    period = cell.get('dimensions', {}).get('DATATIME') or fallback_period
    bound = '{' + text(cell['periodExpression']).replace(' ', '') + '}' if cell.get('periodExpression') else None
    return [period_label(period) if period and (text(h).strip().replace(' ', '') == bound or h == '[DATATIME]') else h
            for h in cell.get('headers', [])]


def measure_label(fact):
    if fact.get('measureTitle'):
        return fact['measureTitle']
    # Only strip whole, proven member labels. The dimensional identity below is
    # never inferred from these labels or from splitting this display title.
    names = {text(v).strip() for v in fact.get('dimensionNames', {}).values()}
    names.update(member_label(fact, key).strip() for key in fact.get('dimensions', {}))
    title = text(fact.get('title') or '_'.join(fact.get('headers', [])))
    parts = [p for p in title.split('_') if p.strip() and p.strip() not in names]
    # Legacy saved facts may still contain a period header. Remove only the
    # expression explicitly present in this cell's source address, not any {}.
    expression = text(fact.get('cellExp'))
    parts = [p for p in parts if not (p.startswith('{') and p.endswith('}') and p[1:-1]
             and re.fullmatch(r'[+\-]?\d+[NJYH](?:,[+\-]?\d+[NJYH])*', p[1:-1])
             and (',' + p[1:-1] + ',') in expression)]
    return '_'.join(parts) or text(fact.get('measureCode')) or '数值'


def source_data_period(fact):
    return ((fact.get('dimensions') or {}).get('DATATIME') or fact.get('dataPeriod') or fact.get('selectionDataPeriod')
            or (fact.get('period') if fact.get('period') != fact.get('executionPeriod') else None))


def actual_periods(facts):
    groups = OrderedDict()
    other = []
    for fact in facts:
        code = source_data_period(fact)
        if not code:
            continue
        match = re.fullmatch(r'(\d{4})([NJYH])(\d{4})', str(code))
        if not match:
            other.append(period_label(code, fact.get('dimensionNames', {}).get('DATATIME')))
            continue
        year, kind, n = match.groups()
        groups.setdefault((year, kind), set()).add(int(n))
    labels = []
    for (year, kind), values in sorted(groups.items()):
        ns = sorted(values)
        if kind in ('J', 'Y') and len(ns) > 1 and ns == list(range(ns[0], ns[-1] + 1)):
            labels.append(f'{year}年第{ns[0]}—{ns[-1]}季度' if kind == 'J' else f'{year}年{ns[0]}—{ns[-1]}月')
        else:
            labels.extend(period_label(f'{year}{kind}{n:04}') for n in ns)
    return '、'.join(dict.fromkeys(labels + other))


def data_periods(table):
    observed = actual_periods(table.facts)
    if observed:
        return observed
    if not table.facts:
        selected = table.context.get('dataPeriodSelection') or {}
        codes = [code for group in selected.get('groups', []) for code in group.get('selectedPeriods', [])]
        observed = actual_periods({'dataPeriod': code} for code in codes or table.context.get('queriedDataPeriods', ()))
    return observed or ('未返回' if table.context.get('period') or table.context.get('dataPeriodSelection') else '')


def _empty_filter_periods(data, target, result):
    """A filter may return zero rows after reading a particular metric at confirmed periods."""
    target_id = target.get('logicalTargetId') or target.get('id')
    columns = {b['batchId']: b['logicalTargetColumns'][target_id] for b in (data.get('valueFilter') or {}).get('batches', [])
               if b.get('batchId') and b.get('status') == 'SUCCEEDED' and target_id in (b.get('logicalTargetColumns') or {})}
    context = result.get('context') or {}
    if not columns or not context.get('baseContext') or not context.get('target'):
        return []
    periods = []
    for step in data.get('stepResults') or []:
        for batch in step.get('batchResults') or []:
            column = columns.get(batch.get('batchId'))
            if not column or batch.get('disposition') != 'SUCCEEDED':
                continue
            resolved = {v for axis in (batch.get('data') or {}).get('resolvedPeriods', [])
                        if axis.get('dimCode') == 'DATATIME' for v in axis.get('values', [])}
            for metric in batch.get('metrics') or []:
                origin = metric.get('context') or {}
                code = (metric.get('selectedDimensions') or {}).get('DATATIME')
                if (metric.get('name') == column and code in resolved and all(origin.get(k) == context.get(k)
                        for k in ('baseContext', 'target', 'period', 'requestedDimensions', 'effectiveDimensions'))):
                    periods.append(code)
    return list(dict.fromkeys(periods))


def result_metric(result, target):
    """Use role-bound business members and matched title terms for this particular result set."""
    from .budget_dimensions import SYSTEM_DIMENSIONS
    from .formula_labels import short_caption
    labels = []
    for match in result.get('resolvedTerms') or []:
        role = match.get('dimensionCode')
        if role and role not in SYSTEM_DIMENSIONS:
            label = match.get('label') or match.get('term')
            if label:
                labels.append(short_caption(label))
    labels.extend(t for t in result.get('resolvedTitleTerms') or [] if isinstance(t, str) and t.strip())
    return ' / '.join(dict.fromkeys(labels)) or target.get('label') or result.get('label') or ''


@dataclass
class BusinessTable:
    ref: str
    context: dict
    set_id: str
    interpretation: str
    labels: dict
    scenario: bool
    facts: list
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    fixed: dict = field(default_factory=dict)
    dimensions: list = field(default_factory=list)
    locations: dict = field(default_factory=dict)
    duplicates: bool = False
    row_index: dict = field(default_factory=dict)
    single_value_target: str | None = None
    result_index: int = 0

    def __post_init__(self):
        if not self.facts:
            self.fixed = dict(self.context.get('effectiveDimensions') or {})
            return
        dimensions = sorted({k for fact in self.facts for k in fact.get('dimensions', {})})
        self.fixed = {k: self.facts[0]['dimensions'][k] for k in dimensions
                      if k in self.facts[0].get('dimensions', {}) and
                      all(k in f.get('dimensions', {}) and f['dimensions'][k] == self.facts[0]['dimensions'][k] for f in self.facts)}
        self.dimensions = [k for k in dimensions if k not in self.fixed and not (self.scenario and k == 'MD_SCENE')]
        column_keys, row_keys = {}, {}
        for fact in self.facts:
            dims = fact.get('dimensions', {})
            label = member_label(fact, 'MD_SCENE') or '情景未确认' if self.scenario else measure_label(fact)
            key = (text(fact.get('measureCode')), dims.get('MD_SCENE') if self.scenario else None,
                   None if self.scenario else label, fact.get('unit'), fact.get('valueType'))
            if key not in column_keys:
                col = {'id': 'C' + str(len(self.columns) + 1), 'label': label, 'unit': fact.get('unit'),
                       'measureCode': fact.get('measureCode'), 'valueType': fact.get('valueType', 'DECIMAL_STRING'),
                       'scenario': dims.get('MD_SCENE') if self.scenario else None}
                column_keys[key] = col; self.columns.append(col)
            col = column_keys[key]
            # Scenario identity is the member code. A dependency can lack a
            # translated label while the target carries its source-bound name.
            if self.scenario and col['label'] == text(col['scenario']) and label != text(col['scenario']):
                col['label'] = label
            row_key = tuple((k, k in dims, dims.get(k)) for k in dimensions if not (self.scenario and k == 'MD_SCENE'))
            choices = row_keys.setdefault(row_key, [])
            row = next((r for r in choices if col['id'] not in r['cells']), None)
            if row is None:
                if choices:
                    self.duplicates = True
                row = {'id': 'R' + str(len(self.rows) + 1), 'dimensions': {k: dims.get(k) for k in self.dimensions},
                       'labels': {k: member_label(fact, k) for k in self.dimensions}, 'cells': {}}
                choices.append(row); self.rows.append(row)
            row['cells'][col['id']] = fact
            self.locations.setdefault(fact['factId'], []).append({'tableRef': self.ref, 'rowRef': row['id'], 'columnRef': col['id']})
        # Duplicate display names do not identify columns. Distinguish them in
        # the readable header while preserving the actual code as its identity.
        counts = Counter(c['label'] for c in self.columns)
        ordinals = Counter()
        for col in self.columns:
            if counts[col['label']] > 1:
                ordinals[col['label']] += 1
                col['publicLabel'] = f"{col['label']}（来源列{ordinals[col['label']]}）"
                col['label'] += '（' + text(col['scenario'] or col['measureCode']) + '，' + col['id'] + '）'
        self.row_index = {row['id']: row for row in self.rows}
        for key in self.dimensions:
            aliases = {}
            for row in self.rows:
                members = aliases.setdefault(row['labels'][key], {})
                members.setdefault(row['dimensions'][key], len(members) + 1)
            for row in self.rows:
                if len(aliases[row['labels'][key]]) > 1:
                    row.setdefault('displayLabels', {})[key] = row['labels'][key] + '（' + text(row['dimensions'][key]) + '）'
                    name = row['labels'][key]
                    row.setdefault('publicLabels', {})[key] = f"{name}（同名成员{aliases[name][row['dimensions'][key]]}）"

    def column_label(self, col, label=None):
        kind = {'STRING': '文本', 'BOOLEAN': '布尔', 'DATE': '日期', 'DATETIME': '日期时间'}.get(col.get('valueType'))
        return (label or col['label']) + (f'（{kind}）' if kind else f"（{col['unit']}）" if col.get('unit') else '')

    def scope(self):
        target = self.context.get('target', {})
        scope = [self.context.get('taskName') or target.get('taskCode'), self.context.get('formName') or target.get('formCode')]
        opening = self.context.get('period') or self.context.get('executionPeriod') or (self.fixed.get('DATATIME') if not self.facts else None)
        if opening:
            scope.append('数据加载时期：' + period_label(opening))
        for key in sorted(self.fixed):
            if (self.scenario and key == 'MD_SCENE') or (not self.facts and key == 'DATATIME'):
                continue
            anchor = self.facts[0] if self.facts else {'dimensions': self.fixed,
                'dimensionNames': self.context.get('dimensionNames', {})}
            label = self.labels.get(key, key)
            if not self.facts:
                label = f'数据加载条件（{label}）'
            scope.append(label + '：' + member_label(anchor, key))
        return '；'.join(escape(v) for v in scope if v)

    def coverage(self):
        counts = []
        for key in self.dimensions:
            if key == 'DATATIME':
                continue
            members = {f.get('dimensions', {}).get(key) for f in self.facts if key in f.get('dimensions', {})}
            counts.append(f"{len(members)}个{self.labels.get(key, key)}" + ('（含合计）' if BUDGET_TOTAL_MEMBER in members else ''))
        numeric = sum(f.get('valueState') == 'PRESENT' and f.get('valueType') == 'DECIMAL_STRING' for f in self.facts)
        counts.append(f'{len(self.rows)}行、{len(self.columns)}个数值/值列、{len(self.facts)}条来源事实、{numeric}个有效数值')
        return '；'.join(counts)

    def period_selection(self):
        groups = self.context.get('dataPeriodSelection', {}).get('groups', [])
        fact_ids = {f['factId'] for f in self.facts}
        summaries = OrderedDict()
        for group in groups:
            if not fact_ids.intersection(group.get('selectedFactIds', [])):
                continue
            key = (group.get('requestedPeriod'), tuple(group.get('selectedPeriods', [])),
                   tuple(group.get('missingValuePeriods', [])), tuple(group.get('skippedEmptyPeriods', [])), bool(group.get('fallback')))
            summaries[key] = summaries.get(key, 0) + 1
        lines = []
        for (requested, selected, missing, skipped, fallback), count in summaries.items():
            if not (fallback or missing or skipped):
                continue
            label = lambda codes: actual_periods([{'dataPeriod': code} for code in codes])
            line = f'时期取值（{count}个业务组）：所问{period_label(requested)}；取用{label(selected) or "未取得"}'
            if fallback: line += '；按来源规则下探'
            if missing: line += '；缺值时期：' + label(missing)
            if skipped: line += '；跳过空值时期：' + label(skipped)
            lines.append(line)
        return lines

    def select_rows(self, filters=None, *, strict=False):
        if filters is None:
            return self.rows
        if not isinstance(filters, list) or len(filters) > 20:
            raise AnalysisError('INVALID_TABLE_FILTER', 'filters 须为不超过20项的列表。')
        from .member_filters import resolve
        terms = []
        domains = {k: {} for k in self.dimensions}
        for row in self.rows:
            for k in self.dimensions:
                domains[k].setdefault(row['dimensions'][k], set()).add(row['labels'][k])
        for cond in filters:
            if not isinstance(cond, dict) or set(cond) - {'column', 'operator', 'value'} or cond.get('operator') not in ('EQ', 'IN'):
                raise AnalysisError('INVALID_TABLE_FILTER', '结果内维度过滤使用 column、operator=EQ/IN、value。')
            if not isinstance(cond.get('column'), str):
                raise AnalysisError('INVALID_TABLE_FILTER', '过滤列必须是这张表的变化维度。')
            vals = cond.get('value') if cond['operator'] == 'IN' else [cond.get('value')]
            if not isinstance(vals, list) or not vals or len(vals) > 100 or any(not isinstance(v, str) for v in vals):
                raise AnalysisError('INVALID_TABLE_FILTER', '过滤值须为非空字符串或字符串列表。')
            terms.append((cond['column'], vals))
        predicates = resolve(set(self.dimensions), domains, {k: {self.labels.get(k, k)} for k in self.dimensions},
                             terms, strict=strict)
        return [row for row in self.rows if all(row['dimensions'][k] in vs for k, vs in predicates)]

    def select_columns(self, columns=None):
        if columns is None:
            return self.columns
        if (not isinstance(columns, list) or not columns or len(columns) > 100
                or any(not isinstance(c, str) for c in columns) or len(set(columns)) != len(columns)):
            raise AnalysisError('INVALID_TABLE_COLUMNS', 'columns 使用无重复的列标识或完整列名。')
        selected = []
        for name in columns:
            matches = [c for c in self.columns if name in (c['id'], c['label'], self.column_label(c)) or
                       ('sourceLabel' in c and name in (c['sourceLabel'], self.column_label(c, c['sourceLabel'])))]
            if len(matches) != 1:
                raise AnalysisError('INVALID_TABLE_COLUMNS', '列未找到或列名不唯一，请使用返回的 C 列标识。')
            if matches[0] not in selected:
                selected.append(matches[0])
        return selected

    def markdown(self, rows=None, columns=None, with_scope=True):
        rows = self.rows if rows is None else rows
        columns = self.columns if columns is None else columns
        lines = [f'**{self.ref}**']
        if with_scope:
            lines.extend([self.scope(), '实际数据时期：' + (actual_periods(self.facts) or '未确认'), '原表覆盖：' + self.coverage(), f'本次展示：{len(rows)}行。'])
            lines.extend(self.period_selection())
        source_titles = any(f.get('sourceTitle') for f in self.facts)
        headings = ['行号'] + (['来源标题'] if source_titles else []) + [self.labels.get(k, k) for k in self.dimensions] + [self.column_label(c) for c in columns]
        lines.extend(['', '| ' + ' | '.join(escape(h) for h in headings) + ' |', '| ' + ' | '.join('---' for _ in headings) + ' |'])
        for row in rows:
            titles = ' / '.join(dict.fromkeys(f['sourceTitle'] for f in row['cells'].values() if f.get('sourceTitle')))
            values = [row['id']] + ([titles] if source_titles else []) + [row.get('displayLabels', {}).get(k, row['labels'][k]) for k in self.dimensions]
            for col in columns:
                fact = row['cells'].get(col['id'])
                values.append(cell_text(fact))
            lines.append('| ' + ' | '.join(escape(v) for v in values) + ' |')
        if self.duplicates:
            lines.append('相同维度存在多个来源事实，已分别保留行，未合并或加总。')
        return '\n'.join(lines)


def scenario_measure(fact):
    return (fact.get('sourceMeasureCode') or fact.get('measureCode')) == 'ZB_JE2'


def build_tables(receipt):
    from .single_value_presentation import prepare_single_values

    data = receipt.get('data', {})
    catalog_rows = data.get('dimensionCatalog') or []
    def table_labels(context, facts=()):
        models = source_models(facts) or ([context['modelCode']] if context.get('modelCode') else [])
        labels = dimension_labels(catalog_rows, models=models, context=context)
        labels['DATATIME'] = '实际时期'
        return labels
    tables = []
    results = data.get('resultSets', [])
    contexts = (loading_names_for_empty_results(results) if any(not result.get('rows') for result in results)
                else (result.get('context', {}) for result in results))
    for result_index, (result, context) in enumerate(zip(results, contexts)):
        groups = OrderedDict()
        if not result.get('rows'):
            target = next((t for t in data.get('targets', []) if t.get('id') == result.get('targetId')), {})
            if periods := _empty_filter_periods(data, target, result):
                context = {**context, 'queriedDataPeriods': periods}
            table = BusinessTable('T' + str(len(tables) + 1), context,
                                 result.get('resultSetId', ''), result.get('interpretationId', ''), table_labels(context), False, [],
                                 result_index=result_index)
            table.columns = [{'id': 'C1', 'label': result_metric(result, target) or '查询值',
                              'unit': result.get('unit'), 'valueType': result.get('valueType')}]
            tables.append(table)
            continue
        for fact in result.get('rows', []):
            if not isinstance(fact.get('factId'), str):
                raise AnalysisError('INVALID_BUSINESS_FACT', '业务事实缺少稳定标识。')
            model = text(fact.get('cellExp')).split('[', 1)[0]
            key = (model, scenario_measure(fact))
            groups.setdefault(key, []).append(fact)
        for (_, special), facts in groups.items():
            tables.append(BusinessTable('T' + str(len(tables) + 1), context,
                          result.get('resultSetId', ''), result.get('interpretationId', ''), table_labels(context, facts), special, facts,
                          result_index=result_index))
    prepare_single_values(data, tables)
    return tables


def catalog(tables):
    return [{'tableRef': t.ref, 'rowCount': len(t.rows), 'factCount': len(t.facts),
             'columns': [{'id': c['id'], 'label': t.column_label(c)} for c in t.columns]} for t in tables]


def ranked_sources(data):
    presentation = data.get('presentation') or {}
    sets = data.get('resultSets') or []
    if (len(sets) < 2 or presentation.get('verified') is not True
            or presentation.get('scope') != 'EACH_RESULT_SET' or presentation.get('valueOrder') not in ('ASC', 'DESC')):
        return ''
    labels = []
    for result in sets:
        context = result.get('context') or {}
        source = context.get('formName') or (context.get('target') or {}).get('formCode') or result.get('resultSetId')
        labels.append(f'{escape(source)} {len(result.get("rows", []))} 条')
    order = '升序' if presentation['valueOrder'] == 'ASC' else '降序'
    return f'逐来源{order}的记录数：' + '、'.join(labels) + '。'


def state_markdown(receipt, *, aligned=False):
    from .query_empty_result import business_outcome
    from .formatting import grouped_value
    data = receipt.get('data', {})
    outcome = business_outcome(data)
    if outcome == 'NO_MATCH':
        lines = ['未定位到符合本次条件的运行单元格。']
    elif outcome == 'NO_DATA' and data.get('complete'):
        lines = ['本次查询没有数据。']
    elif data.get('complete') and receipt.get('status') == 'SUCCEEDED':
        lines = ['预算查询已完成。']
    else:
        lines = ['本次查询尚未完整完成。']
    if data.get('question'):
        lines.append('查询问题：' + escape(data['question']))
    from .aggregation_explanation import receipt_markdown
    lines.extend(receipt_markdown(receipt))
    filtering = data.get('valueFilter')
    if filtering and filtering.get('basis') in ('BUDGET_NATIVE_RESULT_WHERE', 'BUDGET_NATIVE_FILTER'):
        targets = {t.get('logicalTargetId', t.get('id')): t.get('label', t.get('id')) for t in data.get('targets', [])}
        def label(condition):
            op = condition.get('operator')
            if op in ('AND', 'OR'):
                return '（' + (' 且 ' if op == 'AND' else ' 或 ').join(label(child) for child in condition.get('children', [])) + '）'
            if op == 'NOT':
                return '非（' + label(condition['children'][0]) + '）'
            target = condition.get('targetId') or (next(iter(targets)) if len(targets) == 1 else '')
            sign = {'LT': '<', 'LE': '≤', 'GT': '>', 'GE': '≥', 'EQ': '=', 'NE': '≠', 'IS_NULL': '为空', 'IS_NOT_NULL': '非空'}.get(op, op)
            return text(targets.get(target, target)) + ' ' + sign + ' ' + grouped_value(text(condition.get('value')))
        lines.append('数值筛选：' + escape(label(filtering.get('condition', {}))) + '。由预算服务按同一实体和数据粒度执行；空值不按零处理。')
        for index, batch in enumerate(filtering.get('batches', []), 1):
            if batch.get('filterStage') == 'HAVING':
                counts = batch.get('aggregationSummary') or {}
                fields = [('leafTargetCombinationCount', '末级目标组合'), ('detailRowCount', '参与汇总明细'),
                          ('groupCountBeforeHaving', '筛选前分组'), ('filteredOutGroupCount', '过滤分组')]
            else:
                counts = batch.get('rangeSummary') or {}
                fields = [('targetCombinationCount', '范围目标组合'), ('alignedRowCount', '筛选前对齐记录'),
                          ('matchedTargetCount', '命中目标组合'), ('filteredOutRowCount', '过滤记录')]
            summary = '；'.join(f'{name}：{grouped_value(counts[key])}' for key, name in fields if key in counts)
            if summary:
                lines.append(f'筛选口径 {index}：{summary}。')
    elif filtering:
        comparison = {'LT': '<', 'LE': '≤', 'GT': '>', 'GE': '≥', 'EQ': '=', 'NE': '≠'}.get(filtering.get('operator'), '?')
        lines.append(f"数值筛选：{comparison} {escape(grouped_value(text(filtering.get('value'))))}；已核对 {grouped_value(filtering.get('populationCount', 0))} 项来源值，"
                     f"命中 {grouped_value(filtering.get('matchedCount', 0))} 项；总体{'已完整确认' if filtering.get('populationComplete') else '尚未完整确认'}。空值不按零处理。")
    failures = data.get('unresolvedGroups', [])
    if receipt.get('error'):
        failures = [receipt['error']] + failures
    if failures:
        lines.append(f'未解决事项：{len(failures)}项。')
        for item in failures:
            lines.append('- ' + escape('；'.join(text(item.get(k)) for k in ('taskCode', 'formCode', 'code', 'message') if item.get(k))))
    if data.get('failureSummary', {}).get('issueCount') and not failures:
        lines.append('异常摘要：' + escape(encode(data['failureSummary'])))
    ranking = ranked_sources(data)
    if ranking:
        lines.append(ranking)
    elif not aligned and len({r.get('interpretationId') for r in data.get('resultSets', [])}) > 1:
        lines.append('以下按各自的来源口径分别列示。')
    substituted = {fact for result in data.get('resultSets', [])
                   for fact in (result.get('analysis') or {}).get('scenarioFallbackFactIds', [])}
    if substituted:
        lines.append(f'其中{len(substituted)}项通过对应预算格定位，按表格标注的目标情景取数。')
        if any(fact.get('factId') in substituted and fact.get('valueState') == 'PRESENT'
               and fact.get('value') is not None for result in data.get('resultSets', []) for fact in result.get('rows', [])):
            lines.append('这些项目没有目标情景运行格及其公式。已返回的数值可用于对比。')
    return '\n'.join(lines)


def _cursor(capture_ref, table, row, column, selection):
    value = {'capture': capture_ref, 'table': table, 'row': row, 'column': column, 'selection': selection}
    return base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip('=')


def business_view(receipt, capture_ref, *, max_chars=24000, tableRef=None, columns=None, filters=None, cursor=None, limit=None):
    from .single_value_presentation import single_value_markdown, source_name

    tables = build_tables(receipt)
    if tableRef is not None:
        selected = [t for t in tables if t.ref == tableRef]
        if not selected:
            raise AnalysisError('TABLE_NOT_FOUND', '该保存结果不存在指定业务表。')
    else:
        if columns is not None or filters is not None:
            raise AnalysisError('TABLE_REQUIRED', '选择列或过滤时须指定 tableRef。')
        selected = tables
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise AnalysisError('INVALID_QUERY_PAGE', '业务表 limit 须为1—1000。')
    selection = digest({'tableRef': tableRef, 'columns': columns, 'filters': filters})
    start, offset, column_offset = 0, 0, 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            pos = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
            if pos['capture'] != capture_ref or pos['selection'] != selection:
                raise ValueError()
            start, offset, column_offset = pos['table'], pos['row'], pos['column']
            if any(type(v) is not int or v < 0 for v in (start, offset, column_offset)) or start >= len(selected):
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise AnalysisError('INVALID_TABLE_CURSOR', '游标须原样使用且属于同一结果和筛选条件。') from exc
    data = receipt.get('data', {})
    single_value = bool(tables) and tables[0].single_value_target is not None
    result = {'viewType': 'business_tables', 'captureRef': capture_ref, 'purpose': data.get('purpose'),
              'status': receipt.get('status'), 'complete': bool(data.get('complete')), 'allRowsIncluded': False,
              'markdown': state_markdown(receipt), 'tableCount': len(tables), 'tables': [],
              'readTool': 'QueryResultRead'}
    result['note'] = '表格只调整排布，未加总。行号及C列标识在同一captureRef内稳定；计算可用Calculate的valueRefs引用。执行覆盖与展示分页分别判断。'
    if single_value:
        result['note'] = '单值答复沿用“项目／内容”条件表，按业务表列出原值和单位。计算引用见读取入口中的 tableRef、rowRef 和列 id。'
    if ranked_sources(data):
        result['note'] += '按用户指定的表样与名次范围交付；要求全部排序时，逐来源列全成员和对应数值，不能用另一个口径的一条例子代替它的清单。'
    if data.get('grouping') and len({t.set_id for t in tables}) > 1:
        result['note'] += '当前分组查询有多个结果集。承接此前多份数据且用户没有缩小范围时，在 subjectSources 中同时采用相关来源，按口径分别解释；用户点名单一来源时按其选择。'
    if any((scope.get('condition') or {}).get('hierarchy') for scope in data.get('scopeResolution', [])):
        result['note'] += HIERARCHY_SCOPE_GUIDANCE
    # Large diagnostics retain a precise bounded read; they never turn complete.
    if len(result['markdown']) > max_chars // 3:
        result['markdown'] = result['markdown'][:max_chars // 3] + '\n异常详情未完整展示，请沿 diagnosticsRead 读取。'
        result['diagnosticsRead'] = {'captureRef': capture_ref, 'pointer': '/data/unresolvedGroups', 'limit': 20}
    state = result['markdown']
    def next_read(index, row, col):
        return {'captureRef': capture_ref, **({'tableRef': tableRef} if tableRef else {}),
                **({'columns': columns} if columns is not None else {}), **({'filters': filters} if filters is not None else {}),
                **({'limit': limit} if limit else {}), 'cursor': _cursor(capture_ref, index, row, col, selection)}
    for index in range(start, len(selected)):
        table = selected[index]
        rows, cols = table.select_rows(filters), table.select_columns(columns)
        begin = offset if index == start else 0
        cstart = column_offset if index == start else 0
        if begin > len(rows) or cstart >= len(cols):
            raise AnalysisError('INVALID_TABLE_CURSOR', '游标超出实际表格范围。')
        entry = {'tableRef': table.ref, 'rowCount': len(table.rows), 'factCount': len(table.facts), 'selectedRowCount': len(rows)}
        if single_value:
            entry['sourceName'] = source_name(table)
            if table.rows:
                entry['rowRef'] = table.rows[0]['id']
        # Binary-search complete rows. Reserve space for cursor and metadata.
        remaining = max_chars - len(encode(result)) - len(encode(entry)) - 1600
        count = min(len(rows) - begin, limit or len(rows), 1 if cstart else len(rows))
        def page_markdown(page, chosen_columns):
            if single_value and (page or not table.rows):
                return single_value_markdown([table])
            return table.markdown(page, chosen_columns)

        def fits(page, chosen_columns):
            metadata = [{'id': c['id'], 'label': table.column_label(c)} for c in chosen_columns]
            return len(encode(page_markdown(page, chosen_columns))) + len(encode(metadata)) <= remaining
        low, high = 0, count
        while low < high:
            mid = (low + high + 1) // 2
            if fits(rows[begin:begin + mid], cols[cstart:]):
                low = mid
            else:
                high = mid - 1
        take, cend = low, len(cols)
        if take == 0 and begin < len(rows):
            if result['tables']:
                result['nextRead'] = next_read(index, begin, cstart); break
            # A wide row is split by whole value columns, preserving its R id.
            for end in range(cstart + 1, len(cols) + 1):
                if not fits(rows[begin:begin + 1], cols[cstart:end]):
                    break
                cend, take = end, 1
            if take == 0:
                raise AnalysisError('BUSINESS_CELL_TOO_LARGE', '单个业务单元格或口径超过输出容量；原始结果已保存。')
        chosen = rows[begin:begin + take]
        result['markdown'] += '\n\n' + page_markdown(chosen, cols[cstart:cend])
        entry.update(returnedRows=take, columns=[{'id': c['id'], 'label': table.column_label(c)} for c in cols[cstart:cend]])
        result['tables'].append(entry)
        if cend < len(cols):
            result['nextRead'] = next_read(index, begin, cend); break
        if begin + take < len(rows):
            result['nextRead'] = next_read(index, begin + take, 0); break
    result['allRowsIncluded'] = 'nextRead' not in result and start == 0 and offset == 0 and column_offset == 0
    result['readComplete'] = 'nextRead' not in result
    result['materialState'] = {'executionComplete': result['complete'],
                               'presentationComplete': result['allRowsIncluded'],
                               'reviewStatus': 'NOT_IMPLIED_BY_QUERY_SUCCESS'}
    from .query_empty_result import business_outcome
    if outcome := business_outcome(data):
        result['materialState']['businessOutcome'] = outcome
    if single_value and len(result['tables']) > 1 and all(e['returnedRows'] for e in result['tables']):
        shown = selected[start:start + len(result['tables'])]
        combined = state + '\n\n' + single_value_markdown(shown)
        if len(encode({**result, 'markdown': combined})) <= max_chars:
            result['markdown'] = combined
    if len(encode(result)) > max_chars:
        raise AnalysisError('BUSINESS_VIEW_CAPACITY', '业务表格封装超过容量，原始结果已保存。')
    return result


def resolve_cell(receipt, table_ref, row_ref, column_ref, tables=None):
    table = next((t for t in (build_tables(receipt) if tables is None else tables) if t.ref == table_ref), None)
    if table is None:
        raise AnalysisError('TABLE_NOT_FOUND', '结果中不存在指定表。')
    row = table.row_index.get(row_ref)
    cols = table.select_columns([column_ref])
    fact = row['cells'].get(cols[0]['id']) if row else None
    if fact is None:
        raise AnalysisError('TABLE_CELL_NOT_FOUND', '引用没有对应的来源事实，不能当成零。')
    if fact.get('valueState') != 'PRESENT' or fact.get('valueType') != 'DECIMAL_STRING':
        raise AnalysisError('NON_NUMERIC_TABLE_CELL', '引用必须对应已取得的数值，空值或未取得数据不能当成零。')
    return fact


def model_text(value):
    """The business body is actual Markdown, not an escaped JSON string."""
    if isinstance(value, dict) and value.get('viewType') == 'scope_candidates':
        def cell(text):
            return str(text or '').replace('|', '\\|').replace('\n', ' ')
        rows = ['| 类型 | 正式名称 | 编码 | candidateRef |', '| --- | --- | --- | --- |']
        rows += ['| ' + ' | '.join(cell(item.get(key)) for key in ('kind', 'name', 'key', 'candidateRef')) + ' |'
                 for item in value['scopeCandidates']]
        navigation = {key: item for key, item in value.items() if key not in ('scopeCandidates', 'note')}
        # Each candidate list is built from one captured search.
        term = next((item['searchText'] for item in value['scopeCandidates'] if item.get('searchText')), None)
        binding_text = encode(term) if term else '本表候选所在搜索的原词'
        return ('\n'.join(rows) + '\n\n' + value['note']
                + f'\n选中后在原 execute_budget_query 中提交 scope.bindings，填写所选行的 candidateRef 即可，宿主沿用检索原词 {binding_text}。'
                  '沿用原目标、成员、时期及情景。原 selection=CELLS 时仍按单元格取数。'
                + '\n\n查询上下文及读取入口：\n```json\n' + encode(navigation) + '\n```')
    if isinstance(value, dict) and value.get('viewType') == 'result_sources':
        sources = [{key: item[key] for key in ('sourceRef', 'label', 'rowCount', 'grain') if key in item}
                   for item in value['sources']]
        navigation = {'sources': sources, 'nextOffset': value['nextOffset'], 'diagnostics': value.get('diagnostics', [])}
        return value['markdown'] + '\n\n' + value['boundary'] + '\n\n来源引用：\n```json\n' + encode(navigation) + '\n```'
    if isinstance(value, dict) and value.get('viewType') == 'business_tables':
        metadata = {k: v for k, v in value.items() if k not in ('markdown', 'note')}
        return value['markdown'] + '\n\n' + value.get('note', '') + '\n\n读取引用：\n```json\n' + encode(metadata) + '\n```'
    if isinstance(value, dict) and value.get('viewType') == 'source_formula_tables':
        return value['markdown'] + '\n\n' + value.get('note', '') + '\n\n公式引用及状态：\n```json\n' + encode({k: v for k, v in value.items() if k not in ('markdown', 'note')}) + '\n```'
    if isinstance(value, dict) and isinstance(value.get('pages'), list) and any(
            page.get('receipt', {}).get('viewType') == 'business_tables' for page in value['pages']):
        sections = [encode({k: v for k, v in value.items() if k != 'pages'})]
        for page in value['pages']:
            sections.append(model_text(page.get('receipt', page)))
            extra = {k: v for k, v in page.items() if k not in ('receipt', 'captureRef')}
            if extra: sections.append(encode(extra))
        return '\n\n'.join(sections)
    if isinstance(value, dict) and isinstance(value.get('results'), list) and any(
            page.get('viewType') in ('business_tables', 'source_formula_tables') for result in value['results'] for page in result.get('pages', [])):
        sections = []
        for result in value['results']:
            sections.extend(model_text(page) for page in result.get('pages', []))
            sections.append(encode({k: v for k, v in result.items() if k != 'pages'}))
        sections.append(encode({k: v for k, v in value.items() if k != 'results'}))
        return '\n\n'.join(sections)
    if isinstance(value, dict) and any(isinstance(item, dict) and 'formulaPresentation' in item for item in value.get('derivedValues', [])):
        from copy import deepcopy
        metadata = deepcopy(value); sections = []
        for item in metadata['derivedValues']:
            if 'formulaPresentation' in item:
                presentation = item.pop('formulaPresentation')
                sections.append(model_text(presentation))
                item['formulaPresentationTarget'] = presentation['targetFactRef']
        return encode(metadata) + '\n\n' + '\n\n'.join(sections)
    from .business_dataset import model_tables
    metadata, tables = model_tables(value)
    if tables:
        return '\n\n'.join(tables) + '\n\n执行引用与字段说明：\n```json\n' + encode(metadata) + '\n```'
    return encode(value)
