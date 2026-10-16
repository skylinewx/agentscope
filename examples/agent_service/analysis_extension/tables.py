"""Business tables from explicit value groups; selection is by member, not cell."""
from collections import OrderedDict

from .formula_selection import selected_nodes
from .models import AnalysisError, decimal_value, digest
from .formatting import business_formula, formula_labels, unit_for, number_text, view_label


def member_key(fact):
    return 'member_' + digest(fact['objectId'])[:24]


def measure_key(fact, scope):
    identity = {
        **{k: fact.get(k) for k in ('bindingRef', 'cellKey', 'measureCode', 'period', 'sourceOrigins', 'unit', 'scale')},
        'dimensions': {k: v for k, v in fact.get('dimensions', {}).items() if k != scope.get('memberDimension')},
        'dataContext': fact.get('dataContext', fact.get('context')),
    }
    # Query identity belongs to each observation, not to the business column.
    # Duplicate observations for one member still fail in business_tables.
    if 'metricKey' in fact:
        identity['cellKey'] = fact['metricKey']
    return 'column_' + digest(identity)[:24]


def measure_label(fact):
    return ' / '.join(fact.get('headers') or []) or '来源项目'


def object_label(fact):
    return fact.get('objectLabel') or '名称待核对'


def ordered_facts(artifact):
    """Resolve model-selected orders before shared member pagination or Top N."""
    facts = {f['factId']: f for f in artifact['facts']}
    derived = {v['derivedId']: v for v in artifact['derivedValues']}
    plan = next((v for v in artifact['viewPlan'] if v['type'] == 'ORDER'), None)
    refs = []
    if plan:
        for ref in plan['valueRefs']:
            if ref in derived:
                value = derived[ref]
                refs.extend(value['orderedFactIds'] + value['unavailableFactIds'])
            else:
                refs.append(ref)
    if not plan:
        primary = next((v for v in artifact['viewPlan'] if v['type'] in ('TABLE', 'BAR_VALUES', 'BAR_RANK')), None)
        if primary:
            if primary.get('orderRef'):
                value = derived[primary['orderRef']]
                refs.extend(value['orderedFactIds'] + value['unavailableFactIds'])
            else:
                refs.extend(ref for ref in primary['valueRefs'] if ref in facts)
    ordered = [facts[ref] for ref in dict.fromkeys(refs + list(facts))]
    members = OrderedDict()
    for fact in ordered:
        members.setdefault(fact['objectId'], []).append(fact)
    # A table's local order never constrains another table. Only explicit ORDER
    # controls the raw/global object list; ordered_for_view handles each view.
    return [fact for group in members.values() for fact in group]



def select_members(original, artifact, params, default_column_refs):
    scope = artifact['scope']
    columns = OrderedDict()
    for fact in original:
        key = measure_key(fact, scope)
        columns.setdefault(key, {'key': key, 'label': measure_label(fact), 'unit': fact.get('unit') or ''})
    column = params.get('columnKey')
    if column is not None and column not in columns:
        raise AnalysisError('INVALID_VIEW_COLUMN', '筛选或排序列不属于当前分析。')
    if column is None:
        defaults = {measure_key(f, scope) for f in original if f['factId'] in default_column_refs}
        column = next(iter(defaults)) if len(defaults) == 1 else next(iter(columns)) if len(columns) == 1 else None
    active = params['filter'] != 'all' or params['sort'] != 'published'
    if active and column is None:
        raise AnalysisError('VIEW_COLUMN_REQUIRED', '请选择一个具体指标列进行筛选或排序。')
    anchors = [f for f in original if measure_key(f, scope) == column] if column else list(original)
    members = list(dict.fromkeys(f['objectId'] for f in original))
    if active:
        by_member = {}
        for f in anchors:
            if f['objectId'] in by_member:
                raise AnalysisError('ORDER_UNAVAILABLE', '该指标在同一对象上存在多个来源值，请先消除冲突。')
            by_member[f['objectId']] = f
        def matches(member):
            f = by_member.get(member)
            present = f is not None and f.get('valueState') == 'PRESENT'
            if params['filter'] == 'all': return True
            if params['filter'] == 'unavailable': return not present
            if not present: return False
            number = decimal_value(f['value'])
            return {'positive': number > 0, 'negative': number < 0, 'zero': number == 0}[params['filter']]
        members = [member for member in members if matches(member)]
        if params['sort'] != 'published':
            available = [m for m in members if m in by_member and by_member[m].get('valueState') == 'PRESENT']
            available_members = set(available)
            absent = [m for m in members if m not in available_members]
            available.sort(key=lambda m: (m, by_member[m]['factId']))
            available.sort(key=lambda m: decimal_value(by_member[m]['value']), reverse=params['sort'] == 'desc')
            members = available + absent
    if params['topN'] is not None:
        members = members[:params['topN']]
    selected = set(members)
    positions = {member: i for i, member in enumerate(members)}
    facts = [f for f in original if f['objectId'] in selected]
    facts.sort(key=lambda f: positions[f['objectId']])
    return facts, members, list(columns.values()), column


