"""Period trends for reports that explicitly request the existing structured components."""
from .models import AnalysisError
from .period_trends import TREND_HELP, selected_trends, trend_catalog


def scenes_for_views(artifact, views):
    views = [view for view in views if view.get('trendRef')]
    if not views:
        return []
    charts = selected_trends(trend_catalog(artifact), [view['trendRef'] for view in views])
    return [series_scene(view['viewId'], charts[view['trendRef']], series) for view in views
            for series in charts[view['trendRef']]['series']]


def series_scene(view_id, chart, series):
    periods = chart['periods']
    start, end = periods[0]['order'], periods[-1]['order']
    title = chart['metric'] + ('（' + chart['unit'] + '）' if chart['unit'] else '') + ' · ' + series['label']
    marks = [{'kind': 'text', 'x': 12, 'y': 16, 'text': chart['range'][1], 'tone': 'label'},
             {'kind': 'text', 'x': 12, 'y': 172, 'text': chart['range'][0], 'tone': 'label'}]
    last_tick = -1000
    for i, period in enumerate(periods):
        x = 80 + 460 * (period['order'] - start) / (end - start)
        if i in (0, len(periods) - 1) or x - last_tick >= 110 and 540 - x >= 110:
            marks.append({'kind': 'text', 'x': x - 36, 'y': 198, 'text': period['label'], 'tone': 'label'})
            last_tick = x
    previous, refs = None, []
    for point in series['points']:
        period = periods[point['periodIndex']]
        if point['value'] is None:
            previous = None
            continue
        x = 80 + 460 * (period['order'] - start) / (end - start)
        y = 162 - 136 * point['height']
        if previous and period['order'] == previous[0] + 1:
            marks.append({'kind': 'line', 'x1': previous[1], 'y1': previous[2], 'x2': x, 'y2': y, 'tone': 'positive'})
        marks.append({'kind': 'rect', 'x': x-2, 'y': y-2, 'width': 4, 'height': 4, 'tone': 'positive', 'rowKey': point['ref']})
        refs.append(point['ref'])
        previous = period['order'], x, y
    if len(marks) > 1000:
        raise AnalysisError('DIAGRAM_LIMIT', '所选时期序列超过固定组件的图元容量，可通过正文折线图交付完整序列。',
                            {'hint': TREND_HELP})
    return {'type': 'PERIOD_TREND', 'viewId': view_id, 'title': title, 'description': chart['context'],
            'width': 620, 'height': 210, 'zeroBaseline': 0, 'primitives': marks, 'rowKeys': refs}
