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
    for proof in result.get('populationEvidence') or []:
        for key in ('members', 'factRefs'):
            if isinstance(proof.get(key), list):
                proof[key + 'Count'] = len(proof.pop(key))
    # Exclusions, pending members, real scope limitations and group roles remain.
    result['detailRead'] = {'section': 'summary', 'analysisRef': analysis_ref}
    return result


def project(value, artifact):
    result = deepcopy(value)
    result['scope'] = compact_scope(value.get('scope', {}), value['analysisRef'])
    result.pop('compositionMaterials', None)
    result['verificationPendingCount'] = len(result.pop('verificationTasks', []))
    for entry in result.get('documentInputs', []):
        entry.pop('usage', None)
        # field records already contain every scalar field and its meaning.
        if entry.get('fields'):
            entry.pop('scalarFields', None)
        entry.pop('checkSummary', None)  # Same computed check appears below.
    from .period_trends import choices, trend_catalog
    trends = trend_catalog(artifact)
    from .report_markdown import catalog, figure_links
    result['markdownFigures'] = figure_links(catalog(artifact, {}))
    if trends:
        options = choices(trends)
        result['periodTrends'] = {'usage': '在 Markdown 正文中按业务重点选一个指标和比较范围，将对应 Markdown 图片引用单独成段。宿主按真实时期绘图。',
                                  'choices': options[:8]}
        if len(options) > 8:
            result['periodTrends']['more'] = {'section': 'periodTrends', 'analysisRef': value['analysisRef'], 'offset': 8}
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
    if not (artifact.get('plan') or {}).get('workPlan'):
        result['projection'] += '本次没有工作项，发布只需引用本次结果。省略 workRefs；objectiveRef 是业务目标。'
    return result


def catalog_read(catalog):
    return {key: catalog[key] for key in ('recordCount', 'sampleRead', 'outputContract') if key in catalog}


def render(value, artifact, input_markdown=''):
    projected = project(value, artifact)
    materials = projected.pop('formulaMaterials')
    python_result = bool(value.get('pythonRunRef'))
    if (input_markdown or python_result) and 'pythonInputCatalog' in projected:
        projected['pythonInputCatalog'] = catalog_read(projected['pythonInputCatalog'])
    lines = [input_markdown] if input_markdown else []
    if python_result:
        lines.append(f"发布本次成果用 AnalysisPublish 的 analysisRef={value['analysisRef']}。"
                     f"保留本次计算并继续追加 Python 时，用本次返回的 inputSetRef={value['inputSetRef']}。"
                     "旧输入集合固定在当时的结果，分别从它计算会生成独立分支。")
        projected.pop('next', None)  # The native Build continuation uses different arguments.
    trends = projected.pop('periodTrends', None)
    if trends:
        lines.append('可用时期折线图\n' + model_text(trends))
    lines.append(('本次计算结果与可用引用' if python_result else '分析构建结果与可直接使用的引用')
                 + '：\n' + model_text(projected))
    for view in materials:
        lines.append('公式数据集：' + view['valueRef'] + '\n\n' + model_text(view))
    return '\n\n'.join(lines)
