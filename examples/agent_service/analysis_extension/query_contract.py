"""Model-facing query contract; execution/session proofs remain host-owned."""
from copy import deepcopy
from .capture import leased_mcp_origin


def model_schema(native):
    schema = deepcopy(native)
    props = schema.get('properties', {})
    props.pop('sourceSelection', None)
    props.pop('contextText', None)
    props.pop('conditionContext', None)
    props.pop('requestId', None)
    props.pop('purpose', None)
    props['materialRole'] = {'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE'],
        'description': '本次查询的资料用途，默认 PRIMARY；对照取数使用 REFERENCE。材料种类仅由 AnalysisTask.materials 声明，底层 purpose 由宿主生成。'}
    schema['required'] = [key for key in schema.get('required', []) if key not in ('requestId', 'purpose')]
    scope = props.get('scope', {}).get('properties', {})
    scope['bindings'] = {'type': 'array', 'maxItems': 32, 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['text', 'candidateRef'],
        'properties': {'text': {'type': 'string', 'minLength': 1},
                       'candidateRef': {'type': 'string', 'minLength': 1}}},
        'description': '任务或表样简称先 search_entities，选择 scopeCandidates 的 candidateRef，text 保留用户原话；多候选结合上下文选择，不默认第一条。若发现本轮曾把同一检索词误填为未经确认的维度成员，用 AnalysisTask.scopeEdits 明确撤回该维度猜测后再绑定。'}
    props['basis'] = {'type': 'object', 'additionalProperties': False, 'required': ['kind'],
                     'properties': {'kind': {'type': 'string', 'enum': ['INTENT', 'FACTS', 'MEMBERS']},
                                    'sourceRef': {'type': 'string', 'minLength': 1}},
                     'allOf': [{'if': {'properties': {'kind': {'enum': ['FACTS', 'MEMBERS']}}},
                                'then': {'required': ['sourceRef']}, 'else': {'not': {'required': ['sourceRef']}}}],
                     'description': '省略或 INTENT 按本次条件查询；FACTS 只在 sourceRef 的冻结原事实内筛选/排序，不换指标、时期或新增属性分组。MEMBERS 沿用实际成员，可保留原指标增加 groupBy，也可查询新指标/时期/情景，不沿用旧数值条件；这是预算服务重新取数。成员范围已由 sourceRef 给定，不在共享短语或目标中重复旧机构/项目等成员条件。未改变的指标可省略 intentDraft.targets，由宿主恢复已确认目标。引用来自来源目录，不编造。'}
    def strip_evidence(value):
        if isinstance(value, dict):
            fields = value.get('properties', {})
            for key in ('sourceText', 'confirmedSource'):
                fields.pop(key, None)
            if isinstance(value.get('required'), list):
                value['required'] = [key for key in value['required'] if key not in ('sourceText', 'confirmedSource')]
            for child in value.values():
                strip_evidence(child)
        elif isinstance(value, list):
            for child in value:
                strip_evidence(child)
    strip_evidence(props.get('intentDraft', {}))
    draft = props.get('intentDraft', {})
    draft['required'] = [key for key in draft.get('required', []) if key not in ('targets', 'sharedConditions')]
    if 'sharedConditions' in draft.get('properties', {}):
        draft['properties']['sharedConditions']['description'] = (
            '本次共享条件。省略、[] 或非空列表均保留仍适用的已确认条件；非空列表补充本次条件。'
            '更换或解除情景、机构等条件使用 AnalysisTask.intent/scopeEdits，宿主同步释放旧条件及其校验上下文。')
    if 'targets' in draft.get('properties', {}):
        targets = draft['properties']['targets']
        targets['description'] = targets.get('description', '') + ' 继续同一指标只改范围/排序/分组时可省略，宿主复用已确认目标；更换指标必须填写。'
    scope['bindings']['description'] += ' 绑定后保持原 selection、指标、机构、时期和情景；仅因找到表样候选不改成 FULL_FORM。'
    dimension_scopes = draft.get('properties', {}).get('dimensionScopes')
    if dimension_scopes:
        dimension_scopes['description'] += (' 仅本次查询改变范围时填写；已在 AnalysisTask.scopeEdits 声明的相同选择器由宿主注入，无需重复。'
                                            'dimensionHint 只写维度名，范围放 selector；不写“组织机构（全部单位）”。'
                                            '继续同一范围时 HIERARCHY 省略的 level/where/rootText 沿用；解除层级或其他限制使用 scopeEdits 完整替换选择器，不能靠漏填清除。')
    return schema


async def configure(toolkit, resource_ids):
    if not hasattr(toolkit, '_get_available_tools') or not hasattr(toolkit, 'tool_groups'):
        return
    available = await toolkit._get_available_tools([group.name for group in toolkit.tool_groups])
    for registration in available.values():
        tool = registration.tool
        origin = leased_mcp_origin(tool, resource_ids)
        if origin and origin['originalToolName'] == 'execute_budget_query':
            # Each leased tool belongs to this agent session; never mutate the shared MCP tool.
            tool.model_input_schema = model_schema(tool.input_schema)
