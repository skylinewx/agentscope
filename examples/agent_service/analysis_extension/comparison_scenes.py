"""Exact additive-formula waterfalls and captured parent/child comparisons."""
from decimal import Decimal, localcontext, Inexact, DecimalException

from .formulas import Expression
from .formatting import number_text, unit_for, formula_labels
from .formula_selection import selected_nodes
from .models import AnalysisError, decimal_value, digest
from .check_visibility import show_checks


def affine(tree):
    """Ordered (reference-or-constant, coefficient) terms; refuse nonlinear attribution."""
    kind = tree[0]
    if kind == 'ref': return [(tree[1], Decimal(1))]
    if kind == 'number': return [(None, decimal_value(tree[1]))]
    if kind == 'sum': return [term for child in tree[1] for term in affine(child)]
    if kind == 'unary': return [(ref, -n if tree[1] == '-' else n) for ref, n in affine(tree[2])]
    left, right = affine(tree[2]), affine(tree[3])
    if tree[1] in ('+', '-'):
        return left + [(ref, -n if tree[1] == '-' else n) for ref, n in right]
    left_constant = all(ref is None for ref, _ in left)
    right_constant = all(ref is None for ref, _ in right)
    if tree[1] == '*' and (left_constant or right_constant):
        constant, terms = (left, right) if left_constant else (right, left)
        coefficient = sum((n for _, n in constant), Decimal(0))
        return [(ref, coefficient*n) for ref, n in terms]
    if tree[1] == '/' and right_constant:
        divisor = sum((n for _, n in right), Decimal(0))
        if divisor: return [(ref, n/divisor) for ref, n in left]
    raise AnalysisError('NON_ADDITIVE_CHART', '该公式不能精确拆成增减项，请用 FORMULA 关系图。')


def comparison_scenes(artifact):
    from .presentation import scene
    from .column_scenes import column_scene
    from .validation import render_parts
    known = {f['factId']: f for f in artifact['facts']}
    values = {v['derivedId']: v for v in artifact['derivedValues']}
    charts = []
    for view in artifact['viewPlan']:
        if view['type'] not in ('WATERFALL', 'RECONCILIATION'): continue
        for ref in view['valueRefs']:
            value = values[ref]
            if view['type'] == 'RECONCILIATION':
                check = value['reconciliation']
                data = [('父级来源值', check['parentValue']), ('直属下级合计', check['childSum']),
                        ('差额（父级 − 下级合计）', check['difference'])]
                if any(n is None for _, n in data):
                    raise AnalysisError('INCOMPLETE_COMPARISON', '差额图需要完整可用的父项与直属子项。')
                rows = [{'rowKey': 'node_'+digest([ref,i])[:24], 'label': label, 'exact': n,
                         'display': number_text(n, view.get('numberFormat'), grouping=True), 'unit': unit_for(value,view)}
                        for i,(label,n) in enumerate(data)]
                chart = (scene if view.get('orientation') == 'horizontal' else column_scene)(rows, 'RECONCILIATION', '')
                chart.update(viewId=view['viewId'], title=view.get('title','汇总对照'), collapsed=view.get('collapsed',False))
                charts.append(chart)
                continue
            nodes = selected_nodes(value['formulaTrace'], view)
            labels = formula_labels(value['formulaTrace'], known, view)
            for node in nodes:
                if not node.get('expression') or node.get('boundary'): continue
                if node['localCheck']['value'] is None:
                    raise AnalysisError('INCOMPLETE_COMPARISON', '瀑布图需要完整的本层依赖值。')
                fact = known[node['factRef']]
                dependencies = {d['address']: known[d['factRef']] for d in node['dependencies']}
                rows, spans, total = [], [], Decimal(0)
                try:
                    with localcontext() as context:
                        context.prec = 512
                        context.traps[Inexact] = True
                        for index,(address, coefficient) in enumerate(affine(Expression(node['expression']).tree)):
                            item = dependencies[address] if address else fact
                            delta = decimal_value(item['value'])*coefficient if address else coefficient
                            name = labels[item['factId']] if address else '公式常数'
                            if address and abs(coefficient) != 1: name += ' × ' + str(abs(coefficient))
                            rows.append({'rowKey':'node_'+digest([ref,node['factRef'],index])[:24], 'label':name,
                                'exact':str(delta), 'display':number_text(str(delta),view.get('numberFormat'),grouping=True), 'unit':unit_for(fact,view)})
                            spans.append((total,total+delta)); total += delta
                        if total != decimal_value(node['localCheck']['value']):
                            raise AnalysisError('COMPOSITION_MISMATCH','公式增减项与本层计算结果不一致。')
                        observed = decimal_value(node['observed'])
                        difference = observed-total
                        totals = [(labels[fact['factId']] + ('（按公式计算）' if difference else ''),total,(Decimal(0),total),True)]
                        if show_checks(artifact, view, node['localCheck']):
                            totals += [('差额（来源 − 计算）',difference,(total,observed),False),
                                       (labels[fact['factId']]+'（来源值）',observed,(Decimal(0),observed),True)]
                        for label,number,span,is_total in totals:
                            rows.append({'rowKey':'node_'+digest([ref,node['factRef'],label])[:24], 'label':label,
                                'total':is_total,'exact':str(number), 'display':number_text(str(number),view.get('numberFormat'),grouping=True), 'unit':unit_for(fact,view)})
                            spans.append(span)
                except DecimalException as exc:
                    raise AnalysisError('NON_ADDITIVE_CHART','该公式无法精确拆成增减项，请用 FORMULA 关系图。') from exc
                chart = (scene if view.get('orientation') == 'horizontal' else column_scene)(rows,'WATERFALL','',spans)
                caption = next((c for c in view.get('nodeDescriptions',[]) if c['nodeRef']==node['factRef']),None)
                chart.update(viewId=view['viewId'], title=view.get('title','增减构成')+' · '+labels[fact['factId']],
                    description=render_parts({**known,**values},caption['parts'])[0] if caption else '', collapsed=view.get('collapsed',False))
                charts.append(chart)
    return charts
