"""对冻结页面批量修正文本文字，不重新生成模板，也不修改数值绑定。"""
from collections import Counter
from copy import deepcopy
from html import escape, unescape
from html.parser import HTMLParser
import json
import re

from .free_document import SLOT
from .models import AnalysisError, digest


class _TextNodes(HTMLParser):
    """记录原始源码中的文本区间；标签、属性、脚本及样式均不是修正目标。"""

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0] + [match.end() for match in re.finditer('\n', source)]
        self.raw_tag = None
        self.nodes = []
        self.feed(source)
        self.close()
        source_digest = digest(source)
        for node in self.nodes:
            node['text'] = unescape(source[node['start']:node['end']])
            node['textRef'] = 'text_' + digest([source_digest, node['start'], node['end']])[:24]

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.raw_tag = tag

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag == self.raw_tag:
            self.raw_tag = None

    def _append(self, length):
        if self.raw_tag:
            return
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        if self.nodes and self.nodes[-1]['end'] == start:
            self.nodes[-1]['end'] = start + length
        else:
            self.nodes.append({'start': start, 'end': start + length})

    def handle_data(self, data):
        self._append(len(data))

    def handle_entityref(self, name):
        self._reference('&' + name)

    def handle_charref(self, name):
        self._reference('&#' + name)

    def _reference(self, raw):
        line, column = self.getpos()
        end = self.offsets[line - 1] + column + len(raw)
        self._append(len(raw) + int(self.source[end:end + 1] == ';'))


def text_targets(source, statement):
    """为单个静态文本节点中真实存在的原句提供定位。"""
    if not statement:
        return []
    return [{'textRef': node['textRef'], 'oldText': statement,
             'context': node['text'][max(0, node['text'].index(statement) - 120):
                                     node['text'].index(statement) + len(statement) + 120]}
            for node in _TextNodes(source).nodes
            if statement in node['text'] and len(node['text']) <= 12000]


def statement_fragments(source, statement, compiled_document):
    """按宿主实际绑定值精确匹配完整原句，再返回原有文本节点；不猜测语义相似度。"""
    if not statement or not compiled_document:
        return []
    marker = '<script id="analysis-host-data" type="application/json">'
    compiled = compiled_document.get('html', '')
    if compiled.count(marker) != 1:
        return []
    try:
        payload = json.loads(compiled.split(marker, 1)[1].split('</script>', 1)[0])
        displays = {key: value['display'] for key, value in payload['values'].items()}
    except (KeyError, TypeError, ValueError):
        return []
    nodes = _TextNodes(source).nodes
    characters, owners = [], []
    for index, node in enumerate(nodes):
        rendered = SLOT.sub(lambda match: str(displays.get(match[1], match[0])), node['text'])
        # 与浏览器一样折叠连续空白，保留词间边界；不删除数字、标点或否定词。
        for character in rendered:
            if character.isspace():
                if characters and characters[-1] != ' ':
                    characters.append(' ')
                    owners.append(index)
            else:
                characters.append(character)
                owners.append(index)
    full_text = ''.join(characters)
    quote = re.sub(r'\s+', ' ', statement).strip()
    if not quote:
        return []
    selected = set()
    for match in re.finditer(re.escape(quote), full_text):
        selected.update(owners[match.start():match.end()])
    return [
        {'textRef': node['textRef'], 'oldText': node['text'], 'matchMode': 'STATEMENT_FRAGMENT',
         'context': '这是原句对应的现有静态文本片段；仅修改必要片段，保留其中的数值占位符。'}
        for index, node in enumerate(nodes)
        if index in selected and len(node['text']) <= 12000 and SLOT.sub('', node['text']).strip()
    ]


