"""Readable names for bound formula operands; raw headers and expressions stay in the document."""
from collections import Counter
import re

from .business_tables import period_label

_NUMBERING = re.compile(r'^[一二三四五六七八九十百〇零0-9]+、\s*')
_FILL_NOTE = re.compile(r'[（(][^（）()]*以[“"‘\']?[-−－][”"’\']?号填列[）)]$')
_PERIOD_TOKEN = re.compile(r'\{([+-]?\d+[NJYH](?:,[+-]?\d+[NJYH])*)\}')


def is_period_heading(header, fact):
    code = (fact.get('dimensions') or {}).get('DATATIME') or fact.get('period')
    if code and header in (code, period_label(code), (fact.get('dimensionNames') or {}).get('DATATIME')):
        return True
    match = _PERIOD_TOKEN.fullmatch(header)
    return bool(match and (match[1] == fact.get('periodExpression') or
                ',' + match[1] + ',' in str(fact.get('expression') or fact.get('cellExp') or '')))


def short_caption(raw):
    short = _FILL_NOTE.sub('', _NUMBERING.sub('', raw.strip())).strip()
    return re.sub(r'^(?:加|减)[：:]\s*', '', short) or raw


def component_labels(facts, aliases):
    if not facts:
        return {}
    def qualifier(f, dim):
        return (f.get('dimensions') or {}).get(dim) or (f.get('period') if dim == 'DATATIME' else None)
    dims = ('DATATIME', 'MD_SCENE')
    varying = {dim for dim in dims if len({qualifier(f, dim) for f in facts}) > 1}
    common = set.intersection(*(set(f.get('headers') or []) for f in facts))
    labels = {}
    for f in facts:
        headers = [s for s in f.get('headers') or [] if s and not is_period_heading(s, f)]
        # A header shared by all inputs can describe the scenario. Do not strip a
        # common metric from a same-metric, multi-period comparison.
        choice = [s for s in headers if s not in common] or headers
        scene = (f.get('dimensionNames') or {}).get('MD_SCENE')
        choice = [s for s in choice if s != scene] or choice
        raw = ' / '.join(choice) or f.get('measureTitle') or '名称待确认'
        name = aliases.get(f['factId']) or short_caption(raw)
        for dim in dims:
            if dim not in varying:
                continue
            value = qualifier(f, dim)
            label = period_label(value) if dim == 'DATATIME' else (f.get('dimensionNames') or {}).get(dim)
            if value and (label or '情景名称待确认') not in name:
                name += ' / ' + (label or '情景名称待确认')
        labels[f['factId']] = name
    collisions = Counter(labels.values())
    for f in facts:
        ref = f['factId']
        if collisions[labels[ref]] > 1:
            original = ' / '.join(f.get('headers') or [])
            # The printed source cell address distinguishes equal captions without
            # inferring that two different inputs are one fact.
            labels[ref] = ' · '.join(dict.fromkeys(x for x in (labels[ref], original, f.get('objectLabel'), f.get('cellAddress')) if x))
    return labels
