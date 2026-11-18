"""按已声明的交付引用组织核对材料；完整来源和产物仍由分析存储保留。"""
from copy import deepcopy


def _references(value, known):
    if isinstance(value, str):
        return {value} if value in known else set()
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return set().union(*(_references(child, known) for child in children)) if children else set()


def project_analysis_review(material):
    """保留主体概览和错误；只剔除无交付引用、无计算依赖的探索性表格。"""
    derived = {item['derivedId']: item for item in material.get('derived', [])}
    runs = {item['runRef']: item for item in material.get('pythonRuns', []) if item.get('runRef')}
    known = set(derived) | set(runs)
    roots = _references({key: material.get(key) for key in ('documentBindings', 'claims', 'completion')}, known)
    if not roots:
        # 无结构化交付引用时，不能凭文字、金额相似或最近执行顺序猜测材料范围。
        return material
    required = set(roots)
    # 正文可能直接提及标量；错误、空值及诊断也不能因未展示而隐藏。
    for reference, item in derived.items():
        if item.get('outputKind') != 'TABLE' or item.get('executionStatus') != 'COMPLETED' or item.get('diagnostics'):
            required.add(reference)
    required.update(reference for reference, run in runs.items() if run.get('status') not in (None, 'COMPLETED'))
    pending = list(required)
    while pending:
        reference = pending.pop()
        record = derived.get(reference, runs.get(reference, {}))
        dependencies = _references(record, known) - required
        required.update(dependencies)
        pending.extend(dependencies)
    kept_derived = [item for item in material.get('derived', []) if item['derivedId'] in required]
    kept_runs = [item for item in material.get('pythonRuns', []) if not item.get('runRef') or item['runRef'] in required]
    if len(kept_derived) == len(derived) and len(kept_runs) == len(material.get('pythonRuns', [])):
        return material
    result = deepcopy(material)
    result['derived'], result['pythonRuns'] = deepcopy(kept_derived), deepcopy(kept_runs)
    result['reviewProjection'] = {
        'basis': 'DELIVERY_REFERENCES_AND_TRANSITIVE_INPUTS',
        'retainedFactCount': len(material.get('facts', [])),
        'omittedExploratoryTableCount': len(derived) - len(kept_derived),
        'omittedPythonRunCount': len(material.get('pythonRuns', [])) - len(kept_runs),
        'allSelectedFactsPreserved': True,
        'meaning': '完整主体事实、范围、空值、诊断和实际执行输入仍保留；仅未被交付或计算引用的探索表格不进入本次核对，原产物未删除。',
    }
    return result
