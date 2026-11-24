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
from .completion_issues import ISSUE_SCHEMA
from .execution_resources import PYTHON_DEFAULT_TIMEOUT_SECONDS, PYTHON_MAX_TIMEOUT_SECONDS

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
BATCH_SELECTOR['properties']['rowIndices']['description'] = '原生选择器中的物理行下标。业务表会合并多个指标为一行。全部返回行省略此字段，已筛选的来源沿用它返回的范围。'
EMPTY_SELECTOR = obj({'captureRef': TEXT, 'resultSetId': TEXT}, ['captureRef', 'resultSetId'])
SELECTOR = {"oneOf": [FORM_SELECTOR, BATCH_SELECTOR, EMPTY_SELECTOR]}
FORM_SELECTOR['properties']['preparedTargets'] = {'const': True,
    'description': '优先使用：复用本体已定位目标及依赖，不填坐标和公式引用；用 targetDimensions 缩小到本题已确认的维度，未给条件则保留全部候选。'}
FORM_SELECTOR['properties']['targetDimensions'] = {**STRING_MAP,
    'description': '仅用于 preparedTargets：从已返回候选中按已确认的维值筛选目标；不约束依赖节点，不猜未知维度。'}
FORM_SELECTOR['required'] = ['captureRef', 'stepId']
FORM_SELECTOR['oneOf'] = [{'required': ['cellKeys'], 'not': {'anyOf': [{'required': ['preparedTargets']}, {'required': ['targetDimensions']}]}},
                          {'required': ['preparedTargets'], 'not': {'anyOf': [{'required': ['cellKeys']},
                                                                            {'required': ['includeFormulaDependencies']}]}}]
for selector in (FORM_SELECTOR, BATCH_SELECTOR, EMPTY_SELECTOR):
    selector['properties'].update(role={'enum': ['PRIMARY', 'REFERENCE', 'STRUCTURE']},
                                  grain=array(TEXT, 16, 1), selectionReason=TEXT,
                                  inputRef=TEXT, sourceRef=TEXT)

SCOPE = obj({"label": TEXT, "members": {**array(TEXT, 1000, 1), "description": "使用 inventory 的 memberRef 或来源唯一的 objectId/key，不能填名称。跨来源同名 ID 必须用 memberRef；构建后以返回的 objectId 选择成员，MCP 请求仍用 sourceObjectId/原始维度。"}, "memberDimension": TEXT,
             "contextMode": {"enum": ["SINGLE_SNAPSHOT", "COMPOSITE"], "description": "预算默认同连接、sourceId/snapshotId/asOf/schemaHash 等数据边界，可包含重开后的不同 contextId/querySessionId；真实边界不同用 COMPOSITE，保留各查询上下文，不自动证明可比。"},
             "referenceMembers": {**array(TEXT, 1000), "description": "旧式 scope 中为不与 members 重叠的额外比较对象。按 selection.role 声明输入时由宿主生成，允许对照总体包含主体子集；默认计算仅主体，对照计算用 inputRefs、select.inputRole=REFERENCE 或对应 batchId 明确选取。"},
             "reportTopics": array(obj({"title": TEXT, "question": TEXT, "factRefs": array(TEXT, 1000, 1)}, ["title", "question", "factRefs"]), 50, 1),
             "expectedDimensions": {**STRING_MAP, "description": "本次全部所选单元格共同适用的维度约束。以 inventory.resolvedDimensions 为准；公式各项目不同的科目维度不能固定为目标科目。"}, "selectionReason": TEXT,
             "excludedMembers": array(obj({"objectId": TEXT, "reason": TEXT, "captureRef": TEXT}, ["objectId", "reason", "captureRef"]), 1000),
             "population": obj({"captureRef": TEXT, "stepId": TEXT, "memberPointer": TEXT}, ["captureRef", "memberPointer"])}, ["label", "selectionReason"])
SCOPE['properties']['members']['description'] = '批量 TABLE 默认省略，由宿主按所选行自动生成，包含空属性组。非批量或显式主体使用 inventory 的 memberRef 或来源唯一 key；不能抄名称或猜引用。'
OPERATION = obj({"id": TEXT, "operator": {"enum": ["SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC", "DESCRIBE"]},
                 "select": obj({"cellKey": TEXT, "measureCode": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "sourceContextId": TEXT, "dimensions": STRING_MAP,
                                "batchId": TEXT, "columnName": TEXT, "sourceObjectId": TEXT, "inputRole": {"enum": ["PRIMARY", "REFERENCE"]},
                                "inputRef": TEXT, "inputRefs": {**array(TEXT, 100, 1), 'uniqueItems': True}}),
                 "inputRefs": array(TEXT, 1000, 1), "ruleRef": TEXT, "comparabilityRef": {**TEXT, "description": "已登记、适用于该算子和输入范围的可比规则 ID；从 AnalysisInspect(protocol).rules 发现，不自行编造。"}, "relationRef": TEXT, "aggregation": {"enum": ["SUM"]},
                 "expression": {"type": "string", "minLength": 1, "maxLength": 4096}, "wholePopulation": {"type": "boolean"},
                 "direction": {"enum": ["asc", "desc"]}, "decimalPlaces": {"type": "integer", "minimum": 0, "maximum": 16}}, ["id", "operator"])
OPERATION['properties']['top'] = {'type': 'integer', 'minimum': 1, 'maximum': 1000,
    'description': 'DESCRIBE 可选：统计按 direction 排序后的前 N 项合计及占比；仍保留全部来源行，不截断主体。'}
