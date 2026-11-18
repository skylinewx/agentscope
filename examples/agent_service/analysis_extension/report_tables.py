"""Fill the approved detail-card component with grouped business table data."""
from html import escape
import re
from .formatting import inline_markup


def column_class(label):
    """本组件数据列均为数值事实；排名、序号按财务表格惯例居中。"""
    title = re.sub(r'[（(][^()（）]*[)）]\s*$', '', label.strip()).strip()
    if (re.search(r'(序号|行号|排名|名次|位次)$', title)
            or re.search(r'(^|\s)(rank|ranking)$', title, re.I)
            or re.fullmatch(r'(row\s*(no\.?|number)|serial\s*(no\.?|number)|no\.?|#)', title, re.I)):
        return 'ordinal'
    if (re.search(r'(编码|代码|编号|名称|说明|备注|描述)$', title)
            or title in ('年度', '年份', '月份', '日期', '时期', '期间', '单位', '机构', '项目', '科目', '板块')
            or re.search(r'(^|[\s_])(code|id|name|description|remarks?|notes?)$', title, re.I)
            or title.lower() in ('year', 'month', 'date', 'period')):
        return 'text'
    return 'number'


def render_details(document, indexes=None, grouped=False):
    output = []
    represented = {i for s in document['sections'] for b in s['blocks'] for i in b['tableIndexes']}
    for index, table in enumerate(document['tables']):
        if (indexes is None and index in represented) or (indexes is not None and index not in indexes): continue
        if not table['rows']: continue
        notes = any(row.get('note') or len(row['cells']) < len(table['columns']) or any(cell.get('state') not in ('', '已取得') for cell in row['cells'].values()) for row in table['rows'])
        headers = '<th scope="col" class="text">' + ('项目' if table['kind'] != 'values' else '名称') + '</th>'
        for column in table['columns']:
            title = column['label'] + ('（' + column['unit'] + '）' if column['unit'] else '')
            headers += '<th scope="col" class="' + column_class(column['label']) + '">' + escape(title) + '</th>'
        if notes: headers += '<th scope="col" class="text">说明</th>'
        rows = []
        for row in table['rows']:
            cells = '<td class="text">' + escape(row['label']) + '</td>'
            explanations = [row['note']] if row.get('note') else []
            for column in table['columns']:
                value = row['cells'].get(column['key'])
                shown_value = value['display'] if value else '—'
                if value and value['unit'] and value['unit'] != column['unit']:
                    shown_value += ' ' + value['unit']
                cells += '<td class="' + column_class(column['label']) + '"' + (' data-row-key="' + escape(value['rowKey'], quote=True) + '"' if value else '') + '>' + escape(shown_value) + '</td>'
                if value and value.get('state') not in ('', '已取得'):
                    explanations.append(column['label'] + '：' + value['state'])
                elif value is None:
                    explanations.append(column['label'] + '：—')
            if notes: cells += '<td class="text">' + escape('；'.join(explanations)) + '</td>'
            rows.append('<tr data-row-key="' + escape(row['memberKey'], quote=True) + '">' + cells + '</tr>')
        def render_rows(selected):
            return ('<div class="table-wrap" role="region" aria-label="明细表，窄屏可横向滚动" tabindex="0"><table><thead><tr>'
                    + headers + '</tr></thead><tbody>' + ''.join(selected) + '</tbody></table></div>')
        if table['collapsed']:
            content = f'<details><summary>查看明细（{len(rows)} 项）</summary>{render_rows(rows)}</details>'
        else:
            size = table['pageSize']
            content = render_rows(rows[:size])
            if len(rows) > size:
                content += f'<details><summary>查看其余 {len(rows) - size} 项（共 {len(rows)} 项）</summary>{render_rows(rows[size:])}</details>'
        title_id = 'detail-title-' + str(index)
        note = '<p class="chart-question">' + inline_markup(table['note']) + '</p>' if table.get('note') and not grouped else ''
        classes = 'detail-card' if grouped else 'card detail-card'
        heading = 'h3' if grouped else 'h2'
        output.append(f'<section class="{classes}" aria-labelledby="{title_id}"><{heading} id="{title_id}">{escape(table["title"])}</{heading}>{note}{content}</section>')
    return '\n'.join(output)
