"""Bounded relationship scenes from frozen formulas and captured hierarchies."""
from .formulas import Expression
from .formatting import formula_text, formula_labels, unit_for, number_text, view_label
from .formula_selection import selected_nodes
from .models import AnalysisError, decimal_value, digest
from .relations import hierarchy
from .check_visibility import has_difference, show_checks
from .tables import measure_label, object_label


def amount(value, unit='', options=None):
    return ('—' if value is None else number_text(value, options, grouping=True)) + (' ' + unit if unit else '')


def validate_relationship_scene(scene):
    def bounded(value):
        if isinstance(value, str) and len(value.encode('utf-16-le', errors='surrogatepass')) // 2 > 16000:
            raise AnalysisError('DIAGRAM_LIMIT', '关系图文本超过显示容量，请设置业务简称或缩小展示范围。')
        if isinstance(value, dict):
            for item in value.values(): bounded(item)
        elif isinstance(value, list):
            for item in value: bounded(item)
    if len(scene['relationship']['inputs']) > 100:
        raise AnalysisError('DIAGRAM_LIMIT', '单幅关系图超过 100 个输入，请分组展示。')
    bounded(scene)
    return scene


def relationship_scene(kind, title, description, result, inputs, equation='', status=''):
    """Semantic text flows in HTML; the layout draws connectors without text coordinates."""
    return validate_relationship_scene({'type': kind, 'title': title, 'description': description, 'width': 600,
            'height': 0, 'zeroBaseline': 0, 'rowKeys': [x['key'] for x in inputs] + [result['key']],
            'primitives': [], 'relationship': {'equation': equation, 'inputs': inputs, 'result': result,
                                             'note': status, 'checks': []}})


def relation_scenes(artifact):
    known = {f['factId']: f for f in artifact['facts']}
    derived = {v['derivedId']: v for v in artifact['derivedValues']}
    scenes = []
    def item(fact, metric=True, note='', label=None):
        value = fact.get('value') if fact.get('valueState') == 'PRESENT' else None
        return {'key': 'node_' + digest(fact['factId'])[:24],
                'label': label or view_label(view, fact['factId'], measure_label(fact) if metric else object_label(fact)),
                'display': amount(value, unit_for(fact, view), view.get('numberFormat')),
                'emphasis': fact['factId'] in view.get('emphasisRefs', []),
                'tone': 'negative' if value is not None and decimal_value(value) < 0 else 'positive',
                'note': note or ('数据暂缺' if value is None else '')}
    for view in artifact['viewPlan']:
        seen = set()
        from .validation import render_parts
        description = ''  # A view caption belongs once to its containing block, not every formula node.
        group_size = view.get('maxInputsPerScene', 12)
        if view['type'] == 'FORMULA':
            for ref in view['valueRefs']:
                trace = derived[ref]['formulaTrace']
                boundaries = set(trace.get('externalBoundaries', []))
                nodes = {n['factRef']: n for n in trace['nodes']}
                labels = formula_labels(trace, known, view)
                def node_label(fact):
                    return labels[fact['factId']]
                primary = next((n['factRef'] for n in trace['nodes'] if len(n['dependencies']) > 1), trace['targetFactRef'])
                for node in selected_nodes(trace, view):
                    if not node['expression'] or node['factRef'] in seen or node.get('boundary'): continue
                    seen.add(node['factRef'])
                    fact = known[node['factRef']]
                    names, inputs = {}, []
                    for dep in node['dependencies']:
                        child = known.get(dep['factRef'])
                        names[dep['address']] = node_label(child) if child else '未取得的项目'
                        child_note = '已按本表数值计入；外表公式未展开' if artifact['technicalDetails'] and dep['factRef'] in boundaries else ''
                        inputs.append(item(child, note=child_note, label=node_label(child)) if child else {'key': 'node_'+digest(dep)[:24], 'label': '未取得的项目', 'display': '—', 'note': '需补充来源明细'})
                    from .formula_contributions import contributions
                    impacts = contributions(node, known, view)
                    for dep, element in zip(node['dependencies'], inputs): element.update(impacts.get(dep.get('factRef'), {}))
                    equation = ''
                    try: equation = formula_text(Expression(node['expression']).tree, names) + ' = ' + node_label(fact)
                    except AnalysisError: pass  # Unsupported source syntax remains visible as a gap, never evaluated here.
                    title = view.get('title') or object_label(fact) + ' · ' + node_label(fact)
                    if view.get('title') and len(view.get('nodeRefs', [])) != 1: title += ' · ' + node_label(fact)
                    # Split only wide fan-outs; each continuation explicitly says how many dependencies are shown.
                    groups = [inputs[i:i+group_size] for i in range(0, len(inputs), group_size)] or [[]]
                    for index, group in enumerate(groups):
                        partial = f'（依赖组 {index+1}/{len(groups)}，共 {len(inputs)} 项）' if len(groups) > 1 else ''
                        checks = node['localCheck']
                        visible_checks = show_checks(artifact, view, checks)
                        graph = relationship_scene('FORMULA', title+partial, description,
                            item(fact, label=node_label(fact), note='原值' if visible_checks and has_difference(checks) else ''), group, equation)
                        graph['relationship']['layout'] = view.get('relationLayout', 'compact')
                        graph['relationship']['checks'] = [
                            {'label': '本层计算值', 'display': amount(checks['value'], unit_for(fact, view), view.get('numberFormat'))},
                            {'label': '本层差额（原值 − 本层计算值）', 'display': amount(checks['difference'], unit_for(fact, view), view.get('numberFormat'))}] if visible_checks else []
                        caption = next((c for c in view.get('nodeDescriptions', []) if c['nodeRef'] == node['factRef']), None)
                        graph['description'] = render_parts({**known, **derived}, caption['parts'])[0] if caption else ''
                        graph['viewId'] = view['viewId']
                        graph['collapsed'] = view.get('collapsed', False)
                        graph['primary'] = node['factRef'] == primary and index == 0
                        scenes.append(graph)
        elif view['type'] == 'HIERARCHY':
            parent, children, complete = hierarchy(artifact, view)
            groups = [children[i:i+group_size] for i in range(0, len(children), group_size)] or [[]]
            for index, group in enumerate(groups):
                title = view.get('title') or object_label(parent) + ' · ' + measure_label(parent)
                partial = f'（第 {index+1}/{len(groups)} 组）' if len(groups) > 1 else ''
                graph = relationship_scene('HIERARCHY', title+partial, description, item(parent, False),
                    [item(f, False) for f in group], status='' if complete else '当前仅列示部分直接下级')
                graph['viewId'] = view['viewId']
                graph['relationship']['layout'] = view.get('relationLayout', 'compact')
                if view.get('comparisonRef'):
                    comparison = derived[view['comparisonRef']]['reconciliation']
                    graph['relationship']['checks'] = [
                        {'label': '直属下级合计', 'display': amount(comparison['childSum'], unit_for(parent, view), view.get('numberFormat'))},
                        {'label': '差额（父级 − 下级合计）', 'display': amount(comparison['difference'], unit_for(parent, view), view.get('numberFormat'))}] if show_checks(artifact, view, comparison) else []
                graph['collapsed'] = view.get('collapsed', False)
                scenes.append(graph)
    if len(scenes) > 100:
        raise AnalysisError('DIAGRAM_LIMIT', '关系图超过 100 幅，请按分析目标缩小展示范围。')
    return [validate_relationship_scene(scene) for scene in scenes]