def business_tables(artifact, facts, rows, published_order=True):
    known = {f['factId']: f for f in artifact['facts']}
    values = {v['derivedId']: v for v in artifact['derivedValues']}
    public = {f['factId']: r for f, r in zip(facts, rows)}
    selected = {f['factId'] for f in facts}
    plans = [v for v in artifact['viewPlan'] if v['type'] == 'TABLE' or (v['type'] in ('FORMULA', 'HIERARCHY') and v.get('includeDetails') is True)]
    tables = []
    for plan in plans:
        first_table = len(tables)
        plan_refs = set(plan['valueRefs'])
        from .presentation_options import ordered_for_view
        group = [f for f in facts if f['factId'] in plan_refs]
        if published_order: group = ordered_for_view(artifact, plan, group)
        if group:
            columns, members = OrderedDict(), OrderedDict()
            for fact in group:
                column = measure_key(fact, artifact['scope'])
                column_label = view_label(plan, fact['factId'], measure_label(fact))
                if column in columns and columns[column]['label'] != column_label:
                    raise AnalysisError('AMBIGUOUS_TABLE_LABEL', '同一指标列的展示简称须一致。')
                columns.setdefault(column, {'key': column, 'label': column_label, 'unit': unit_for(fact, plan)})
                row_name = view_label(plan, fact['factId'], object_label(fact), 'object')
                row = members.setdefault(fact['objectId'], {'memberKey': member_key(fact), 'label': row_name, 'cells': {}})
                if row['label'] != row_name:
                    raise AnalysisError('AMBIGUOUS_TABLE_LABEL', '同一对象行的业务简称须一致。')
                if column in row['cells']:
                    raise AnalysisError('AMBIGUOUS_TABLE', '同一对象和指标存在重复数据，不能选择其中一个作为表格值。')
                row['cells'][column] = {**public[fact['factId']], 'unit': unit_for(fact, plan),
                    'display': number_text(fact['value'], plan.get('numberFormat'), grouping=True) if fact.get('valueState') == 'PRESENT' else '—'}
            title = plan.get('title') or (next(iter(columns.values()))['label'] if len(columns) == 1 else '相关指标明细')
            if len(members) == 1 and not plan.get('title'):
                title = next(iter(members.values()))['label'] + ' · ' + title
            tables.append({'tableKey': 'table_' + digest(plan['viewId'])[:24], 'title': title,
                           'columns': list(columns.values()), 'rows': list(members.values()), 'kind': 'values'})
        for ref in plan['valueRefs']:
            value = values.get(ref)
            if not value: continue
            if not value.get('formulaTrace'):
                sources = [known[fid] for fid in value.get('sourceFactIds', []) if fid in known]
                if not sources or not {f['factId'] for f in sources} <= selected: continue
                # A cross-member result retains its original scope; filters must not relabel it as a new total.
                from .presentation import display
                member = member_key(sources[0])
                labels = list(dict.fromkeys(object_label(f) + ' · ' + measure_label(f) for f in sources))
                names = {'SUM': '合计', 'DIFFERENCE': '差额', 'RATIO': '比值', 'LINEAR': '按登记公式复算', 'CONVERT': '按登记规则换算', 'COUNT': '有效成员', 'RECONCILE': '父子汇总差额', 'ARITHMETIC': '计算结果'}
                if value['operator'] not in names: continue
                number = value.get('value')
                cell = {'rowKey': 'computed_' + digest(ref)[:24], 'valueRef': 'value_' + digest(ref)[:24],
                        'memberKey': member, 'label': names[value['operator']], 'measure': '结果',
                        'exact': number, 'display': display(number, plan.get('numberFormat')) if number is not None else '—',
                        'unit': unit_for(value, plan), 'state': ''}
                notes = [d['message'] for d in value.get('diagnostics', [])] if artifact['technicalDetails'] else []
                tables.append({'tableKey': 'table_' + digest([plan['viewId'], ref])[:24],
                    'title': plan.get('title') or names[value['operator']], 'kind': 'calculation',
                    'columns': [{'key': 'result', 'label': names[value['operator']], 'unit': cell['unit']}],
                    'rows': [{'memberKey': member, 'label': '；'.join(labels), 'cells': {'result': cell}, 'note': '；'.join(notes)}],
                    'note': plan.get('description', '')})
                continue
            trace = value['formulaTrace']
            target = known[trace['targetFactRef']]
            if not any(f['objectId'] == target['objectId'] for f in facts): continue
            from .presentation import display
            shown_nodes = selected_nodes(trace, plan)
            labels = formula_labels(trace, known, plan)
            units = {unit_for(known[node['factRef']], plan) for node in shown_nodes}
            common_unit = next(iter(units)) if len(units) == 1 else ''
            columns = [{'key': key, 'label': label, 'unit': common_unit}
                       for key, label in [('observed', '来源值'), ('localCalculated', '本层计算'), ('localDifference', '本层差额'), ('recalculated', '逐层展开计算'), ('difference', '展开后差额')]]
            trace_rows = []
            for node in shown_nodes:
                fact = known[node['factRef']]
                cells = {}
                for column in columns:
                    number = node['localCheck']['value'] if column['key'] == 'localCalculated' else node['localCheck']['difference'] if column['key'] == 'localDifference' else node[column['key']]
                    cells[column['key']] = {'rowKey': 'trace_' + digest([ref, fact['factId'], column['key']])[:24],
                        'valueRef': 'value_' + digest([ref, fact['factId'], column['key']])[:24], 'memberKey': member_key(target),
                        'label': labels[fact['factId']], 'measure': column['label'], 'display': display(number, plan.get('numberFormat')) if number is not None else '—',
                        'exact': number, 'unit': unit_for(fact, plan), 'state': ''}
                note = '；'.join(d['message'] for d in node['diagnostics'])
                if node.get('boundary') == 'EXTERNAL_REFERENCE':
                    note = '跨表来源，本次以本表已取得值为止，未展开外表计算。'
                trace_rows.append({'memberKey': member_key(target), 'label': labels[fact['factId']], 'cells': cells,
                                   'note': ((node['expression'] or '') + ('；' + note if note else '')) if artifact['technicalDetails'] else business_formula(node, known, plan, labels)})
            tables.append({'tableKey': 'table_' + digest([plan['viewId'], ref])[:24],
                'title': plan.get('title') or object_label(target) + ' · ' + labels[target['factId']],
                'columns': columns, 'rows': trace_rows, 'kind': 'formula', 'note': plan.get('description', '')})
        for table in tables[first_table:]:
            table.update(viewId=plan['viewId'], collapsed=plan.get('collapsed', False), pageSize=plan.get('pageSize', 12))
            if 'note' not in table: table['note'] = plan.get('description', '')
    # Match the public reader's limits before the publication transaction commits.
    if len(tables) > 120:
        raise AnalysisError('TABLE_LIMIT', '表格分组超过展示容量，请缩小本次展示计划。')
    def text_bound(value):
        if not isinstance(value, str) or len(value.encode('utf-16-le')) > 32000:
            raise AnalysisError('TABLE_LIMIT', '表格标题或说明超过展示容量，请缩小本次展示计划。')
    for table in tables:
        if len(table['columns']) > 1000 or len(table['rows']) > 50000:
            raise AnalysisError('TABLE_LIMIT', '表格行列超过展示容量，请缩小本次展示计划。')
        text_bound(table['title'])
        text_bound(table.get('note', ''))
        for column in table['columns']:
            text_bound(column['label']); text_bound(column['unit'])
        for row in table['rows']:
            text_bound(row['label']); text_bound(row.get('note', ''))
    return tables


def completeness_notes(artifact):
    scope, notes = artifact['scope'], []
    if not scope.get('populationComplete'):
        notes.append('所选成员清单尚未证明枚举完整。')
    if scope.get('pendingMembers'):
        notes.append(f"仍有 {len(scope['pendingMembers'])} 个所选成员尚未取得数据。")
    if not scope.get('selectedDataComplete'):
        notes.append('来源分页或所选数据仍不完整；成员枚举完成不代表必要指标已取齐。')
    if any(f.get('objectLabelState') in ('MISSING', 'CONFLICT') for f in artifact['facts']):
        notes.append('部分对象名称尚未取得唯一的来源证明。')
    return notes
