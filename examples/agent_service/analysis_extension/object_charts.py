"""Each model-selected comparison owns its references, order, labels and axis."""
from .engine import calculate
from .formatting import unit_for, view_label
from .validation import validate_source_chart
from .presentation_options import ordered_for_view


def object_charts(artifact, facts, rows, registry, params):
    from .presentation import scene, display, opaque, CHART_ROWS
    known = {f['factId']: f for f in artifact['facts']}
    values = {v['derivedId']: v for v in artifact['derivedValues']}
    charts, unavailable = [], []
    for plan in artifact['viewPlan']:
        kind = plan['type']
        if kind not in ('BAR_VALUES', 'BAR_RANK'): continue
        refs = set(plan['valueRefs']) if kind == 'BAR_VALUES' else {
            ref for value in plan['valueRefs'] for ref in values.get(value, {}).get('sourceFactIds', [])}
        if kind == 'BAR_VALUES':
            validate_source_chart([known[ref] for ref in plan['valueRefs']], artifact['scope'])
        selected = [f for f in facts if f['factId'] in refs]
        if kind == 'BAR_RANK' and selected:
            scope = {**artifact['scope'], 'members': list(dict.fromkeys(f['objectId'] for f in selected))}
            original = values[plan['valueRefs'][0]]
            operation = {'id': 'viewRank', 'operator': 'RANK',
                         **{key: original[key] for key in ('comparabilityRef', 'direction') if key in original}}
            rank = calculate(selected, scope, [operation], registry)[0]
            if rank['calculationValidation'] != 'PASS': selected = []
        title = plan.get('title') or ('来源原值分布' if kind == 'BAR_VALUES' else '所选对象对比')
        selected_ids = {f['factId'] for f in selected}
        chart_rows = [{**row, 'unit': unit_for(row, plan), 'display': display(row['exact'], plan.get('numberFormat')),
                       'label': view_label(plan, fact['factId'], row['label'])}
                      for row, fact in zip(rows, facts) if fact['factId'] in selected_ids and fact['valueState'] == 'PRESENT']
        if not chart_rows:
            unavailable.append(title + '：当前范围没有可用于此图的数值。')
            continue
        if params['sort'] == 'published':
            ordered = ordered_for_view(artifact, plan, selected)
            order = {opaque('row_', [artifact['analysisRef'], f['factId']]): i for i, f in enumerate(ordered)}
            chart_rows.sort(key=lambda row: order[row['rowKey']])
        note = f'（展示 {CHART_ROWS} / {len(chart_rows)} 项）' if len(chart_rows) > CHART_ROWS else ''
        from .column_scenes import column_scene
        render_scene = column_scene if plan.get('orientation') == 'vertical' else scene
        chart = render_scene(chart_rows[:CHART_ROWS], kind, note)
        chart.update(viewId=plan['viewId'], title=title, collapsed=plan.get('collapsed', False))
        charts.append(chart)
    return charts, unavailable
