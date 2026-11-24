"""Business names and tabular projection of the selected, frozen source facts."""
from collections import defaultdict

from .budget_dimensions import SYSTEM_DIMENSION_LABELS, dimension_labels, has_chinese
from .business_tables import BusinessTable, BUDGET_TOTAL_MEMBER, cell_text, data_periods, period_label, result_metric, scenario_measure, source_data_period
from .formatting import NUMERIC_TYPES


def member_name(fact, key):
    code = (fact.get('dimensions') or {}).get(key)
    name = (fact.get('dimensionNames') or {}).get(key)
    if key == 'DATATIME':
        return period_label(code, name)
    if code is None:
        return str(name or '空值') if key in (fact.get('dimensions') or {}) else '未返回'
    if name and (name != code or has_chinese(name)):
        return str(name)
    if not name and has_chinese(code):
        return code
    return '合计' if code == BUDGET_TOTAL_MEMBER else '名称待确认'


def field_name(labels, code):
    if code == 'DATATIME':
        return '数据时期'
    caption = labels.get(code)
    if caption and (caption != code or not caption.startswith('MD_')):
        return caption
    # A Chinese source caption can serve as a column key in a captured grid.
    return code if has_chinese(code) else '维度名称待确认'


def display_facts(facts):
    """Resolve names only within one source snapshot, form, dimension role and member."""
    def namespace(f):
        expression = f.get('expression') or f.get('cellExp') or ''
        model = f.get('modelCode') or (expression.partition('[')[0] if '[' in expression else None)
        return (f.get('sourceContextId'), f.get('bindingRef'), model)
    names = defaultdict(lambda: defaultdict(set))
    for fact in facts:
        origin = namespace(fact)
        if not all(origin[:2]):
            continue
        for dim, name in (fact.get('dimensionNames') or {}).items():
            code = (fact.get('dimensions') or {}).get(dim)
            if code is not None and name and name != code:
                domain = (fact.get('dimensionModels') or {}).get(dim)
                names[origin[:2], dim, code][origin[2], domain].add(name)
    result = []
    for fact in facts:
        dim_names = dict(fact.get('dimensionNames') or {})
        dims = fact.get('dimensions') or {}
        for dim, code in dims.items():
            origin = namespace(fact)
            domain = (fact.get('dimensionModels') or {}).get(dim)
            options = names.get((origin[:2], dim, code), {})
            labels = {name for (model, known_domain), entries in options.items()
                      if (origin[2] == model or (domain and domain == known_domain and not all((origin[2], model))))
                      and domain == known_domain for name in entries}
            if len(labels) == 1 and (not dim_names.get(dim) or dim_names[dim] == code):
                dim_names[dim] = next(iter(labels))
        row = dict(fact)
        if dim_names or 'dimensionNames' in fact:
            row['dimensionNames'] = dim_names
        grain = fact.get('grain') or list((fact.get('rowIdentity') or {}).keys())
        raw = str(fact.get('objectLabel') or '').split(' / ')
        if grain and (not fact.get('objectLabel') or any(str(dims[key]) in raw for key in grain if key in dims)):
            label = ' / '.join(member_name(row, key) for key in grain if key in dims)
            if label:
                row['objectLabel'] = label
        result.append(row)
    return result


def metric_name(fact):
    from .business_tables import actual_headers
    from .formula_labels import is_period_heading
    names = {str(v).strip() for v in (fact.get('dimensionNames') or {}).values()}
    headers = actual_headers(fact, fact.get('period'))
    parts = [s for s in headers if s and s.strip() not in names and not is_period_heading(s, fact)]
    return ' / '.join(parts) or fact.get('measureTitle') or next(
        (s for s in headers if s and not is_period_heading(s, fact)), '查询值')


def comparison_labels(dataset, facts):
    """Factor constant grain coordinates out of labels, preserving every frozen fact and row key."""
    values = [row['values'] for row in [*dataset['rows'], *dataset.get('unavailable', [])]]
    if len(values) < 2 or any(row.get('factRef') not in facts for row in values):
        return
    selected = [facts[row['factRef']] for row in values]
    grain = selected[0].get('grain') or []
    if len(grain) < 2 or any(f.get('grain') != grain for f in selected):
        return
    if any(not set(grain) <= f.get('dimensions', {}).keys() for f in selected):
        return
    constant = [k for k in grain if len({(f['dimensions'][k], member_name(f, k)) for f in selected}) == 1]
    changing = [k for k in grain if k not in constant]
    if not constant or not changing:
        return
    names = [' / '.join(member_name(f, k) for k in changing) for f in selected]
    if len(set(names)) != len(names) or any('名称待确认' in name for name in names):
        return
    field = 'objectLabel' if dataset['kind'] == 'ORDERED' else 'object'
    if any(field not in row for row in values):
        return
    for row, name in zip(values, names):
        row[field] = name
    dataset['commonMemberNames'] = [member_name(selected[0], k) for k in constant]


