"""Single public task contract for the coordinated result-reference upgrade."""

DESCRIPTION = (
    '每个真实用户轮先 declare mode/goal，DIRECT 用于查询、筛选、排序和简单统计，ANALYSIS 用于分析及构成，REPORT 用于明确报告。'
    '分析方法由本题与来源决定，合理解释不要求唯一成因；声明不代表执行完成。materials 按 PRIMARY/REFERENCE/STRUCTURE 分别选择 VALUES/FORMULAS；宿主生成底层操作，不填写 purpose。'
    'materials 是唯一材料计划，可调整；完整查询与分析覆盖分别核对。成功空结果可正常采用，不要求改条件补查。'
    '多组分析先分别判断主体要解释什么、对照用于比较什么，再选择各组材料；增加对照不取消主体形成分析。'
    '主体形成与总体比较同时需要时，PRIMARY 选 VALUES+FORMULAS，REFERENCE 选 VALUES；明确只比规模/排名/分布时两组均可 VALUES。goal 不能自行缩减原问题。'
    '明确调查用 investigations 的真实 userQuote 和范围登记。只审查不取数/计算时声明 executionPolicy 相应字段 false，本轮不能自行放开。'
    'intent 是本轮增量修改：省略保留、非空业务原文设置、null 清除。新轮默认不继承分组/排序，明确沿用时填写；同轮补充其他字段不清除它们。'
    'context CONTINUE 承接相关已交付结果，NEW 开始新目标并保留未修改的时期、机构、情景、币种和管理版本，SYNTHESIZE 明确综合多个上下文。声明选择历史上下文用 context.contextRefs，引用来自 contexts 目录。'
    'sources 只读取来源目录。需要选择时在 subjectSources 提供真实 sourceRef；省略保留已采用集合，非空列表完整替换。'
    '多个 PRIMARY 可同时分析；REFERENCE 独立统计，不混入主体。membersFrom 表示沿用另一来源的成员，不重复套用它的指标筛选。'
    '数值筛选只在 execute_budget_query.intentDraft.valueFilter 给出；basis FACTS 在旧结果内筛选，MEMBERS 沿用成员查询新指标/时期。'
    '不重复填写 changedFields/changedDimensions/subjectFilter。scopeEdits 用真实业务维度声明 selector，null 仅用于用户明确要求解除该维度旧限制；查询的实际选择器可单次覆盖。'
    '换题或 NEW 本身不授权清除系统维度；不能为定位新指标主动解除机构、时期、情景、币种或管理版本。历史业务条件的定位放宽交由宿主处理。'
    '声明 FORMULAS 后宿主按已选对象准备，公式目标需要缩小时用 {sourceRef,factRefs:[实际引用]}；字符串是 factId/memberRef，维值映射必须填来源编码，展示名称不能代替引用。多来源分别指定，不猜编码、坐标和公式。'
    'select_results 保留脚本结果选择：resultRefs 是 pythonRunRef/outputRefs，不是预算 sourceRef。'
    '按用户原意覆盖必要内容，不从负数或业务名称机械升级分析。分析/构成/报告用 DOCUMENT，普通查询可 TEXT。'
    'blocked 只引用真实失败和未完成范围；status 返回宿主实际状态。'
)


def schema(obj, array, text, string_map, intent_fields):
    nullable_text = {**text, 'type': ['string', 'null'], 'description': '省略保留本轮决定；非空业务原文设置；null 明确清除。'}
    intents = {field: dict(nullable_text) for field in intent_fields}
    intents['formExecutionYear'] = {**nullable_text, 'pattern': '^[1-9][0-9]{3}$',
        'description': '明确固定年度表时填写四位开表年份；数据时期用 period；null 清除。'}
    intents['task']['description'] = '预算任务名称；预算/实际/预计放 scenario，不猜任务编码；null 清除。'
    member_selector = obj({'type': {'enum': ['VALUES', 'ALL', 'HIERARCHY', 'FILTER']},
                          'members': array(text, 1000, 1), 'rootText': text,
                          'level': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                          'where': {'type': 'object'}}, ['type'])
    source_choice = obj({'sourceRef': text, 'role': {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
                         'membersFrom': text}, ['sourceRef'])
    result = obj({'action': {'enum': ['declare', 'status', 'blocked', 'select_results', 'contexts', 'sources']},
        'mode': {'enum': ['DIRECT', 'ANALYSIS', 'REPORT']}, 'goal': text,
        'intent': obj(intents),
        'context': obj({'mode': {'enum': ['CONTINUE', 'NEW', 'SYNTHESIZE']},
                        'contextRefs': {**array(text, 20), 'uniqueItems': True}}, ['mode']),
        'contextRefs': {**array(text, 20), 'description': '仅 action=sources：目录需要检索的历史上下文；declare 选择上下文使用 context.contextRefs。省略读取当前相关来源。'},
        'subjectSources': array(source_choice, 100, 1),
        'scopeEdits': array(obj({'dimensionHint': text, 'selector': {'anyOf': [member_selector, {'type': 'null'}]}},
                               ['dimensionHint', 'selector']), 32),
        'materials': array(obj({'role': {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
            'kinds': {**array({'enum': ['VALUES', 'FORMULAS']}, 2, 1), 'uniqueItems': True}}, ['role', 'kinds']), 3, 1),
        'investigations': array(obj({key: text for key in ('requirement', 'userQuote', 'scope')},
                                   ['requirement', 'userQuote', 'scope']), 20),
        'executionPolicy': obj({'sourceQueries': {'type': 'boolean'}, 'computations': {'type': 'boolean'}}, []),
        'formulaSubjects': {**array({'oneOf': [string_map, text,
            obj({'sourceRef': text, 'inputRef': text, 'factRefs': array(text, 1000, 1)}, ['sourceRef', 'factRefs'])]}, 100, 1),
            'description': '优先使用 {sourceRef,factRefs:[来源实际返回的 factId/memberRef]}。展示名称不是引用；维值映射只接受来源编码。多个来源分别指定，不能默认最近一个。'},
        'groupingCaptureRefs': {**array(text, 20), 'uniqueItems': True},
        'delivery': {'enum': ['TEXT', 'DOCUMENT']},
        'resultRefs': array(obj({'pythonRunRef': text, 'outputRefs': array(text, 1000, 1)},
                                 ['pythonRunRef', 'outputRefs']), 50, 1),
        'search': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'reason': text, 'failedToolCallIds': array(text, 20, 1), 'evidenceCaptureRefs': array(text, 20, 1)}, ['action'])
    result['allOf'] = [{'if': {'properties': {'action': {'const': 'declare'}}},
                        'then': {'required': ['mode', 'goal']}},
                       {'if': {'required': ['contextRefs']},
                        'then': {'properties': {'action': {'const': 'sources'}}, 'required': ['action']}}]
    result['properties']['materials']['description'] = (
        '按完整问题分别选择主体与对照需要的证据，不由比较算子反推材料。'
        '解释主体结果形成用 PRIMARY VALUES+FORMULAS，对照仅比较用 REFERENCE VALUES；'
        '明确只比较规模或分布时两组 VALUES 即可，不自动展开对照全体公式。')
    return result
