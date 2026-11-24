"""Single public task contract for the coordinated result-reference upgrade."""

DIRECT_QUERY_DECLARATION = ('并列比较多项取值用 DIRECT + comparison；同一张表里的不同指标也各占一项。shared 填共同原词，每项 terms 填自己的词，宿主直接取数。单项或联合筛选用 queryTargets 登记目标 id。'
                            '另需计算、换算或解释查询过程时省略 queryTargets。完成查询后宿主附来源表，正文直接解读；额外要求仍须完成。')

DESCRIPTION = (
    f'每个用户轮先 declare。{DIRECT_QUERY_DECLARATION}宿主展示查询条件和结果表，有值时附一两句业务解读，整次无数据或全为空值时直接交表。ANALYSIS 用于分析，REPORT 用于明确报告。'
    '按对象选择取值 VALUES 或数据构成 DATA_COMPOSITION。具体数值的分析先看来源构成，横向口径比较取值；'
    '复合问题可先取值判断，再为相关对象追加目标。可选调查按需开展。'
    'goal 默认用本轮原问。沿用上一批成员查新值时只声明本轮目标；用户要求续做时再接续未完成的任务。'
    'intent 和 scopeEdits 只填变化项，null 明确解除。'
    '只填本轮条件。普通追问由宿主继承维值。指向旧结果时用 references 写原话和用途。'
    'contexts/sources 查看可选来源。subjectSources 完整替换采用集合，分主体和对照；目标可只追加自己的来源。'
    '建立或续用报告后直接发布。action=tools 查看工具，按需填 toolName。'
    '数值筛选用查询 valueFilter，FACTS 沿用旧事实，MEMBERS 沿用成员取新值。'
    '仅审核旧报告时提供 analysisRef 和 executionPolicy。status 查看详情，blocked 引用实际失败。'
)