def appendix_tables(artifact):
    scopes = artifact.get('scope', {}).get('sourceScopes') or []
    catalogs = defaultdict(dict)
    for scope in scopes:
        labels = dict(scope.get('dimensionLabels') or {})
        for col in scope.get('columns', []):
            role = (scope.get('columnRoles') or {}).get(col['name'], {})
            if role.get('kind') in ('DIMENSION', 'PROPERTY') and col.get('label') and col['label'] != col['name']:
                labels.setdefault(role.get('valueColumn') or col['name'], col['label'])
        catalogs[scope.get('captureRef'), scope.get('batchId')].update(labels)
    groups = {}
    for fact in artifact.get('facts', []):
        # Repeated selection of an underlying fact already has one row in facts.
        # Retain all its roles, distinct records and independently loaded scopes.
        roles = tuple(sorted({v['inputRole'] for v in fact.get('inputMemberships', []) if v.get('inputRole')})) or (fact.get('inputRole') or '',)
        key = (fact.get('sourceContextId'), fact.get('bindingRef'), fact.get('captureRef'), fact.get('batchId'),
               fact.get('observationId'), fact.get('taskName'), fact.get('formName'), fact.get('executionPeriod'), roles)
        dims = {k: v for k, v in (fact.get('effectiveDimensions') or {}).items() if k != 'DATATIME'}
        dims.update(fact.get('dimensions') or {})
        if period := source_data_period(fact):
            dims.setdefault('DATATIME', period)
        display_names = fact.get('dimensionNames') or {}
        if not dims and fact.get('objectId') is not None:
            dims['对象'] = fact['objectId']
            display_names = {**display_names, '对象': fact.get('objectLabel')}
        groups.setdefault(key, []).append({**fact, 'dimensions': dims, 'dimensionNames': display_names,
                                           'valueType': fact.get('valueType') or 'DECIMAL_STRING', 'measureTitle': metric_name(fact),
                                           'measureCode': fact.get('sourceMeasureCode') or fact.get('measureCode')})
    form_fields = defaultdict(lambda: defaultdict(set))
    for key in groups:
        if key[0] and key[1]:
            for dim, label in catalogs[key[2], key[3]].items():
                form_fields[key[:2]][dim].add(label)
    result = []
    for key, facts in groups.items():
        first = facts[0]
        shared = {dim: next(iter(names)) for dim, names in form_fields[key[:2]].items() if len(names) == 1}
        labels = {**SYSTEM_DIMENSION_LABELS, **shared, **catalogs[key[2], key[3]]}
        scenario = not key[4] and all(scenario_measure(f) for f in facts)
        if (key[4] and all(f.get('cellAddress') for f in facts) and len({f['measureCode'] for f in facts}) == 1
                and len({f['measureTitle'] for f in facts}) > 1):
            # A source cell caption describes its address. Keep it on the row:
            # turning each caption of a single native measure into a different
            # metric produces a diagonal table full of artificial missing cells.
            from .formula_labels import short_caption
            facts = [{**f, 'sourceTitle': short_caption(f['measureTitle']), 'measureTitle': '来源值'} for f in facts]
        context = {**(first.get('formContext') or {}), 'formName': key[6], 'taskName': key[5]}
        context['period'] = context.get('period') or key[7]
        table = BusinessTable('T' + str(len(result) + 1), context, '', '', labels, scenario, facts)
        result.append((table, key[-1]))
    for scope in scopes:
        if 'emptyContext' in scope:
            context = scope['emptyContext']
            labels = dimension_labels(scope.get('dimensionCatalog', []), models=[context['modelCode']] if context.get('modelCode') else [], context=context)
            table = BusinessTable('T' + str(len(result) + 1), context, scope['resultSetId'], '', labels, False, [])
            table.columns = [{'id': 'C1', 'label': scope.get('label') or '查询值'}]
            result.append((table, (scope.get('inputRole') or '',)))
    return result


def conditions(table, *, excluded_dimensions=()):
    fixed = []
    if table.context.get('taskName'):
        fixed.append(('任务', table.context['taskName']))
    period = data_periods(table)
    if period:
        fixed.append(('数据时期', period))
    anchor = table.facts[0] if table.facts else {'dimensions': table.fixed, 'dimensionNames': table.context.get('dimensionNames') or {}}
    for key in sorted(table.fixed.keys() - {'DATATIME'} - set(excluded_dimensions)):
        fixed.append((field_name(table.labels, key), member_name(anchor, key)))
    titles = {f.get('sourceTitle') for f in table.facts}
    if len(titles) == 1 and next(iter(titles)):
        title = next(iter(titles))
        if title not in {value for _, value in fixed}:
            fixed.append(('来源标题', title))
    return fixed


