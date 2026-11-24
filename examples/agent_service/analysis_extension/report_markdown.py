"""Compile a business Markdown draft against the current frozen analysis and approved report style."""
from decimal import Decimal
from html import escape
import re
from urllib.parse import quote
from unicodedata import normalize

from markdown_it import MarkdownIt

from .display_tables import appendix_tables, conditions, visible_table
from .formatting import NUMERIC_TYPES, group_written_amounts, grouped_value
from .models import AnalysisError, digest
from .report import REPORT_CSS


_MONEY_UNIT = re.compile(r'(?:[十百千万亿]*元|[十百千万亿]*(?:美元|港元|欧元|日元|英镑)|%|％|百分点)')
_SIGNED = re.compile(r'[-−](?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?([%％]?)')
_PERIOD_COLUMN = re.compile(r'时期|期间|季度|月份|年度|半年|周次|日期|(?:^|[^a-z])(?:period|quarter|month|year|week|date)(?:$|[^a-z])', re.I)
_COORDINATE_HEADING = re.compile(r'^(?:时期|期间|年份|年度|季度|月份|日期|(?:fiscal )?year|month|period|quarter|date)$|(?:编码|代码|标识|(?:^|[_\s])(?:id|code))$', re.I)


def financial_header(label):
    unit = re.search(r'[（(]([^）)]+)[）)]$', label)
    return bool(unit and _MONEY_UNIT.fullmatch(unit[1]))


def draft_numeric_fields(artifact):
    """Carry source-declared measure labels and units into otherwise untyped Markdown cells."""
    labels, units = {}, set()
    def register(label, kind, unit):
        if label and kind:
            labels.setdefault(label, set()).add(kind)
        if unit and kind in NUMERIC_TYPES:
            units.add(unit)
    for fact in artifact.get('facts', []):
        register(fact.get('measureTitle'), fact.get('valueType', 'DECIMAL_STRING'), fact.get('unit'))
    for result in artifact.get('derivedValues', []):
        for col in result.get('columns') or []:
            if isinstance(col, dict) and not col.get('dimension'):
                register(col.get('label') or col.get('name'), col.get('type'), col.get('unit'))
    return {'numericLabels': {label for label, kinds in labels.items() if kinds <= NUMERIC_TYPES}, 'numericUnits': units}


def cell_class(value, financial):
    value = str(value or '').strip()
    if value.startswith(('关注：', '关注:')):
        return 'business-attention'
    match = _SIGNED.fullmatch(value)
    if match and (financial or match[1]) and Decimal(value[1:].rstrip('%％').replace(',', '')) != 0:
        return 'business-negative'
    return ''


def style_markdown_tables(state):
    headers, index = [], 0
    for i, token in enumerate(state.tokens):
        if token.type == 'inline':
            for child in token.children or []:
                if child.type == 'text':
                    child.content = group_written_amounts(child.content)
        if token.type == 'table_open':
            headers = []
        elif token.type == 'tr_open':
            index = 0
        elif token.type in ('th_open', 'td_open'):
            inline = state.tokens[i + 1]
            value = ''.join(child.content for child in inline.children or [] if child.type in ('text', 'code_inline'))
            if token.type == 'th_open':
                financial = financial_header(value)
                unit = re.search(r'[（(]([^）)]+)[）)]$', value)
                numeric = (financial or value in state.env.get('numericLabels', ()) or
                           bool(unit and unit[1] in state.env.get('numericUnits', ())))
                heading = value[:unit.start()].strip() if unit else value
                headers.append((financial, numeric and not _COORDINATE_HEADING.search(heading)))
            else:
                financial, numeric = headers[index] if index < len(headers) else (False, False)
                if numeric:
                    for child in inline.children or []:
                        if child.type == 'text':
                            child.content = grouped_value(child.content)
                name = cell_class(value, financial)
                if name:
                    token.attrJoin('class', name)
            index += 1


def parser():
    md = MarkdownIt('commonmark', {'html': False}).enable('table')
    md.core.ruler.after('inline', 'business_cells', style_markdown_tables)
    # The approved template scrolls individual wide tables inside their card.
    md.renderer.rules['table_open'] = lambda *_: '<div class="table-wrap"><table>\n'
    md.renderer.rules['table_close'] = lambda *_: '</table></div>\n'
    return md