OPERATION['properties']['inputRefs']['items'] = SCALAR_REFERENCE
OPERATION['properties']['taskId'] = {**TEXT, 'description': '多个同算子工作项时，指定这项运算归属。只有一个匹配工作项时宿主自动登记已执行结果。'}
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
            "trendRef": {**TEXT, "description": "显式组件附加时期折线，选用构建返回的 trendRef。"},
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
                metadata['analysisNativeResult'] = {'analysisRef': value.get('analysisRef')}
                metadata['calculationPerformance'] = value['calculationSummary']
                logging.getLogger(__name__).info('分析运算汇总 总数=%s 已完成=%s 受阻=%s 重复失败=%s',
                    value['calculationSummary']['operationCount'], value['calculationSummary']['completedCount'],
                    value['calculationSummary']['blockedCount'], value['calculationSummary']['repeatedFailureCount'])
            if self.method in ('inspect', 'read_query_result') and isinstance(value.get('results'), list):
                metadata['readPerformance'] = {
                    'requestCount': len(value['results']),
                    'failedRequestCount': sum(item.get('status') == 'ERROR' for item in value['results']),
                    'partialRequestCount': sum(item.get('status') == 'PARTIAL' for item in value['results'])}
            if self.method == 'read_query_result':
                pages = (page for item in value['results'] if item.get('status') != 'ERROR'
                         for page in item['pages']) if 'results' in value else (value,)
                metadata['analysisReadCaptureRefs'] = list(dict.fromkeys(
                    page['captureRef'] for page in pages if isinstance(page.get('captureRef'), str)))
            if isinstance(delivery, dict) and delivery.get("registered") is True and isinstance(delivery.get("processSummary"), dict):
                metadata["process_summary"] = delivery["processSummary"]
            from .business_tables import model_text
            if self.method == 'read_query_result' and value.get('view') == 'EXECUTION':
                from .history_references import trace_markdown
                return ToolChunk(content=[TextBlock(text=trace_markdown(value))], metadata=metadata)
            if self.method == 'build' and 'derivedValues' in value:
                from .build_projection import render
                from .python_input_markdown import frozen_input_markdown
                artifact = self.service.store.get_analysis(self.service.owner, value['analysisRef'])
                material = frozen_input_markdown(self.service, value, artifact)
                return ToolChunk(content=[TextBlock(text=render(value, artifact, material))], metadata=metadata)
            if self.method == 'task' and kwargs.get('action') == 'prepare_inputs':
                from .build_projection import catalog_read
                from .python_input_markdown import frozen_input_markdown
                material = frozen_input_markdown(self.service, value)
                if material:
                    info = {**value, 'pythonInputCatalog': catalog_read(value['pythonInputCatalog'])}
                    return ToolChunk(content=[TextBlock(text=material + '\n\n' + model_text(info))], metadata=metadata)
            if self.method == 'python' and value.get('pythonRunRef'):
                metadata['analysisNativeResult'] = {key: value[key] for key in ('analysisRef', 'pythonRunRef', 'resultRefs') if key in value}
                if value.get('analysisRef') and value.get('inputSetRef'):
                    from .build_projection import render
                    artifact = self.service.store.get_analysis(self.service.owner, value['analysisRef'])
                    return ToolChunk(content=[TextBlock(text=render(value, artifact))], metadata=metadata)
                if 'scope' in value and value.get('analysisRef'):
                    from .build_projection import compact_scope
                    value = {**value, 'scope': compact_scope(value['scope'], value['analysisRef'])}
            if self.method == 'task' and kwargs.get('action', 'declare') == 'declare':
                from .task_projection import render
                return ToolChunk(content=[TextBlock(text=render(value))], metadata=metadata)
            if self.method == 'task' and kwargs.get('action') == 'sources':
                directory = value['markdown']
                if value['diagnostics']:
                    directory += '\n\n' + model_text({'diagnostics': value['diagnostics']})
                return ToolChunk(content=[TextBlock(text=directory)], metadata=metadata)
            if self.method == 'publish':
                from .publication_projection import project
                return ToolChunk(content=[TextBlock(text=model_text(project(value)))], metadata=metadata)
            return ToolChunk(content=[TextBlock(text=model_text(value))], metadata=metadata)
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
    # This operation changes the live user-turn contract. It must finish before
    # the model can query or build, including while the requirement model runs.
    is_state_injected = True

    async def call(self, _agent_state=None, **kwargs):
        # The middleware already binds service.turn to this live agent state.
        # Consume the injected state argument instead of passing it as business input.
        return await super().call(**kwargs)

    from .task_contract import DESCRIPTION as description, schema as _task_schema
    input_schema = _task_schema(obj, array, TEXT, STRING_MAP, INTENT_FIELDS)

    @property
    def model_input_schema(self):
        turn = getattr(self.service, 'turn', None)
        return _TASK_EXECUTION if turn is None or turn.state.get('mode') else _TASK_DECLARATION


class QueryResultRead(_AnalysisTool):
    name = 'QueryResultRead'
    method = 'read_query_result'
    is_read_only = True
    description = '读取当前会话已保存结果，不重新查询。预算查询默认返回业务 Markdown 表格；按返回的 tableRef、C列标识选择 columns，用 filters 对变化维度做 EQ/IN 过滤，严格复制 nextRead 游标续页。行号在同一 captureRef 内稳定。完整数据已返回时不要重复读取。执行覆盖 complete 与本次展示 readComplete 分开。显式排错才使用 pointer/fields 读取原始 JSON 片段；不读取整个 resultSet 对象。其他来源沿其实际 readOptions 读取。'
    input_schema = obj({'captureRef': {**TEXT, 'description': '从已保存查询回执复制 captureRef。sourceRef 用于选择分析来源。'}, 'pointer': {'type': 'string', 'maxLength': 2048},
                        'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                        'search': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'fields': array(TEXT, 30, 1),
                        'tableRef': TEXT, 'columns': array(TEXT, 100, 1), 'cursor': {'type': 'string', 'maxLength': 2048},
                        'filters': array(obj({'column': TEXT, 'operator': {'enum': ['EQ', 'IN']},
                            'value': {'oneOf': [TEXT, array(TEXT, 100, 1)]}}, ['column', 'operator', 'value']), 20)}, ['captureRef'])
    description += ' 多个已知对象或片段用 requests:[单次参数,...] 一次读取；allPages=true 由宿主在输出额度内续页。每项独立返回状态、pages、readComplete 和 nextRead，局部失败不丢失其他结果。资料已在 analysisMaterials 中给出时直接使用，不重复读取。'
    single_read_schema = deepcopy(input_schema)
    shared_read_schema = deepcopy(single_read_schema)
    shared_read_schema.pop('required', None)
    input_schema = obj({**single_read_schema['properties'], 'requests': array(shared_read_schema, 12, 1),
                        'allPages': {'type': 'boolean', 'description': '单次或批量读取均支持。按明确范围续页；受输出额度约束，未读完时返回准确 nextRead。'}})
    input_schema['oneOf'] = [{'required': ['captureRef'], 'not': {'required': ['requests']}},
                            {'required': ['requests'], 'not': {'anyOf': [{'required': [key]} for key in single_read_schema['properties'] if key != 'captureRef']}}]
    input_schema['allOf'] = [{'if': {'required': ['requests'], 'not': {'required': ['captureRef']}},
                             'then': {'properties': {'requests': array(single_read_schema, 12, 1)}}}]
    description += ' 同一回执的批量读取可以在顶层给 captureRef；项内显式引用优先。单次读取也可 allPages=true，统一返回逐项 pages/readComplete/nextRead，不触发来源查询。'
    input_schema['properties'].update(view={'const': 'EXECUTION'}, contextRef=TEXT)
    input_schema['oneOf'].append({'required': ['view', 'contextRef'], 'maxProperties': 2})
    input_schema['dependentRequired'] = {'view': ['contextRef'], 'contextRef': ['view']}
    input_schema['allOf'].append({'if': {'required': ['view']}, 'then': {'maxProperties': 2}})
    description += ' 解释旧查询使用 view=EXECUTION 和 contextRef，仅读取原执行记录。'