def _title_dimension(table):
    """A source cell title can identify the row even when its member has no translated name."""
    if len(table.dimensions) != 1 or not all(f.get('sourceTitle') and f.get('cellAddress') for f in table.facts):
        return None
    from .formula_labels import short_caption
    dim = table.dimensions[0]
    by_title, by_member = {}, {}
    for fact in table.facts:
        code = fact['dimensions'].get(dim)
        title = fact['sourceTitle']
        name = (fact.get('dimensionNames') or {}).get(dim)
        if (code is None or by_title.setdefault(title, code) != code or by_member.setdefault(code, title) != title
                or name and name != code and short_caption(name) != title):
            return None
    return dim


def visible_table(table, rows=None, columns=None):
    """User-facing cells. Internal row/column references remain on BusinessTable."""
    cols = table.columns if columns is None else columns
    names = []
    for col in cols:
        label = col.get('publicLabel') or col['label']
        if (label == col.get('measureCode') or (table.scenario and label == col.get('scenario'))) and not has_chinese(label):
            label = ('情景名称待确认' if table.scenario else
                     '查询值' if len(table.columns) == 1 and table.dimensions else '指标名称待确认')
        names.append(table.column_label(col, label))
    titles = {f.get('sourceTitle') for f in table.facts}
    show_titles = len(titles) > 1
    represented = _title_dimension(table) if show_titles else None
    dimensions = [k for k in table.dimensions if k != represented]
    headers = [field_name(table.labels, k) for k in dimensions] + names
    if show_titles:
        headers.insert(0, '来源标题')
    body = []
    for row in table.rows if rows is None else rows:
        fact = next(iter(row['cells'].values()))
        dims = [row.get('publicLabels', {}).get(k) or member_name(fact, k) for k in dimensions]
        if show_titles:
            dims.insert(0, ' / '.join(dict.fromkeys(f['sourceTitle'] for f in row['cells'].values() if f.get('sourceTitle'))))
        body.append(dims + [cell_text(row['cells'].get(c['id']), grouped=True) for c in cols])
    return headers, body


def query_markdown(table, rows=None):
    from .single_value_presentation import _row, source_name
    from .business_tables import escape
    fields = conditions(table)
    lines = ['**' + escape(source_name(table)) + '**', '', '**查询条件**', '', _row(['项目', '内容']), _row(['---', '---'])]
    lines.extend(_row(pair) for pair in fields)
    headers, body = visible_table(table, rows)
    lines.extend(['', '**查询结果**', '', _row(headers), _row(['---'] * len(headers))])
    lines.extend(_row(row) for row in body)
    if not body:
        lines.extend(['', '本次查询没有数据。'])
    return '\n'.join(lines)


def aligned_filter_view(data, tables):
    """Project an executed native filter batch side by side only at its exact entity grain."""
    filtering = data.get('valueFilter') or {}
    batches = filtering.get('batches') or []
    sets = data.get('resultSets') or []
    if (len(tables) < 2 or len(tables) != len(sets) or len(batches) != 1
            or filtering.get('executionStatus') != 'EXECUTED' or batches[0].get('status') != 'SUCCEEDED'
            or filtering.get('basis') not in ('BUDGET_NATIVE_FILTER', 'BUDGET_NATIVE_RESULT_WHERE')
            or (data.get('presentation') or {}).get('valueOrder')):
        return None
    targets = {t['id']: t for t in data.get('targets', [])}
    ids = [targets.get(s.get('targetId'), {}).get('logicalTargetId', s.get('targetId')) for s in sets]
    if len(set(ids)) != len(ids) or set(ids) != set(batches[0].get('logicalTargetColumns') or {}):
        return None
    first = tables[0]
    base = first.context.get('baseContext')
    binding = first.context.get('target') or {}
    if not base or not binding.get('formBindingKey') or not first.facts:
        return None
    grain = set(sets[0].get('grain') or [])
    keys = set(first.facts[0]['dimensions'])
    if not grain or not grain <= keys:
        return None
    from .budget_dimensions import source_models
    models = source_models(first.facts)
    for table, result in zip(tables, sets):
        if (table.set_id != result.get('resultSetId') or len(table.columns) != 1 or table.duplicates
                or not table.facts or table.context.get('baseContext') != base or table.context.get('target') != binding
                or table.context.get('period') != first.context.get('period') or set(result.get('grain') or []) != grain
                or table.context.get('requestedDimensions') != first.context.get('requestedDimensions')
                or table.context.get('effectiveDimensions') != first.context.get('effectiveDimensions')
                or len(models) > 1 or source_models(table.facts) != models
                or any(set(f['dimensions']) != keys for f in table.facts)):
            return None
    # Only coordinates explicitly resolved to each target may be pivoted into column headings.
    varying = {k for k in keys if all(k in t.fixed for t in tables) and len({t.fixed[k] for t in tables}) > 1}
    if not varying or not varying <= grain:
        return None
    headings, by_table = [], []
    entity_dims = sorted(keys - varying)
    for table, result in zip(tables, sets):
        resolved = {(r.get('dimensionCode'), r.get('objectCode')) for r in result.get('resolvedTerms', [])
                    if not r.get('titleFallback')}
        label = result_metric(result, targets.get(result['targetId'], {}))
        if not label or any((k, table.fixed[k]) not in resolved for k in varying):
            return None
        headings.append(table.column_label(table.columns[0], label))
        records = {tuple(f['dimensions'][k] for k in entity_dims): f for f in table.facts}
        if len(records) != len(table.facts) or by_table and records.keys() != by_table[0].keys():
            return None
        by_table.append(records)
    if len(set(headings)) != len(headings) or len(first.rows) != batches[0].get('rowCount'):
        return None
    fixed = conditions(first, excluded_dimensions=varying)
    headers = [field_name(first.labels, k) for k in first.dimensions] + headings
    rows = []
    for row in first.rows:
        f = next(iter(row['cells'].values()))
        identity = tuple(f['dimensions'][k] for k in entity_dims)
        labels = [row.get('publicLabels', {}).get(k) or member_name(f, k) for k in first.dimensions]
        rows.append(labels + [cell_text(records[identity], grouped=True) for records in by_table])
    return fixed, headers, rows