def format_query_markdown(markdown, evidence):
    """Apply the report's typed number formatting to a text reply, preserving its original Markdown."""
    if not re.search(r'\d{4}', markdown):
        return markdown
    facts = ({**fact, 'measureTitle': fact.get('measureTitle') or fact.get('title')}
             for source in (evidence or {}).get('sources', []) for fact in source.get('facts', []))
    tokens = parser().parse(markdown, draft_numeric_fields({'facts': facts}))
    lines = markdown.splitlines(keepends=True)
    cells = None
    for i, token in enumerate(tokens):
        if token.type == 'tr_open' and token.map:
            line = token.map[0]
            cells = re.split(r'(?<!\\)\|', lines[line])
            column = int(not cells[0].strip(' \t>'))
        elif token.type == 'tr_close' and cells is not None:
            lines[line] = '|'.join(cells)
            cells = None
        elif token.type == 'td_open' and cells is not None:
            inline = tokens[i + 1]
            # Only a plain or emphasized numeric cell may change. Leave links, code and labels verbatim.
            children = inline.children or []
            if column < len(cells) and cells[column].strip() == inline.content and all(
                    c.type in ('text', 'strong_open', 'strong_close', 'em_open', 'em_close') for c in children):
                texts = [c.content for c in children if c.type == 'text' and c.content]
                original = inline.content.strip('*_')
                if len(texts) == 1 and texts[0] != original and texts[0].replace(',', '') == original.replace(',', ''):
                    cells[column] = cells[column].replace(original, texts[0], 1)
            column += 1
        elif token.type == 'inline' and cells is None and token.map:
            if all(c.type in ('text', 'softbreak', 'strong_open', 'strong_close', 'em_open', 'em_close') for c in token.children or []):
                for index in range(*token.map):
                    lines[index] = group_written_amounts(lines[index])
    return ''.join(lines)


def page(title, body):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>{REPORT_CSS} .business-negative,.business-attention{{color:var(--subtract)}} .business-attention{{font-weight:600}} details.source-data>summary{{cursor:pointer}}</style></head><body>'
            f'<main class="wrap presentation-composition"><header><h1>{escape(title)}</h1></header>{body}</main></body></html>')


def table_page(markdown):
    return page('完整查询结果', '<section class="card">' + parser().render(markdown) + '</section>')


def catalog(artifact, trends):
    choices = {}
    for value in artifact.get('derivedValues', []):
        operator = value.get('operator')
        formula = operator == 'FORMULA_CHECK' and (value.get('formulaTrace') or {}).get('nodes')
        if not formula and value.get('calculationValidation') != 'PASS':
            continue
        if formula:
            kind = 'waterfall'
        elif operator in ('SORT', 'RANK', 'DESCRIBE'):
            kind = 'comparison'
        elif operator == 'PYTHON' and value.get('outputKind') in ('TABLE', 'CHART_DATA'):
            kind = 'table'
        else:
            continue
        ref = value['derivedId']
        target = (value.get('formulaTrace') or {}).get('targetFactRef')
        subject = next((f for f in artifact.get('facts', []) if f.get('factId') == target), {})
        title = value.get('label') or value.get('title') or subject.get('objectLabel') or value.get('stepId') or ref
        choices['charts/' + quote(ref, safe='') + '.svg'] = (kind, ref, title)
    for ref, trend in trends.items():
        choices['charts/' + ref + '.svg'] = ('trend', ref, trend['metric'])
    return choices


def figure_links(choices):
    labels = {'waterfall': '公式构成', 'comparison': '数值比较', 'trend': '时期趋势', 'table': '计算明细表（引用即展示全表）'}
    return [{'名称': title, '类型': labels[kind], '引用': f'![{labels[kind]}]({url})'}
            for url, (kind, _, title) in choices.items()]


def html_table(headers, rows, set_key=None, financial=()):
    def e(value):
        return escape('空值' if value is None else str(value))
    result = ['<div class="table-wrap"><table><thead><tr>']
    result.extend('<th scope="col">' + e(label) + '</th>' for label in headers)
    attr = f' data-analysis-set="{escape(set_key)}"' if set_key else ''
    result.append('</tr></thead><tbody' + attr + '>')
    for ref, cells in rows:
        attr = f' data-analysis-ref="{escape(ref)}"' if ref and set_key else ''
        result.append('<tr' + attr + '>')
        for index, val in enumerate(cells):
            allowed = financial.get(ref, ()) if isinstance(financial, dict) else financial
            css = cell_class(val, index in allowed)
            result.append((f'<td class="{css}">' if css else '<td>') + e(val) + '</td>')
        result.append('</tr>')
    return ''.join(result) + '</tbody></table></div>'


