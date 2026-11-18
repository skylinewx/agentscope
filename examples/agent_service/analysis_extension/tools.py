"""AgentScope host tools: selection and calculation, not budget queries."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from time import perf_counter

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from .models import AnalysisError, json_values
from .followup_scope import CHANGE_FIELDS, INTENT_FIELDS
from .native_analysis import STATISTIC_FIELDS
from .arithmetic_contract import ARITHMETIC_SYNTAX, MAX_ARITHMETIC_INPUTS

TEXT = {"type": "string", "minLength": 1, "maxLength": 1024}
STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}, "maxProperties": 128}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


SCALAR_REFERENCE = {'oneOf': [TEXT, obj({'valueRef': TEXT, 'field': TEXT, 'nodeRef': TEXT}, ['valueRef'])]}


FORM_SELECTOR = obj({"captureRef": TEXT, "stepId": TEXT, "bindingRef": TEXT, "executionPeriod": TEXT, "observationId": TEXT, "cellKeys": array(TEXT, 1000, 1),
                     "includeFormulaDependencies": {"type": "boolean", "description": "cellKeys 只填需要分析的目标。宿主沿同一观察的已捕获公式结构补齐表内依赖，并自动执行各目标的 FORMULA_CHECK；无需手工列依赖或另发检查操作。"}}, ["captureRef", "stepId", "cellKeys"])
BATCH_SELECTOR = obj({"captureRef": TEXT, "stepId": TEXT, "batchId": TEXT,
                      "columnNames": array(TEXT, 32, 1),
                      "rowIndices": array({"type": "integer", "minimum": 0}, 50000)},
                     ["captureRef", "stepId", "batchId"])
BATCH_SELECTOR['properties']['columnNames']['description'] = '单指标回执可省略，由宿主使用唯一指标列；多指标须选择实际指标列名，维度列自动保留。'
SELECTOR = {"oneOf": [FORM_SELECTOR, BATCH_SELECTOR]}
FORM_SELECTOR['properties']['preparedTargets'] = {'const': True,
    'description': '优先使用：复用本体已定位目标及依赖，不填坐标和公式引用；用 targetDimensions 缩小到本题已确认的维度，未给条件则保留全部候选。'}
FORM_SELECTOR['properties']['targetDimensions'] = {**STRING_MAP,
    'description': '仅用于 preparedTargets：从已返回候选中按已确认的维值筛选目标；不约束依赖节点，不猜未知维度。'}
FORM_SELECTOR['required'] = ['captureRef', 'stepId']
FORM_SELECTOR['oneOf'] = [{'required': ['cellKeys'], 'not': {'anyOf': [{'required': ['preparedTargets']}, {'required': ['targetDimensions']}]}},
                          {'required': ['preparedTargets'], 'not': {'anyOf': [{'required': ['cellKeys']},
                                                                            {'required': ['includeFormulaDependencies']}]}}]
for selector in (FORM_SELECTOR, BATCH_SELECTOR):
    selector['properties'].update(role={'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
                                  grain=array(TEXT, 16, 1), selectionReason=TEXT)

SCOPE = obj({"label": TEXT, "members": {**array(TEXT, 1000, 1), "description": "使用 inventory 的 memberRef 或来源唯一的 objectId/key，不能填名称。跨来源同名 ID 必须用 memberRef；构建后以返回的 objectId 选择成员，MCP 请求仍用 sourceObjectId/原始维度。"}, "memberDimension": TEXT,
             "contextMode": {"enum": ["SINGLE_SNAPSHOT", "COMPOSITE"], "description": "预算默认同连接、sourceId/snapshotId/asOf/schemaHash 等数据边界，可包含重开后的不同 contextId/querySessionId；真实边界不同用 COMPOSITE，保留各查询上下文，不自动证明可比。"},
             "referenceMembers": {**array(TEXT, 1000), "description": "额外比较或背景对象；与主体 members 不重叠，不计入 population。计算用 inputRefs 或 select.objectId 显式选取。"},
             "reportTopics": array(obj({"title": TEXT, "question": TEXT, "factRefs": array(TEXT, 1000, 1)}, ["title", "question", "factRefs"]), 50, 1),
             "expectedDimensions": {**STRING_MAP, "description": "本次全部所选单元格共同适用的维度约束。以 inventory.resolvedDimensions 为准；公式各项目不同的科目维度不能固定为目标科目。"}, "selectionReason": TEXT,
             "excludedMembers": array(obj({"objectId": TEXT, "reason": TEXT, "captureRef": TEXT}, ["objectId", "reason", "captureRef"]), 1000),
             "population": obj({"captureRef": TEXT, "stepId": TEXT, "memberPointer": TEXT}, ["captureRef", "memberPointer"])}, ["label", "selectionReason"])
SCOPE['properties']['members']['description'] = '批量 TABLE 默认省略，由宿主按所选行自动生成，包含空属性组。非批量或显式主体使用 inventory 的 memberRef 或来源唯一 key；不能抄名称或猜引用。'
OPERATION = obj({"id": TEXT, "operator": {"enum": ["SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC", "DESCRIBE"]},
                 "select": obj({"cellKey": TEXT, "measureCode": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "sourceContextId": TEXT, "dimensions": STRING_MAP,
                                "batchId": TEXT, "columnName": TEXT, "sourceObjectId": TEXT, "inputRole": {"enum": ["PRIMARY", "REFERENCE"]}}),
                 "inputRefs": array(TEXT, 1000, 1), "ruleRef": TEXT, "comparabilityRef": {**TEXT, "description": "已登记、适用于该算子和输入范围的可比规则 ID；从 AnalysisInspect(protocol).rules 发现，不自行编造。"}, "relationRef": TEXT, "aggregation": {"enum": ["SUM"]},
                 "expression": {"type": "string", "minLength": 1, "maxLength": 4096}, "wholePopulation": {"type": "boolean"},
                 "direction": {"enum": ["asc", "desc"]}, "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}}, ["id", "operator"])
OPERATION['properties']['top'] = {'type': 'integer', 'minimum': 1, 'maximum': 1000,
    'description': 'DESCRIBE 可选：统计按 direction 排序后的前 N 项合计及占比；仍保留全部来源行，不截断主体。'}
OPERATION['properties']['inputRefs']['items'] = SCALAR_REFERENCE
OPERATION['properties']['expression']['description'] = f'ARITHMETIC：最多 {MAX_ARITHMETIC_INPUTS} 个 inputRefs，按顺序绑定 v0、v1 等；每个输入必须参与，不传 variables，不抄原金额。' + ARITHMETIC_SYNTAX
OPERATION['allOf'] = [{'if': {'properties': {'operator': {'const': 'ARITHMETIC'}}},
                       'then': {'required': ['inputRefs', 'expression'],
                                'properties': {'inputRefs': array(SCALAR_REFERENCE, MAX_ARITHMETIC_INPUTS, 1)}},
                       'else': {'properties': {'inputRefs': array(TEXT, 1000, 1)}}}]
OPERATION['properties']['checkScope'] = {'enum': ['LOCAL', 'RECURSIVE'],
    'default': 'LOCAL',
    'description': 'FORMULA_CHECK 默认 LOCAL，按直接依赖的来源值计算；用户明确要求逐层追溯才用 RECURSIVE。深层缺数不否定已完成的本层核对。'}
NUMBER_FORMAT = obj({"grouping": {"type": "boolean"}, "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}})
CLAIM = obj({"missingEvidence": array(TEXT, 20, 1), "claimId": TEXT, "scopeId": TEXT, "kind": {"enum": ["fact", "computed", "hypothesis"], "description": "按实际 parts 引用选择：仅来源 factId 用 fact；包含派生 derivedId 用 computed；有条件的原因推测用 hypothesis。文字提到计算关系并不等于引用了派生结果；新计算须有真实派生引用。"},
             "parts": array({"oneOf": [obj({"text": {"type": "string", "maxLength": 2000}}, ["text"]),
                          obj({"valueRef": TEXT, "nodeRef": TEXT, "format": NUMBER_FORMAT, "field": {"enum": ["value", "unit", "positive", "negative", "zero", "observed", "unavailable", "sourceValue", "localValue", "localDifference", "difference", "childSum", "absoluteDifference", "localAbsoluteDifference"]}}, ["valueRef"])]}, 100, 1)}, ["claimId", "scopeId", "kind", "parts"])
CLAIM['properties']['parts']['items']['oneOf'][1]['properties']['field']['enum'] += ['total', *STATISTIC_FIELDS]
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
        from .models import digest
        turn = getattr(self.service, 'turn', None)
        failures = turn.state.setdefault('analysisInputFailures', {}) if turn is not None else {}
        input_key = digest([self.method, {key: value for key, value in kwargs.items() if key != 'requestKey'}])
        try:
            if self.method == 'build' and input_key in failures:
                failure = failures[input_key]
                raise AnalysisError(failure['code'], failure['message'],
                                    {**failure['details'], 'repeatedRequest': True,
                                     'recoveryAction': 'CORRECT_IDENTIFIED_INPUT',
                                     'hint': '相同失败输入已验证，未再次构建或取数；按具体诊断修正参数，不能删除必要成员绕过。'})
            if self.validates_input_in_call:
                await self.service.validate_publication_input(kwargs)
            value = await getattr(self.service, self.method)(**kwargs)
            logging.getLogger(__name__).info("分析阶段=%s 状态=成功 版本=%s 耗时毫秒=%.2f", self.method, value.get("analysisRef", value.get("originAnalysisRef", "")), (perf_counter() - started) * 1000)
            delivery = value.get("delivery", value)
            metadata = {}
            if self.method == 'build' and value.get('calculationSummary'):
                metadata['calculationPerformance'] = value['calculationSummary']
                logging.getLogger(__name__).info('分析运算汇总 总数=%s 已完成=%s 受阻=%s 重复失败=%s',
                    value['calculationSummary']['operationCount'], value['calculationSummary']['completedCount'],
                    value['calculationSummary']['blockedCount'], value['calculationSummary']['repeatedFailureCount'])
            if self.method in ('inspect', 'read_query_result') and isinstance(value.get('results'), list):
                metadata['readPerformance'] = {
                    'requestCount': len(value['results']),
                    'failedRequestCount': sum(item.get('status') == 'ERROR' for item in value['results']),
                    'partialRequestCount': sum(item.get('status') == 'PARTIAL' for item in value['results'])}
            if isinstance(delivery, dict) and delivery.get("registered") is True and isinstance(delivery.get("processSummary"), dict):
                metadata["process_summary"] = delivery["processSummary"]
            return ToolChunk(content=[TextBlock(text=json.dumps(json_values(value), ensure_ascii=False, allow_nan=False))], metadata=metadata)
        except AnalysisError as exc:
            if self.method == 'build' and exc.code in ('UNKNOWN_MEMBER_REFERENCE', 'SCOPE_MISMATCH', 'BATCH_GRAIN_MISMATCH', 'UNDECLARED_SCOPE_EXCLUSION'):
                failures[input_key] = {'code': exc.code, 'message': str(exc), 'details': exc.details or {}}
                while len(failures) > 32:
                    failures.pop(next(iter(failures)))
            logging.getLogger(__name__).info("分析阶段=%s 状态=拒绝 错误=%s 耗时毫秒=%.2f", self.method, exc.code, (perf_counter() - started) * 1000)
            return ToolChunk(content=[TextBlock(text=json.dumps({"schemaVersion": 1, "error": exc.as_dict()}, ensure_ascii=False))], state=ToolResultState.ERROR)


class AnalysisTask(_AnalysisTool):
    name = 'AnalysisTask'
    method = 'task'
    is_concurrency_safe = False
    description = '每个真实用户轮先登记本轮任务并用 intent 保留完整业务限定。DIRECT：普通查询/筛选/排序/简单统计/一般答复或必要澄清，不构建分析；ANALYSIS：分析、指标公式构成及分布构成查询，按问题完成所需 workPlan 与来源核验并首次丰富展示；构成查询不自动追加原因或预测；REPORT：用户明确要求报告，报告使用自由 HTML。不能根据负值、业务名称或工具存在推定分析。status 返回宿主实际完成检查；blocked 仅用于真实障碍，引用本轮失败调用 ID，或有明确诊断的分组 INCOMPLETE、完整确认不支持的 UNSUPPORTED 回执 evidenceCaptureRefs，并说明未完成范围。声明不证明完成，同轮不能降级绕过检查。'
    description += ' select_results 用 resultRefs 指定普通答复实际使用的脚本产物（pythonRunRef、outputRefs 中的 derivedId），宿主恢复必要输入链；探索记录仍保存但不充当最终答案。多个运行的结果须一并选择，不默认采用最近运行。'
    description += ' action=declare 必须同时给出 mode 和 goal；context.mode 是追问承接方式，不能代替顶层任务 mode。'
    description += ' 分组发现仅提供候选；执行原生聚合自动记录选用口径。多个候选择用或替换时，可在同一次 declare 用 groupingCaptureRefs 明确当前选用的发现回执；用户明确要求的多个口径须全部保留。'
    description += ' contexts 可按 search/offset/limit 读取本会话业务上下文目录。承接较早结果、继续未完成任务或综合多个结果时，在 context.contextRefs 引用目录的真实 contextRef；失败任务与已交付结果分别保存，不混用意图和范围。'
    input_schema = obj({'action': {'enum': ['declare', 'status', 'blocked', 'select_results', 'contexts']}, 'mode': {'enum': ['DIRECT', 'ANALYSIS', 'REPORT']},
                        'search': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                        'resultRefs': array(obj({'pythonRunRef': TEXT, 'outputRefs': array(TEXT, 1000, 1)}, ['pythonRunRef', 'outputRefs']), 50, 1),
                        'context': obj({'mode': {'enum': ['CONTINUE', 'NEW', 'SYNTHESIZE'], 'description': '普通关联追问 CONTINUE（默认继承上一轮有效范围）；独立新题 NEW；明确要求综合多轮内容才用 SYNTHESIZE。'},
                                        'contextRefs': {**array(TEXT, 20), 'description': 'CONTINUE 可指定一个历史结果或未完成任务的 contextRef；SYNTHESIZE 指定相关多个，NEW 不带引用。来自宿主业务上下文目录，不是本体 contextId。'},
                                        'changedFields': {**array({'enum': list(CHANGE_FIELDS)}, len(CHANGE_FIELDS)),
                                                          'uniqueItems': True, 'description': '只列本轮实际改变的 intent 字段；grouping/order 等变化不解除 population、task、period 边界。'},
                                        'userQuote': {**TEXT, 'description': '换题、综合或改变既定范围时，引用本轮用户表达该意图的原话；只改变时期不放开对象范围。'}}, ['mode']),
                        'intent': obj({key: {**TEXT, 'minLength': 0, 'description': '保留用户原始业务限定，追问沿用已确认条件；未指定可省略，空字符串按未提供处理；不猜系统编码。'}
                                       for key in INTENT_FIELDS}),
                        'requiredEvidence': {'enum': ['VALUES', 'FORMULAS'], 'description': '预算指标构成、负值原因、公式分析使用 FORMULAS；须取得分析对象的表展开和适用公式，不能用批量金额代替。'},
                        'formulaSubjects': {**array(STRING_MAP, 100, 1), 'uniqueItems': True,
                            'description': '解释上一轮已确定的明细对象时，在本次 declare 中直接提供需解释的主体维值映射列表。须唯一命中已选批量表的真实维值行，宿主一次取得各对象 INPUT_VO 并准备目标；总体分析仍可沿用原明细。不填写本体能力标识或单元格坐标。'},
                        'groupingCaptureRefs': {**array(TEXT, 20), 'uniqueItems': True,
                            'description': '可选：当前采用的分组发现 captureRef 全集。省略时沿用明确意图及原生聚合请求；仅探索未选用的候选不必取数。不能借此省略用户要求的口径。'},
                        'goal': {**TEXT, 'description': '登记用户实际要求的目标与范围。自行选用的例子、图表或附加明细属于展示选择。'},
                        'reason': TEXT, 'failedToolCallIds': array(TEXT, 20, 1), 'evidenceCaptureRefs': array(TEXT, 20, 1),
                        'delivery': {'enum': ['TEXT', 'DOCUMENT'], 'description': '分析、数据构成查询及报告使用 DOCUMENT，首次提供适用图形、解读和明细；普通单值、筛选、排序及简单统计可用 TEXT。'}}, ['action'])
    input_schema['allOf'] = [{'if': {'properties': {'action': {'const': 'declare'}}},
                              'then': {'required': ['mode', 'goal']}}]


class QueryResultRead(_AnalysisTool):
    name = 'QueryResultRead'
    method = 'read_query_result'
    is_read_only = True
    description = '只读当前会话已保存的完整查询回执，不触发查询或分析。优先复制 readOptions.read 的调用参数、nextRead 的续页参数；fields 只是导航投影，未选字段按本题需要另读。分析已知 capture 时可直接用 analysisRead 调用 AnalysisInspect，无需先通读原回执。pointer 原样复制 stepReads/readOptions/omitted 返回的 JSON Pointer，根为空字符串；stepId 不是数组下标。没有已知路径时先读根获得实际子路径；预算 v2 可读 /data/stepResults 的 stepId 字段以获得 itemPointers，其他来源使用其实际 JSON 结构，不能从 s2 推导下标 2。数组用 offset/limit 分页；容量不足自动减少完整返回项，严格沿 nextOffset 继续，不自行加 limit。search 按原文片段筛选，fields 选择直接字段。单个对象内部内容不受 limit 限制；过大时按错误 readOptions 的实际子路径或 availableFields 读取，不重复减 limit。itemPointers 指向原回执位置。文本 offset 为字符位置，limit 每单位 100 字符。来源完整性仍看 sourceState 及分支 completeness/resumeRef；读取分页不等于来源分页。'
    input_schema = obj({'captureRef': TEXT, 'pointer': {'type': 'string', 'maxLength': 2048},
                        'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                        'search': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'fields': array(TEXT, 30, 1)}, ['captureRef'])
    description += ' 多个已知对象或片段用 requests:[单次参数,...] 一次读取；allPages=true 由宿主在输出额度内续页。每项独立返回状态、pages、readComplete 和 nextRead，局部失败不丢失其他结果。资料已在 analysisMaterials 中给出时直接使用，不重复读取。'
    single_read_schema = deepcopy(input_schema)
    input_schema = obj({**single_read_schema['properties'], 'requests': array(single_read_schema, 12, 1),
                        'allPages': {'type': 'boolean', 'description': '仅用于 requests；在同一明确范围内自动读取后续页，超出模型额度时保留准确续读入口。'}})
    input_schema['oneOf'] = [{'required': ['captureRef'], 'not': {'anyOf': [{'required': ['requests']}, {'required': ['allPages']}]}},
                            {'required': ['requests'], 'not': {'anyOf': [{'required': [key]} for key in single_read_schema['properties']]}}]


WORK_ITEM = obj({'taskId': TEXT, 'title': TEXT, 'kind': {'enum': ['VALUES', 'COMPUTATION', 'RELATION']},
                 'refs': array(SCALAR_REFERENCE, 1000),
                 'requiredEvidence': {'enum': ['VALUES', 'FORMULAS'], 'description': '需要解释构成的工作项标 FORMULAS，并用 targetRefs 指定本项对象；适用于 Python 和原生算子。概览、排序、参考对象不自动扩为公式目标。'},
                 'operator': {**OPERATION['properties']['operator'], 'description': 'COMPUTATION 所需的真实运算；可由最终结果的已完成 inputRefs 依赖链证明。FORMULA_CHECK/RECONCILE 须直接引用各目标的检查结果，可同时附带已完成的差额等计算；附属计算不替代目标核对。'},
                 'targetRefs': {**array(TEXT, 1000, 1), 'description': 'COMPUTATION 须处理的目标 factId，配合 operator。批量公式分析列出全部目标事实，每个目标须有自己的 FORMULA_CHECK，不能用另一个公式的操作数充数。'}}, ['taskId', 'title', 'kind', 'refs'])
WORK_REFS = array(obj({'taskId': TEXT, 'refs': array(SCALAR_REFERENCE, 1000),
                      'operator': {'type': 'string', 'description': '可显式修正普通计算工作项的方法为已有结果的算子；不改变目标范围，不得替换 FORMULA_CHECK/RECONCILE/SUM/LINEAR/CONVERT 的来源核对和业务规则要求。'}}, ['taskId', 'refs']), 100)
OPERATION['properties']['inputRefs']['description'] = '非空的实际 factId/derivedId，或同一计划中已经定义的前序运算 id。按 select 筛选时省略 inputRefs，不能传 []。宿主解析并保存真实引用，不需要手工拼前缀。'
WORK_ITEM['properties']['refs']['description'] = 'VALUES 使用 factId；COMPUTATION 使用真实 derivedId 或本计划运算 id；RELATION 使用 relationRef。尚未得到结果填空数组。'
WORK_ITEM['properties']['refs']['description'] += ' COMPUTATION 可用 {valueRef,field,nodeRef?} 引用已有标量。统计任务引用 DESCRIBE 及其字段，不为每个统计值另建 ARITHMETIC。'
OPERATION['properties']['inputRefs']['description'] += ' ARITHMETIC 可用 {valueRef,field,nodeRef?} 直接计算已返回字段，如 negativeSum/sum；其他算子使用完整 ID。'
OPERATION['properties']['ruleRef'] = {**TEXT, 'description': '适用的已登记规则；同一原生聚合批次、同一指标、互斥组且来源 CalcType=SUM 时，SUM 可省略，由宿主使用来源规则；其他情况不推定可加。'}
WORK_ITEM['properties']['operator']['enum'] = [*OPERATION['properties']['operator']['enum'], 'PYTHON']
VIEW['properties']['nodeRefs']['description'] = '选择 FORMULA_CHECK 已追溯的 factRef；各 valueRefs 依次展示，每条追溯内按此列表顺序展示节点。只选择能说明当前问题的关键层，不必把整条链重复画出；省略才展示全部。运算符和计算依赖顺序不改变。'
VIEW['properties']['includeDetails']['description'] = '仅 FORMULA/HIERARCHY；按阅读需要附带明细。关键图不必附全表；完整核对数据可独立用 TABLE 引用同一派生结果并 collapsed=true。'

REPAIRS = array(obj({'op': {'enum': ['replace', 'remove', 'add', 'replaceText']},
                    'path': {**TEXT, 'description': '错误返回的 JSON Pointer，如 /viewPlan/2/nodeRefs。add 可用 /presentation/labels/- 追加数组项，也可用 0 至当前长度的数字下标插入；replace/remove 须指向已有项。父路径须存在，数组下标不带前导零。仅修改指定路径，其他内容完整保留。'},
                    'value': {'description': 'replace/add 时按原字段契约提供；remove 不传。replaceText 仅 path=/document/html，value={oldText:textTargets原文片段,newText:修正文字,textRef:可选真实引用}；原文须在单个静态文本节点中唯一匹配，优先用 textTargets 的 oldText/textRef；跨标签原句由宿主定位为现有片段，修改必要片段。保留原数值占位符，可补充引用页面已有 bindings 中的值；不新增绑定定义。多处修改一次提交。'}}, ['op', 'path']), 100, 1)

# Put the rules at their input fields so callers do not need to infer variants
# from a long tool description or load a reference document to fix one field.
for key, description in {
    'valueRefs': 'FORMULA/WATERFALL 填 FORMULA_CHECK 的 derivedId；RECONCILIATION 填 RECONCILE 的 derivedId；TABLE/BAR_VALUES 填来源 factId；BAR_RANK 填 RANK 的 derivedId；METRIC 填标量引用；HIERARCHY 首项父 factId，其后全部直属子项同指标 factId。',
    'nodeRefs': '仅 FORMULA/WATERFALL；选择要画出的计算层，填 formulaTrace.nodes[].factRef，按列表顺序展示。选中一个公式节点会展示其全部直接操作数；叶子节点不会生成独立公式图。不是用来删减单条公式的运算项。省略才展示全部计算层。',
    'includeDetails': '仅 FORMULA/HIERARCHY 支持，true 为图下附表。TABLE/RECONCILIATION 等必须省略此字段（false 也不传）。',
    'groupId': '引用 presentation.groups 的 groupId。包括 TABLE 在内，同组视图必须连续；不同单位或主题可自行分组。',
    'pageSize': '当前视图所附每张表独立的每页行数 1～100；不裁掉其他表的行。',
    'labels': '当前视图的局部简称，覆盖共享名称；同指标列需统一。跨视图优先在 presentation.labels 指定 role=measure/object。',
    'showUnit': '由模型选择是否显示来源单位，空来源字段仍为空。业务语义支持的金额默认元或换算后单位按统一规则在标题或文字中表达，不回写来源 unit，不添加未标注提示。',
    'orderRef': '仅 TABLE/BAR_VALUES/BAR_RANK；引用已完成 SORT/RANK 的 derivedId，各表顺序独立。',
    'descriptionParts': '模型编写业务说明；text 支持 **重点**，金额用 valueRef。与 description 二选一。',
}.items():
    VIEW['properties'][key] = {**VIEW['properties'][key], 'description': description}
CLAIM['properties']['kind']['description'] = '仅引用来源 factId 的陈述用 fact；引用计算 derivedId 用 computed；需要条件的推测用 hypothesis。不得为通过校验删除结论或替换金额。'
CLAIM['properties']['parts']['description'] = '业务解读与精确引用。用 text 中的 **...** 突出重点；不要把表格逐行复述，不写空单位提示或内部标识。'
_VALUE_PART = CLAIM['properties']['parts']['items']['oneOf'][1]['properties']
_VALUE_PART['valueRef'] = {**TEXT, 'description': '来源原值填 factId，field 仅 value/unit。公式计算填 FORMULA_CHECK derivedId，并用 nodeRef 选择节点；不能对 factId 直接取 localValue/difference。其他计算填实际 derivedId。'}
_VALUE_PART['field']['description'] = '来源 factId 仅 value/unit。FORMULA_CHECK：sourceValue=来源值；localValue/localDifference/localAbsoluteDifference=本层计算值、来源减本层的差额及绝对值；difference/absoluteDifference 始终为来源减逐层的差额及绝对值。value 未指定 nodeRef 时遵循操作 checkScope（LOCAL 为本层，RECURSIVE 为逐层），指定 nodeRef 时为该节点逐层值。本层 PASS 不保证逐层字段可用；本层差异用 localAbsoluteDifference，不因逐层缺数重新取数。nodeRef 省略时为目标节点。其他算子按实际字段选择。'
_VALUE_PART['nodeRef'] = {**TEXT, 'description': '与 FORMULA_CHECK derivedId 配套使用的实际 factRef；节点图注显式绑定当前节点。省略表示追溯目标，不是当前画面自动选择的节点。'}
_VALUE_PART['format'] = {**NUMBER_FORMAT, 'description': '本片段的显示格式，不继承视图 numberFormat。按阅读需要选择 grouping/decimalPlaces，并与同口径图表保持一致；不改原值、不换算单位。'}
VIEW['properties']['nodeDescriptions']['description'] = '逐节点图注，nodeRef 指实际 factRef；parts 中的公式引用也显式给 nodeRef。描述本层计算用 localValue，逐层复算用 value，来源用 sourceValue。不能因单个字段引用合法就省略图文口径复核。'
VIEW['properties']['showChecks']['description'] += ' WATERFALL 始终按直接输入的来源值计算本层贡献，showChecks=false 不会把计算终点改成来源值；为 true 时另显示差额桥接和来源终点。多层图的下一层仍使用其直接输入来源值。'
PRESENTATION['properties']['labels']['description'] = '模型声明业务简称。measure 复用于同源同口径同指标的全部事实，object 复用于同一对象；不改来源名称。公式用简洁指标名，避免序号、填列说明和重复年份。重建/综合时从已有结果 summary.presentation.labels 取回仍适用的简称，核对当前事实引用并完整提交；仅补新增简称不会自动继承旧映射。'
WORK_REFS['items']['properties']['refs']['description'] = '发布阶段填已有真实引用：VALUES 用 factId，COMPUTATION 用 derivedId 或 {valueRef,field,nodeRef?}，RELATION 用 relationRef；不使用计划内别名。'
WORK_ITEM['properties']['kind']['description'] = 'RELATION 只枚举关系成员；数值汇总核对另外声明 COMPUTATION，operator=RECONCILE；不能以枚举完成代替数值核对完成。'
OPERATION['properties']['operator']['description'] = '按来源数值排序选择 SORT（含无单位原值及负数）；RANK 另外要求可比单位、缩放和口径。FORMULA_CHECK 每项仅处理一个目标；批量目标分别运算。NO_CAPTURED_FORMULA 表示当前捕获未提供可复算公式，检查完成但不产生数值、不证明来源一致；executionStatus=COMPLETED 且 completionReason=SOURCE_VALUE_MISSING 表示来源空值已核对，无须补取；calculationValidation 仍 BLOCKED，不能作为有效数值。其他断点按实际缺口处理。分析方法与汇总预期由任务选择，算子不自动安排业务流程。'
OPERATION['properties']['operator']['description'] += ' DESCRIBE 一次完成同一指标的来源值正负零/缺值计数、合计、均值、极值、排序、占比及可选 top；提供可直接绑定的 TABULAR 表和 statistics/counts。描述统计不证明业务合并或层级去重；业务口径聚合使用来源接口。用 select.batchId+columnName 隔离实际集合，不能以是否存在某个维度猜输入类型。'
SCOPE['properties']['population']['description'] = '主体 members 的完整枚举来源，必须恰好对应 members。额外比较对象用 referenceMembers；不要为加入比较值删除本字段。'


class AnalysisBuild(_AnalysisTool):
    name = "AnalysisBuild"
    method = "build"
    description = (
        '从已捕获来源构建分析或报告。普通查询直接答复。首次传 captureRefs/scope/operations/requestKey，目标和用途已确定时由宿主生成 selections；需要指定子集、对照或候选才传选项。'
        '已有分析追加计算或工作项传 baseAnalysisRef/operations/requestKey，省略三个输入字段以沿用冻结事实、范围、粒度和有效 Python 结果；换数据才完整提交。'
        '已知 analysisMaterials/selections 直接使用，缺字段才定向 Inspect。批量 TABLE 选择 batchId/columnNames/可选 rowIndices，省略行号取全部实际行；'
        'scope.members 可省略，由选择器生成。显式成员用真实 memberRef 或唯一 objectId，名称不能替代 ID；expectedDimensions 仅共同口径。'
        'selections 的 PRIMARY/REFERENCE/STRUCTURE 区分主体、对照、结构，grain 为实际维度列；批量默认全部分组维度，不再按机构树去重。'
        '网格 cellKeys 是真实目标，includeFormulaDependencies=true 自动补齐同观察已捕获依赖并执行 LOCAL FORMULA_CHECK，已有检查不重复提交。'
        '同计划 inputRefs 可用前序 operation.id，COMPUTATION.refs 可用本计划运算 id，宿主解析真实 derivedId。'
        '单纯原值展示 operations=[]，workPlan 可省略；本体准备的目标使用 preparedTargets=true，自动带入来源值、依赖、公式计算和默认工作项，无需另填 targetRefs 或空的 VALUES 工作项。'
        '必要的额外计算和关系检查可声明工作项，概览不扩大公式范围。金额和构成使用 PRIMARY/REFERENCE，STRUCTURE 仅用于模板结构，不纳入数值计算。'
        '续建继承已有工作项及 population，不通过省略或空数组删除；partialReason 保留真实未完成范围。'
        '同连接固定 sourceId/snapshotId/asOf/schemaHash 可纳入多查询身份；跨边界用 COMPOSITE，各事实保留来源，汇编不证明可比。'
        '跨期/跨指标运算需要适用可比规则；原值顺序用 SORT，可比业务排名才 RANK。分组金额直接使用原生聚合，宿主另附同绑定 STRUCTURE 样本。'
        '公式核对执行完成不表示数值可用：来源真实空值保持 null/计算 BLOCKED，检查已完成时无需补取。'
        '绑定 documentInputs 展示本层/逐层结果与差额，不能用本层通过证明逐层。综合报告用 reportTopics 声明用户所指主题。'
        '分析方法见 data-analysis，页面交付见 report-generation；字段形状与运算约束见当前 Schema。')
    input_schema = obj({"captureRefs": array(TEXT, 1000, 1), "selections": array(SELECTOR, 1000, 1), "scope": SCOPE,
                        "operations": {**array(OPERATION, 100), "description": "必填；不需要运算时显式传 []，不能省略。"}, 'workPlan': array(WORK_ITEM, 100), "requestKey": TEXT, "baseAnalysisRef": TEXT, "actionRunRef": TEXT},
                       ["operations", "requestKey"])
    input_schema["anyOf"] = [{"required": ["captureRefs", "scope"]}, {"required": ["baseAnalysisRef"]}]
    input_schema["dependentRequired"] = {'selections': ['captureRefs', 'scope'], 'scope': ['captureRefs'], 'captureRefs': ['scope']}
    description += ' 优先使用原生能力，必要计算可一次 operations 提交。DESCRIBE 已提供来源值分布表、计数、合计和占比；公式数据集由 FORMULA_CHECK 提供，不为制作表格重写 Python。返回 documentInputs 给出直接可用的绑定方式，AI 继续决定解释、布局与配色。'


class AnalysisPython(_AnalysisTool):
    name = 'AnalysisPython'
    method = 'python'
    is_concurrency_safe = False
    description = '通用的 AI Python 分析执行：数据清洗、连接、透视、比较、统计、诊断、条件测算和图表数据准备。analysisRef+inputRefs 引用已有事实或代码产物；也可用 captureRefs+scope 汇编已准备的查询结果；范围有歧义、指定子集或对照时再传 selections；fileInputs 可补充工作区 JSON/CSV/TSV/TXT 文件，文件单独执行返回 pythonRunRef。inputs 含 ref、精确 value、dimensionNames、formula、inputRole、grain；表产物含 columns/rows，文件含 data。支持循环、条件、推导式、自定义函数以及 D/Decimal、json_read/json_write、csv_read/csv_write；支持正常 import/from import：decimal、collections、itertools、statistics、datetime、json、csv、io、math，以及已锁定的 pandas/NumPy/SciPy/statsmodels。第三方库由宿主自动准备并复用隔离环境；代码不能安装库或任意读写文件、联网。金额使用 Decimal/object，pandas 空值汇总保留 null；浮点金额与统计浮点产物当前不接收，不使用科学计数法输出。result 是非空数组，每项有 name/label 和来源 inputRefs。唯一网格被完整读取后，省略的来源引用可由宿主补记。kind=SCALAR 另需 value/unit，可用 decimalPlaces 指定输出小数位0～16（ROUND_HALF_EVEN，仅处理输出）；TABLE或CHART_DATA 另需 columns[{name,label,type,unit?,decimalPlaces?}]/rows，decimalPlaces只用于DECIMAL_STRING列，type=STRING/DECIMAL_STRING/BOOLEAN；DIAGNOSTIC 需 data；FILE 需 filename/text，只能输出 csv/tsv/json/txt。宿主保存代码、真实输入依赖、输出、错误及耗时，表产物可继续作为输入或 document.datasets 的 valueRef。金额使用 D("0.1")，null 不隐式变零。不替代预算取数、任务机构口径、CalcType、原生聚合或真实来源公式；执行成功仍需审查逻辑与解释。'
    description += ' 仅当现有原生能力不足以表达本题必要处理时编写代码。先看已有工具说明，不必试调用；fallbackReason 说明具体能力缺口，purpose 说明代码处理的内容。原因、代码和实际运行结果持久保存，供提取公共能力；不因偏好 Python 重写已有公式计算、分布统计或预算原生聚合。'
    input_schema = obj({'analysisRef': {**TEXT, 'description': '仅填写工具实际返回的业务分析 analysisRef；纯文件执行的 pythonRunRef 不能填这里。'}, 'inputRefs': array(TEXT, 5000, 0),
                        'fallbackReason': {'type': 'string', 'minLength': 1, 'maxLength': 2000,
                                           'description': '说明哪些必要处理无法由当前原生能力或其组合完成，例如完整网格文件的动态筛选与透视。不是内部思维过程，只记录简要的功能缺口。修复代码沿用同一缺口说明。'},
                        'code': {'type': 'string', 'minLength': 1, 'maxLength': 24000,
                                 'description': 'inputs 是宿主输入数组，每项有 ref。grid_rows(ref, where) 返回迭代器，使用 for cell in grid_rows(...) 遍历，不能下标访问或直接 len；其中 cell 是字典，含 sourceRef、key、value、dimensions 等，没有 ref。每项 result 必须有 name、label、inputRefs；依赖用实际 inputs[*]["ref"]，网格行可用 cell["sourceRef"]，不能用 cell["ref"] 或单元格坐标代替。value 已规范化，不从 displayValue 猜原值。当前执行器未开放 try/except、getattr、isinstance；无需为已知字典结构编写通用对象反射。对象分组使用实际 dimensions/grain/inputRole，名称仅展示；未知字段先 Inspect。空值按业务含义处理，不猜字段，不自动补零。'}, 'purpose': TEXT,
                        'requestKey': TEXT, 'timeoutSeconds': {'type': 'integer', 'minimum': 1, 'maximum': 60},
                        'answerOutputs': {**array(TEXT, 1000, 1), 'description': '本次产物足以支撑普通查询答复时，填写 result 中用于最终答复的 name（含需要引用的 DIAGNOSTIC），同次登记最终结果；探索阶段省略。多个运行联合回答用 AnalysisTask(select_results) 完整选择。不会将运行成功视作业务核对通过。'},
                        'captureRefs': array(TEXT, 1000, 1), 'selections': array(SELECTOR, 1000, 1), 'scope': SCOPE,
                        'operations': {**array(OPERATION, 100), 'description': '可同次先执行来源算子，如多个 FORMULA_CHECK，再运行脚本。算子沿用现有业务校验；脚本默认同时取得事实和本次算子产物，派生输入含 operator/formulaTrace。不需要先单独 Build。'},
                        'workPlan': {**array(WORK_ITEM, 100), 'description': '可随代码一次登记必要工作，不必执行后再 Build。COMPUTATION.refs 可用本次 result 项的 name，宿主绑定真实 derivedId；已有工作按 taskId 继承，不删未完成目标。'},
                        'fileInputs': array(obj({'path': TEXT}, ['path']), 20, 1),
                        'gridInputs': array(obj({'captureRef': TEXT, 'fileRef': TEXT}, ['captureRef', 'fileRef']), 20, 1)},
                       ['code', 'purpose', 'fallbackReason', 'requestKey'])
    input_schema['dependentRequired'] = deepcopy(AnalysisBuild.input_schema['dependentRequired'])
    description += ' 只有汇编事实或传入已有 analysisRef 时才创建可发布的 analysisRef，workPlan 可同次登记并引用 result.name，直接用返回的 derivedValues 发布。仅文件执行返回 pythonRunRef，不创建 analysisRef；继续计算复用 continuation.input，不把执行引用当分析引用。已有分析用 analysisRef+inputRefs 继续，失败按 retryInput 修代码，保留已取得的输入。'
    description += ' 大网格使用 gridInputs[{captureRef,fileRef}]，来源工具或 AnalysisInspect(gridFiles) 返回引用。inputs 中 kind=GRID_FILE 的 ref 可传给 grid_rows(ref,where) 流式遍历，返回迭代器而非列表，文件不进入 inputs.data；where 支持 cellKeys、dimensions、measureCode、headerText、mapTypes、startRow/endRowExclusive/startColumn/endColumnExclusive。返回单元格保留 key/headers/formula/rawValueJson，另有 sourceRef 而非 ref；result.inputRefs 引用输入文件的 inputs[*].ref。value 为原始精确值，dimensions 已合并表外条件。fileReads 由工具记录扫描量、命中量、是否遍历完成；只读取样例不得当作全表分析。最多60秒。需要生成报告时可与已有 analysisRef 同时使用，或先用脚本定位 cellKeys 再 AnalysisBuild，复用原文件，不重查预算。'
    description += ' 同一范围上的相关计算一次输出多个 result 项，例如排序表、占比、差额与图表数据；不强制每项单独执行。小表/诊断直接随 derivedValues 返回，payloadComplete=true 表示已完整返回，无需再 Inspect。代码依赖未知输入时才分步探索；不能为减少轮数省略必要公式或范围判断。'
    description += ' workPlan 引用脚本产物时用 kind=COMPUTATION、operator=PYTHON、refs=[result.name]，不因代码做比例而填写原生 RATIO；原生算子工作项须真实提供 operations。VALUES 仅引用实际 factId，不能填结果名称或 batchId。'
    description += ' 纯网格只传工具参数 gridInputs。脚本从 inputs 读取，例如单文件 ref=inputs[0]["ref"]；各产物的 inputRefs 可写 [ref]。TABLE/CHART_DATA 的 rows 必须是二维数组，例如 [["项目甲", "1.20"]]，每行按 columns 的顺序取值，不是字典列表。'
    description += ' 网格含文本表头；计算金额前按实际 measureCode 或 cellKeys 选中数据格，再读 value，空值保留。mapTypes 必须为数组，不是 mapType。'


class AnalysisInspect(_AnalysisTool):
    name = "AnalysisInspect"
    method = "inspect"
    is_read_only = True
    description = "分页读取本会话分析输入、精确值引用和校验诊断，不触发新来源查询。已知适用 captureRef 时 inventory 仅加载该捕获，不扫描无关历史；需要另一次捕获的名称时定向读 entities 并纳入引用。nextRead 是保留本次筛选条件的续读参数，可直接使用。section=entities 读取对象 key/label/modelCode/resourceIri 及名称 captureRef（可按 objectId 或 captureRef 筛选；搜索候选只补名称，不证明成员完整）；section=results 发现本会话已有结果及主题范围；selectors 按 captureRef 返回实际 stepId/memberPointers/populationSelector；inventoryIssues 分页读被隔离的无效输入。facts/inventory 返回受字符预算限制，严格沿 nextOffset 继续，不自行加 limit；facts 支持 factRef 定位及 fields 精确选择，detail=full 恢复全字段；来源捕获字段为 captureRefs 数组，inventory 的 captureRef 是另一种结构。INVALID_FIELDS 返回 availableFields，不猜字段名。inventory 默认 detail=compact，以 contextRef 引用顶层 contexts，detail=full 读全元数据；支持 captureRef/stepId/cellKey 精确选择，对象筛选须同时给出 memberDimension；inventory/capture 的 memberRef 是成员来源引用，sourceObjectId 是原始 ID。跨来源同名 ID 用 memberRef 填 members/referenceMembers，Build 后的选择使用实际返回的 objectId；memberRef 不能作为 MCP 业务 ID。名称按 dimensionModels/objectModelCode 匹配实体模型。section=inventory 汇集当前会话各次查询的单元格、对象名称、任务时期、值及引用，支持 memberDimension/objectId/bindingRef/period/measureCode/search 分页筛选，不合并不同观察；section=captures 列输入；capture 定位单元格；receipt 核对原请求；protocol 查算子及已登记规则；facts/derived/summary 读分析；formulas 加 factRef 分页读表内依赖及具体断点；order 按 derivedId 分页读排序结果；relations 读取来源证明的一级关系，用于 HIERARCHY 图；省略 analysisRef 时发现全会话已捕获关系，带 analysisRef 时仅读该版本。"
    description += ' summary 同时返回模型声明的 presentation（含共享 labels）和 viewPlan（含局部 labels），用于综合或重建时恢复展示约定；由模型筛选、补充并重新提交，不自动套用旧布局。'
    description += ' queryContexts 按最新优先读取本会话已捕获的查询条件、绑定、指标定位和任务机构类型，每页最多 10 个回执；追问只增加分组等条件时先复用仍适用的上下文，不重新发现任务和指标。未命中当前页时沿 nextOffset 继续；失败回执保留失败原因。'
    input_schema = obj({"section": {"enum": ["materials", "gridFiles", "inventory", "inventoryIssues", "entities", "results", "selectors", "captures", "queryContexts", "capture", "receipt", "rows", "protocol", "facts", "derived", "formulas", "summary", "claims", "verificationRecords", "relations", "order", "delivery", "findings", "actions", "preview", "python", "products"]},
                        'pythonRunRef': {**TEXT, 'description': 'section=python 读取成功、失败、取消或超时的执行记录；或用 analysisRef 列出该分析已登记的 Python 计算。'},
                        "previewRef": {**TEXT, 'description': '仅 section=preview，传成功预览返回的 repairRef；读取当前文档的页面检查记录，不会代替浏览器运行检查。'},
                        "analysisRef": TEXT, "captureRef": TEXT,
                        "derivedId": {**TEXT, "description": "section=order 使用 SORT/RANK 派生引用。section=formulas 使用 factRef，不能填这里；section=derived 列出当前版本已完成计算。"}, "stepId": TEXT, "search": TEXT,
                        "detail": {"enum": ["compact", "full"]}, "fields": {**array(TEXT, 30, 1), 'description': '仅 section=facts 可用；inventory 不传 fields，用 detail=compact 和对象/单元格筛选。'}, "cellKey": TEXT, "factRef": {**TEXT, "description": "section=formulas 必填目标来源 factId；section=facts 可按实际 factId 定位。公式计算结果的 derivedId 不放这里。"}, "memberDimension": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "measureCode": TEXT,
                        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "findingPlan": array(FINDING_CHECK, 100), "thresholds": array(THRESHOLD, 20)})
    description += ' 查询返回的 analysisMaterials 已含值、公式、上下文和真实选择器；材料足够时直接 Build/Python。section=materials 按 captureRef 补读同一份资料。多个目标或部分用 requests:[单次参数,...] 一次读取，allPages=true 自动续页；每项必须明确 captureRef/analysisRef/pythonRunRef，不扫描其他历史，不发出源查询。'
    description += ' materials 只用 captureRef/offset/limit，不接受对象或字段筛选；定向筛选使用 inventory/facts。'
    description += ' 同一问题已知多个 capture 或检索目标时，将所有独立 inventory 读取合并到 requests；每项用本题实际 search/cellKey。返回的值、公式和上下文足够后直接计算，不再分别读 capture 重复取同一单元格。'
    single_read_schema = deepcopy(input_schema)
    from .batch_reads import CAPTURE_SECTIONS, ANALYSIS_SECTIONS
    single_read_schema['properties']['section']['enum'] = list(CAPTURE_SECTIONS + ANALYSIS_SECTIONS) + ['python']
    single_read_schema['required'] = ['section']
    single_read_schema['oneOf'] = [
        {'properties': {'section': {'enum': list(CAPTURE_SECTIONS)}}, 'required': ['captureRef']},
        {'properties': {'section': {'enum': list(ANALYSIS_SECTIONS)}}, 'required': ['analysisRef']},
        {'properties': {'section': {'const': 'python'}}, 'anyOf': [{'required': ['pythonRunRef']}, {'required': ['analysisRef']}]},
    ]
    single_fields = list(input_schema['properties'])
    input_schema['properties'].update(requests=array(single_read_schema, 12, 1), allPages={'type': 'boolean'})
    input_schema['oneOf'] = [{'not': {'anyOf': [{'required': ['requests']}, {'required': ['allPages']}]}},
                            {'required': ['requests'], 'not': {'anyOf': [{'required': [key]} for key in single_fields]}}]
    shared_read_schema = deepcopy(single_read_schema)
    shared_read_schema.pop('oneOf')
    input_schema['properties']['requests'] = array(shared_read_schema, 12, 1)
    input_schema['oneOf'][1]['not']['anyOf'] = [{'required': [key]} for key in single_fields if key != 'analysisRef']
    input_schema['allOf'] = [{'if': {'required': ['requests'], 'not': {'required': ['analysisRef']}},
                             'then': {'properties': {'requests': array(single_read_schema, 12, 1)}}}]
    description += ' 同一分析的批量读取可在顶层给 analysisRef，requests 每项只写 section 和具体目标；项内显式引用优先，不自动跨分析或会话。'


DOCUMENT_BINDING = obj({'key': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
                        'valueRef': _VALUE_PART['valueRef'], 'field': _VALUE_PART['field'],
                        'nodeRef': _VALUE_PART['nodeRef'], 'format': NUMBER_FORMAT}, ['key', 'valueRef'])
DOCUMENT_DATASET = obj({'key': DOCUMENT_BINDING['properties']['key'], 'valueRef': TEXT,
                        'nodeRefs': array(TEXT, 200, 1), 'format': NUMBER_FORMAT}, ['key', 'valueRef'])
DOCUMENT_DATASET['description'] = '绑定 SORT/RANK、FORMULA_CHECK 或 RECONCILE 的真实 derivedId。window.analysisSets[key] 提供后端排好序的 rows、公式 nodes/operands/values/contributions 或 parent/children/values。nodeRefs 仅为公式选择关键计算层，每层仍保留全部操作数。数字为精确字符串，display 已格式化。'
DOCUMENT_DATASET['description'] += ' 也接受 AnalysisPython 的 TABLE/CHART_DATA 产物，生成 kind=TABULAR 的 columns/rows/rowCount；复用已有页面组件绘图，不改模板。'
DOCUMENT_DATASET['description'] += ' DESCRIBE 同样直接提供 TABULAR columns/rows 与 statistics/counts；不需要为组装展示表另写 Python。公式集合从 window.analysisSets[key].nodes 读取：节点金额用 node.values.sourceValue/localValue/localDifference 的 value/display；节点状态是 localStatus/recursiveStatus。直接操作数在 node.operands，金额为 operand.value/display。不能把公式节点的金额对象当成普通 fact，也不能把真实 null 补零。'
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
DOCUMENT['description'] += ' 同一零基线线性条形须调用 analysisCharts.bindLinearBars(items,{extent:真实轨道元素})，items 每项为 {element:实际条形元素,valueKey:绑定key} 或 {element,setKey:集合key,factRef:行引用}。函数绑定数值、标记和长度；模型仍决定布局、颜色、文字与分析重点。每次调用默认独立刻度，全量与局部放大分别调用。点图、区间、堆叠终点等不适用图型不套用。diagnostics.renderChecks.viewports[].numericChecks 说明受检范围，NOT_DECLARED 不表示图形正确。'


class AnalysisPublish(_AnalysisTool):
    validates_input_in_call = True
    name = "AnalysisPublish"
    method = "publish"
    # 工具描述直接给出完整最短路径，不要求为机械确认再读多份文档。
    description = ('发布分析版本，默认由模型编写自由 HTML 页面，决定布局、图表和解释，复用用户已确认模板。原生优先仅指计算；用户明确选择显式组件时才用 claims+viewPlan。首次提交 analysisRef/requestKey/document{title,path 或 html,bindings,datasets}，通常带 finalize=true：宿主检查冻结页面、核对内容，通过后同次发布；不通过返回具体诊断和 repairAndFinalize。'
        '文字实质错误 textRepairable=true 时优先沿用 repairAndFinalize，一次传齐 repairs:[{op:"replaceText",path:"/document/html",value:{oldText:textTargets原文片段,newText:修正文字,textRef:textTargets引用}}]，直接修改冻结页面，无需 Read/Edit/Build。结合 issues 的原句与 repairEvidence 一并修正正文、摘要、图注。textRepairable=false 不猜定位；标签、脚本或绑定确需改变时编辑当前 HTML（已有 documentEditSource 则用其 path），用 {op:"replace",path:"/document/path",value:实际文件路径} 刷新；不重抄原 bindings/datasets。'
        'previewOnly=true 仅预览，与 finalize=true 二选一；成功预览也可原样使用 confirmPublish。语义 REVISE 只修实质错误，合理解释不反复润色；NOT_REVIEWED/UNAVAILABLE 不是通过。宿主完成两种宽度测量则复用记录，不重复调用浏览器 MCP；确实不可用时保留未检查状态，不放过已测错误。'
        '线性条形使用 analysisCharts.bindLinearBars(items,{extent:trackElement})；第二参数必填，trackElement 是已挂入页面的真实轨道元素，每项用 {element,valueKey} 或 {element,setKey,factRef}。原生 DESCRIBE 的 TABULAR 与 ORDERED 集均可直接绑定。'
        '仅 delivery.registered=true 才交付链接。workRefs 补已声明工作项；真实缺失用 partialReason。显式组件用 claims+viewPlan，与 document 二选一。参数不套 arguments，title/path/html/bindings/datasets 均在 document 内。已发布版本修改时宿主自动续版，无需版本参数。')
    input_schema = obj({"document": DOCUMENT, "finalize": {"type": "boolean", "description": "true 在一次调用内完成预览、页面检查、内容核对和通过后的发布；有问题返回保存草稿及局部修复入口，不自动改写业务内容。"}, "previewOnly": {"type": "boolean", "description": "true 只检查预览，不发布；与 finalize=true 互斥。"}, "presentation": PRESENTATION, "analysisRef": TEXT, "claims": array(CLAIM, 50), "viewPlan": array(VIEW, 20), "requestKey": TEXT,
                        'repairRef': {**TEXT, 'description': '失败或预览返回的保存草案。通常复用 repairAndFinalize，以 analysisRef/requestKey/repairRef/repairs 和 finalize=true 完成修复检查及通过后的发布；仅预览才用 previewOnly=true，不重写完整计划。'},
                        'repairs': {**REPAIRS, 'minItems': 0},
                        'workRefs': {**WORK_REFS, 'description': '为已有 taskId 关联真实结果或字段；可显式修正普通计算的 operator。保留目标范围及来源核对、业务汇总要求，不删除或重命名工作项。'},
                        'partialReason': {'type': 'string', 'minLength': 1, 'maxLength': 2000, 'description': '用户要求的指标、范围、必要构成或核对确有缺口时说明。用户所需分组齐全、页面仅节选可选成员示例时正常完整交付。部分页面正文放 {{analysisPartialReason}}，聊天也说明缺口。'},
                        'renderCheckUnavailableReason': {'type': 'string', 'minLength': 1, 'maxLength': 1000, 'description': '仅浏览器检查确实不可用时填写具体原因；记录未检查状态，不证明布局合格，也不能跳过已测到的错误。修复草案时通过 repairs 修改此字段。'},
                        "technicalDetails": {"type": "boolean"},
                        "findingPlan": array(FINDING_CHECK, 100), "actionPlan": array(ACTION_PLAN, 300),
                        "thresholds": array(THRESHOLD, 20),
                        "hypotheses": array(obj({"findingId": TEXT, "statement": TEXT, "missingEvidence": array(TEXT, 20, 1)}, ["findingId", "statement", "missingEvidence"]), 30)}, ["analysisRef", "requestKey"])
    input_schema['oneOf'] = [{'required': ['document'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['viewPlan']}, {'required':['presentation']}]}},
                             {'required': ['claims', 'viewPlan'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['document']}]}},
                             {'required': ['repairRef', 'repairs'], 'not': {'anyOf': [{'required': [key]} for key in
                              ('claims', 'viewPlan', 'document', 'presentation', 'workRefs', 'partialReason', 'renderCheckUnavailableReason', 'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses')]}}]
    input_schema['allOf'] = [{'not': {'required': ['finalize', 'previewOnly'],
                                    'properties': {'finalize': {'const': True}, 'previewOnly': {'const': True}}}}]


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
    description += ' 明确导出已有报告时，AnalysisTask 使用 mode=REPORT；成功登记后交付返回的 markdown，不为满足本轮构建要求再次 Build 或 Publish。'
