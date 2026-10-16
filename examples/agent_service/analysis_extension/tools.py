"""AgentScope host tools: selection and calculation, not budget queries."""

from __future__ import annotations

import json
import logging
from time import perf_counter

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from .models import AnalysisError, json_values

TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}, "maxProperties": 128}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


SELECTOR = obj({"captureRef": TEXT, "stepId": TEXT, "bindingRef": TEXT, "cellKeys": array(TEXT, 1000, 1)}, ["captureRef", "stepId", "cellKeys"])
SCOPE = obj({"label": TEXT, "members": {**array(TEXT, 1000, 1), "description": "使用 inventory 的 memberRef 或来源唯一的 objectId/key，不能填名称。跨来源同名 ID 必须用 memberRef；构建后以返回的 objectId 选择成员，MCP 请求仍用 sourceObjectId/原始维度。"}, "memberDimension": TEXT,
             "contextMode": {"enum": ["SINGLE_SNAPSHOT", "COMPOSITE"], "description": "预算默认同连接、sourceId/snapshotId/asOf/schemaHash 等数据边界，可包含重开后的不同 contextId/querySessionId；真实边界不同用 COMPOSITE，保留各查询上下文，不自动证明可比。"},
             "referenceMembers": {**array(TEXT, 1000), "description": "额外比较或背景对象；与主体 members 不重叠，不计入 population。计算用 inputRefs 或 select.objectId 显式选取。"},
             "reportTopics": array(obj({"title": TEXT, "question": TEXT, "factRefs": array(TEXT, 1000, 1)}, ["title", "question", "factRefs"]), 50, 1),
             "expectedDimensions": {**STRING_MAP, "description": "本次全部所选单元格共同适用的维度约束。以 inventory.resolvedDimensions 为准；公式各项目不同的科目维度不能固定为目标科目。"}, "selectionReason": TEXT,
             "excludedMembers": array(obj({"objectId": TEXT, "reason": TEXT, "captureRef": TEXT}, ["objectId", "reason", "captureRef"]), 1000),
             "population": obj({"captureRef": TEXT, "stepId": TEXT, "memberPointer": TEXT}, ["captureRef", "memberPointer"])}, ["label", "members", "selectionReason"])