def appendix(artifact, counts=None):
    tables = appendix_tables(artifact)
    if not tables:
        return ''
    output = []
    rows = records = numeric = 0
    for table, roles in tables:
        name = table.context.get('formName') or '查询结果'
        role = '、'.join({'PRIMARY': '主体', 'REFERENCE': '对照'}.get(r, '') for r in roles if r)
        headers, body = visible_table(table)
        rows += len(body)
        records += len(table.facts)
        numeric += sum(f.get('valueState') == 'PRESENT' and f.get('valueType') == 'DECIMAL_STRING' for f in table.facts)
        output.append('<h3>' + escape(name + ('（' + role + '）' if role else '')) + '</h3>')
        context = '；'.join(k + '：' + v for k, v in conditions(table))
        if context:
            output.append('<p>' + escape(context) + '</p>')
        financial = {len(headers) - len(table.columns) + i for i, col in enumerate(table.columns)
                     if col.get('valueType') == 'DECIMAL_STRING' and _MONEY_UNIT.fullmatch(col.get('unit') or '')}
        output.append(html_table(headers, ((None, row) for row in body), financial=financial))
        if not body:
            output.append('<p>本次查询没有数据。</p>')
    if counts is not None:
        counts.update(tables=len(tables), rows=rows, records=records, numeric=numeric)
    title = f'{len(tables):,} 张表，{rows:,} 行，{numeric:,} 个有效数值'
    return ('<section class="card"><h2>查询数据</h2><details class="source-data"><summary>' + title +
            '</summary>' + ''.join(output) + '</details></section>')


def dataset_html(kind, key, dataset, periods=None):
    attr = escape(key)
    if kind == 'waterfall':
        return f'<div data-analysis-waterfalls="{attr}"></div>'
    cols = dataset.get('columns', [])
    columns = [c for c in cols if c['name'] != 'factRef' and not c.get('dimension')]
    if dataset['kind'] == 'ORDERED':
        columns = [c for c in columns if c['name'] in ('objectLabel', 'display', 'unit')]
    headers = [c.get('label', c['name']) + (f"（{c['unit']}）" if c.get('unit') else '') for c in columns]
    time_fields = {c['name'] for c in columns if periods and c.get('type') == 'STRING'
                   and _PERIOD_COLUMN.search(c.get('label') or c['name'])
                   and not re.search(r'编码|代码|标识|\b(?:id|code)\b', c.get('label') or c['name'], re.I)}
    def display_row(row):
        values = row['values']
        cells = [values.get(c['name']) for c in columns]
        for index, col in enumerate(columns):
            if col['name'] in time_fields and isinstance(cells[index], str):
                cells[index] = periods.get(cells[index], cells[index])
            elif cells[index] is not None and col.get('type') in NUMERIC_TYPES:
                if not _COORDINATE_HEADING.search(col.get('label') or col['name']):
                    cells[index] = grouped_value(cells[index])
        return values.get('factRef'), cells
    rows = [display_row(row) for row in dataset['rows']]
    financial = {i for i, c in enumerate(columns) if c.get('type') == 'DECIMAL_STRING' and
                 (_MONEY_UNIT.fullmatch(c.get('unit') or '') or financial_header(headers[i]))}
    if dataset['kind'] == 'ORDERED':
        positions = {i for i, c in enumerate(columns) if c['name'] == 'display'}
        financial = {row['values'].get('factRef'): positions for row in dataset['rows']
                     if _MONEY_UNIT.fullmatch(row['values'].get('unit') or '')}
    table = html_table(headers, rows, key if kind == 'comparison' else None, financial)
    common = dataset.get('commonMemberNames') or []
    caption = '<p>共同条件：' + escape('、'.join(common)) + '</p>' if common else ''
    if dataset.get('unavailable'):
        table += html_table(headers, [display_row(row) for row in dataset['unavailable']])
    if kind == 'comparison':
        return caption + f'<div data-markdown-comparison="{attr}"></div>' + table
    return caption + table


