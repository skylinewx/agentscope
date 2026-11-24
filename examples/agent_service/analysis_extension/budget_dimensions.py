"""System dimensions and source-provided business dimension names."""

SYSTEM_DIMENSION_LABELS = {
    'DATATIME': '实际时期', 'MD_ORG': '组织机构', 'MD_SCENE': '情景',
    'MD_CURRENCY': '币种', 'MD_MGRVER': '管理版本', 'MD_STAGE': '阶段',
}
SYSTEM_DIMENSIONS = frozenset(SYSTEM_DIMENSION_LABELS)


def has_chinese(text):
    return isinstance(text, str) and any('\u3400' <= char <= '\u9fff' for char in text)


def source_models(facts):
    models = {fact['modelCode'] for fact in facts if isinstance(fact.get('modelCode'), str) and fact['modelCode']}
    for fact in facts:
        expression = fact.get('cellExp')
        if not isinstance(expression, str):
            continue
        model, separator, _ = expression.partition('[')
        if separator and model:
            models.add(model)
    return models


def catalog_model_codes(models):
    """Map only the budget service's annual, quarterly and monthly table suffixes."""
    codes = set(models)
    return codes | {model[:-2] for model in codes if model.endswith(('_N', '_J', '_Y'))}


def dimension_labels(catalog=(), *, models=(), context=None):
    """Use actual model metadata; an unresolved business dimension keeps its code."""
    names = {}
    source_codes = catalog_model_codes(models)
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        code, label, model = entry.get('dimCode'), entry.get('label'), entry.get('modelCode')
        if not isinstance(code, str) or not isinstance(label, str) or not code or not label or code == label:
            continue
        if model and source_codes and model not in source_codes:
            continue
        names.setdefault(code, set()).add(label)
    labels = dict(SYSTEM_DIMENSION_LABELS)
    labels.update({code: next(iter(values)) for code, values in names.items() if len(values) == 1})
    def apply_field_label(code, name):
        if not code or not name or name == code:
            return
        if has_chinese(name) or not has_chinese(labels.get(code, '')):
            labels[code] = name

    for code, name in ((context or {}).get('dimensionLabels') or {}).items():
        apply_field_label(code, name)
    for field in (context or {}).get('dimensions') or []:
        if isinstance(field, dict):
            apply_field_label(field.get('code'), field.get('name'))
    return labels


def loading_names_for_empty_results(results):
    """Use names from the same budget snapshot and value domain, including when a filter leaves no rows."""
    from .identity import dimension_models
    from .models import AnalysisError

    def scope(context):
        base = context.get('baseContext') or {}
        return base.get('sourceId'), base.get('snapshotId'), context.get('period')

    names = {}
    for result in results:
        context = result.get('context') or {}
        origin = scope(context)
        if not result.get('rows') or not all(origin):
            continue
        try:
            models = dimension_models(context)
        except AnalysisError:
            continue
        for row in result['rows']:
            domains = dict(models)
            for match in row.get('conditionEvidence') or []:
                if not isinstance(match, dict) or match.get('verifiedBy') != 'RUNTIME_DIMENSION':
                    continue
                dim = match.get('dimensionCode')
                if dim and match.get('baseDataCode') and match.get('objectCode') == (row.get('dimensions') or {}).get(dim):
                    domains.setdefault(dim, match['baseDataCode'])
            for dim, label in (row.get('dimensionNames') or {}).items():
                code = (row.get('dimensions') or {}).get(dim)
                if code and domains.get(dim) and label and label != code:
                    names.setdefault((origin, dim, domains[dim], code), set()).add(label)

    contexts = []
    for result in results:
        context = result.get('context') or {}
        if result.get('rows'):
            contexts.append(context)
            continue
        labels = dict(context.get('dimensionNames') or {})
        fixed = context.get('effectiveDimensions') or {}
        for field in context.get('dimensions') or []:
            if not isinstance(field, dict):
                continue
            dim = field.get('code')
            code = fixed.get(dim)
            if not code or (labels.get(dim) and labels[dim] != code):
                continue
            name = field.get('valueLabel') if field.get('value') == code else None
            if name and name != code:
                parts = name.split(maxsplit=1)
                labels[dim] = parts[1] if len(parts) == 2 and parts[0] == code else name
            else:
                found = names.get((scope(context), dim, field.get('valueModelCode'), code), set())
                if len(found) == 1:
                    labels[dim] = next(iter(found))
        contexts.append({**context, 'dimensionNames': labels})
    return contexts
