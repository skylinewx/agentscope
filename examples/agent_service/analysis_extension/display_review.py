"""Advisory display diagnostics; never rewrite the model's presentation."""
from decimal import Decimal
from .presentation_options import prepare_views
from .formatting import formula_labels
from .formula_selection import selected_nodes
from .check_visibility import has_difference, show_checks


def review(artifact, document):
    prepared = prepare_views(artifact)
    known = {f['factId']: f for f in artifact['facts']}
    derived = {v['derivedId']: v for v in artifact['derivedValues']}
    warnings = []
    def warn(code, view, message, **details):
        warnings.append({'code': code, 'viewId': view['viewId'], 'message': message, **details})
    for view in prepared['viewPlan']:
        if view['type'] in ('FORMULA', 'WATERFALL'):
            for ref in view['valueRefs']:
                trace = derived[ref]['formulaTrace']
                labels = formula_labels(trace, known, view)
                selected = selected_nodes(trace, view)
                displayed = {n['factRef']: n for n in selected if n.get('expression') and not n.get('boundary')}
                for node in selected:
                    if not node.get('expression') or node.get('boundary'): continue
                    used = [node['factRef']] + [d['factRef'] for d in node['dependencies'] if d.get('factRef') in known]
                    long = [{'valueRef': key, 'label': labels[key]} for key in used if len(labels[key]) > 20]
                    if long: warn('LONG_FORMULA_LABELS', view, '部分公式标签较长，请判断是否需要业务简称。', nodeRef=node['factRef'], labels=long)
                    if len(node['dependencies']) == 1:
                        warn('SINGLE_INPUT_LAYER', view, '该层只有一个输入，请判断是否提供了值得单独展示的信息。', nodeRef=node['factRef'])
                    local, recursive = node['localCheck']['value'], node.get('recalculated')
                    if local is not None and recursive is not None and Decimal(local) != Decimal(recursive):
                        warn('FORMULA_CALCULATION_BASES_DIFFER', view,
                             '本层计算与逐层展开结果不同。图注及结论按所述口径引用：原值 sourceValue、本层 localValue、逐层 value；本层一致不能说成逐层一致。',
                             valueRef=ref, nodeRef=node['factRef'], sourceValue=node['observed'], localValue=local,
                             value=recursive, localDifference=node['localCheck']['difference'], difference=node['difference'])
                    if has_difference(node['localCheck']) and not show_checks(artifact, view, node['localCheck']):
                        warn('FORMULA_DIFFERENCE_HIDDEN', view,
                             '本图原值与公式测算值不同，但 showChecks=false；请在本节点说明中就近区分原值、测算值与差额，或开启 showChecks。差额不是业务构成项目。',
                             valueRef=ref, nodeRef=node['factRef'], sourceValue=node['observed'], localValue=local,
                             localDifference=node['localCheck']['difference'], difference=node['localCheck']['difference'])
                    if view['type'] == 'WATERFALL':
                        for dep in node['dependencies']:
                            child = displayed.get(dep.get('factRef'))
                            if not child or not has_difference(child['localCheck']) or show_checks(artifact, view, child['localCheck']): continue
                            warn('WATERFALL_SOURCE_TRANSITION', view,
                                 '所选下层瀑布图止于本层计算值，上层却使用该项目的来源值，两者有差额。核对组说明和节点图注；可开启下层差额对照、明确区分两种口径，或调整选层。不能将两张图描述为无差额衔接的来源值路径。',
                                 valueRef=ref, nodeRef=child['factRef'], consumerNodeRef=node['factRef'],
                                 sourceValue=child['observed'], localValue=child['localCheck']['value'],
                                 localDifference=child['localCheck']['difference'])
        if view['type'] == 'BAR_VALUES':
            values = [abs(Decimal(known[ref]['value'])) for ref in view['valueRefs'] if known[ref].get('value') is not None and Decimal(known[ref]['value']) != 0]
            if values and max(values) / min(values) > 1000:
                warn('WIDE_VALUE_RANGE', view, '同轴数值差异较大，小值可能难以辨认；可按问题选择局部比较，明确范围与尺度。')
        if view.get('includeDetails') and not view.get('collapsed'):
            warn('EXPANDED_DETAILS', view, '图后附有展开的明细，请判断是否重复、是否需要折叠。')
    scenes = document['scenes']
    # Compare the projected order, including global ORDER and per-view ordering.
    views = {v['viewId']:v for v in prepared['viewPlan']}
    for table in document['tables']:
        for scene in scenes:
            if scene['type'] not in ('BAR_VALUES','BAR_RANK'): continue
            other=scene['rowKeys']
            for column in table['columns']:
                order=[r['cells'][column['key']]['rowKey'] for r in table['rows'] if column['key'] in r['cells'] and r['cells'][column['key']]['exact'] is not None]
                if set(order)==set(other) and order!=other:
                    warn('PAIRED_ORDER_DIFFERS', views[scene['viewId']], '相同数据的表格与图表顺序不同；若表达同一排名，请复用相同 orderRef。', relatedViewId=table['viewId'])
    return {'visualInspectionPerformed': False, 'advisoryOnly': True, 'warnings': warnings[:100],
            'warningCount': len(warnings), 'sceneCount': len(scenes), 'tableCount': len(document['tables']),
            'scenes': [{'viewId': s.get('viewId'), 'type': s['type'], 'width': s['width'], 'height': s['height'],
                        'inputCount': len(s.get('relationship', {}).get('inputs', []))} for s in scenes],
            'next': '检查预览和适用 Skill；仅调整有必要的项目。没有实际查看页面，不能声称视觉检查完成。'}
