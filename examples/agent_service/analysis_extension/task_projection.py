"""Readable task decisions; host-owned historical proof stays in persisted state."""
from .business_tables import encode


def render(value):
    def cell(item):
        if isinstance(item, (dict, list)):
            item = encode(item)
        return str(item if item is not None else '').replace('|', '\\|').replace('\n', ' ')

    sections = [f"本轮任务：{value.get('mode', '')}；{value.get('goal', '')}", value.get('intentSummary', '')]
    if value.get('comparisonStatus'):
        sections.append(value['comparisonStatus'])
        return '\n\n'.join(section for section in sections if section)
    for trace in value.get('executionTraces', []):
        from .history_references import trace_markdown
        sections.append(trace_markdown(trace))
    members = [item for item in value.get('historyReferences', []) if item['use'] == 'MEMBERS']
    if members:
        sections.append('按已选成员重新取数：' + '、'.join(cell(item['sourceRef']) for item in members) +
                        '。提交本轮指标和时期；成员绑定由宿主传入。')
    for item in value.get('historyReferences', []):
        if item['use'] in ('TASK', 'FORM'):
            sections.append(('采用原表样' if item['use'] == 'FORM' else '采用原任务') + ' ' + cell(item['name']))
    intent = value.get('intent', {})
    if intent:
        sections.append('\n'.join(['| 有效条件 | 内容 |', '|---|---|',
            *(f'| {cell(key)} | {cell(item)} |' for key, item in intent.items())]))
    sources = value.get('subjectSources', [])
    if value.get('reusedAnalysis'):
        report = value['reusedAnalysis']
        sections.append(f"已复用报告 {report['analysisRef']} 和原计算。原审核状态 {report.get('reviewStatus') or '未登记'}。"
                        '仅查看已有意见用 AnalysisPublish 的 VIEW；要求审核时，已完成用 RECHECK，待审用 RESUME。'
                        '实质问题按定位修订；仅有建议时展示原结论及建议即可。需要查看冻结明细仍可使用 AnalysisInspect。')
    elif sources:
        sections.append('\n'.join(['| 用途 | 来源 | 运算输入组 | 行数 | 内容 |', '|---|---|---|---:|---|',
            *(f"| {cell(item['role'])} | {cell(item['sourceRef'])} | {cell(item['inputRef'])} | {cell(item.get('rowCount'))} | {cell(item.get('label'))} |"
              for item in sources)]))
        sections.append('以上输入组已采用。统计整组用 inputRef，细分列用来源 columnName。')
    elif value.get('mode') in ('ANALYSIS', 'REPORT'):
        sections.append('尚未明确采用来源组。需要选择多个候选或子集时，先读 sources，再以 subjectSources 完整声明采用集合。')

    prepared = value.get('preparedFormulaInputs')
    if prepared is not None:
        targets = prepared.get('targets', [])
        ready = prepared.get('ready', sum('captureRef' in target and 'error' not in target for target in targets))
        sections.append(f"公式材料：预期 {prepared['expected']} 个目标，就绪 {ready} 个。")
        sections.append('\n'.join(['| 目标 | 输入组 | 状态 | 材料或缺口 |', '|---|---|---|---|',
            *(f"| {cell(item.get('subject'))} | {cell(item.get('inputRef'))} | {'待补' if 'error' in item else '已准备'} | {cell(item.get('error', item.get('captureRef')))} |"
              for item in targets)]))
        if prepared.get('boundaries'):
            sections.append('来源边界：' + cell(prepared['boundaries']))
        if value.get('pendingMembers'):
            sections.append('尚待确认的成员来源：' + cell(value['pendingMembers']))
        elif value.get('preparedAnalysis'):
            base = value['preparedAnalysis']
            sections.append('已复算并保存构成。' + cell({k: base[k] for k in ('analysisRef', 'inputSetRef', 'documentDraft') if k in base}))
            sections.extend(str(s) for s in base.get('material', []))
            sections.append('可用图表：' + cell(base.get('markdownFigures', [])))
        elif ready == prepared['expected'] and prepared['expected'] > 0:
            sections.append('公式目标及依赖已取得，标准复算使用已选主体的 FORMULA_CHECK。')
            if value.get('delivery') == 'DOCUMENT':
                sections.append('每个主体默认展开最能解释结果的构成层，其余已返回的计算层可折叠查看。')
        else:
            sections.append(prepared.get('next', '按实际缺口处理，保留已完成目标。'))
    else:
        sections.append(value.get('next', ''))
    if value.get('delivery') == 'DOCUMENT' and not value.get('reusedAnalysis') and not value.get('pendingMembers'):
        sections.append('构建后可直接提交 Markdown 正文或写入 documentDraft.path，从 markdownFigures 复制所需图表引用。')
    if value.get('mode') == 'DIRECT':
        sections.append('仅查询/排序时取得结果即可答复；用户未要求的合计、占比及报告无需作为交付前置步骤。'
                        '继续同一指标仅改范围/排序时，查询可省略未变化的 targets，由宿主恢复已确认条件。')

    objectives = value.get('objectives', [])
    if objectives:
        for item in objectives:
            if item.get('membersFromObjective'):
                sections.append(f"目标 {item['objectiveRef']} 使用取值目标 {item['membersFromObjective']} 的实际成员。")
        bound = any(i.get('sourceRef') for i in objectives)
        table = ['| 目标引用 | 用途 | 业务能力 | 需要回答的问题 |' + (' 已绑定来源 |' if bound else ''),
                 '|---|---|---|---|' + ('---|' if bound else '')]
        for item in objectives:
            capability = '数据构成' if item['capability'] == 'DATA_COMPOSITION' else '取值'
            row = f"| {cell(item['objectiveRef'])} | {cell(item['role'])} | {capability} | {cell(item.get('question'))} |"
            table.append(row + (f" {cell(item.get('sourceRef'))} |" if bound else ''))
        sections.append('\n'.join(table))
        if (value.get('mode') in ('ANALYSIS', 'REPORT') and not value.get('reusedAnalysis')
                and {i['capability'] for i in objectives if i['role'] == 'PRIMARY'} == {'VALUES'}):
            sections.append('当前主体只取值。要解释具体数值如何形成，选定对象后为主体追加 DATA_COMPOSITION；只做口径比较时可直接使用已取值。')
    policy = {key: flag for key, flag in value.get('executionPolicy', {}).items() if flag is False}
    if policy:
        sections.append('本轮执行限制 ' + cell(policy))
    directory = value.get('availableContexts', {})
    contexts = directory.get('contexts', [])
    if contexts:
        sections.append('\n'.join(['| 上下文 | 用户原问 | 条件 |', '|---|---|---|',
            *(f"| {cell(i['contextRef'])} | {cell(i.get('request'))} | {cell(i.get('intent'))} |" for i in contexts)]))
        if directory.get('nextOffset') is not None:
            sections.append(f"更多上下文使用 contexts，nextOffset={directory['nextOffset']}。")
    context = value.get('followupScope', {}).get('context', {})
    latest = value.get('followupScope', {}).get('latestSourceScope')
    if latest:
        sections.append('本次选择与最近交付的范围不同。上轮用户原问：' + cell(latest['request'])
                        + '；已生效范围：' + cell(latest['conditions'])
                        + '。未指定新机构时沿用此范围，可用 context.contextRefs 选择 ' + cell(latest['contextRef'])
                        + '；本轮明确的机构或层级优先。')
    if context.get('contextRefs'):
        sections.append('采用的上下文 ' + cell(context['contextRefs']))
    if value.get('selectedGroupingCaptureRefs'):
        sections.append(cell(value['selectedGroupingCaptureRefs']))
    return '\n\n'.join(section for section in sections if section)
