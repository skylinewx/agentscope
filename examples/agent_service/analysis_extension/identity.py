"""Resolve entity names with source model identity, never with a bare key."""


def dimension_models(form):
    from .models import AnalysisError
    models = {}
    for d in form.get('dimensions', []):
        if not isinstance(d, dict) or not d.get('code') or not d.get('valueModelCode'): continue
        if d['code'] in models and models[d['code']] != d['valueModelCode']:
            raise AnalysisError('DIMENSION_MODEL_CONFLICT', '同一维度的来源模型身份不一致，不能任意选择名称。')
        models[d['code']] = d['valueModelCode']
    return models


def names_for(index, model, member, origin):
    # Missing metadata is not a license to choose one of several entity models.
    return index.get((origin, model, member), set())


def display_names(facts, member_dimension):
    """只给同来源、值域与时期的代码回退补名称，不改原始事实和证据摘要。"""
    from .models import digest
    groups = {}
    for fact in facts:
        model = fact.get('objectModelCode') or fact.get('dimensionModels', {}).get(member_dimension)
        # 缺少值域或来源时，宁可保留原显示，不能跨类型猜测同码成员。
        if not model or not fact.get('sourceOrigins'):
            continue
        key = digest([fact.get('objectId'), model, fact['sourceOrigins'], fact.get('period')])
        groups.setdefault(key, []).append(fact)
    result = {}
    for group in groups.values():
        names = {fact['objectLabel'] for fact in group
                 if fact.get('objectLabel') and fact.get('objectLabelState') == 'RESOLVED'
                 and fact['objectLabel'] != fact.get('sourceObjectId')}
        if len(names) != 1:
            continue
        name = next(iter(names))
        for fact in group:
            if (not fact.get('objectLabel') or fact['objectLabel'] == fact.get('sourceObjectId')
                    or fact['objectLabel'] == name):
                result[fact['factId']] = name
    return result


def same_source_position(left, right, same_snapshot=True):
    """按实际来源与完整维度关联证据，分析粒度和显示名称不参与来源身份。"""
    if left.get('factId') and left.get('factId') == right.get('factId'):
        return True
    required = ('sourceContextId', 'bindingRef') if same_snapshot else ('bindingRef',)
    if any(not left.get(key) or left.get(key) != right.get(key) for key in required):
        return False
    left_dims = {**left.get('effectiveDimensions', {}), **left.get('dimensions', {})}
    right_dims = {**right.get('effectiveDimensions', {}), **right.get('dimensions', {})}
    if not left_dims or left_dims != right_dims:
        return False
    left_measure = left.get('sourceMeasureCode') or left.get('measureCode')
    right_measure = right.get('sourceMeasureCode') or right.get('measureCode')
    if not left_measure or left_measure != right_measure:
        return False
    if left.get('period') != right.get('period') or left.get('sourceOrigins') != right.get('sourceOrigins'):
        return False
    if not same_snapshot and any(not left.get('context', {}).get(key)
                                 or left['context'][key] != right.get('context', {}).get(key)
                                 for key in ('sourceId', 'schemaHash')):
        return False
    # 值域模型不同的同名维度不能合并；表绑定仍提供任务、表样及机构类型边界。
    left_models, right_models = left.get('dimensionModels', {}), right.get('dimensionModels', {})
    if any(left_models[key] != right_models[key] for key in left_models.keys() & right_models.keys()):
        return False
    left_category = left.get('formContext', {}).get('target', {}).get('organizationCategory')
    right_category = right.get('formContext', {}).get('target', {}).get('organizationCategory')
    return not (left_category and right_category and left_category != right_category)


def same_form_observation(left, right):
    """同一表观察中的单元格可跨行列、科目；不能跨来源、表观察或运行条件。"""
    fields = ('observationId', 'bindingRef', 'sourceOrigins', 'context', 'sourceContextId')
    if any(left.get(key) != right.get(key) for key in fields):
        return False
    if left.get('observationId') and left.get('bindingRef'):
        left_runtime, right_runtime = left.get('formContext'), right.get('formContext')
        return not (left_runtime and right_runtime and left_runtime != right_runtime)
    return left.get('objectId') == right.get('objectId')