def schema(obj, array, text, string_map, intent_fields):
    nullable_text = {**text, 'type': ['string', 'null'], 'description': '省略保留本轮决定；非空业务原文设置；null 明确清除。'}
    intents = {field: dict(nullable_text) for field in intent_fields}
    intents['formExecutionYear'] = {**nullable_text, 'pattern': '^[1-9][0-9]{3}$',
        'description': '用户明确限定从哪一年度的表内取数时，填写数据加载时期的四位年份。所查数据的时期填 period。传 null 可清除限定。'}
    intents['task']['description'] = '预算任务名称；预算/实际/预计放 scenario，不猜任务编码；null 清除。'
    intents['form']['description'] = '用户明确要从某张表取数时填表名。仅提到的业务对象写在查询项，由本体定位表样。'
    intents['order']['description'] = '用户要求排序时填排序依据和方向。联合筛选默认按同一对象并排展示各指标。'
    member_selector = obj({'type': {'enum': ['VALUES', 'ALL', 'HIERARCHY', 'FILTER']},
                          'members': array(text, 1000, 1), 'rootText': {**text, 'description':
                              '点名机构用原名。ALL 含本级和全部下属，HIERARCHY 仅填根时取全部下属。'},
                          'level': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                          'where': {'type': 'object'}}, ['type'])
    source_choice = obj({'sourceRef': text, 'role': {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
                         'membersFrom': text}, ['sourceRef'])
    result = obj({'action': {'enum': ['declare', 'status', 'blocked', 'select_results', 'contexts', 'sources', 'tools']},
        'mode': {'enum': ['DIRECT', 'ANALYSIS', 'REPORT']}, 'goal': text,
        'intent': obj(intents),
        'references': array(obj({'use': {'enum': ['FACTS', 'MEMBERS', 'FORM', 'TASK', 'QUERY_TRACE', 'REPORT', 'REQUERY']},
            'mention': text, 'role': {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
            'contextRef': text, 'sourceRef': text,
            'sourceCode': {**text, 'description': '引用原表样或原任务且仍有多个候选时，选用返回的编码。'}}, ['use', 'mention']), 20, 1),
        'context': obj({'mode': {'enum': ['CONTINUE', 'NEW', 'SYNTHESIZE'],
                                 'description': '普通取数直接填写本轮目标，系统处理可沿用的条件和表样。明确选择先前结果或综合多个来源时再指定上下文。'},
                        'contextRefs': {**array(text, 20), 'uniqueItems': True}}, ['mode']),
        'contextRefs': {**array(text, 20), 'description': '仅 action=sources：目录需要检索的历史上下文；declare 选择上下文使用 context.contextRefs。省略读取当前相关来源。'},
        'subjectSources': array(source_choice, 100, 1),
        'scopeEdits': array(obj({'dimensionHint': {**text, 'description': '元数据中的维度名称；层级数值填 selector.level。若此前登记了错误维度名，在同一声明中对原名称填 selector=null，并按正确维度名保留原选择条件。'}, 'selector': {'anyOf': [member_selector, {'type': 'null'}]}},
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
    result['properties']['toolName'] = {**text, 'description': 'action=tools 选目录中的工具名。'}
    result['properties']['comparison'] = obj({
        'shared': array(text, 32),
        'items': array(obj({'terms': array(text, 32, 1), 'periodText': {**text, 'description': '用户原文时期，例如“去年同期”；解析的年份放 periodRequest。'},
                            'scenario': {**text, 'description': '该项的目标情景原词。'},
                            'form': text, 'task': text,
                            'dimensions': {'type': 'object', 'additionalProperties': array(text, 1000, 1),
                                           'description': '该项独有的维度名及成员原词列表。'},
                            'periodRequest': {'type': 'object', 'description': '单期例 {"kind":"PERIOD","start":{"type":"YEAR","yearOffset":-1}}，参照期由所选原值提供。指定年份用 year 代替 yearOffset。区间 RANGE 用 start/end，年初累计 YEAR_TO_DATE 用 end。'},
                            'sameAs': {'type': 'integer', 'minimum': 1, 'description': '与前面某个历史项使用同指标、同表和同批对象时，填它的项序号，另填新时期或情景。'},
                            'sourceRef': {**text, 'description': '沿用已交付数值时，从来源目录选择；可单独填。'},
                            'contextRef': text, 'mention': {**text, 'description': '历史原值项填写本轮的指代原话。新查询项只需 terms。'}}), 100, 2),
        'groups': array({**array({'type': 'integer', 'minimum': 1}, 100, 2), 'uniqueItems': True}, 100, 1)
    }, ['items'])
    result['properties']['comparison']['description'] = ('普通多项对比。shared 填共同业务原词，items 每项 terms 只填区别词；时期可用 periodText。'
        '同一句比较不同时期或情景，每个指标在每种口径各列一项，填 terms、scenario、时期原话 periodText，以及解析出的 periodRequest。'
        '追问沿用上轮原值时才用 sourceRef。按原对象换时期或情景用 sameAs 和变化的 scenario 或 periodText、periodRequest。'
        '宿主从原值确定表样、对象和参照期。sameAs 自动配对；其他配对用 groups 如 [[1,3],[2,4]]。'
        '声明即取数。两门槛同时满足用查询筛选，数据构成或趋势分析用 objectives。')
    result['properties']['queryTargets'] = {**array(text, 100, 1), 'uniqueItems': True,
          'description': '单项查询、排序或多条件联合筛选时登记目标 id，例如 ["t1"]，取数 intentDraft.targets 复用这些 id。并列取值比较使用 comparison.items。还需要换算单位、选取限定名次或另行计算、分析时省略。'}
    result['properties']['objectives'] = array(obj({
        'capability': {'enum': ['VALUES', 'DATA_COMPOSITION']},
        'role': {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
        'question': {**text, 'maxLength': 500},
        'objectiveRef': text, 'sourceRef': text,
        'independent': {'type': 'boolean', 'description': '已有目标采用同一来源、用途和能力，确需另立业务问题时使用。更新已有目标用 objectiveRef。'},
        'membersFromObjective': {'oneOf': [{'type': 'integer', 'minimum': 1}, text],
            'description': '用上一查询已选成员时直接填 sourceRef。本轮需要先筛选再分析时，在产生新成员的 VALUES 目标省略 sourceRef，在此填该目标序号或引用。父目标显式绑定 sourceRef 时，其成员已确定。'},
        'memberSourceRef': {**text, 'description': '仅当该取值目标返回多个候选范围时选择。'},
        'exploratory': {'type': 'boolean', 'description': '额外探索可标记 true；采用探索结论时仍需依据。'},
        'factRefs': {**array(text, 100, 1), 'uniqueItems': True},
    }, ['capability']), 20, 1)
    result['properties']['objectives']['description'] = (
        '按对象选取值或数据构成。简单查数可省略。多个目标用简短 question 区分。'
        '取得结果后可追加目标，或用返回的 objectiveRef 细化已有目标。'
        '分析一部分对象时先按条件取值，采用已筛结果，或同时选择来源和其中的实际行。'
        'sourceRef 指定整个来源，factRefs 指定其中的行。对照用 REFERENCE。')
    result['properties']['scopeEdits']['description'] = (
        '机构以本轮明确指向为准，未指定时沿用已选范围。单个机构与该机构的下属层级是两种范围。')
    result['properties']['references']['description'] = (
        '仅明确指向旧对象时填写。FACTS 用原值，MEMBERS 以原成员查新指标，QUERY_TRACE 解释原查询，'
        'REPORT 续审报告，REQUERY 按原条件重新查询。仅沿用原表样或原任务用 FORM / TASK。mention 用本轮原词。出现多候选后选实际引用。')
    result['allOf'] = [{'if': {'properties': {'action': {'const': 'declare'}}},
                        'then': {'required': ['mode']}},
                       {'if': {'required': ['contextRefs']},
                        'then': {'properties': {'action': {'const': 'sources'}}, 'required': ['action']}}]
    result['properties']['materials']['description'] = (
        '按完整问题分别选择主体与对照需要的证据，不由比较算子反推材料。'
        '解释主体结果形成用 PRIMARY VALUES+FORMULAS，对照仅比较用 REFERENCE VALUES；'
        '明确只比较规模或分布时两组 VALUES 即可，不自动展开对照全体公式。')
    return result