WORK_ITEM = obj({'taskId': TEXT, 'title': TEXT, 'kind': {'enum': ['VALUES', 'COMPUTATION', 'RELATION']},
                 'refs': array(SCALAR_REFERENCE, 1000),
                 'operator': {**OPERATION['properties']['operator'], 'description': 'COMPUTATION 所需的真实运算；可由最终结果的已完成 inputRefs 依赖链证明。FORMULA_CHECK/RECONCILE 须直接引用各目标的检查结果，可同时附带已完成的差额等计算；附属计算不替代目标核对。'},
                 'targetRefs': {**array(TEXT, 1000, 1), 'description': 'COMPUTATION 须处理的目标 factId，配合 operator。批量公式分析列出全部目标事实，每个目标须有自己的 FORMULA_CHECK，不能用另一个公式的操作数充数。'}}, ['taskId', 'title', 'kind', 'refs'])
WORK_REFS = array(obj({'taskId': TEXT, 'refs': array(SCALAR_REFERENCE, 1000),
                      'operator': {'type': 'string', 'description': '可显式修正普通计算工作项的方法为已有结果的算子；不改变目标范围，不得替换 FORMULA_CHECK/RECONCILE/SUM/LINEAR/CONVERT 的来源核对和业务规则要求。'}}, ['taskId', 'refs']), 100)
OPERATION['properties']['inputRefs']['description'] = '非空的实际 factId/derivedId，或同一计划中已经定义的前序运算 id。按 select 筛选时省略 inputRefs，不能传 []。宿主解析并保存真实引用，不需要手工拼前缀。'
WORK_ITEM['properties']['refs']['description'] = 'VALUES 使用 factId；COMPUTATION 使用真实 derivedId 或本计划运算 id；RELATION 使用 relationRef。尚未得到结果填空数组。'
WORK_ITEM['properties']['refs']['description'] += ' COMPUTATION 可用 {valueRef,field,nodeRef?} 引用已有标量。统计任务引用 DESCRIBE 及其字段，不为每个统计值另建 ARITHMETIC。'
WORK_ITEM['properties']['refs']['description'] += ' 只有一个 VALUES 工作项且主体数据为完整、同口径单指标时，空 refs 由宿主关联本版本主体事实；多指标、多取值任务或待补范围须明确引用，参考数据不自动加入。'
WORK_ITEM['properties']['title'] = {**TEXT, 'description': '回答用户问题所需的业务结果；先取得必要数据再选择方法，不把可选分析或取数前猜测的实现步骤登记为必交目标。'}
WORK_ITEM['properties']['kind']['description'] = '按交付依据选：VALUES 列示来源单元格，COMPUTATION 使用计算结果（含 Python 汇总或比较），RELATION 核对实际成员关系。'
OPERATION['properties']['inputRefs']['description'] += ' ARITHMETIC 可用 {valueRef,field,nodeRef?} 直接计算已返回字段，如 negativeSum/sum；其他算子使用完整 ID。'
OPERATION['properties']['ruleRef'] = {**TEXT, 'description': '适用的已登记规则；同一原生聚合批次、同一指标、互斥组且来源 CalcType=SUM 时，SUM 可省略，由宿主使用来源规则；其他情况不推定可加。'}
WORK_ITEM['properties']['operator']['enum'] = [*OPERATION['properties']['operator']['enum'], 'PYTHON']
for contract in (WORK_ITEM, WORK_REFS['items']):
    contract['properties']['objectiveRef'] = {**TEXT, 'description': '归属不明确或纠正误填的关系项时，选择本轮已返回的数据构成目标。配合 FORMULA_CHECK 使用同一目标的全部公式结果。'}
    contract['properties']['revisionReason'] = {**TEXT, 'description': '修正已失败且未绑定业务规则的 SUM/LINEAR 方法时填写：误选原因及新方法如何回答原目标。须关联真实失败和同范围的成功结果；不能替换来源公式、勾稽或换算，不缩小业务目标。'}
WORK_REFS['items']['properties']['operator']['description'] = '关联实际结果的方法。普通方法可显式修正；误选 SUM/LINEAR 且实际执行失败、未绑定规则时可用 revisionReason 纠正。来源公式/勾稽/换算及已绑定业务规则不能降级。'
VIEW['properties']['nodeRefs']['description'] = '数据构成主图省略此字段，展示本次返回的全部计算层。另画重点比较图时可选 formulaTrace.nodes[].factRef，按指定顺序展示，每层保留全部直接操作数。'
VIEW['properties']['includeDetails']['description'] = '仅 FORMULA/HIERARCHY；按阅读需要附带明细。关键图不必附全表；完整核对数据可独立用 TABLE 引用同一派生结果并 collapsed=true。'

REPAIRS = array(obj({'op': {'enum': ['replace', 'remove', 'add', 'replaceText', 'appendMarkdown']},
                    'path': {**TEXT, 'description': '错误返回的 JSON Pointer，如 /viewPlan/2/nodeRefs。add 可用 /presentation/labels/- 追加数组项，也可用 0 至当前长度的数字下标插入；replace/remove 须指向已有项。父路径须存在，数组下标不带前导零。仅修改指定路径，其他内容完整保留。'},
                    'value': {'description': 'replace/add 按字段契约提供值，remove 省略。replaceText 用反馈的 repairPath，value={oldText:原稿唯一片段,newText:修正文字,textRef:可选真实引用}，优先使用 textTargets。保留原数值占位符。新引用可在同批用 add 向 /document/bindings/- 登记实际结果，再在 newText 中使用 {{key}}。多处修改一次提交。'}}, ['op', 'path']), 100, 1)

