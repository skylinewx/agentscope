"""Approved report layout, filled only from the saved presentation document."""
from html import escape
from pathlib import Path

from .models import AnalysisError
from .report_tables import render_details
from .formatting import inline_markup

REPORT_MAX_BYTES = 10 * 1024 * 1024
REPORT_TEMPLATE_VERSION = "business-reference-1"
REPORT_CSS = Path(__file__).with_name("report.css").read_text(encoding="utf-8")
COLORS = {"positive": "#185fa5", "negative": "#c3502f", "axis": "#b7c1ce", "label": "#606b7b", "value": "#1f2328", "surface": "#f1f5f9"}


def svg(scene):
    if not scene:
        return ""
    accessible_title = scene.get('title') or scene['description'] or '数值图表'
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {scene["width"]} {scene["height"]}" role="img" aria-label="{escape(accessible_title, quote=True)}">',
             f'<title>{escape(accessible_title)}</title>']
    for item in scene["primitives"]:
        color = COLORS[item["tone"]]
        if item["kind"] == "text":
            parts.append(f'<text x="{item["x"]}" y="{item["y"]}" style="fill:{color}" font-size="13">{escape(item["text"])}</text>')
        elif item["kind"] == "line":
            parts.append(f'<line x1="{item["x1"]}" x2="{item["x2"]}" y1="{item["y1"]}" y2="{item["y2"]}" stroke="{color}"/>')
        elif item["kind"] == "rect":
            parts.append(f'<rect x="{item["x"]}" y="{item["y"]}" width="{item["width"]}" height="{item["height"]}" fill="{color}"/>')
        else:
            raise AnalysisError("UNSUPPORTED_VERSION", "不支持该图元版本。")
    return "".join(parts) + "</svg>"


def render(document, layout="standard"):
    if layout not in ("standard", "table"):
        raise AnalysisError("INVALID_LAYOUT", "报告布局仅支持 standard 或 table。")
    if document.get('freeDocument'):
        if layout != 'standard':
            raise AnalysisError('DOCUMENT_SCOPE_FIXED', '自由页面按原版导出；调整布局需发布新版本。')
        from .free_document import standalone_html
        return standalone_html(document['freeDocument'])
    e = escape
    title = e(document['reportTitle'] or ("筛选范围分析报告" if document["isFiltered"] else "完整分析报告"))
    from .report_scenes import render_sections, render_claims
    overview, charts_html = render_sections(document, layout)
    claims = render_claims(document, document['summaryClaimIndexes'])
    summary_html = (f'<section class="card summary" data-position="{document["summaryPosition"]}" aria-labelledby="summary-title">'
                    f'<h2 id="summary-title">{e(document["summaryTitle"])}</h2>{claims}</section>') if claims else ''
    support = {"SUPPORTED": "该类型主张有依据", "NEEDS_EVIDENCE": "待补证据", "CONTRADICTED": "新证据不再支持原观察"}
    insights = []
    for finding in document["findings"] if document["technicalDetails"] else []:
        explanations = "".join(f'<p>{"待验证的业务原因假设：" if item["kind"] == "business_cause_hypothesis" else "说明："}{e(item["statement"])}</p>' +
            (f'<p>待确认：{e("；".join(item["missingEvidence"]))}</p>' if item["missingEvidence"] else "") for item in finding["explanations"])
        actions = "".join(f'<p><strong>下一步：</strong>{e(a["goal"])}</p><p>判断用途：{e(a["expectedDecision"])}</p><p>所需资料：{e("；".join(a["requiredEvidence"]))}</p><p>{e(a["availabilityReason"])}</p><p>处理顺序依据：{e(a["priorityBasis"])}</p>'
            for a in document["actions"] if finding["findingId"] in a["findingRefs"])
        insights.append(f'<article><h3>{e(support[finding["support"]])}</h3><p>{e(finding["statement"])}</p><p>依据：{e(finding["basis"])}；版本：{e(finding["ruleVersion"])}；适用条件：{e(finding["applicability"])}</p>{explanations}{actions}</article>')
    if not document['technicalDetails']:
        insights = [f'<article><h3>{e(a["goal"])}</h3><p>{inline_markup(a["expectedDecision"])}</p></article>' for a in document['actions']]
    findings_html = '<section class="card findings"><h2>原发布范围的发现与建议</h2>' + ''.join(insights) + '</section>' if insights else ''
    limits = "".join(f'<li>{e(c)}</li>' for c in document["limitations"])
    details_html = render_details(document)
    sources = "".join(f'<p>数据来源：{e(source)}</p>' for source in document["sources"])
    summary_note = f'<p class="note">{e(document["claimsScopeLabel"])}</p>' if document['claimsScopeLabel'] else ''
    source_section = (f'<aside class="sources" aria-labelledby="sources-title"><h2 id="sources-title">读数提示与来源说明</h2>{sources}<ul>{limits}</ul></aside>') if document['technicalDetails'] else ''
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="analysis-report-template" content="{REPORT_TEMPLATE_VERSION}">
<title>{title}</title><style>{REPORT_CSS}</style></head>
<body><main class="wrap presentation-composition">
<header aria-labelledby="report-title"><h1 id="report-title">{title}</h1><p class="scope">{e(document['scopeLabel'])}</p></header>
{summary_note}
{summary_html if document['summaryPosition'] == 'start' else ''}
{overview}{charts_html}{findings_html}
{details_html}
{summary_html if document['summaryPosition'] == 'end' else ''}
{source_section}
<footer class="note report-footer">分析版本：{e(document['analysisRef'].rsplit(':', 1)[-1])} · 发布时刻：{e(document.get('publishedAt') or '未记录')}</footer>
</main></body></html>'''
    if len(html.encode("utf-8")) > REPORT_MAX_BYTES:
        raise AnalysisError("REPORT_TOO_LARGE", "完整报告超过 10 MiB，未交付；请显式选择较小范围生成另一个报告。")
    return html
