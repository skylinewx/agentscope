"""Model-authored documents bound to immutable analysis values, without a layout DSL."""
from __future__ import annotations

from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from .models import AnalysisError, digest
from .formatting import number_text, validate_number_format
from .validation import selected_value

DOCUMENT_VERSION = 'free-html-1'
MAX_SOURCE_BYTES = 512 * 1024
MAX_DOCUMENT_BYTES = 768 * 1024
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")
KEY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
SLOT = re.compile(r'\{\{([A-Za-z][A-Za-z0-9_-]{0,63})\}\}')
PARTIAL_SLOT = 'analysisPartialReason'


def fail(code, message, path='/document', **details):
    raise AnalysisError(code, message, {'path': path, **details})


def freeze_source(document, workspace):
    """Resolve a workspace file once. Repairs and final publication use this copy."""
    result = deepcopy(document)
    if 'path' not in result:
        return result
    if workspace is None:
        fail('DOCUMENT_WORKSPACE_REQUIRED', '当前宿主未提供可读取的展示工作区。', '/document/path')
    raw = result['path']
    if not isinstance(raw, str) or len(raw) > 1024 or '\\' in raw or ':' in raw or raw.startswith('/'):
        fail('INVALID_DOCUMENT_PATH', 'path 使用当前工作区内的相对 .html 路径。', '/document/path')
    parts = raw.removeprefix('./').split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts) or not raw.lower().endswith('.html'):
        fail('INVALID_DOCUMENT_PATH', 'path 使用当前工作区内的相对 .html 路径，不允许父目录。', '/document/path')
    root = Path(workspace).resolve()
    candidate = root
    for part in parts:
        candidate /= part
        if candidate.is_symlink() or candidate.is_junction():
            fail('INVALID_DOCUMENT_PATH', '文档路径不能经过链接或重解析目录。', '/document/path')
    if not candidate.resolve().is_relative_to(root):
        fail('INVALID_DOCUMENT_PATH', '文档必须位于当前工作区。', '/document/path')
    try:
        with candidate.open('rb') as stream:
            content = stream.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            fail('DOCUMENT_TOO_LARGE', '页面源文件超过 512 KiB，请精简页面。', '/document/path')
        result['html'] = content.decode('utf-8-sig')
    except (OSError, UnicodeError) as exc:
        fail('DOCUMENT_READ_FAILED', '无法读取 UTF-8 HTML 文件；核对当前工作区路径。', '/document/path')
    result.pop('path')
    return result


class DocumentCheck(HTMLParser):
    """Check document boundaries and self-contained resources; never choose styling."""
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.head_end = None
        self.counts = {'html': 0, 'head': 0, 'body': 0}
        self.ids = set()
        self.scripts = 0
        self.raw_text = None
        self.text_slots = set()

    def handle_data(self, data):
        keys = set(SLOT.findall(data))
        if keys and self.raw_text:
            fail('INVALID_DOCUMENT_BINDING', '占位符仅用于 HTML 文本；脚本使用 window.analysisData。', '/document/html')
        self.text_slots.update(keys)

    def handle_endtag(self, tag):
        if tag == self.raw_text:
            self.raw_text = None

    def handle_comment(self, data):
        if SLOT.search(data):
            fail('INVALID_DOCUMENT_BINDING', '数值占位符不能放在注释中。', '/document/html')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        if SLOT.search(self.get_starttag_text()):
            fail('INVALID_DOCUMENT_BINDING', '占位符仅用于 HTML 文本；图形属性通过 window.analysisData 设置。', '/document/html')
        if tag in ('script', 'style'):
            self.raw_text = tag
        pairs = dict(attrs)
        if len(pairs) != len(attrs):
            fail('INVALID_DOCUMENT_HTML', '同一元素不能包含重复属性。', '/document/html')
        if tag in self.counts:
            self.counts[tag] += 1
        if tag == 'head':
            line, col = self.getpos()
            self.head_end = sum(map(len, self.lines[:line-1])) + col + len(self.get_starttag_text())
        if tag in ('base', 'iframe', 'frame', 'frameset', 'object', 'embed', 'portal'):
            fail('DOCUMENT_EXTERNAL_CONTENT', f'页面不接受 {tag}；图形在当前文档内生成。', '/document/html')
        if tag == 'meta' and 'http-equiv' in pairs:
            fail('DOCUMENT_POLICY', '页面无需自带 http-equiv；隔离与资源策略由宿主提供。', '/document/html')
        if tag == 'script':
            self.scripts += 1
            if self.head_end is None or 'src' in pairs:
                fail('DOCUMENT_EXTERNAL_CONTENT', '脚本须为 head/body 内的内联脚本，不能加载外部脚本。', '/document/html')
        if tag == 'link':
            fail('DOCUMENT_EXTERNAL_CONTENT', '样式和字体使用文档内资源，不加载外部 link。', '/document/html')
        if 'id' in pairs:
            value = pairs['id']
            if value in self.ids or value in ('analysis-host-data', 'analysis-host-runtime'):
                fail('INVALID_DOCUMENT_HTML', '页面存在重复或宿主保留的 id。', '/document/html', elementId=value)
            self.ids.add(value)
        for attr, value in attrs:
            if attr in ('src', 'href', 'xlink:href', 'action', 'formaction', 'poster', 'srcset', 'ping'):
                value = (value or '').strip()
                local = attr in ('href', 'xlink:href') and value.startswith('#')
                image = tag == 'img' and attr == 'src' and value.startswith(('data:image/png;', 'data:image/jpeg;', 'data:image/webp;', 'data:image/gif;'))
                if not local and not image:
                    fail('DOCUMENT_EXTERNAL_CONTENT', '页面仅使用内部锚点、内联图形及内嵌图片。', '/document/html', attribute=attr)


