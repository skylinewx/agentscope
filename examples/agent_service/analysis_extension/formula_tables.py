"""Render captured formulas using their bound fact references, never global A1 lookup."""
from collections import OrderedDict

from .business_tables import build_tables, escape, encode
from .formulas import Expression
from .models import AnalysisError, digest


def analysis_tables(facts):
    groups = OrderedDict()
    for fact in facts:
        context = {k: fact.get(k) for k in ('sourceContextId', 'bindingRef', 'observationId', 'taskName', 'formName', 'executionPeriod')}
        key = digest(context)
        group = groups.setdefault(key, {'resultSetId': key, 'interpretationId': '', 'context': context, 'rows': []})
        dims = {**fact.get('effectiveDimensions', {}), **fact.get('dimensions', {})}
        if fact.get('period'):
            dims['DATATIME'] = fact['period']
        title = '_'.join(fact.get('headers', [])) or fact.get('objectLabel', '')
        group['rows'].append({**fact, 'title': title, 'sourceTitle': title,
                              'dimensions': dims, 'valueType': fact.get('valueType') or 'DECIMAL_STRING'})
    return build_tables({'data': {'resultSets': list(groups.values())}})


def readable_expression(expression, dependencies, locations, tables):
    parsed = Expression(expression)
    refs = {}
    for dep in dependencies:
        address, ref = dep.get('address'), dep.get('factRef')
        if address in refs and refs[address] != ref:
            raise AnalysisError('FORMULA_REFERENCE_AMBIGUOUS', '当前公式地址对应多个来源事实。')
        refs[address] = ref

    def reference(address):
        fact_ref = refs.get(address)
        if fact_ref not in locations:
            raise AnalysisError('FORMULA_DEPENDENCY_MISSING', '公式依赖没有可追溯的业务单元格。', {'address': address})
        place = locations[fact_ref]
        table = tables[place['tableRef']]
        col = next(c for c in table.columns if c['id'] == place['columnRef'])
        return f"{place['tableRef']}.{place['rowRef']}[{col['label']}]"

    def visit(node):
        kind = node[0]
        if kind == 'number': return node[1]
        if kind == 'ref': return reference(node[1])
        if kind == 'unary': return node[1] + '(' + visit(node[2]) + ')'
        if kind == 'sum': return 'SUM(' + ', '.join(visit(arg) for arg in node[1]) + ')'
        return '(' + visit(node[2]) + ' ' + node[1] + ' ' + visit(node[3]) + ')'
    return visit(parsed.tree)


def formula_view(trace, facts, analysis_ref, *, offset=0, limit=20, max_chars=16000):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AnalysisError('INVALID_QUERY_PAGE', '公式分页使用非负 offset 和1—100的 limit。')
    tables = analysis_tables(facts)
    by_table = {t.ref: t for t in tables}
    locations = {}
    for table in tables:
        for ref, places in table.locations.items():
            if len(places) != 1 or ref in locations:
                raise AnalysisError('FORMULA_REFERENCE_AMBIGUOUS', '分析事实没有唯一的展示位置。')
            locations[ref] = places[0]
    nodes = [n for n in trace.get('nodes', []) if n.get('expression') or n.get('formula')]
    selected = nodes[offset:offset + limit]
    formula_ids = {fact['factId']: 'F' + str(index + 1) for index, fact in enumerate(facts)}

    def render(chosen):
        lines = ['| 公式编号 | 目标单元格 | 来源公式的可读表达 | 本格核对 | 递归核对 |', '| --- | --- | --- | --- | --- |']
        used, references = set(), []
        for node in chosen:
            ref = node['factRef']; place = locations.get(ref)
            formula_id = formula_ids.get(ref, 'F_' + digest(ref)[:12])
            if place:
                col = next(c for c in by_table[place['tableRef']].columns if c['id'] == place['columnRef'])
                target = f"{place['tableRef']}.{place['rowRef']}[{col['label']}]"
            else:
                target = ref
            used.add(ref)
            used.update(d['factRef'] for d in node.get('dependencies', []) if d.get('factRef'))
            try:
                expression = readable_expression(node.get('expression', ''), node.get('dependencies', []), locations, by_table)
            except AnalysisError as error:
                expression = '无法完整映射：' + error.code + '；原式：' + str(node.get('expression') or node.get('formula') or '')
            local = node.get('localCheck', {})
            check = str(local.get('status', '未确认'))
            if local.get('difference') is not None:
                check += '；差额=' + str(local['difference'])
            if local.get('value') is not None:
                check += '；计算值=' + str(local['value'])
            lines.append('| ' + ' | '.join(escape(v) for v in (formula_id, target, expression, check, node.get('status', '未确认'))) + ' |')
            references.append({'formulaRef': formula_id, 'targetFactRef': ref, 'target': place,
                               'sourceExpression': node.get('expression'), 'dependencies': node.get('dependencies', [])})
        for table in tables:
            if not nodes and trace.get('targetFactRef'):
                used.add(trace['targetFactRef'])
            rows = [r for r in table.rows if any(f['factId'] in used for f in r['cells'].values())]
            if rows:
                lines.extend(['', table.markdown(rows)])
        if not nodes:
            lines.append('没有已捕获且可展示的来源公式；按目标核对状态说明，不推测公式。')
        return '\n'.join(lines), references

    body, refs = render(selected)
    while len(encode({'markdown': body, 'references': refs})) > max_chars - 1600 and len(selected) > 1:
        selected = selected[:max(1, len(selected) // 2)]; body, refs = render(selected)
    result = {'viewType': 'source_formula_tables', 'analysisRef': analysis_ref, 'targetFactRef': trace.get('targetFactRef'),
              'markdown': body, 'formulaCount': len(nodes), 'returnedFormulaCount': len(selected),
              'references': refs, 'status': trace.get('status'), 'targetLocalCheck': trace.get('targetLocalCheck'),
              'pendingDependencyCount': len(trace.get('pendingDependencies', [])),
              'readComplete': offset + len(selected) >= len(nodes),
              'note': '公式来自保存的来源表达式；引用按该公式已绑定的factRef转换。F/T/R引用在同一analysisRef内稳定，编号可不连续。本格核对与递归核对分别判断。需要补充计算时，Calculate.valueRefs 使用本表 analysisRef、tableRef、rowRef、columnRef；不能改用 captureRef 或原查询表的行列号。'}
    if not result['readComplete']:
        result['nextRead'] = {'section': 'formulas', 'analysisRef': analysis_ref, 'factRef': trace.get('targetFactRef'),
                              'offset': offset + len(selected), 'limit': limit}
    if len(encode(result)) > max_chars:
        # Keep the source and true status, with bounded native dependency pages.
        result['markdown'] = '单条公式及依赖超过本次展示容量，请按依赖事实引用读取完整材料。'
        result.pop('references')
        result['readComplete'] = False
        result['detailRead'] = {'section': 'formulas', 'analysisRef': analysis_ref, 'factRef': trace.get('targetFactRef'), 'detail': 'full'}
        result['presentationIssue'] = 'FORMULA_DISPLAY_CAPACITY'
    return result
