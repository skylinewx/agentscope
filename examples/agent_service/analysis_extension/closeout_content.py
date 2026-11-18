"""展示修复到限时保存已审查的静态说明，不发布失败图形或重新生成解释。"""
from copy import deepcopy
from html.parser import HTMLParser

from .models import digest


class ReviewedParagraphs(HTMLParser):
    """仅提取完整的说明段落；动态图形、表格和脚本由事实组件替代。"""
    blocks = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote'}
    excluded = {'head', 'script', 'style', 'table', 'svg', 'canvas', 'noscript'}
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__()
        self.stack, self.parts, self.paragraphs = [], [], []
        self.block_depth = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in self.excluded
                  or 'hidden' in attributes or attributes.get('aria-hidden') == 'true')
        if tag in self.void:
            if tag == 'br' and self.block_depth is not None:
                self.parts.append('\n')
            return
        self.stack.append((tag, hidden))
        if not hidden and tag in self.blocks and self.block_depth is None:
            self.block_depth = len(self.stack)
            self.parts = []

    def handle_endtag(self, tag):
        position = next((index for index in range(len(self.stack) - 1, -1, -1)
                         if self.stack[index][0] == tag), None)
        if position is None:
            return
        if self.block_depth is not None and position < self.block_depth:
            text = ''.join(self.parts).strip()
            if text:
                self.paragraphs.append(text)
            self.block_depth, self.parts = None, []
        del self.stack[position:]

    def handle_data(self, data):
        if self.block_depth is not None and self.stack and not self.stack[-1][1]:
            self.parts.append(data)


def evidence_digest(artifact):
    return digest({key: artifact.get(key) for key in ('facts', 'derivedValues', 'scope', 'planDigest')})


def preview_review_status(service, artifact, closeout_reason=None):
    fallback = 'UNAVAILABLE' if closeout_reason in ('REVIEW_UNAVAILABLE', 'REVIEW_CAPACITY') else 'NOT_REVIEWED'
    if service.turn is None:
        return fallback
    from .review_control import progress
    proof = progress(service.turn).get('lastPublicationPreview', {})
    if (proof.get('analysisRef') == artifact['analysisRef']
            and proof.get('evidenceDigest') == evidence_digest(artifact)
            and (not artifact.get('freeDocument')
                 or artifact['freeDocument']['contentDigest'] == proof.get('contentDigest'))):
        return proof.get('semanticReview', 'NOT_REVIEWED')
    return fallback


def reviewed_publication(service, artifact):
    """核对宿主保存的精确预览、证据及审查状态，不采用模型自报通过。"""
    if service.turn is None:
        return None
    from .review_control import progress
    from .free_document import compile_document
    proof = progress(service.turn).get('lastPublicationPreview', {})
    if (proof.get('analysisRef') != artifact['analysisRef'] or proof.get('semanticReview') != 'PASS'
            or proof.get('evidenceDigest') != evidence_digest(artifact)):
        return None
    saved = service.store.get_publication_proposal(service.owner, proof['previewRef'])
    if not saved.get('document') or saved.get('analysisRef') != artifact['analysisRef']:
        return None
    proposed = deepcopy(artifact)
    proposed.update({key: saved[key] for key in ('workRefs', 'partialReason') if saved.get(key) is not None})
    document = compile_document(proposed, saved['document'])
    if document['contentDigest'] != proof.get('contentDigest'):
        return None
    return {'document': document, 'previewRef': proof['previewRef'],
            'workRefs': saved.get('workRefs'), 'partialReason': saved.get('partialReason'),
            'publicationCheckCode': proof.get('publicationCheckCode')}


def reviewed_preview(service, artifact):
    """页面仍不可发布时，只保留经过相同证据审查的完整静态说明。"""
    publication = reviewed_publication(service, artifact)
    if publication is None:
        return None
    document = publication['document']
    parser = ReviewedParagraphs()
    parser.feed(document['html'])
    parser.close()
    # 容量不够时不截掉结论的限定条件，完整保留原草案，回退到数据摘要。
    if not parser.paragraphs or len(parser.paragraphs) > 100 or sum(map(len, parser.paragraphs)) > 24000:
        return None
    return {'paragraphs': parser.paragraphs, 'previewRef': publication['previewRef'],
            'contentDigest': document['contentDigest'], 'workRefs': publication['workRefs'],
            'partialReason': publication['partialReason'],
            'publicationCheckCode': publication['publicationCheckCode']}