def compile_source(artifact, document, appendix_counts=None):
    from .document_data import compile_datasets
    from .period_trends import axis_label, choices, observed_period, trend_catalog
    from .free_document import MAX_SOURCE_BYTES
    source = document.get('markdown')
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > MAX_SOURCE_BYTES:
        raise AnalysisError('INVALID_DOCUMENT', '请提交非空、最多 512 KiB 的 Markdown 正文。', {'path': '/document/markdown'})
    md = parser()
    trends = trend_catalog(artifact)
    directory = catalog(artifact, trends)
    specifications = list(document.get('datasets', []))
    keys = {item['valueRef']: item['key'] for item in specifications}
    occupied = {item['key'] for item in specifications}
    figures = {}
    periods = {code: axis_label(code) for code in {observed_period(f) for f in artifact.get('facts', [])} if code}
    def figure(url, caption):
        if url not in directory:
            raise AnalysisError('UNKNOWN_REPORT_FIGURE', '图引用未绑定到本次分析，请从可用图表复制 Markdown 引用。',
                                {'path': '/document/markdown', 'figure': url, 'choices': figure_links(directory)})
        kind, ref, title = directory[url]
        if kind == 'trend':
            markup = f'<div data-analysis-trend="{escape(ref)}"></div>'
        else:
            if ref not in keys:
                key = 'figure_' + digest(ref)[:16]
                if key in occupied:
                    raise AnalysisError('INVALID_DOCUMENT_DATASET', '图表集合键与现有绑定重复，请更换自定义键。')
                keys[ref] = key
                occupied.add(key)
                specifications.append({'key': key, 'valueRef': ref})
            key = keys[ref]
            data, _ = compile_datasets(artifact, [next(s for s in specifications if s['key'] == key)])
            markup = dataset_html(kind, key, data[key], periods)
        figures[url] = title
        return '<figure><figcaption>' + escape(caption or title) + '</figcaption>' + markup + '</figure>'

    tokens = md.parse(source, draft_numeric_fields(artifact))
    sections, part = [], []
    top_headings = [t for t in tokens if t.type == 'heading_open' and t.tag == 'h1' and t.level == 0]
    def title_key(value):
        return re.sub(r'[\s:·、，,]+', '', normalize('NFKC', value)).casefold()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == 'heading_open' and token.level == 0:
            # A single opening H1 is the draft's document title. Multiple peer H1s
            # in an existing draft are sections; keep them under the published title.
            if i == 0 and (token.tag == 'h1' and len(top_headings) == 1 or
                           title_key(tokens[i + 1].content) == title_key(document['title'])):
                i += 3
                continue
            if token.tag == 'h1':
                token.tag = tokens[i + 2].tag = 'h2'
        if token.type == 'heading_open' and token.level == 0 and token.tag in ('h1', 'h2') and part:
            sections.append(part)
            part = []
        if (token.type == 'paragraph_open' and i + 2 < len(tokens) and tokens[i+1].type == 'inline'
                and len(tokens[i+1].children or []) == 1 and tokens[i+1].children[0].type == 'image'):
            img = tokens[i+1].children[0]
            from markdown_it.token import Token
            block = Token('html_block', '', 0)
            url = img.attrGet('src')
            # Only an adjacent, identical bound table is mechanically redundant.
            if not (part and getattr(part[-1], 'meta', {}).get('tableUse') == (url, img.content)):
                block.content = figure(url, img.content)
                if directory[url][0] == 'table':
                    block.meta['tableUse'] = (url, img.content)
                part.append(block)
            i += 3
            continue
        if any(child.type == 'image' for child in token.children or []):
            raise AnalysisError('INVALID_REPORT_FIGURE', '每个图表引用使用独立段落，方便定位和呈现。')
        part.append(token)
        i += 1
    if part:
        sections.append(part)
    rendered = [md.renderer.render(part, md.options, {}) for part in sections]
    # Mechanical coverage is limited to actual formula results and unambiguous views.
    extra = [u for u, (kind, _, _) in directory.items() if kind == 'waterfall' and u not in figures]
    if trends and not any(directory[u][0] == 'trend' for u in figures):
        if len(trends) > 1:
            raise AnalysisError('DOCUMENT_TREND_REQUIRED', '报告涉及多个时期。请按本次分析重点选择一条折线图，将该行 Markdown 引用追加到正文。',
                                {'path': '/document/markdown', 'trendChoices': choices(trends)[:8],
                                 'repairHint': '用返回的 repairRef、requestKey、finalize:true 和 repairs。每个补丁使用 op=appendMarkdown、path=/document/markdown、value=所选行的 Markdown 原文。',
                                 'requiresBuild': False, 'requiresSourceQuery': False})
        extra.extend(u for u in directory if directory[u][0] == 'trend')
    if not figures and not extra:
        extra.extend(u for u in directory if directory[u][0] == 'comparison')
    extra_html = ''.join(figure(url, directory[url][2]) for url in extra)
    start = rendered.pop(0) if rendered else ''
    end = rendered.pop() if rendered else ''
    if artifact.get('partialReason') and '{{analysisPartialReason}}' not in source:
        end += '<p>{{analysisPartialReason}}</p>'
    body = '<section class="overview">' + start + '</section><div class="grid">'
    body += ''.join('<section class="card card--main">' + part + '</section>' for part in rendered)
    if extra_html:
        body += '<section class="card card--main">' + extra_html + '</section>'
    body += '</div>'
    if end:
        body += '<section class="card summary">' + end + '</section>'
    body += appendix(artifact, appendix_counts)
    return page(document['title'], body), specifications, list(figures)
