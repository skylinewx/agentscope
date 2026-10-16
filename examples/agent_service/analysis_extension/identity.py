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