def compile_document(artifact, document):
    if not isinstance(document, dict) or set(document) - {'title', 'html', 'bindings', 'datasets'}:
        fail('INVALID_DOCUMENT', 'document 需要 title、html/path 和 bindings/datasets。')
    title, source = (document.get(k) for k in ('title', 'html'))
    bindings = document.get('bindings', [])
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        fail('INVALID_DOCUMENT', '页面需要简短的业务标题。', '/document/title')
    if not isinstance(source, str) or not source.strip():
        fail('INVALID_DOCUMENT', '页面 HTML 不能为空。', '/document/html')
    if len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        fail('DOCUMENT_TOO_LARGE', '页面源文件超过 512 KiB。', '/document/html')
    if not isinstance(bindings, list) or len(bindings) > 2000:
        fail('INVALID_DOCUMENT', 'bindings 最多 2000 个真实数值引用。', '/document/bindings')
    from .document_data import compile_datasets
    datasets, dataset_refs = compile_datasets(artifact, document.get('datasets', []))
    if not bindings and not datasets:
        fail('INVALID_DOCUMENT', 'bindings 与 datasets 至少提供一个真实引用。')
    known = {f['factId']: f for f in artifact['facts']}
    known.update({v['derivedId']: v for v in artifact['derivedValues']})
    payload, refs = {}, set()
    for i, binding in enumerate(bindings):
        path = f'/document/bindings/{i}'
        if not isinstance(binding, dict) or set(binding) - {'key', 'valueRef', 'field', 'nodeRef', 'format'}:
            fail('INVALID_DOCUMENT_BINDING', '绑定仅接受 key/valueRef/field/nodeRef/format。', path)
        key, ref = binding.get('key'), binding.get('valueRef')
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in payload or key == PARTIAL_SLOT:
            fail('INVALID_DOCUMENT_BINDING', 'key 需要唯一的字母开头标识，最长 64 字符。', path+'/key')
        if not isinstance(ref, str) or ref not in known:
            fail('INVALID_VALUE_REF', '绑定必须引用当前版本的事实或计算结果。', path+'/valueRef')
        item = known[ref]
        if item.get('calculationValidation', 'PASS') != 'PASS':
            fail('UNSUPPORTED_CLAIM', '该计算未通过执行条件检查，不能作为展示数字。', path+'/valueRef')
        try:
            if 'format' in binding: validate_number_format(binding['format'])
            raw = selected_value(item, binding.get('field'), binding.get('nodeRef'))
            formatted = '' if raw is None else (raw if binding.get('field') == 'unit' else number_text(raw, binding.get('format'), grouping=True))
        except AnalysisError as exc:
            raise AnalysisError(exc.code, str(exc), {**(exc.details or {}), 'path': path}) from exc
        payload[key] = {'value': raw, 'display': formatted, 'unit': item.get('unit') or '',
                        'objectLabel': item.get('objectLabel', ''), 'measure': ' / '.join(item.get('headers', [])),
                        'valueState': item.get('valueState'), 'valueRef': ref,
                        'field': binding.get('field', 'value'), 'nodeRef': binding.get('nodeRef')}
        refs.add(ref)
    used = set(SLOT.findall(source))
    partial = artifact.get('partialReason')
    unknown = used - set(payload) - ({PARTIAL_SLOT} if partial else set())
    if unknown:
        fail('INVALID_DOCUMENT_BINDING', 'HTML 中的占位标识未在 bindings 声明。', '/document/html', unknownKeys=sorted(unknown))
    checked = DocumentCheck(source)
    checked.feed(source)
    checked.close()
    if checked.counts != {'html': 1, 'head': 1, 'body': 1} or checked.head_end is None:
        fail('INVALID_DOCUMENT_HTML', '提交完整 HTML 文档，包含唯一的 html、head、body。', '/document/html')
    if partial and PARTIAL_SLOT not in checked.text_slots:
        fail('PARTIAL_RESULT_UNDISCLOSED', '部分交付须在可见正文放置 {{analysisPartialReason}}，位置与样式由模型决定。', '/document/html')
    updates = {item['taskId']: item['refs'] for item in artifact.get('workRefs', [])}
    for task in artifact.get('plan', {}).get('workPlan', []):
        if task['kind'] != 'COMPUTATION': continue
        for ref in updates.get(task['taskId'], task['refs']):
            value = known.get(ref, {})
            if (value.get('operator') in ('SORT', 'RANK') and value.get('calculationValidation') == 'PASS'
                    and ref not in dataset_refs):
                fail('ORDER_DATASET_REQUIRED', '已声明的排序须绑定 document.datasets 并遍历 rows，不能仅绑定金额后手排。',
                     '/document/datasets', valueRef=ref, taskId=task['taskId'])
    data_json = json.dumps({'values': payload, 'sets': datasets}, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    frame_key = digest({'source': source, 'data': payload, 'sets': datasets, 'partialReason': partial})
    runtime = Path(__file__).with_name('document_runtime.js').read_text('utf-8').replace('FRAME_KEY', json.dumps(frame_key))
    injection = (f'<meta http-equiv="Content-Security-Policy" content="{escape(CSP, quote=True)}">'
                 f'<script id="analysis-host-data" type="application/json">{data_json}</script>'
                 f'<script id="analysis-host-runtime">{runtime}</script>')
    # Never substitute inside the injected source labels or numeric payload.
    replace = lambda part: SLOT.sub(lambda m: escape(str(partial if m[1] == PARTIAL_SLOT else payload[m[1]]['display'])), part)
    compiled = replace(source[:checked.head_end]) + injection + replace(source[checked.head_end:])
    if len(compiled.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        fail('DOCUMENT_TOO_LARGE', '绑定后的页面超过展示容量，请缩小展示范围。')
    return {'version': DOCUMENT_VERSION, 'title': title.strip(), 'html': compiled,
            'contentDigest': digest(compiled), 'frameKey': frame_key,
            'bindings': deepcopy(bindings), 'valueRefs': sorted(refs | dataset_refs),
            'datasets': datasets, 'datasetBindings': deepcopy(document.get('datasets', [])),
            'staticBindingKeys': sorted(checked.text_slots), 'hasScripts': bool(checked.scripts),
            'bindingCount': len(bindings), 'datasetCount': len(datasets)}


def diagnostics(document):
    return {'visualInspectionPerformed': False, 'warnings': [], 'document': {
        'version': DOCUMENT_VERSION, 'bindingCount': document['bindingCount'], 'datasetCount': document['datasetCount'],
        'contentDigest': document['contentDigest'], 'sourceValuesValidated': True,
        'freeTextSemanticsValidated': False, 'scriptOutputValidated': False,
        'note': '已校验引用与计算条件；实际图形、自由文字及交互需查看预览。'}}


def static_fact_refs(artifact):
    doc = artifact.get('freeDocument', {})
    keys = set(doc.get('staticBindingKeys', []))
    known = {v['derivedId']: v for v in artifact['derivedValues']}
    facts = {f['factId'] for f in artifact['facts']}
    result = set()
    for binding in doc.get('bindings', []):
        if binding['key'] not in keys: continue
        ref, field = binding['valueRef'], binding.get('field', 'value')
        if ref in facts and field == 'value': result.add(ref)
        if field == 'sourceValue' and ref in known:
            value = known[ref]
            if 'formulaTrace' in value: result.add(binding.get('nodeRef') or value['formulaTrace']['targetFactRef'])
            elif 'reconciliation' in value: result.add(value['inputRefs'][0])
    return result


def standalone_html(document):
    """The downloaded artifact retains the same isolated content as the inline card."""
    html = escape(document['html'], quote=True)
    title = escape(document['title'])
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>'
            '<style>html,body{margin:0;width:100%;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style>'
            '</head><body>'
            f'<iframe data-analysis-document="free-html-1" title="{title}" sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc="{html}"></iframe>'
            '</body></html>')