OPERATION = obj({"id": TEXT, "operator": {"enum": ["SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC"]},
                 "select": obj({"cellKey": TEXT, "measureCode": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "sourceContextId": TEXT, "dimensions": STRING_MAP}),
                 "inputRefs": array(TEXT, 1000, 1), "ruleRef": TEXT, "comparabilityRef": {**TEXT, "description": "已登记、适用于该算子和输入范围的可比规则 ID；从 AnalysisInspect(protocol).rules 发现，不自行编造。"}, "relationRef": TEXT, "aggregation": {"enum": ["SUM"]},
                 "expression": {"type": "string", "minLength": 1, "maxLength": 4096}, "wholePopulation": {"type": "boolean"},
                 "direction": {"enum": ["asc", "desc"]}, "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}}, ["id", "operator"])
NUMBER_FORMAT = obj({"grouping": {"type": "boolean"}, "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}})
CLAIM = obj({"missingEvidence": array(TEXT, 20, 1), "claimId": TEXT, "scopeId": TEXT, "kind": {"enum": ["fact", "computed", "hypothesis"], "description": "按实际 parts 引用选择：仅来源 factId 用 fact；包含派生 derivedId 用 computed；有条件的原因推测用 hypothesis。文字提到计算关系并不等于引用了派生结果；新计算须有真实派生引用。"},
             "parts": array({"oneOf": [obj({"text": {"type": "string", "maxLength": 2000}}, ["text"]),
                          obj({"valueRef": TEXT, "nodeRef": TEXT, "format": NUMBER_FORMAT, "field": {"enum": ["value", "unit", "positive", "negative", "zero", "observed", "unavailable", "sourceValue", "localValue", "localDifference", "difference", "childSum", "absoluteDifference", "localAbsoluteDifference"]}}, ["valueRef"])]}, 100, 1)}, ["claimId", "scopeId", "kind", "parts"])
PRESENTATION = obj({"labels": array(obj({"valueRef": TEXT, "role": {"enum": ["object", "measure"]}, "label": {"type": "string", "minLength": 1, "maxLength": 120}}, ["valueRef", "role", "label"]), 2000),
                    "groups": array(obj({"groupId": TEXT, "title": TEXT, "description": {"type": "string", "maxLength": 2000}, "descriptionParts": CLAIM['properties']['parts'],
                                         "width": {"enum": ["full", "half"], "description": "本主题通栏或半宽；half 仅在容器足够宽时并排，窄屏自动单列。"},
                                         "claimRefs": array(TEXT, 50), "claimsPosition": {"enum": ["before", "after"]}}, ["groupId", "title"]), 20),
                    "summaryPosition": {"enum": ["start", "end"], "description": "未分配到主题的结论置于报告开头或末尾；省略为 end。"},
                    "summaryTitle": {"type": "string", "minLength": 1, "maxLength": 200},
                    "reportTitle": {"type": "string", "minLength": 1, "maxLength": 200},
                    "claimsCollapsed": {"type": "boolean"}})
PRESENTATION['properties']['labels']['description'] = '模型选择可读简称。公式使用 role=measure，同源同口径同指标跨对象复用；role=object 只缩短对象名。不得改动指标含义；原始标题不变。'
PRESENTATION['properties']['groups']['items']['properties']['claimRefs']['description'] = '选择本次 claims 的 claimId 作为主题解读，按 claimsPosition 放在图表前或后。同一 claimId 只分配一次；未分配结论仍在总结中，不复制或丢弃。'
VIEW = obj({"relationLayout": {"enum": ["compact", "flow"], "description": "FORMULA/HIERARCHY 的组件布局；默认 compact，输入网格紧凑排列；flow 使用输入到结果的横向关系。"}, "showChecks": {"type": "boolean", "description": "FORMULA/HIERARCHY/WATERFALL 是否显示原值与测算值对照。未指定时，存在非零差额或 technicalDetails=true 则显示；明确 false 可关闭数值对照，但应通过节点图注就近解释差异。差额不是额外业务项目。"}, "emphasisRefs": array(TEXT, 100), "orientation": {"enum": ["horizontal", "vertical"], "description": "BAR_VALUES/BAR_RANK/WATERFALL/RECONCILIATION 的排列方向；瀑布图和汇总比较默认 vertical。"}, "groupId": TEXT, "orderRef": TEXT, "nodeDescriptions": array(obj({"nodeRef": TEXT, "parts": CLAIM['properties']['parts']}, ["nodeRef", "parts"]), 100),"showUnit": {"type": "boolean"}, "description": {"type": "string", "maxLength": 2000},
            "descriptionParts": CLAIM['properties']['parts'],
            "maxInputsPerScene": {"type": "integer", "minimum": 1, "maximum": 100},
            "includeDetails": {"type": "boolean"}, "collapsed": {"type": "boolean"},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 100}, "nodeRefs": array(TEXT, 1000, 1),
            "viewId": TEXT, "scopeId": TEXT, "title": TEXT, "relationRef": TEXT, "comparisonRef": TEXT,
            "numberFormat": NUMBER_FORMAT, "labels": array(obj({"valueRef": TEXT, "label": {"type": "string", "minLength": 1, "maxLength": 120}}, ["valueRef", "label"]), 1000),
            "field": {"enum": ["value", "positive", "negative", "zero", "observed", "unavailable"]},
            "type": {"enum": ["METRIC", "TABLE", "BAR_VALUES", "BAR_RANK", "BRIDGE", "ORDER", "FORMULA", "HIERARCHY", "WATERFALL", "RECONCILIATION"]},
            "valueRefs": array(TEXT, 1000, 1)}, ["viewId", "scopeId", "type", "valueRefs"])
THRESHOLD = obj({"factRefs": array(TEXT, 100, 1), "threshold": TEXT, "operator": {"enum": ["lt", "gt"]},
                 "unit": TEXT, "source": TEXT}, ["factRefs", "threshold", "operator", "unit", "source"])
FINDING_CHECK = {"oneOf": [
    obj({"kind": {"const": "SIGN"}, "factRefs": array(TEXT, 1000, 1), "sign": {"enum": ["positive", "negative", "zero"]}}, ["kind", "factRefs", "sign"]),
    obj({"kind": {"enum": ["UNIT_GAP", "MISSING_VALUE"]}, "factRefs": array(TEXT, 1000, 1)}, ["kind", "factRefs"]),
    obj({"kind": {"const": "COVERAGE_GAP"}}, ["kind"]),
    obj({"kind": {"const": "CALCULATION"}, "derivedRefs": array(TEXT, 1000, 1)}, ["kind", "derivedRefs"]),
    obj({"kind": {"const": "THRESHOLD"}, "ruleRef": TEXT}, ["kind", "ruleRef"]),
    obj({"kind": {"const": "RECHECK"}, "previousFindingRef": TEXT, "factRefs": array(TEXT, 1, 1)}, ["kind", "previousFindingRef", "factRefs"]),
]}
ACTION_PLAN = obj({"findingRefs": array(TEXT, 100, 1),
    "check": {"enum": ["VERIFY_VALUE", "CONFIRM_UNIT", "COMPLETE_COVERAGE", "VERIFY_COMPOSITION", "REQUEST_EVIDENCE"]},
    "goal": TEXT, "requiredEvidence": array(TEXT, 20, 1), "expectedDecision": TEXT, "priorityBasis": TEXT},
    ["findingRefs", "check", "goal", "requiredEvidence", "expectedDecision", "priorityBasis"])


class _AnalysisTool(ToolBase):
    is_concurrency_safe = True
    # These tools write private analysis artifacts but never mutate budget data.
    is_read_only = False

    def __init__(self, service):
        super().__init__()
        self.service = service

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="生成或读取当前会话的本地分析结果；不修改来源数据。")

    async def call(self, **kwargs):
        started = perf_counter()
        try:
            if self.validates_input_in_call:
                await self.service.validate_publication_input(kwargs)
            value = await getattr(self.service, self.method)(**kwargs)
            logging.getLogger(__name__).info("analysis stage=%s status=ok ref=%s elapsed_ms=%.2f", self.method, value.get("analysisRef", value.get("originAnalysisRef", "")), (perf_counter() - started) * 1000)
            delivery = value.get("delivery", value)
            metadata = {}
            if isinstance(delivery, dict) and delivery.get("registered") is True and isinstance(delivery.get("processSummary"), dict):
                metadata["process_summary"] = delivery["processSummary"]
            return ToolChunk(content=[TextBlock(text=json.dumps(json_values(value), ensure_ascii=False, allow_nan=False))], metadata=metadata)
        except AnalysisError as exc:
            logging.getLogger(__name__).info("analysis stage=%s status=refused code=%s elapsed_ms=%.2f", self.method, exc.code, (perf_counter() - started) * 1000)
            return ToolChunk(content=[TextBlock(text=json.dumps({"schemaVersion": 1, "error": exc.as_dict()}, ensure_ascii=False))], state=ToolResultState.ERROR)


class QueryResultRead(_AnalysisTool):
    name = 'QueryResultRead'
    method = 'read_query_result'
    is_read_only = True
    description = '只读当前会话已保存的完整查询回执，不触发查询或分析。pointer 原样复制 stepReads/readOptions/omitted 返回的 JSON Pointer，根为空字符串；stepId 不是数组下标。没有已知路径时先读根获得实际子路径；预算 v2 可读 /data/stepResults 的 stepId 字段以获得 itemPointers，其他来源使用其实际 JSON 结构，不能从 s2 推导下标 2。数组用 offset/limit 分页；容量不足自动减少完整返回项，严格沿 nextOffset 继续，不自行加 limit。search 按原文片段筛选，fields 选择直接字段。单个对象内部内容不受 limit 限制；过大时按错误 readOptions 的实际子路径或 availableFields 读取，不重复减 limit。itemPointers 指向原回执位置。文本 offset 为字符位置，limit 每单位 100 字符。来源完整性仍看 sourceState 及分支 completeness/resumeRef；读取分页不等于来源分页。'
    input_schema = obj({'captureRef': TEXT, 'pointer': {'type': 'string', 'maxLength': 2048},
                        'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                        'search': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'fields': array(TEXT, 30, 1)}, ['captureRef'])


WORK_ITEM = obj({'taskId': TEXT, 'title': TEXT, 'kind': {'enum': ['VALUES', 'COMPUTATION', 'RELATION']},
                 'refs': array(TEXT, 1000),
                 'operator': {**OPERATION['properties']['operator'], 'description': 'COMPUTATION 的预期运算类型；必须由该类型的真实计算结果完成。'},
                 'targetRefs': {**array(TEXT, 1000, 1), 'description': 'COMPUTATION 须处理的目标 factId，配合 operator。批量公式分析列出全部目标事实，每个目标须有自己的 FORMULA_CHECK，不能用另一个公式的操作数充数。'}}, ['taskId', 'title', 'kind', 'refs'])
WORK_REFS = array(obj({'taskId': TEXT, 'refs': array(TEXT, 1000)}, ['taskId', 'refs']), 100)
VIEW['properties']['nodeRefs']['description'] = '选择 FORMULA_CHECK 已追溯的 factRef；各 valueRefs 依次展示，每条追溯内按此列表顺序展示节点。只选择能说明当前问题的关键层，不必把整条链重复画出；省略才展示全部。运算符和计算依赖顺序不改变。'
VIEW['properties']['includeDetails']['description'] = '仅 FORMULA/HIERARCHY；按阅读需要附带明细。关键图不必附全表；完整核对数据可独立用 TABLE 引用同一派生结果并 collapsed=true。'

REPAIRS = array(obj({'op': {'enum': ['replace', 'remove', 'add']},
                    'path': {**TEXT, 'description': '错误返回的 JSON Pointer，如 /viewPlan/2/nodeRefs。add 可用 /presentation/labels/- 追加数组项，也可用 0 至当前长度的数字下标插入；replace/remove 须指向已有项。父路径须存在，数组下标不带前导零。仅修改指定路径，其他内容完整保留。'},
                    'value': {'description': 'replace/add 时必填；remove 时不传。仍按原字段契约校验。'}}, ['op', 'path']), 100, 1)

# Put the rules at their input fields so callers do not need to infer variants
# from a long tool description or load a reference document to fix one field.
for key, description in {
    'valueRefs': 'FORMULA/WATERFALL 填 FORMULA_CHECK 的 derivedId；RECONCILIATION 填 RECONCILE 的 derivedId；TABLE/BAR_VALUES 填来源 factId；BAR_RANK 填 RANK 的 derivedId；METRIC 填标量引用；HIERARCHY 首项父 factId，其后全部直属子项同指标 factId。',
    'nodeRefs': '仅 FORMULA/WATERFALL；选择要画出的计算层，填 formulaTrace.nodes[].factRef，按列表顺序展示。选中一个公式节点会展示其全部直接操作数；叶子节点不会生成独立公式图。不是用来删减单条公式的运算项。省略才展示全部计算层。',
    'includeDetails': '仅 FORMULA/HIERARCHY 支持，true 为图下附表。TABLE/RECONCILIATION 等必须省略此字段（false 也不传）。',
    'groupId': '引用 presentation.groups 的 groupId。包括 TABLE 在内，同组视图必须连续；不同单位或主题可自行分组。',
    'pageSize': '当前视图所附每张表独立的每页行数 1～100；不裁掉其他表的行。',
    'labels': '当前视图的局部简称，覆盖共享名称；同指标列需统一。跨视图优先在 presentation.labels 指定 role=measure/object。',
    'showUnit': '由模型选择是否显示来源单位；空单位始终为空，文字中也不添加未标注提示。',
    'orderRef': '仅 TABLE/BAR_VALUES/BAR_RANK；引用已完成 SORT/RANK 的 derivedId，各表顺序独立。',
    'descriptionParts': '模型编写业务说明；text 支持 **重点**，金额用 valueRef。与 description 二选一。',
}.items():
    VIEW['properties'][key] = {**VIEW['properties'][key], 'description': description}
CLAIM['properties']['kind']['description'] = '仅引用来源 factId 的陈述用 fact；引用计算 derivedId 用 computed；需要条件的推测用 hypothesis。不得为通过校验删除结论或替换金额。'
CLAIM['properties']['parts']['description'] = '业务解读与精确引用。用 text 中的 **...** 突出重点；不要把表格逐行复述，不写空单位提示或内部标识。'
_VALUE_PART = CLAIM['properties']['parts']['items']['oneOf'][1]['properties']
_VALUE_PART['valueRef'] = {**TEXT, 'description': '来源原值填 factId，field 仅 value/unit。公式计算填 FORMULA_CHECK derivedId，并用 nodeRef 选择节点；不能对 factId 直接取 localValue/difference。其他计算填实际 derivedId。'}
_VALUE_PART['field']['description'] = '来源 factId 仅 value/unit。FORMULA_CHECK derivedId：sourceValue=节点来源原值；localValue=仅用直接输入来源值做本层计算；value（省略 field 也相同）=逐层展开表内公式后的计算值。localDifference=sourceValue-localValue；difference=sourceValue-value；localAbsoluteDifference/absoluteDifference 分别为两种差额的绝对值。nodeRef 指定实际节点，省略时为追溯目标。本层一致不能推出逐层一致；写“逐层复算”时引用 value，不能引用 localValue。其他算子按其实际字段选择。'
_VALUE_PART['nodeRef'] = {**TEXT, 'description': '与 FORMULA_CHECK derivedId 配套使用的实际 factRef；节点图注显式绑定当前节点。省略表示追溯目标，不是当前画面自动选择的节点。'}
_VALUE_PART['format'] = {**NUMBER_FORMAT, 'description': '本片段的显示格式，不继承视图 numberFormat。按阅读需要选择 grouping/decimalPlaces，并与同口径图表保持一致；不改原值、不换算单位。'}
VIEW['properties']['nodeDescriptions']['description'] = '逐节点图注，nodeRef 指实际 factRef；parts 中的公式引用也显式给 nodeRef。描述本层计算用 localValue，逐层复算用 value，来源用 sourceValue。不能因单个字段引用合法就省略图文口径复核。'
VIEW['properties']['showChecks']['description'] += ' WATERFALL 始终按直接输入的来源值计算本层贡献，showChecks=false 不会把计算终点改成来源值；为 true 时另显示差额桥接和来源终点。多层图的下一层仍使用其直接输入来源值。'
PRESENTATION['properties']['labels']['description'] = '模型声明业务简称。measure 复用于同源同口径同指标的全部事实，object 复用于同一对象；不改来源名称。公式用简洁指标名，避免序号、填列说明和重复年份。重建/综合时从已有结果 summary.presentation.labels 取回仍适用的简称，核对当前事实引用并完整提交；仅补新增简称不会自动继承旧映射。'
WORK_ITEM['properties']['refs']['description'] = 'VALUES 填 factId；COMPUTATION 填实际 derivedId（含 derived_ 前缀）；RELATION 填 relationRef，不填 RECONCILE 派生引用。暂时没有引用填 []，发布前用 workRefs 补齐。'
WORK_ITEM['properties']['kind']['description'] = 'RELATION 只枚举关系成员；数值汇总核对另外声明 COMPUTATION，operator=RECONCILE；不能以枚举完成代替数值核对完成。'
OPERATION['properties']['operator']['description'] = '按来源数值排序选择 SORT（含无单位原值及负数）；RANK 另外要求可比单位、缩放和口径。FORMULA_CHECK 每项仅处理一个目标；批量目标分别运算。'
SCOPE['properties']['population']['description'] = '主体 members 的完整枚举来源，必须恰好对应 members。额外比较对象用 referenceMembers；不要为加入比较值删除本字段。'


class AnalysisBuild(_AnalysisTool):
    name = "AnalysisBuild"
    method = "build"
    description = "从本会话已登记来源适配器捕获的回执构建结构化结果，供分析或报告共用。仅在用户要求分析、图形或报告时使用。普通查询和排序直接文字或表格答复，无需构建发布；预算同连接及 sourceId/snapshotId/asOf/schemaHash 等固定数据边界可纳入多个 contextId/querySessionId，跨快照或来源须显式 scope.contextMode=COMPOSITE，各事实保留 sourceContextId。汇编不表示可比；跨期或跨绑定计算须提供 protocol.rules 中适用的 comparabilityRef，缺规则不自行声明等价。用户指定图形只展示原值时仍须传 operations:[]。必填顶层字段为 captureRefs、selections、scope、operations、requestKey。模型只选择对象、单元格及算子，不能提交替代金额。先用 AnalysisInspect 发现 captureRef、stepId、cellKey 和当前引用。scope.members 填 inventory.memberRef 或来源唯一的 objectId，跨来源重名必须用 memberRef；Build 返回的 objectId 用于后续选择，sourceObjectId 保留原 ID 供 MCP 查询；不能填名称；名称捕获用 inventory.objectLabelCaptureRefs 或 entities 纳入 captureRefs；expectedDimensions 只填全部所选项目共同的口径，使用 resolvedDimensions 核对。综合多轮报告先从用户问题确定所有主题，用 scope.reportTopics 声明 title/question/factRefs，再构建覆盖主题的新结果；不能直接导出最后一个窄范围版本。SORT/COUNT/RANK 用 select 或实际 inputRefs 限定每对象一个指标；不能全选混合指标。派生引用须由本次 Build 的前序运算实际产生，重建时保留仍需使用的运算，不猜引用。单位/规则/完整性不满足时仅阻断对应计算，原始事实仍可核对。FORMULA_CHECK 的 executionStatus 表示执行完成，sourceConsistency/targetLocalCheck 才表示递归/本层一致性；externalBoundaries 的来源值已计入。RECONCILE 显式指定 aggregation=SUM、relationRef、inputRefs（父项在首、其后全部直属子项），返回父项减子项合计，不默认认定来源规则。ARITHMETIC 用 inputRefs 的 v0/v1 等与 expression 调用通用精确计算器生成可引用结果，按需 decimalPlaces；只提供算术能力，不证明业务因果或可加性。workPlan 由模型声明本题必要工作项；多步骤分析应声明，单纯展示不强制创建分析计划。每项 {taskId,title,kind,refs}，kind=VALUES/COMPUTATION/RELATION；refs 指向事实/派生结果/关系，未取得填空。completion 只检查真实引用，不能通过删除待办宣布完成。声明 partialReason 后整体状态保持 INCOMPLETE，declaredWorkState 单独表示声明项状态；任务清单与实际未完成范围保持一致。同一任务补数据或重建时，baseAnalysisRef 引用上次草稿或已发布版本；继承其工作项，更新或新增同 taskId 的真实引用，省略或空 workPlan 不删除旧项。同一主体省略 population 时保留原枚举；改主体须提供对应枚举。主体 members 与 referenceMembers 分开，默认算子只选主体，比较对象须显式 inputRefs/select.objectId。分析方法见 data-analysis，报告组织见 report-generation。"
    description += ' 按原值从高到低展示使用 SORT；只有要求可比业务排名且满足单位/缩放条件时才用 RANK，不手抄排列替代失败运算。批量公式工作项用 operator=FORMULA_CHECK、targetRefs 列出每个待分析目标 factId，refs 列每个目标对应的 derivedId；补齐 missingTargetRefs 后才完成。'
    input_schema = obj({"captureRefs": array(TEXT, 1000, 1), "selections": array(SELECTOR, 1000, 1), "scope": SCOPE,
                        "operations": {**array(OPERATION, 100), "description": "必填；不需要运算时显式传 []，不能省略。"}, 'workPlan': array(WORK_ITEM, 100), "requestKey": TEXT, "baseAnalysisRef": TEXT, "actionRunRef": TEXT},
                       ["captureRefs", "selections", "scope", "operations", "requestKey"])


class AnalysisInspect(_AnalysisTool):
    name = "AnalysisInspect"
    method = "inspect"
    is_read_only = True
    description = "分页读取本会话分析输入、精确值引用和校验诊断，不触发新来源查询。section=entities 读取对象 key/label/modelCode/resourceIri 及名称 captureRef（可按 objectId 或 captureRef 筛选；搜索候选只补名称，不证明成员完整）；section=results 发现本会话已有结果及主题范围；selectors 按 captureRef 返回实际 stepId/memberPointers/populationSelector；inventoryIssues 分页读被隔离的无效输入。facts/inventory 返回受字符预算限制，严格沿 nextOffset 继续，不自行加 limit；facts 支持 factRef 定位及 fields 精确选择，detail=full 恢复全字段；来源捕获字段为 captureRefs 数组，inventory 的 captureRef 是另一种结构。INVALID_FIELDS 返回 availableFields，不猜字段名。inventory 默认 detail=compact，以 contextRef 引用顶层 contexts，detail=full 读全元数据；支持 captureRef/stepId/cellKey 精确选择，对象筛选须同时给出 memberDimension；inventory/capture 的 memberRef 是成员来源引用，sourceObjectId 是原始 ID。跨来源同名 ID 用 memberRef 填 members/referenceMembers，Build 后的选择使用实际返回的 objectId；memberRef 不能作为 MCP 业务 ID。名称按 dimensionModels/objectModelCode 匹配实体模型。section=inventory 汇集当前会话各次查询的单元格、对象名称、任务时期、值及引用，支持 memberDimension/objectId/bindingRef/period/measureCode/search 分页筛选，不合并不同观察；section=captures 列输入；capture 定位单元格；receipt 核对原请求；protocol 查算子及已登记规则；facts/derived/summary 读分析；formulas 加 factRef 分页读表内依赖及具体断点；order 按 derivedId 分页读排序结果；relations 读取来源证明的一级关系，用于 HIERARCHY 图；省略 analysisRef 时发现全会话已捕获关系，带 analysisRef 时仅读该版本。"
    description += ' summary 同时返回模型声明的 presentation（含共享 labels）和 viewPlan（含局部 labels），用于综合或重建时恢复展示约定；由模型筛选、补充并重新提交，不自动套用旧布局。'
    input_schema = obj({"section": {"enum": ["inventory", "inventoryIssues", "entities", "results", "selectors", "captures", "capture", "receipt", "rows", "protocol", "facts", "derived", "formulas", "summary", "claims", "verificationRecords", "relations", "order", "delivery", "findings", "actions", "preview"]},
                        "previewRef": {**TEXT, 'description': '仅 section=preview，传成功预览返回的 repairRef；读取当前文档的页面检查记录，不会代替浏览器运行检查。'},
                        "analysisRef": TEXT, "captureRef": TEXT,
                        "derivedId": {**TEXT, "description": "section=order 使用 SORT/RANK 派生引用。section=formulas 使用 factRef，不能填这里；section=derived 列出当前版本已完成计算。"}, "stepId": TEXT, "search": TEXT,
                        "detail": {"enum": ["compact", "full"]}, "fields": {**array(TEXT, 30, 1), 'description': '仅 section=facts 可用；inventory 不传 fields，用 detail=compact 和对象/单元格筛选。'}, "cellKey": TEXT, "factRef": {**TEXT, "description": "section=formulas 必填目标来源 factId；section=facts 可按实际 factId 定位。公式计算结果的 derivedId 不放这里。"}, "memberDimension": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "measureCode": TEXT,
                        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "findingPlan": array(FINDING_CHECK, 100), "thresholds": array(THRESHOLD, 20)})


DOCUMENT_BINDING = obj({'key': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
                        'valueRef': TEXT, 'field': TEXT, 'nodeRef': TEXT, 'format': NUMBER_FORMAT}, ['key', 'valueRef'])
DOCUMENT_DATASET = obj({'key': DOCUMENT_BINDING['properties']['key'], 'valueRef': TEXT,
                        'nodeRefs': array(TEXT, 200, 1), 'format': NUMBER_FORMAT}, ['key', 'valueRef'])
DOCUMENT_DATASET['description'] = '绑定 SORT/RANK、FORMULA_CHECK 或 RECONCILE 的真实 derivedId。window.analysisSets[key] 提供后端排好序的 rows、公式 nodes/operands/values/contributions 或 parent/children/values。nodeRefs 仅为公式选择关键计算层，每层仍保留全部操作数。数字为精确字符串，display 已格式化。'
DOCUMENT = obj({'title': {'type':'string','minLength':1,'maxLength':300},
                'path': {**TEXT, 'description':'当前工作区内 UTF-8 .html 的相对路径；成功读取后冻结内容。'},
                'html': {'type':'string','minLength':1,'maxLength':524288},
                'bindings': array(DOCUMENT_BINDING, 2000), 'datasets': array(DOCUMENT_DATASET, 50)}, ['title'])
DOCUMENT['anyOf'] = [{'required': ['bindings'], 'properties': {'bindings': {'minItems': 1}}},
                     {'required': ['datasets'], 'properties': {'datasets': {'minItems': 1}}}]
DOCUMENT['oneOf'] = [{'required':['path'], 'not':{'required':['html']}}, {'required':['html'], 'not':{'required':['path']}}]
DOCUMENT['description'] = '自由 HTML 页面，无固定布局或图型。path/html 二选一。bindings 绑定真实 factId/derivedId，可用 field/nodeRef/format。正文 {{key}} 自动填展示值；内联图表脚本从 window.analysisData[key].value/display 读取可信值。完整 HTML 使用内联 CSS/JS/SVG；无外部依赖或 http-equiv。预览冻结源文件与绑定，最终复用同一 repairRef 发布。'
DOCUMENT['description'] += ' bindings/datasets 至少一个非空。表格与图共用同一排序集合 rows，不手抄重排；容器标 data-analysis-set="集合key"，行/图形项标 data-analysis-ref="factRef" 以检查顺序；筛选或分页容器加 data-analysis-partial="true"。公式树使用 factRef 与运算符，由模型为全部操作数编写简称，按问题选图，不能把差额当作来源值的一部分。'
DOCUMENT['description'] += ' 声明了已完成排序工作项时必须绑定其排序集合。部分交付在模型选择的可见正文位置放 {{analysisPartialReason}}，宿主只填声明内容，不插入固定版式。'
DOCUMENT['description'] += ' 选择同一零基线的线性条形时，为定量条形声明 data-analysis-scale 与 data-analysis-bar-value（标量 key），或 data-analysis-bar-set 与 data-analysis-bar-ref（排序集合与 factRef）；不适用的图型不套该标记。diagnostics.renderChecks.viewports[].numericChecks 分别说明计数/比例的受检范围，NOT_DECLARED 不表示图形正确。'


class AnalysisPublish(_AnalysisTool):
    validates_input_in_call = True
    name = "AnalysisPublish"
    method = "publish"
    description = "发布不可变分析版本。展示默认由模型自由编写 HTML/CSS/内联图表，通过 document 提交页面路径或源码及真实值绑定；无需同时填固定 viewPlan 或 presentation。布局、图型、简称、业务文字和交互由模型选择，源数字用 bindings；新计算仍由 AnalysisBuild 算子完成。先 previewOnly=true 检查实际预览与 diagnostics，再用返回 repairRef、repairs=[]、新 requestKey 正式发布；该流程冻结成功预览的源码，文件后续编辑不会改变最终版本。需要修改页面时 repair /document 为新的完整文档输入并再次预览。参数直接放顶层，不嵌套 arguments。仅 delivery.registered=true 才交付返回链接。workRefs 补已声明工作项；真实未完成事项用 partialReason，不删待办。原有显式组件能力使用 claims+viewPlan，与 document 二选一；模型默认使用自由页面。引用校验不证明自由文字、图形和脚本输出正确，未实际看页面不能称视觉验收完成。"
    description += ' 自由页面正式发布前须留下当前内容摘要、两种实际容器宽度的页面检查记录；宿主配置无头渲染服务时，成功预览会自动测量，先读 diagnostics.renderChecks/renderService 并复用有效记录。RUN_RENDER_CHECKS 表示记录仍不完整，使用可用浏览器打开 previewPage 检查，不是重新 Build。浏览器确实不可用才用 renderCheckUnavailableReason，保留未检查状态；不能绕过已测到的错误。repairFields 给出保存草案顶层字段的 add/replace；null 也属于已有字段。PREVIEW_SCOPE 若返回 retryPreview，直接用它重试，勿修改旧草案的基础版本。'
    input_schema = obj({"document": DOCUMENT, "previewOnly": {"type": "boolean", "description": "true 校验并生成与正式发布相同的预览和检查信息，不发布、不登记卡片。返回 repairRef；修正后可继续预览，最终用 repairRef+repairs=[] 原样发布。"}, "presentation": PRESENTATION, "analysisRef": TEXT, "claims": array(CLAIM, 50), "viewPlan": array(VIEW, 20), "requestKey": TEXT,
                        'repairRef': {**TEXT, 'description': '失败或预览返回的保存草案。修复检查传 analysisRef/requestKey/expectedBaseRevision/repairRef/repairs 和 previewOnly=true；成功预览后才省略 previewOnly 或设 false 正式发布，不重写完整计划。'},
                        'repairs': {**REPAIRS, 'minItems': 0},
                        'workRefs': {**WORK_REFS, 'description': '为 Build 已声明的 taskId 补充真实 refs；仅更新引用，不删除或重命名工作项。'},
                        'partialReason': {'type': 'string', 'minLength': 1, 'maxLength': 2000, 'description': '确实需要先交付部分结果时，用业务语言说明尚缺什么。自由页面在正常正文放置 {{analysisPartialReason}} 保留文本槽，由宿主填值；聊天也须说明未完成范围，不把未完成工作说成已完成。'},
                        'renderCheckUnavailableReason': {'type': 'string', 'minLength': 1, 'maxLength': 1000, 'description': '仅浏览器检查确实不可用时填写具体原因；记录未检查状态，不证明布局合格，也不能跳过已测到的错误。修复草案时通过 repairs 修改此字段。'},
                        "expectedBaseRevision": {"type": "integer", "minimum": 0, 'description': '原样使用 AnalysisBuild 返回的 baseRevision，不能填草稿 revision 或 analysisRef 的版本后缀。'}, "technicalDetails": {"type": "boolean"},
                        "findingPlan": array(FINDING_CHECK, 100), "actionPlan": array(ACTION_PLAN, 300),
                        "thresholds": array(THRESHOLD, 20),
                        "hypotheses": array(obj({"findingId": TEXT, "statement": TEXT, "missingEvidence": array(TEXT, 20, 1)}, ["findingId", "statement", "missingEvidence"]), 30)}, ["analysisRef", "requestKey", "expectedBaseRevision"])
    input_schema['oneOf'] = [{'required': ['document'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['viewPlan']}, {'required':['presentation']}]}},
                             {'required': ['claims', 'viewPlan'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['document']}]}},
                             {'required': ['repairRef', 'repairs'], 'not': {'anyOf': [{'required': [key]} for key in
                              ('claims', 'viewPlan', 'document', 'presentation', 'workRefs', 'partialReason', 'renderCheckUnavailableReason', 'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses')]}}]