def aligned_markdown(table, projection, limit=None):
    from .single_value_presentation import _row, source_name
    from .business_tables import escape
    fixed, headers, rows = projection
    return '\n'.join(['**' + escape(source_name(table)) + '**', '', '**查询条件**', '', _row(['项目', '内容']),
                      _row(['---', '---']), *(_row(pair) for pair in fixed),
                      '', '**查询结果**', '',
                      _row(headers), _row(['---'] * len(headers)), *(_row(row) for row in rows[:limit])])


def public_query_markdown(tables, aligned=None, limit=None):
    from .single_value_presentation import single_value_markdown
    if tables and tables[0].single_value_target is not None:
        return single_value_markdown(tables)
    if aligned:
        return aligned_markdown(tables[0], aligned, limit)
    return '\n\n'.join(query_markdown(t, t.rows[:limit]) for t in tables)


def link_copied_source_table(markdown, table, save_table, *, aligned=None, aligned_tables=()):
    """Keep a referenced copy of the complete result available without displaying it twice."""
    import re
    from .report_markdown import parser

    if not table.rows or not aligned and '行号' not in markdown:
        return markdown
    if aligned:
        _, headers, rows = aligned
        numeric = {len(table.dimensions) + i for i, source in enumerate(aligned_tables)
                   if source.columns[0].get('valueType') in NUMERIC_TYPES}
    else:
        headers, body = visible_table(table)
        headers = ['行号', *headers]
        rows = [[r['id'], *values] for r, values in zip(table.rows, body)]
        numeric = {len(headers) - len(table.columns) + index for index, col in enumerate(table.columns)
                   if col.get('valueType') in NUMERIC_TYPES}
    thousands = re.compile(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?')
    def normalized(row):
        return [cell.replace(',', '') if i in numeric and thousands.fullmatch(cell) else cell for i, cell in enumerate(row)]
    expected = [normalized(row) for row in rows]
    lines = markdown.splitlines(keepends=True)
    tokens = parser().parse(markdown)
    spans = []
    for index, token in enumerate(tokens):
        if token.type != 'table_open' or not token.map:
            continue
        actual = []
        for part in tokens[index + 1:]:
            if part.type == 'table_close':
                break
            if part.type == 'tr_open':
                actual.append([])
            elif part.type == 'inline':
                actual[-1].append(part.content.strip())
        if (not actual or len(actual) != len(rows) + 1 or any(len(r) != len(actual[0]) for r in actual[1:])
                or any(actual[0].count(h) != 1 for h in headers)):
            continue
        if not aligned and actual[0] != headers:
            continue
        positions = [actual[0].index(h) for h in headers]
        # Keep any additional, varying business column inline. A uniform annotation or an
        # ordinal can remain in the linked copy alongside the authoritative values above.
        extra = set(range(len(actual[0]))) - set(positions)
        if any(len({r[i] for r in actual[1:]}) > 1 and
               not (actual[0][i] in ('序号', '行号') and
                    all(r[i] in (str(n), f'R{n}') for n, r in enumerate(actual[1:], 1))) for i in extra):
            continue
        if [normalized([r[p] for p in positions]) for r in actual[1:]] == expected:
            spans.append(token.map)
    for start, end in reversed(spans):
        original = ''.join(lines[start:end])
        url = save_table(original)
        lines[start:end] = [f'[查看说明中引用的原表]({url})\n']
    return ''.join(lines)
