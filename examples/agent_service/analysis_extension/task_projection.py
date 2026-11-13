"""Readable task decisions; host-owned historical proof stays in persisted state."""
from .business_tables import encode


def render(value):
    def cell(item):
        if isinstance(item, (dict, list)):
            item = encode(item)
        return str(item if item is not None else '').replace('|', '\\|').replace('\n', ' ')

    sections = [f"本轮任务：{value.get('mode', '')}；{value.get('goal', '')}", value.get('intentSummary', '')]
    intent = value.get('intent', {})
    if intent:
        sections.append('\n'.join(['| 有效条件 | 内容 |', '|---|---|',
            *(f'| {cell(key)} | {cell(item)} |' for key, item in intent.items())]))
    sources = value.get('subjectSources', [])
    if sources:
        sections.append('\n'.join(['| 用途 | 来源 | 运算输入组 | 行数 | 内容 |', '|---|---|---|---:|---|',
            *(f"| {cell(item['role'])} | {cell(item['sourceRef'])} | {cell(item['inputRef'])} | {cell(item.get('rowCount'))} | {cell(item.get('label'))} |"
              for item in sources)]))
        sections.append('以上输入组已采用。AnalysisBuild 直接传 operations/requestKey，不再填写 captureRefs/selections；按 inputRef 分组计算，增加标题可传 scope。统计整个输入组时 select 只需 inputRef；确需细分列时使用材料中的实际 columnName，展示列标题不是列编码。改变输入先修改 AnalysisTask.subjectSources。')
    elif value.get('mode') in ('ANALYSIS', 'REPORT'):
        sections.append('尚未明确采用来源组。需要选择多个候选或子集时，先读 sources，再以 subjectSources 完整声明采用集合。')

    prepared = value.get('preparedFormulaInputs')
    if prepared is not None:
        targets = prepared.get('targets', [])
        ready = sum('captureRef' in target and 'error' not in target for target in targets)
        sections.append(f"公式材料：预期 {prepared['expected']} 个目标，就绪 {ready} 个。")
        sections.append('\n'.join(['| 目标 | 输入组 | 状态 | 材料或缺口 |', '|---|---|---|---|',
            *(f"| {cell(item.get('subject'))} | {cell(item.get('inputRef'))} | {'待补' if 'error' in item else '已准备'} | {cell(item.get('error', item.get('captureRef')))} |"
              for item in targets)]))
        if ready == prepared['expected']:
            sections.append('公式目标及依赖已取得。下一步 AnalysisBuild，宿主自动附上材料和 FORMULA_CHECK；仅需展示构成时 operations=[]。直接使用返回的公式数据集，无需为同一构成再取整表或编写 Python。')
        else:
            sections.append(prepared.get('next', '按实际缺口处理，保留已完成目标。'))
    else:
        sections.append(value.get('next', ''))
    if value.get('delivery') == 'DOCUMENT':
        sections.append('构建完成后使用 AnalysisBuild 返回的 documentDraft.path 写报告，复用已确认模板；同一分析版本的修订沿用该路径。')
    if value.get('mode') == 'DIRECT':
        sections.append('仅查询/排序时取得结果即可答复；用户未要求的合计、占比及报告无需作为交付前置步骤。'
                        '继续同一指标仅改范围/排序时，查询可省略未变化的 targets，由宿主恢复已确认条件。')

    # Preserve every actionable field and diagnostics, without repeating historical
    # native receipts/sourceText in the model's task summary. Full state remains
    # available via status and the query/analysis inspection interfaces.
    navigation = {key: item for key, item in value.items() if key not in (
        'mode', 'goal', 'intent', 'intentSummary', 'next', 'subjectSources', 'preparedFormulaInputs',
        'previousIntent', 'dimensionScopes', 'followupScope', 'queryAnchors', 'selectedContexts',
        'availableContexts')}
    directory = value.get('availableContexts', {})
    contexts = directory.get('contexts', [])
    if contexts:
        navigation['availableContexts'] = {**{key: item for key, item in directory.items() if key != 'contexts'},
            'contexts': [{key: item[key] for key in ('contextRef', 'request', 'goal', 'intent', 'status', 'kind', 'analysisRef')
                          if item.get(key) is not None} for item in contexts]}
    if sources:
        navigation['subjectSources'] = [{key: item[key] for key in ('sourceRef', 'inputRef', 'role', 'membersFrom')
                                        if item.get(key) is not None} for item in sources]
    if prepared is not None:
        navigation['preparedFormulaInputs'] = {key: item for key, item in prepared.items() if key not in ('next', 'targets')}
        navigation['preparedFormulaInputs']['targets'] = [{key: item[key] for key in
            ('subject', 'sourceRef', 'inputRef', 'captureRef', 'factRef', 'error') if key in item}
            for item in prepared.get('targets', [])]
    requirement = navigation.get('analysisRequirement')
    if isinstance(requirement, dict):
        navigation['analysisRequirement'] = {key: requirement[key] for key in
            ('requiredEvidence', 'reason', 'status', 'error', 'decision', 'scope') if key in requirement}
        navigation['requirementDetailRead'] = {'tool': 'AnalysisTask', 'action': 'status'}
    followup = value.get('followupScope', {})
    if followup:
        navigation['context'] = {key: item for key, item in followup.get('context', {}).items()
                                 if key in ('mode', 'contextRefs')}
    sections.append('执行引用与诊断：\n```json\n' + encode(navigation) + '\n```')
    return '\n\n'.join(section for section in sections if section)