# Put the rules at their input fields so callers do not need to infer variants
# from a long tool description or load a reference document to fix one field.
for key, description in {
    'valueRefs': 'FORMULA/WATERFALL 填 FORMULA_CHECK 的 derivedId；RECONCILIATION 填 RECONCILE 的 derivedId；TABLE/BAR_VALUES 填来源 factId；BAR_RANK 填 RANK 的 derivedId；METRIC 填标量引用；HIERARCHY 首项父 factId，其后全部直属子项同指标 factId。',
    'nodeRefs': '仅 FORMULA/WATERFALL。数据构成主图省略，展示全部已返回计算层。附加重点图可选 formulaTrace.nodes[].factRef；各层保留全部直接操作数，叶子不另画。',
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
OPERATION['properties']['operator']['description'] = '按来源数值排序选择 SORT（含无单位原值及负数）；RANK 另外要求可比单位、缩放和口径。FORMULA_CHECK 每项仅处理一个目标；批量目标分别运算。NO_CAPTURED_FORMULA 表示当前捕获未提供可复算公式，检查完成但不产生数值、不证明来源一致；预算来源公式中已读取的空值按0参与运算、除以0返回0；raw value 保留空，formulaTrace 的 arithmeticPolicy/nullAsZeroRefs 说明规则，localCheck.inputValues 是实际运算输入。按真实结果判定 MATCH/MISMATCH。其他未声明空值规则的来源可返回 completionReason=SOURCE_VALUE_MISSING，数值仍不可用。其他断点按实际缺口处理。分析方法与汇总预期由任务选择，算子不自动安排业务流程。'
OPERATION['properties']['operator']['description'] += ' DESCRIBE 一次完成同一指标的来源值正负零/缺值计数、合计、均值、极值、排序、占比及可选 top；提供可直接绑定的 TABULAR 表和 statistics/counts。描述统计不证明业务合并或层级去重；业务口径聚合使用来源接口。用 select.batchId+columnName 隔离实际集合，不能以是否存在某个维度猜输入类型。'
SCOPE['properties']['population']['description'] = '主体 members 的完整枚举来源，必须恰好对应 members。额外比较对象用 referenceMembers；不要为加入比较值删除本字段。'


class AnalysisBuild(_AnalysisTool):
    name = "AnalysisBuild"
    method = "build"
    description = (
        '从已捕获来源构建分析或报告。已采用来源时直接传 operations 和 requestKey，宿主已绑定选区。多个主体分别用 select.inputRef 计算，跨组计算显式列 inputRefs。仅在还未采用来源时，首次传 captureRefs/scope/operations/requestKey，目标和用途确定后由宿主生成 selections。'
        '已有分析追加计算或工作项传 baseAnalysisRef/operations/requestKey，省略三个输入字段以沿用冻结事实、范围、粒度和有效 Python 结果；换数据才完整提交。'
        '仅当前 Schema 提供 selections 时才填写。批量 TABLE 选择 batchId/columnNames/可选 rowIndices，省略行号取全部实际行；'
        'scope.members 可省略，由选择器生成。显式成员用真实 memberRef 或唯一 objectId，名称不能替代 ID；expectedDimensions 仅共同口径。'
        'selections 的 PRIMARY/REFERENCE/STRUCTURE 区分主体、对照、结构，grain 为实际维度列；批量默认全部分组维度，不再按机构树去重。'
        '网格 cellKeys 是真实目标，includeFormulaDependencies=true 自动补齐同观察已捕获依赖并执行 LOCAL FORMULA_CHECK，已有检查不重复提交。'
        '同计划 inputRefs 可用前序 operation.id，COMPUTATION.refs 可用本计划运算 id，宿主解析真实 derivedId。'
        '单纯原值展示 operations=[]，workPlan 可省略；本体准备的目标使用 preparedTargets=true，自动带入来源值、依赖、公式计算和默认工作项，无需另填 targetRefs 或空的 VALUES 工作项。'
        '必要的额外计算和关系检查可声明工作项，概览不扩大公式范围。金额和构成使用 PRIMARY/REFERENCE，STRUCTURE 仅用于模板结构，不纳入数值计算。'
        '续建继承已有工作项及 population，不通过省略或空数组删除；unresolvedItems 关联真实未完成的用户要求与障碍。'
        '同连接固定 sourceId/snapshotId/asOf/schemaHash 可纳入多查询身份；跨边界用 COMPOSITE，各事实保留来源，汇编不证明可比。'
        '跨期/跨指标运算需要适用可比规则；原值顺序用 SORT，可比业务排名才 RANK。分组金额直接使用原生聚合，宿主另附同绑定 STRUCTURE 样本。'
        '预算来源公式中的已确认空值保留原值 null、按0参与运算，除以0返回0；未加载依赖或读取错误不能当0。检查完成不等于来源与复算一致。'
        '绑定 documentInputs 展示本层/逐层结果与差额，不能用本层通过证明逐层。综合报告用 reportTopics 声明用户所指主题。'
        '分析方法见 data-analysis，页面交付见 report-generation；字段形状与运算约束见当前 Schema。')
    input_schema = obj({"captureRefs": array(TEXT, 1000, 1), "selections": array(SELECTOR, 1000, 1), "scope": SCOPE,
                        "operations": {**array(OPERATION, 100), "description": "必填；不需要运算时显式传 []，不能省略。"}, 'workPlan': array(WORK_ITEM, 100), "requestKey": TEXT, "baseAnalysisRef": TEXT, "inputSetRef": TEXT, "actionRunRef": TEXT},
                       ["operations", "requestKey"])
    # A caller may refine presentation scope while retaining explicitly adopted inputs.
    # Whether adoption exists is a stateful service check, not a static schema dependency.
    input_schema["dependentRequired"] = {'selections': ['captureRefs', 'scope'], 'captureRefs': ['scope']}
    input_schema['allOf'] = [
        {'not': {'required': ['baseAnalysisRef', 'inputSetRef']}},
        *[{'not': {'required': ['inputSetRef', field]}} for field in ('captureRefs', 'selections', 'scope')],
    ]

    @property
    def model_input_schema(self):
        # Once sources have been explicitly adopted, the host owns their exact
        # selection. Expose the choices still open to the model, rather than
        # asking it to retype the same capture/selection provenance.
        turn = getattr(self.service, 'turn', None)
        if turn is None or not turn.state.get('adoptedInputs'):
            return None
        schema = deepcopy(self.input_schema)
        for field in ('captureRefs', 'selections'):
            schema['properties'].pop(field, None)
        schema.pop('dependentRequired', None)
        return schema
    description += ' 优先使用原生能力，必要计算可一次 operations 提交。DESCRIBE 已提供来源值分布表、计数、合计和占比；公式数据集由 FORMULA_CHECK 提供，不为制作表格重写 Python。返回 documentInputs 给出直接可用的绑定方式，AI 继续决定解释、布局与配色。'
    description += ' 数值条件产生的新子集先用预算查询 basis=FACTS 和 valueFilter 取得，再采用其 sourceRef；select 只按当前 Schema 定位输入。select.inputRef 的统计使用该组已采用原值，自动补充的公式材料不加入总体计数或合计。'


class AnalysisPython(_AnalysisTool):
    name = 'AnalysisPython'
    method = 'python'
    is_concurrency_safe = False
    description = (
        '用冻结输入进行自由 Python 分析。首次使用 Build 返回的 inputSetRef；追加并保留已算结果用该次 Python 返回的新 inputSetRef。纯文件或网格、追加文件使用 '
        'AnalysisTask(action=prepare_inputs) 一次准备。多个来源先明确用途，不能默认最近一次。失败或取消运行用 retryOfRunRef，'
        '不重新取数或重复拼来源列表。程序注入 inputs，代码可自行筛选、连接、分组、统计和输出多个结果。'
        '仅文件执行返回 pythonRunRef，不创建 analysisRef；继续使用原 inputSetRef。'
        '完整输入不表示所有明细都必须相加；按用户要求选择时期、维度和指标，避免合计与明细重复。'
        '仅原生能力不足时编写代码，fallbackReason 说明能力缺口；普通排序、统计、预算原生聚合和来源公式优先现有算子。'
        'operations 可同次运行前置原生算子，其真实产物一并注入；workPlan 可引用本次 result.name，COMPUTATION.operator=PYTHON，'
        '原生工作项保持原算子。answerOutputs 选择本次用于答复的产物名，探索运行可省略。'
        'inputs 中事实保留 ref/value/valueState/unit/dimensions/dimensionNames/inputMemberships 等真实字段；'
        '预算输入 Markdown 给出本次维度、成员和来源指标的对应关系。dimensions 按成员编码筛选，dimensionNames 取成员显示名称；'
        '预算合计按该维度的字符串 00000000 比较。字段目录由宿主生成，不假设 label 存在。PRIMARY/REFERENCE 分别统计，同一事实可属多组；VALUES 是原数据，FORMULA 是构成材料。'
        '表产物使用 columns[{name,label,type,unit?,decimalPlaces?}] 与 rows[{rowRef,values,valueStates}]，'
        '按 row.values[column.name] 读取；输出 rows 用具名字段。多维分组可用元组作内部键，返回时将组合键拆成各维度列。'
        'GRID_FILE 通过 grid_rows(input["ref"],where) 流式迭代，完整文件不在 data 中；where 支持 '
        'cellKeys、dimensions、measureCode、headerText、mapTypes、startRow/endRowExclusive/startColumn/endColumnExclusive。'
        '网格记录 sourceRef 指输入，保留 key/headers/formula/rawValueJson，dimensions 合并表外条件；按真实指标定位数值格后再计算。'
        '宿主扫描和发送记录不证明算法使用正确。允许普通语法、函数及 import/from import，支持 Decimal、pandas、NumPy、SciPy、statsmodels。'
        'D/Decimal、json_read/json_write、csv_read/csv_write 已提供；执行在无网络、无工作区挂载、只读根目录的独立 Docker 容器，/tmp 可写。'
        '金额使用 Decimal/object，null 不隐式变零；预算来源公式的空值按0和除零返回0由 FORMULA_CHECK 执行。'
        'result 是非空数组，每项 name/label/inputRefs。kind=SCALAR 带 value/unit；TABLE/CHART_DATA 带 columns/rows，'
        '列 type=STRING/DECIMAL_STRING/BOOLEAN；DIAGNOSTIC 带 data；FILE 带 filename/text，仅 csv/tsv/json/txt。'
        'decimalPlaces 可用于标量或 DECIMAL_STRING 列，0～16位 ROUND_HALF_UP，只舍入输出。金额及统计输出不使用浮点或科学计数法。'
        '引用实际 inputs[*].ref；唯一网格完整发送后可由宿主补记省略的输出来源。执行记录、实际输入代码输出均保存，执行成功不等于业务审核通过。'
        '表产物可续算或绑定 document.datasets；小结果 payloadComplete=true 时无需重复 Inspect。字段未知才探索，不限制代码创建新的业务列。')
    input_schema = obj({
        'inputSetRef': {**TEXT, 'description': '宿主返回的完整冻结输入集合；不手填 factRef 列表或猜引用。'},
        'dimensionFilter': {'type': 'object', 'additionalProperties': {'type': 'string'},
                            'description': '可选，在已冻结的结构化事实中按维度和成员筛选后执行。来源中唯一的中文名与编码等效。'},
        'filterInputRef': {**TEXT, 'description': '按维度筛选且集合中有不同用途时，选主体或对照的已采用输入组。'},
        'retryOfRunRef': {**TEXT, 'description': '失败或取消运行返回的 pythonRunRef，沿用原冻结输入；与 inputSetRef、operations 二选一。'},
        'registerRunRef': {**TEXT, 'description': '脚本执行成功、登记失败时只纠正工作项，不重新执行。'},
        'code': {'type': 'string', 'minLength': 1, 'maxLength': 64000},
        'purpose': TEXT,
        'fallbackReason': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'requestKey': TEXT,
        'timeoutSeconds': {'type': 'integer', 'minimum': 1, 'maximum': PYTHON_MAX_TIMEOUT_SECONDS,
                           'default': PYTHON_DEFAULT_TIMEOUT_SECONDS},
        'answerOutputs': {**array(TEXT, 1000, 1), 'description': '用于答复的脚本输出名。本次明确声明唯一的 PYTHON 工作项、未单独选 refs 时，同时归入该项。多工作项分别在 refs 指定输出名。'},
        'operations': array(OPERATION, 100),
        'workPlan': array(WORK_ITEM, 100),
        'supersedesRunRefs': {**array(TEXT, 20), 'description': '明确被本次纠正替代的同分析旧执行引用；purpose 说明纠正理由。允许调整实际输入，宿主记录差异并重新审查，不证明语义等价；新执行失败不替代，仍被交付或计算依赖的旧产物保留。'},
    }, ['requestKey'])
    input_schema['if'] = {'required': ['registerRunRef']}
    input_schema['else'] = {'required': ['code', 'purpose', 'fallbackReason']}
    input_schema['dependentRequired'] = {'filterInputRef': ['dimensionFilter'], 'dimensionFilter': ['inputSetRef']}
    input_schema['oneOf'] = [
        {'title': '新计算，使用已准备的输入', 'required': ['inputSetRef'],
         'not': {'anyOf': [{'required': ['retryOfRunRef']}, {'required': ['registerRunRef']}]}},
        {'title': '重试失败或取消的运行', 'required': ['retryOfRunRef'],
         'not': {'anyOf': [{'required': [k]} for k in ('inputSetRef', 'operations', 'registerRunRef', 'dimensionFilter')]}},
        {'title': '仅登记成功执行的产物', 'required': ['registerRunRef', 'workPlan'],
         'not': {'anyOf': [{'required': [k]} for k in ('code', 'purpose', 'fallbackReason', 'inputSetRef', 'retryOfRunRef', 'operations', 'dimensionFilter', 'supersedesRunRefs', 'timeoutSeconds')]}}
    ]


# Input preparation belongs to the existing task tool. Build already returns
# an inputSetRef, so ordinary analysis does not pay for an additional round trip.
_PREPARE_INPUTS = {
    'analysisRef': TEXT, 'captureRefs': array(TEXT, 1000, 1),
    'selections': array(SELECTOR, 1000, 1), 'scope': SCOPE,
    'operations': array(OPERATION, 100), 'requestKey': TEXT,
    'fileInputs': array(obj({'path': TEXT}, ['path']), 20, 1),
    'gridInputs': array(obj({'captureRef': TEXT, 'fileRef': TEXT}, ['captureRef', 'fileRef']), 20, 1),
}
AnalysisTask.input_schema['properties']['action']['enum'].append('prepare_inputs')
AnalysisTask.input_schema['properties'].update(_PREPARE_INPUTS)
AnalysisTask.input_schema['allOf'].append({
    'if': {'properties': {'action': {'const': 'prepare_inputs'}}, 'required': ['action']},
    'then': obj({'action': {'const': 'prepare_inputs'}, **dict.fromkeys(_PREPARE_INPUTS, {})}, ['action']),
    'else': {'not': {'anyOf': [{'required': [key]} for key in _PREPARE_INPUTS if key != 'analysisRef']}}})
AnalysisTask.input_schema['allOf'].append({'if': {'required': ['analysisRef']},
    'then': {'properties': {'action': {'enum': ['declare', 'prepare_inputs', 'tools']}}}})
AnalysisTask.input_schema['allOf'].append({'if': {'properties': {'action': {'const': 'tools'}}, 'required': ['action']},
    'then': obj({'action': {'const': 'tools'}, 'analysisRef': TEXT}, ['action'])})
AnalysisTask.input_schema['properties']['analysisRef'] = {**TEXT, 'description': 'declare 时复用所选报告的完整输入；prepare_inputs 时冻结分析供计算；tools 可附已知引用核验其归属。'}
AnalysisTask.description += ' 审核已有报告时，declare 用 analysisRef 沿用全部冻结输入。查看原意见、接续待审、独立复核已审正文分别使用 AnalysisPublish 的 VIEW、RESUME、RECHECK。'
AnalysisTask.description += (' prepare_inputs 将已确认 analysisRef 或 captureRefs/selections/scope，以及显式 fileInputs/gridInputs '
    '冻结为 inputSetRef。来源不明确先选择，不默认最新结果。AnalysisBuild 已直接返回 inputSetRef，无需重复准备。'
    '新增前置运算可放 operations；换来源或条件重新准备，纯文件也可独立分析。')

# Each view is constructed once. A tool instance selects its own turn's view.
_TASK_EXECUTION = deepcopy(AnalysisTask.input_schema)
for _field in ('materials', 'formulaSubjects'):
    _TASK_EXECUTION['properties'].pop(_field)
_TASK_DECLARATION = deepcopy(_TASK_EXECUTION)
_TASK_DECLARATION['properties']['action']['enum'] = ['declare', 'contexts', 'sources']
for _field in (_PREPARE_INPUTS.keys() - {'analysisRef'}) | {'failedToolCallIds', 'evidenceCaptureRefs', 'resultRefs', 'reason', 'groupingCaptureRefs'}:
    _TASK_DECLARATION['properties'].pop(_field, None)
_TASK_DECLARATION['allOf'] = [rule for rule in _TASK_DECLARATION['allOf']
                              if rule['if'].get('properties', {}).get('action', {}).get('const') != 'prepare_inputs']


class AnalysisInspect(_AnalysisTool):
    name = "AnalysisInspect"
    method = "inspect"
    is_read_only = True
    description = "分页读取本会话分析输入、精确值引用和校验诊断，不触发新来源查询。已知适用 captureRef 时 inventory 仅加载该捕获，不扫描无关历史；需要另一次捕获的名称时定向读 entities 并纳入引用。nextRead 是保留本次筛选条件的续读参数，可直接使用。section=entities 读取对象 key/label/modelCode/resourceRef 及名称 captureRef（可按 objectId 或 captureRef 筛选；搜索候选只补名称，不证明成员完整）；section=results 发现本会话已有结果及主题范围；selectors 按 captureRef 返回实际 stepId/memberPointers/populationSelector；inventoryIssues 分页读被隔离的无效输入。facts/inventory 返回受字符预算限制，严格沿 nextOffset 继续，不自行加 limit；facts 支持 factRef 定位及 fields 精确选择，detail=full 恢复全字段；来源捕获字段为 captureRefs 数组，inventory 的 captureRef 是另一种结构。INVALID_FIELDS 返回 availableFields，不猜字段名。inventory 默认 detail=compact，以 contextRef 引用顶层 contexts，detail=full 读全元数据；支持 captureRef/stepId/cellKey 精确选择，对象筛选须同时给出 memberDimension；inventory/capture 的 memberRef 是成员来源引用，sourceObjectId 是原始 ID。跨来源同名 ID 用 memberRef 填 members/referenceMembers，Build 后的选择使用实际返回的 objectId；memberRef 不能作为 MCP 业务 ID。名称按 dimensionModels/objectModelCode 匹配实体模型。section=inventory 汇集当前会话各次查询的单元格、对象名称、任务时期、值及引用，支持 memberDimension/objectId/bindingRef/period/measureCode/search 分页筛选，不合并不同观察；section=captures 列输入；capture 定位单元格；receipt 核对原请求；protocol 查算子及已登记规则；facts/derived/summary 读分析；formulas 加 factRef 分页读表内依赖及具体断点；order 按 derivedId 分页读排序结果；relations 读取来源证明的一级关系，用于 HIERARCHY 图；省略 analysisRef 时发现全会话已捕获关系，带 analysisRef 时仅读该版本。"
    description += ' summary 同时返回模型声明的 presentation（含共享 labels）和 viewPlan（含局部 labels），用于综合或重建时恢复展示约定；由模型筛选、补充并重新提交，不自动套用旧布局。'
    description += ' queryContexts 按最新优先读取本会话已捕获的查询条件、绑定、指标定位和任务机构类型，每页最多 10 个回执；追问只增加分组等条件时先复用仍适用的上下文，不重新发现任务和指标。未命中当前页时沿 nextOffset 继续；失败回执保留失败原因。'
    input_schema = obj({"section": {"enum": ["materials", "gridFiles", "inventory", "inventoryIssues", "entities", "results", "selectors", "captures", "queryContexts", "capture", "receipt", "rows", "protocol", "facts", "derived", "formulas", "summary", "claims", "verificationRecords", "relations", "order", "delivery", "findings", "actions", "preview", "python", "products", "inputFields", "periodTrends"]},
                        'pythonRunRef': {**TEXT, 'description': 'section=python 读取成功、失败、取消或超时的执行记录；或用 analysisRef 列出该分析已登记的 Python 计算。'},
                        'pythonPath': {**TEXT, 'description': 'section=python 的完整记录 JSON Pointer，沿返回的 readOptions/read/nextRead 读取代码、输入和输出；支持 offset/limit/fields 分页，默认仅摘要。'},
                        "previewRef": {**TEXT, 'description': '仅 section=preview，传成功预览返回的 repairRef；读取当前文档的页面检查记录，不会代替浏览器运行检查。'},
                        "analysisRef": TEXT, "captureRef": TEXT, "inputSetRef": {**TEXT, "description": "section=inputFields 读取冻结集合的真实字段目录；支持纯文件集合。"},
                        "derivedId": {**TEXT, "description": "当前分析已返回的计算引用。products 自动按类型读取标量、表格或公式核对；formulas 也接受 FORMULA_CHECK 的 derivedId，复用已完成追溯；derived 可定向读取。"}, "stepId": TEXT, "search": TEXT,
                        "detail": {"enum": ["compact", "full"]}, "fields": {**array(TEXT, 30, 1), 'description': 'facts/python/formulas 可选实际字段；公式可选 nodes、pendingDependencies、targetLocalCheck、checkSummary。inventory 用 detail=compact 和对象/单元格筛选。'}, "cellKey": TEXT, "factRef": {**TEXT, "description": "目标来源 factId；formulas 可使用 factRef 或对应 FORMULA_CHECK 的 derivedId，二者同时提供时必须指向同一目标。facts 按实际 factId 定位。"}, "memberDimension": TEXT, "objectId": TEXT, "bindingRef": TEXT, "period": TEXT, "measureCode": TEXT,
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
        {'properties': {'section': {'enum': [item for item in ANALYSIS_SECTIONS if item != 'inputFields']}}, 'required': ['analysisRef']},
        {'properties': {'section': {'const': 'inputFields'}}, 'anyOf': [{'required': ['analysisRef']}, {'required': ['inputSetRef']}]},
        {'properties': {'section': {'const': 'python'}}, 'anyOf': [{'required': ['pythonRunRef']}, {'required': ['analysisRef']}]},
    ]
    single_fields = list(input_schema['properties'])
    input_schema['properties']['allPages'] = {'type': 'boolean'}
    input_schema['oneOf'] = [{'not': {'anyOf': [{'required': ['requests']}, {'required': ['allPages']}]}},
                            {'required': ['requests'], 'not': {'anyOf': [{'required': [key]} for key in single_fields]}}]
    shared_read_schema = deepcopy(single_read_schema)
    shared_read_schema.pop('oneOf')
    input_schema['properties']['requests'] = array(shared_read_schema, 12, 1)
    input_schema['oneOf'][1]['not']['anyOf'] = [{'required': [key]} for key in single_fields if key != 'analysisRef']
    input_schema['allOf'] = [{'if': {'required': ['requests'], 'not': {'required': ['analysisRef']}},
                             'then': {'properties': {'requests': {'items': {'oneOf': single_read_schema['oneOf']}}}}}]
    description += ' 同一分析的批量读取可在顶层给 analysisRef，requests 每项只写 section 和具体目标；项内显式引用优先，不自动跨分析或会话。'


DOCUMENT_BINDING = obj({'key': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
                        'valueRef': _VALUE_PART['valueRef'], 'field': _VALUE_PART['field'],
                        'nodeRef': _VALUE_PART['nodeRef'], 'format': NUMBER_FORMAT,
                        'rowRef': TEXT, 'column': TEXT}, ['key', 'valueRef'])
DOCUMENT_BINDING['description'] = '引用已有计算表中的数值时，valueRef 指向该表，rowRef 和 column 使用实际返回的行引用和数值列名。'
DOCUMENT_BINDING['dependentRequired'] = {'rowRef': ['column'], 'column': ['rowRef']}
DOCUMENT_DATASET = obj({'key': DOCUMENT_BINDING['properties']['key'], 'valueRef': TEXT,
                        'nodeRefs': array(TEXT, 200, 1), 'format': NUMBER_FORMAT}, ['key', 'valueRef'])
DOCUMENT_DATASET['description'] = '绑定 SORT/RANK、FORMULA_CHECK 或 RECONCILE 的真实 derivedId。window.analysisSets[key] 提供后端排好序的 rows、公式 nodes/operands/values/contributions 或 parent/children/values。nodeRefs 仅为公式选择关键计算层，每层仍保留全部操作数。数字为精确字符串，display 已格式化。'
DOCUMENT_DATASET['description'] += ' 也接受 AnalysisPython 的 TABLE/CHART_DATA 产物，生成 kind=TABULAR 的 columns/rows/rowCount；复用已有页面组件绘图，不改模板。'
DOCUMENT_DATASET['description'] += ' DESCRIBE 同样直接提供 TABULAR columns/rows 与 statistics/counts；不需要为组装展示表另写 Python。公式集合从 window.analysisSets[key].nodes 读取：节点金额用 node.values.sourceValue/localValue/localDifference 的 value/display；节点状态是 localStatus/recursiveStatus。直接操作数在 node.operands，金额为 operand.value/display。不能把公式节点的金额对象当成普通 fact。预算来源原始 null 保留，operand.calculationValue 是实际运算输入，contributions/waterfall 已应用空值按0规则。本表公式展示参考 report-generation 的 references/formula-visualization-examples.md；完整多项加减遍历 waterfall.steps 绘制横向瀑布图，后台已返回的相关下层可折叠但不漏项，重点比较图仅作补充；展开深度由后台决定，未见底不影响展示，不为出图继续展开；非线性用关系图或步骤，单输入用衔接说明。不自行重算、把未读数据补零或为画图追查表外。'
DOCUMENT = obj({'title': {'type':'string','minLength':1,'maxLength':300},
                'path': {**TEXT, 'description':'当前工作区内 UTF-8 .md 或 .html 的相对路径。'},
                'markdown': {'type':'string','minLength':1,'maxLength':524288},
                'html': {'type':'string','minLength':1,'maxLength':524288},
                'bindings': array(DOCUMENT_BINDING, 2000), 'datasets': array(DOCUMENT_DATASET, 50)}, ['title'])
DOCUMENT['anyOf'] = [{'required': ['bindings'], 'properties': {'bindings': {'minItems': 1}}},
                     {'required': ['markdown']},
                     {'required': ['datasets'], 'properties': {'datasets': {'minItems': 1}}},
                     {'required': ['html'], 'properties': {'html': {'pattern': 'data-analysis-trend'}}},
                     {'required': ['path']}]
DOCUMENT['description'] = '多时期分析可直接绑定宿主折线图。对于 path，读取冻结页面后统一验证其数值、集合或趋势引用。'
DOCUMENT['oneOf'] = [{'required':[key]} for key in ('markdown', 'path', 'html')]
DOCUMENT_DATASET['description'] += (' 图注需要排序方向时，在现有文案中放 <span data-analysis-order-label="集合key"></span>，'
    '宿主按实际 SORT/RANK/DESCRIBE 输出“从小到大”或“从大到小”。描述统计若只展示汇总数，不要求绘制排序图。'
    '表和图共用 row.values。公式步骤名用 step.label，原表地址仅用 node.cellAddress。解释主体时直接展开关键计算层，衔接和明细可折叠。')
DOCUMENT['description'] = ('Markdown 业务正文写在 markdown 或 .md 草稿 path 中。图表直接复制本次 markdownFigures 中的图片引用，单独成段。'
    '正文 {{key}} 绑定数值。宿主保留报告模板并生成来源明细。HTML 草稿接受相同的绑定和发布检查。')



class AnalysisPublish(_AnalysisTool):
    @property
    def model_input_schema(self):
        turn = getattr(self.service, 'turn', None)
        if (turn and turn.state.get('adoptedReportRef') and not turn.state.get('expandedToolCatalog')
                and not turn.state.get('reportReviewRead')):
            return obj({key: self.input_schema['properties'][key] for key in ('analysisRef', 'requestKey', 'reviewAction')},
                       ['analysisRef', 'requestKey', 'reviewAction'])
        if turn and turn.state.get('deliveryMode') == 'DOCUMENT' and not turn.state.get('expandedToolCatalog'):
            props = self.input_schema['properties']
            document = obj({k: DOCUMENT['properties'][k] for k in ('title', 'markdown', 'path', 'bindings')}, ['title'])
            document['oneOf'] = [{'required': [k]} for k in ('markdown', 'path')]
            document['description'] = '业务正文用 Markdown。图形从 markdownFigures 或时期候选的 Markdown 列复制，独立成段。'
            fields = {key: props[key] for key in ('analysisRef', 'requestKey', 'finalize', 'repairRef', 'repairs',
                                                    'previewOnly', 'workRefs', 'unresolvedItems', 'reviewAction')}
            return obj({**fields, 'document': document}, ['requestKey'])
        return None
    validates_input_in_call = True
    name = "AnalysisPublish"
    method = "publish"
    # 工具描述直接给出完整最短路径，不要求为机械确认再读多份文档。
    description = ('用 Markdown 发布图文分析。提交 analysisRef、requestKey、document:{title,markdown}、finalize:true。'
        'title 是报告总标题，正文从概述或 ## 章节开始。数值用千分位，保留原精度。正文写结论、依据与解释。每幅图从 markdownFigures 或时期候选的 Markdown 列复制图片引用并独立成段，宿主绘图并附查询数据。'
        '内容较长可用 documentDraft.path 写 .md。正文要引用精确结果可用 bindings；宿主自动绑定所选图。'
        '已确认的合理解释保留；实质问题按 repairAndFinalize 用 replaceText 和返回的 repairPath、textTargets 修原稿。'
        '整稿修改用 /document/path。仅 delivery.registered=true 后交付。续审复用 reviewAction 与原 analysisRef。'
        '已发布的 HTML 仍可用原稿修订、续审或导出。')
    input_schema = obj({"document": DOCUMENT, "finalize": {"type": "boolean", "description": "true 在一次调用内完成预览、页面检查、内容核对和通过后的发布；有问题返回保存草稿及局部修复入口，不自动改写业务内容。"}, "previewOnly": {"type": "boolean", "description": "true 只检查预览，不发布；与 finalize=true 互斥。"}, "presentation": PRESENTATION, "analysisRef": TEXT, "claims": array(CLAIM, 50), "viewPlan": array(VIEW, 20), "requestKey": TEXT,
                        'repairRef': {**TEXT, 'description': '失败或预览返回的保存草案。通常复用 repairAndFinalize，以 requestKey/repairRef/repairs 和 finalize=true 完成修复检查及通过后的发布；仅预览才用 previewOnly=true，不重写完整计划。'},
                        'repairs': {**REPAIRS, 'minItems': 0},
                        'workRefs': {**WORK_REFS, 'description': '有工作项时按其 taskId 补真实结果；可修正普通计算的 operator。工作项为空时省略。目标 objectiveRef 不填在此处。'},
                        'unresolvedItems': {**array(ISSUE_SCHEMA, 100),
                                            'description': '真实缺口须含 requirement/reason/basis。WORK_ITEM 引用待完成 taskId；AUDIT_RESULT 引用 FORMULA_CHECK/RECONCILE 的 resultRefs，核对已完成时宿主转为审核发现，不保留未完成标记；INVESTIGATION 仅用 AnalysisTask/Build 返回的 investigations.requirementRef；确实遗漏且尚无工作项的用户要求用 USER_REQUIREMENT/userQuote，由内容核对确认。未明差额成因不自动成为调查要求。[] 清除旧缺口，正文复用 {{analysisPartialReason}}。'},
                        'renderCheckUnavailableReason': {'type': 'string', 'minLength': 1, 'maxLength': 1000, 'description': '仅浏览器检查确实不可用时填写具体原因；记录未检查状态，不证明布局合格，也不能跳过已测到的错误。修复草案时通过 repairs 修改此字段。'},
                        "technicalDetails": {"type": "boolean"},
                        "findingPlan": array(FINDING_CHECK, 100), "actionPlan": array(ACTION_PLAN, 300),
                        "thresholds": array(THRESHOLD, 20),
                        "hypotheses": array(obj({"findingId": TEXT, "statement": TEXT, "missingEvidence": array(TEXT, 20, 1)}, ["findingId", "statement", "missingEvidence"]), 30)}, ["requestKey"])
    input_schema['oneOf'] = [{'required': ['analysisRef', 'document'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['viewPlan']}, {'required':['presentation']}]}},
                             {'required': ['analysisRef', 'claims', 'viewPlan'], 'not': {'anyOf': [{'required':['repairRef']}, {'required':['document']}]}},
                             {'required': ['repairRef', 'repairs'], 'not': {'anyOf': [{'required': [key]} for key in
                              ('claims', 'viewPlan', 'document', 'presentation', 'workRefs', 'unresolvedItems', 'renderCheckUnavailableReason', 'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses')]}}]
    input_schema['allOf'] = [{'not': {'required': ['finalize', 'previewOnly'],
                                    'properties': {'finalize': {'const': True}, 'previewOnly': {'const': True}}}}]
    input_schema['properties']['resumeReview'] = {'const': True, 'description': '对已交付待审报告续审。只传 analysisRef/requestKey/resumeReview=true，沿冻结材料重试未完成段，不重查、不重算、不重写报告。'}
    input_schema['properties']['reviewAction'] = {'enum': ['VIEW', 'RESUME', 'RECHECK'],
        'description': '查看原结论用 VIEW，补未完成的审核用 RESUME，要求再次审核已完成报告用 RECHECK。冻结数据和已审核计算由宿主复用。'}
    for branch in input_schema['oneOf']:
        prior_exclusion = branch.pop('not', None)
        branch['not'] = {'anyOf': ([prior_exclusion] if prior_exclusion else []) + [{'required': [field]} for field in ('resumeReview', 'reviewAction')]}
    input_schema['oneOf'].append({'required': ['analysisRef', 'resumeReview'], 'not': {'anyOf': [
        {'required': [key]} for key in input_schema['properties'] if key not in ('analysisRef', 'requestKey', 'resumeReview')]}})
    input_schema['oneOf'].append({'required': ['analysisRef', 'reviewAction'], 'not': {'anyOf': [
        {'required': [key]} for key in input_schema['properties'] if key not in ('analysisRef', 'requestKey', 'reviewAction')]}})
    description += ' PENDING_REVIEW 为已保存待审，REQUIRES_REVISION 携带实质问题。依据 reportDelivery 说明状态。复核已交付报告时用 reviewAction，提交修订只处理实质问题。'

    async def call(self, **kwargs):
        document = kwargs.get('document')
        if isinstance(document, dict) and 'analysisRef' in kwargs and not {'repairRef', 'resumeReview'} & kwargs.keys():
            # Placement is unambiguous for a fresh document. References and conflicts continue through normal validation.
            for field in ('bindings', 'datasets'):
                misplaced = kwargs.get(field)
                existing = document.get(field, [])
                if isinstance(misplaced, list) and isinstance(existing, list):
                    document = {**document, field: [*existing, *kwargs.pop(field)]}
            kwargs['document'] = document
        return await super().call(**kwargs)


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