def attach_text_targets(issues, document, compiled_document=None):
    """定位只添加提示，不能因为无法定位而改变业务审查结论。"""
    result = deepcopy(issues)
    source = (document or {}).get('html')
    if not isinstance(source, str):
        return result
    for issue in result:
        targets = []
        for statement in [issue.get('statement', ''), *issue.get('relatedStatements', [])]:
            located = text_targets(source, statement) or statement_fragments(source, statement, compiled_document)
            for target in located:
                same_node = [item for item in targets if item['textRef'] == target['textRef']]
                if any(item['oldText'] == target['oldText'] or item.get('matchMode') == 'STATEMENT_FRAGMENT'
                       for item in same_node):
                    continue
                # 一个完整片段已经覆盖该节点内的其他摘录，合并为一次修改，避免重叠。
                if target.get('matchMode') == 'STATEMENT_FRAGMENT':
                    targets = [item for item in targets if item['textRef'] != target['textRef']]
                targets.append(target)
        issue['textRepairable'] = bool(targets)
        if targets:
            issue['textTargets'] = targets
    return result


def apply_text_repairs(source, edits, available_bindings=()):
    """所有匹配都基于同一冻结源码，全部校验成功后一次应用，禁止半批修改。"""
    if not isinstance(source, str):
        raise AnalysisError('INVALID_REPAIR', '文字修正需要已冻结的 HTML 页面。')
    nodes = _TextNodes(source).nodes
    replacements = {}
    for edit_index, edit in edits:
        def fail(message, **details):
            raise AnalysisError('INVALID_REPAIR', message,
                                {'path': f'/repairs/{edit_index}', **details})

        value = edit.get('value')
        if (set(edit) != {'op', 'path', 'value'} or edit['path'] != '/document/html'
                or not isinstance(value, dict) or set(value) - {'oldText', 'newText', 'textRef'}
                or not {'oldText', 'newText'} <= set(value)
                or not isinstance(value['oldText'], str) or not value['oldText'].strip()
                or not isinstance(value['newText'], str) or not value['newText'].strip()
                or max(len(value['oldText']), len(value['newText'])) > 12000
                or 'textRef' in value and not isinstance(value['textRef'], str)):
            fail('replaceText 使用 /document/html，value 提供非空 oldText/newText；textRef 可选。')
        old, new = value['oldText'], value['newText']
        old_slots, new_slots = Counter(SLOT.findall(old)), Counter(SLOT.findall(new))
        # 允许补充引用冻结页面已有的金额（如差额），无需仅为复用已知数据改写整页。
        # 原有数值仍须保留；新字段或绑定定义变化交由原发布修复入口处理。
        if old_slots - new_slots or set(new_slots - old_slots) - set(available_bindings):
            fail('文字修正须保留原数值占位符；可补充引用页面已有绑定，新绑定请使用原发布修复入口。')
        candidates = [node for node in nodes if old in node['text']
                      and ('textRef' not in value or value['textRef'] == node['textRef'])]
        count = sum(len(re.findall('(?=' + re.escape(old) + ')', node['text'])) for node in candidates)
        if count != 1:
            fail('原文不存在或出现多次，未应用任何修改；使用当前 textTargets 定位，或合并为完整原段落。',
                 matchCount=count, textTargets=text_targets(source, old))
        node = candidates[0]
        start = node['text'].index(old)
        changes = replacements.setdefault(node['textRef'], (node, []))[1]
        end = start + len(old)
        if any(start < slot.end() and end > slot.start()
               and not (start <= slot.start() and end >= slot.end()) for slot in SLOT.finditer(node['text'])):
            fail('文字修正不能截断或修改数值占位符。')
        if any(start < other_end and end > other_start for other_start, other_end, _ in changes):
            fail('同批文字修正区间重叠，请合并成一次段落修正。')
        changes.append((start, end, new))
    result = source
    # 从后向前替换，原区间及未涉及的 HTML 字节保持不变。
    for node, changes in sorted(replacements.values(), key=lambda item: item[0]['start'], reverse=True):
        text = node['text']
        for start, end, new in sorted(changes, reverse=True):
            text = text[:start] + new + text[end:]
        result = result[:node['start']] + escape(text, quote=False) + result[node['end']:]
    return result
