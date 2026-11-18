"""Frozen host evidence and the exact Markdown message sent to a reviewer."""
from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
import unicodedata

from .models import digest, json_values


@dataclass(frozen=True)
class ReviewRequest:
    material: dict
    view: dict
    markdown: str
    wire_references: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.markdown)

    def stored(self):
        return {'format': 'REVIEW_REQUEST_MARKDOWN_V1', 'material': self.material,
                'view': self.view, 'markdown': self.markdown, 'wireDigest': digest(self.markdown),
                'wireReferences': self.wire_references}

    def restore_verdict(self, verdict):
        from .review_aliases import replace_references
        result = replace_references(verdict, self.view.get('referenceAliases', {}))
        def reference(ref):
            if ref in self.wire_references: return self.wire_references[ref]
            # Only spelling/spacing of explicit locations is normalized. Do
            # not infer references from matching titles, values or substrings.
            if isinstance(ref,str):
                normalized=re.sub(r'\s+', '', unicodedata.normalize('NFKC',ref))
                if normalized.startswith('业务数据表'): normalized=normalized[4:]
                if re.fullmatch(r'(表\d+第\d+行|Python脚本\d+|结果\d+)',normalized):
                    return self.wire_references.get(normalized,ref)
            return ref
        def restore(node):
            if isinstance(node, dict):
                return {key: [reference(ref) for ref in value]
                        if key == 'evidenceRefs' and isinstance(value, list) else restore(value)
                        for key, value in node.items()}
            if isinstance(node, list):
                return [restore(child) for child in node]
            return node
        return restore(result)


def make_review_request(value, *, query=False):
    if isinstance(value, ReviewRequest): return value
    # Saved historical inputs are structured JSON; Markdown is never parsed.
    if isinstance(value, str): value = json.loads(value)
    if value.get('format') == 'REVIEW_REQUEST_MARKDOWN_V1':
        if digest(value['markdown']) != value['wireDigest']:
            from .models import AnalysisError
            raise AnalysisError('REVIEW_MATERIAL_CHANGED', '保存的审查正文摘要不一致。')
        return ReviewRequest(deepcopy(value['material']), deepcopy(value['view']), value['markdown'],
                             deepcopy(value.get('wireReferences', {})))
    material = json_values(deepcopy(value))
    from .readable_material import readable_analysis_material
    from .review_materials import markdown_review_material
    from .review_periods import period_labels
    view = readable_analysis_material(markdown_review_material(material) if query else material, all_identities=True)
    periods = period_labels(material)
    if periods and not view.get('materialFormat', {}).get('periods'): view['periodGlossary'] = periods
    from .review_business_markdown import render_business_markdown
    wire_material = markdown_review_material(material) if query else material
    wire_references = {}
    markdown = render_business_markdown(wire_material, view.get('referenceAliases', {}), view, wire_references)
    return ReviewRequest(material, view, markdown, wire_references)


def review_material(value):
    if isinstance(value, ReviewRequest): return deepcopy(value.material)
    if isinstance(value, str): return json.loads(value)
    if value.get('format') == 'REVIEW_REQUEST_MARKDOWN_V1': return deepcopy(make_review_request(value).material)
    return deepcopy(value)
