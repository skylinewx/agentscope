"""Responsive semantic relationship diagrams within the approved report cards."""
from html import escape as e
from .formatting import inline_markup


def relationship(scene):
    graph=scene['relationship']
    def node(item, result=False):
        operator = ('<span class="relation-operator">'+e(item['operator'])
                    + (' '+e(item['coefficient'])+' ×' if item.get('coefficient') else '')+'</span>') if item.get('operator') else ''
        magnitude = (f'<div class="relation-magnitude" role="img" aria-label="本图相对贡献大小，影响值 {e(item["contribution"], quote=True)}" title="影响值 {e(item["contribution"], quote=True)}">'
                     f'<span style="width:{item["weight"]}%"></span></div>') if 'weight' in item else ''
        return (f'<div class="relation-node {"relation-result" if result else ""}" data-tone="{e(item.get("tone", "positive"), quote=True)}"'
                + (' data-emphasis="true"' if item.get('emphasis') else '') + '>'
                + f'<p class="relation-label">{operator}{e(item["label"])}</p><p class="relation-value">{e(item["display"])}</p>'
                + magnitude + (f'<p class="relation-caption">{e(item["note"])}</p>' if item.get('note') else '') + '</div>')
    equation = f'<p class="relation-equation">{e(graph["equation"])}</p>' if graph['equation'] else ''
    checks=''.join(f'<div><p class="relation-caption">{e(c["label"])}</p><p class="relation-value">{e(c["display"])}</p></div>' for c in graph['checks'])
    inputs='<div class="relation-inputs">'+''.join(node(item) for item in graph['inputs'])+'</div>'
    output='<div class="relation-output">'+node(graph['result'],True)+(f'<div class="relation-checks">{checks}</div>' if checks else '')+'</div>'
    first,last=(output,inputs) if scene['type']=='HIERARCHY' else (inputs,output)
    return (equation + f'<div class="relation-layout relation-{e(graph.get("layout", "compact"), quote=True)}">'
            + first + '<div class="relation-arrow" aria-hidden="true">→</div>' + last + '</div>'
            + (f'<p class="relation-caption">{e(graph["note"])}</p>' if graph['note'] else ''))


def chart_content(chart):
    from .report import svg
    # Preserve the drawing's label size; wide comparisons scroll in their card.
    drawing = '' if 'relationship' in chart else svg(chart).replace('<svg ', f'<svg style="min-width:{chart["width"]}px" ', 1)
    graphic=('<div class="relation-container">'+relationship(chart)+'</div>') if 'relationship' in chart else f'<div class="chart" role="region" aria-label="图表，可横向滚动" tabindex="0">{drawing}</div>'
    return graphic + (f'<p class="chart-question">{inline_markup(chart["description"])}</p>' if chart['description'] else '')


def chart_html(chart):
    title=e(chart.get('title') or '数值图表')
    if chart.get('collapsed'):
        return f'<details class="diagram-fold"><summary>{title}</summary><div class="diagram-content">{chart_content(chart)}</div></details>'
    return f'<article class="diagram-content"><h3>{title}</h3>{chart_content(chart)}</article>'


def metric_html(metrics):
    return '<div class="metrics">'+''.join(
        f'<article class="metric"><p class="metric-label">{e(m["label"])}</p><p class="metric-value">{e(m["display"])}</p>'
        + (f'<p class="metric-detail">{e(m["unit"])}</p>' if m['unit'] else '') + '</article>' for m in metrics)+'</div>'


def render_claims(document, indexes):
    text = ''.join(f'<p data-claim-index="{i}">{inline_markup(document["claims"][i])}</p>' for i in indexes)
    if text and document['claimsCollapsed']:
        text = '<details class="diagram-fold"><summary>展开分析解读</summary><div class="diagram-content">' + text + '</div></details>'
    return text


def render_sections(document, layout):
    from .report_tables import render_details
    scenes=document['scenes']
    overview, content, reached_charts = [], [], False
    for section in document['sections']:
        metrics_only=all(not b['sceneIndexes'] and not b['tableIndexes'] for b in section['blocks'])
        if not metrics_only: reached_charts=True
        if layout == 'table' and not section['claimIndexes'] and not any(b['metricIndexes'] or b['tableIndexes'] for b in section['blocks']): continue
        body=(f'<h2>{e(section["title"])}</h2>' if section['title'] else '')
        if section['description']: body+=f'<p class="chart-question">{inline_markup(section["description"])}</p>'
        local = render_claims(document, section['claimIndexes'])
        local = f'<div class="topic-claims" data-position="{section["claimsPosition"]}">{local}</div>' if local else ''
        if section['claimsPosition'] == 'before': body += local
        for block in section['blocks']:
            if layout == 'table' and not block['metricIndexes'] and not block['tableIndexes']: continue
            # Each model caption belongs to exactly one view block.
            body+='<div class="presentation-block">'
            if block['title'] and (block['metricIndexes'] or len(block['sceneIndexes'])>1): body+=f'<h3>{e(block["title"])}</h3>'
            if block['description']: body+=f'<p class="chart-question">{inline_markup(block["description"])}</p>'
            if block['metricIndexes']: body+=metric_html([document['metrics'][i] for i in block['metricIndexes']])
            if layout=='standard': body+=''.join(chart_html(scenes[i]) for i in block['sceneIndexes'])
            if block['tableIndexes']: body+=render_details(document, block['tableIndexes'], grouped=True)
            body+='</div>'
        if section['claimsPosition'] == 'after': body += local
        if metrics_only and not reached_charts:
            overview.append(f'<section class="metric-group" data-width="{section["width"]}">'+body+'</section>')
        else:
            content.append(f'<section class="card card--main presentation-group" data-width="{section["width"]}">'+body+'</section>')
    overview_html='<section class="overview presentation-grid" aria-label="核心结论">'+''.join(overview)+'</section>' if overview else ''
    if document['chartUnavailable'] and layout=='standard':
        content.append(f'<section class="card"><h2>图表说明</h2><p class="note">{e(document["chartUnavailable"])}</p></section>')
    return overview_html, '<div class="grid presentation-grid">'+''.join(content)+'</div>' if content else ''