class AnalysisFollowUp(_AnalysisTool):
    name = "AnalysisFollowUp"
    method = "follow_up"
    is_concurrency_safe = False
    description = "仅在用户发送已登记行动的完整追问后恢复事实、范围与实际只读能力，创建或复用宿主追查记录；不执行预算查询。已选择 data-analysis 时先读其 actions-and-follow-up。用户改目标时新建分析并保留 baseAnalysisRef。只有返回 RUNNING 才继续查询；使用返回的 actionRunId 构建新版本，不能自行标 completed。"
    input_schema = obj({"actionRef": TEXT, "requestKey": TEXT}, ["actionRef", "requestKey"])


class AnalysisRenderReport(_AnalysisTool):
    name = "AnalysisRenderReport"
    method = "render_report"
    description = "由 AgentScope 宿主 reports 能力提供，report-generation 指导使用，从已发布的查询或分析结果 analysisRef 生成并登记自包含 HTML，自由页面按发布时冻结的内容导出，复用同一份图文与交互，不重新套模板、不新增分析。仅导出传入版本的范围，不会自动收集此前对话。“综合以上”须先按 report-generation 的 report-scope 流程构建并发布覆盖各主题的新结果。默认导出完整发布范围；显式 viewParameters 才导出筛选范围。自由页面使用默认完整范围及 standard 布局；需要更改内容或范围时创建新分析版本，并通过 AnalysisPublish(document=...) 重新预览发布。返回已登记 exportRef 和工作区路径；保持幂等请求标识。"
    input_schema = obj({"analysisRef": TEXT, "requestKey": TEXT, "layout": {"enum": ["standard", "table"]},
                        "viewParameters": obj({"filter": {"enum": ["all", "positive", "negative", "zero", "unavailable"]},
                                               "columnKey": {"anyOf": [TEXT, {"type": "null"}]},
                                               "sort": {"enum": ["published", "asc", "desc"]},
                                               "topN": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 1000}, {"type": "null"}]}})}, ["analysisRef", "requestKey"])
