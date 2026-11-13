"""Readable build handoff; immutable artifacts and rendering datasets stay complete."""
from copy import deepcopy

from .business_tables import encode, model_text


def compact_scope(scope, analysis_ref):
    result = deepcopy(scope)
    for key in ('members', 'referenceMembers', 'observedMembers', 'observedReferenceMembers', 'memberBindings'):
        if isinstance(result.get(key), list):
            result[key + 'Count'] = len(result.pop(key))
    for group in result.get('inputGroups', []):
        for key in ('members', 'factRefs', 'formulaFactRefs'):
            if isinstance(group.get(key), list):
                group[key + 'Count'] = len(group.pop(key))
    # Exclusions, pending members, real scope limitations and group roles remain.
    result['detailRead'] = {'section': 'summary', 'analysisRef': analysis_ref}
    return result


def project(value, artifact):
    result = deepcopy(value)
    result['scope'] = compact_scope(value.get('scope', {}), value['analysisRef'])
    result.pop('compositionMaterials', None)
    result['verificationPendingCount'] = len(result.pop('verificationTasks', []))
    guidance = {}
    for entry in result.get('documentInputs', []):
        if entry.get('usage'):
            guidance.setdefault(entry['operator'], entry.pop('usage'))
        # field records already contain every scalar field and its meaning.
        if entry.get('fields'):
            entry.pop('scalarFields', None)
        entry.pop('checkSummary', None)  # Same computed check appears below.
    result['datasetUsage'] = guidance
    for work in result.get('completion', {}).get('items', []):
        details = work.get('items', [])
        if work.get('state') == 'READY' and all(item.get('ready') for item in details):
            work['refs'] = [item['ref'] for item in details]
            work.pop('items', None)
    materials = []
    full = {item['derivedId']: item for item in artifact.get('derivedValues', [])}
    for derived in result.get('derivedValues', []):
        trace = full.get(derived['derivedId'], {}).get('formulaTrace')
        if not trace:
            continue
        derived.pop('formulaPresentation', None)
        derived.pop('formulaTrace', None)
        # Unchanged calculations are already in the conversation; keep status,
        # differences and dataset identity, with an exact read if details needed.
        if derived.get('unchanged'):
            continue
        from .formula_tables import formula_view
        view = formula_view(trace, artifact['facts'], value['analysisRef'], limit=20, max_chars=24000)
        view['valueRef'] = derived['derivedId']
        materials.append(view)
    result['formulaMaterials'] = materials
    result['projection'] = ('输入组按 inputRef 复用，成员及来源凭据保存在 summary；'
                            '公式材料展示实际构成、完整数值与计算关系，超出单次容量沿 nextRead 读取。'
                            'document.datasets 绑定完整冻结结果，不受本摘要分页影响。')
    return result


def render(value, artifact):
    projected = project(value, artifact)
    materials = projected.pop('formulaMaterials')
    lines = ['分析构建结果与可直接使用的引用：\n```json\n' + encode(projected) + '\n```']
    for view in materials:
        lines.append('公式数据集：' + view['valueRef'] + '\n\n' + model_text(view))
    return '\n\n'.join(lines)
