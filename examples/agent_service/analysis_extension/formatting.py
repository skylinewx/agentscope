"""Small presentation capabilities; choices and narrative belong to the model."""
from html import escape
import re

from .formulas import Expression
from .models import AnalysisError


NUMERIC_TYPES = frozenset({'DECIMAL_STRING', 'INTEGER', 'NUMBER'})
_DECIMAL_PARTS = re.compile(r'([+−-]?)([0-9]+)(\.[0-9]+)?')


def grouped_value(value):
    """Group only the integer part of a display value. Keep signs and source scale verbatim."""
    raw = str(value)
    parts = _DECIMAL_PARTS.fullmatch(raw)
    if not parts and 'e' in raw.lower():
        from .models import NUMBER
        if NUMBER.fullmatch(raw):
            try:
                return number_text(raw, grouping=True)
            except AnalysisError:
                return raw
    if not parts or len(parts[2]) <= 3:
        return raw
    whole = parts[2]
    first = len(whole) % 3 or 3
    groups = [whole[:first], *(whole[i:i + 3] for i in range(first, len(whole), 3))]
    return parts[1] + ','.join(groups) + (parts[3] or '')


_PROSE_UNIT = r'(?:[万亿千百]?(?:美元|港元|欧元|日元|英镑|元)|[%％]|个百分点)'
_QUANTITY_IN_TEXT = re.compile(
    r'(?<![A-Za-z0-9_.,])([+−-]?[0-9]{4,}(?:\.[0-9]+)?)'
    r'(?=(?:\s*(?:[~～–—]|至|到)\s*[+−-]?[0-9][0-9,]*(?:\.[0-9]+)?)?\s*' + _PROSE_UNIT + r')')


def group_written_amounts(text):
    """Only explicit monetary/percentage quantities in prose, never bare identifiers or years."""
    return _QUANTITY_IN_TEXT.sub(lambda match: grouped_value(match[1]), text)


def unit_for(value, view):
    return (value.get('unit') or '') if view.get('showUnit') is True else ''


def inline_markup(text):
    """Render model-authored emphasis without accepting HTML or active content."""
    parts, previous = [], 0
    for match in re.finditer(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', text):
        parts.extend((escape(text[previous:match.start()]), '<strong>', escape(match[1]), '</strong>'))
        previous = match.end()
    parts.append(escape(text[previous:]))
    return ''.join(parts).replace('\n', '<br>')


def formula_text(tree, names, minimum=0):
    """Business labels with only precedence-preserving parentheses."""
    kind = tree[0]
    if kind == 'ref': return names.get(tree[1], '未取得的项目')
    if kind == 'number': return tree[1]
    if kind == 'sum':
        precedence = 1
        text = ' + '.join(formula_text(t, names, 2) for t in tree[1])
    elif kind == 'unary':
        precedence = 3
        text = tree[1] + formula_text(tree[2], names, 4)
    else:
        precedence = 1 if tree[1] in ('+', '-') else 2
        text = (formula_text(tree[2], names, precedence)
                + {'+': ' + ', '-': ' − ', '*': ' × ', '/': ' ÷ '}[tree[1]]
                + formula_text(tree[3], names, precedence + 1))
    return '(' + text + ')' if precedence < minimum else text


def formula_labels(trace, known, view):
    """One label map for diagrams, waterfalls and details; aliases remain model-authored."""
    facts = [known[n['factRef']] for n in trace['nodes'] if n['factRef'] in known]
    from .formula_labels import component_labels
    return component_labels(facts, {f['factId']: view_label(view, f['factId'], '') for f in facts})


def business_formula(node, known, view=None, labels=None):
    if not node.get('expression') or node.get('boundary'): return ''
    if labels is None:
        refs = dict.fromkeys([node['factRef']] + [d['factRef'] for d in node['dependencies']])
        labels = formula_labels({'nodes': [{'factRef': ref} for ref in refs]}, known, view or {})
    names = {dep['address']: labels.get(dep['factRef'], '未取得的项目') for dep in node['dependencies']}
    try:
        return labels[node['factRef']] + ' = ' + formula_text(Expression(node['expression']).tree, names)
    except AnalysisError:
        return ''


def validate_number_format(options):
    if not isinstance(options, dict) or set(options) - {'grouping', 'decimalPlaces'}:
        raise AnalysisError('INVALID_NUMBER_FORMAT', '数值展示仅支持 grouping 和 decimalPlaces。')
    if 'grouping' in options and type(options['grouping']) is not bool:
        raise AnalysisError('INVALID_NUMBER_FORMAT', 'grouping 必须是布尔值。')
    if 'decimalPlaces' in options and (type(options['decimalPlaces']) is not int or not 0 <= options['decimalPlaces'] <= 16):
        raise AnalysisError('INVALID_NUMBER_FORMAT', '展示小数位数须在 0 至 16 之间。')
    return options


def number_text(value, options=None, grouping=False):
    """Presentation only: no mutation, conversion or floating-point arithmetic."""
    from decimal import Decimal, localcontext, ROUND_HALF_UP
    from .models import decimal_value
    options = validate_number_format(options) if options is not None else {}
    number = decimal_value(value)
    with localcontext() as context:
        context.prec = 512
        if 'decimalPlaces' in options:
            number = number.quantize(Decimal(1).scaleb(-options['decimalPlaces']), rounding=ROUND_HALF_UP)
        if not number: number = number.copy_abs()
        return format(number, ',f' if options.get('grouping', grouping) else 'f')


def view_label(view, ref, fallback, role=None):
    role = role or ('object' if view.get('type') in ('BAR_VALUES', 'BAR_RANK', 'HIERARCHY') else 'measure')
    shared = view.get('_sharedLabelLookup', {}).get(role, {}).get(ref, fallback)
    default_role = 'object' if view.get('type') in ('BAR_VALUES', 'BAR_RANK', 'HIERARCHY') else 'measure'
    if role != default_role: return shared
    return view.get('_localLabelLookup', {}).get(role, {}).get(ref,
        next((item['label'] for item in view.get('labels', []) if item['valueRef'] == ref), shared))
