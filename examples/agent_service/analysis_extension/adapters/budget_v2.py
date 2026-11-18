"""Budget v2 form/observation adapter with original-request checks."""

from __future__ import annotations

from collections import defaultdict
import re

from ..models import AnalysisError, decimal_text, decimal_value, digest, exact_loads, json_values
from ..sources import SourceAdapter, source_context, combined_context
from .budget_periods import period_execution, period_coverage

def budget_receipt(response: dict) -> dict:
    candidates = []
    for block in response.get("content", []):
        if block.get("type") != "text":
            continue
        try:
            value = exact_loads(block["text"])
        except AnalysisError:
            continue
        if isinstance(value, dict) and value.get("contractVersion") == "2.0":
            candidates.append(value)
    if len(candidates) != 1:
        raise AnalysisError("RECEIPT_UNAVAILABLE", "该输入不包含唯一、完整的预算 v2 回执。")
    return candidates[0]


def declares_v2(schema):
    version = schema.get("properties", {}).get("contractVersion", {})
    if version.get("const") == "2.0" or version.get("enum") == ["2.0"]:
        return True
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return all(declares_v2(branch) for branch in schema[key])
    return any(declares_v2(branch) for branch in schema.get("allOf", []))


CONTEXT_FIELDS = ("contextId", "sourceId", "snapshotId", "asOf", "schemaHash")


def data_context(context):
    # Reopening a pinned dataset changes query identity, not its data boundary.
    # Preserve every other field, including any source-provided schema metadata.
    return {key: value for key, value in (context or {}).items() if key != 'contextId'}


def budget_source_context(capture, receipt):
    return source_context(capture, receipt['context'], receipt['querySessionId'], data_context(receipt['context']))


FAILED = {"FAILED", "ERROR", "DENIED", "INTERRUPTED", "IN_PROGRESS", "CANCELLED"}


def batch_query_context(capture, receipt):
    """保留请求范围及预算实际执行范围，不能把元数据枚举当成预算授权范围。"""
    # 失败回执可能只有部分字段；诊断整理不能掩盖原始来源异常。
    def mapping(value):
        return value if isinstance(value, dict) else {}

    def objects(value):
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    data = mapping(receipt.get('data'))
    results = {step.get('stepId'): step for step in objects(data.get('stepResults')) if isinstance(step.get('stepId'), str)}
    queries = []
    for step in objects(mapping(mapping(capture.get('request')).get('plan')).get('steps')):
        if step.get('operation') != 'BATCH_DATA_QUERY':
            continue
        query = {key: value for key, value in mapping(step.get('batchQuery')).items()
                 if key in ('metrics', 'batchDimensions', 'periodRequest', 'resultWhere', 'aggregation', 'having', 'orderBy')}
        tables = []
        step_id = step.get('id')
        result = results.get(step_id, {}) if isinstance(step_id, str) else {}
        for table in objects(result.get('batchResults')):
            data = mapping(table.get('data'))
            metric_contexts = []
            for index, metric in enumerate(objects(table.get('metrics'))):
                context = {key: metric[key] for key in ('name', 'measureCode', 'selectedDimensions', 'context', 'dimensionModels',
                           'taskName', 'formName', 'selectionObservationId', 'selectionCellKey') if key in metric}
                if metric.get('selectionObservationId') and metric.get('selectionCellKey'):
                    # 已定位观察保存了原表外条件；不能再混入与 observationId 互斥的 periodRequest/refresh。
                    context['reuseSelection'] = {'observationId': metric['selectionObservationId'],
                                                 'cellKey': metric['selectionCellKey']}
                    period = mapping(metric.get('context')).get('period')
                    if period:
                        context['reuseSelection']['periodCode'] = period
                metric_contexts.append(context)
            tables.append({**{key: table[key] for key in ('batchId', 'disposition', 'message', 'resolvedScope') if key in table},
                           'budTaskId': mapping(table.get('request')).get('budTaskId'),
                           'metricContexts': metric_contexts,
                           **{key: data[key] for key in ('resolvedPeriods', 'rangeSummary', 'aggregationSummary', 'rowCount') if key in data}})
        queries.append({'stepId': step.get('id'), 'requestedScope': query, 'results': tables})
    return queries


def _incomplete_details(capture: dict, receipt: dict | None = None) -> dict:
    if receipt is None:
        try:
            receipt = budget_receipt(capture.get("response", {}))
        except (AnalysisError, AttributeError, KeyError, TypeError, ValueError):
            receipt = {}
    details = {"captureRef": capture.get("captureRef"), "captureState": capture.get("captureState"),
               "path": "/captureRefs", "nextAction": "INSPECT_CAPTURE",
               "hint": "先读取失败回执确认未取得的范围；失败输入不能用于完整分析，也不能改成零或删去必要工作项来宣布完成。"}
    if capture.get("captureRef"):
        details["nextRead"] = {"section": "receipt", "captureRef": capture["captureRef"]}
    details.update({key: receipt[key] for key in ("executionId", "status") if key in receipt})
    source_error = receipt.get("error")
    if isinstance(source_error, dict):
        details["sourceError"] = {key: source_error[key] for key in
                                  ("code", "category", "message", "details", "retryable", "recoveryAction") if key in source_error}
    details['batchQueries'] = batch_query_context(capture, receipt)
    failures = []
    data = receipt.get("data")
    data = data if isinstance(data, dict) else {}
    steps = data.get("stepResults")
    steps = steps if isinstance(steps, list) else []
    for step in [*steps, {"formResults": data.get("formResults")}]:
        if not isinstance(step, dict) or not isinstance(step.get("formResults"), list):
            continue
        for form in step["formResults"]:
            if isinstance(form, dict) and form.get("disposition") == "FAILED":
                failures.append({"stepId": step.get("stepId"), "status": form.get("status"),
                                 "message": str(form.get("message", ""))[:500]})
    details["failedSteps"] = failures[:100]
    details["failedStepCount"] = len(failures)
    return details


def receipt_from_capture(capture: dict) -> dict:
    if str(capture.get("captureState", "")).upper() not in ("SUCCESS", "SUCCEEDED"):
        raise AnalysisError("CAPTURE_INCOMPLETE", "该工具调用未成功完成，不能作为完整分析输入。", _incomplete_details(capture))
    receipt = budget_receipt(capture["response"])
    if receipt.get("status") not in ("SUCCEEDED", "PARTIAL"):
        raise AnalysisError("EXECUTION_INCOMPLETE", "预算执行尚未完成或失败。", _incomplete_details(capture, receipt))
    request = capture["request"]
    context = receipt.get("context") or {}
    for name in CONTEXT_FIELDS:
        if request.get(name) is not None and request[name] != context.get(name):
            raise AnalysisError("REQUEST_MISMATCH", "回执的查询上下文与原请求不一致。")
    if request.get("querySessionId") and request["querySessionId"] != receipt.get("querySessionId"):
        raise AnalysisError("REQUEST_MISMATCH", "回执的查询会话与原请求不一致。")
    return receipt


def form_rows(receipt: dict):
    data = receipt.get("data") or {}
    for step in data.get("stepResults", []):
        for form in step.get("formResults", []):
            yield step.get("stepId"), form
    for form in data.get("formResults", []):
        yield None, form


def pointer(value, path: str):
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        raise AnalysisError("INVALID_POINTER", "来源定位需要有界 JSON Pointer。")
    try:
        for part in path.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AnalysisError("POINTER_NOT_FOUND", "来源中未找到指定字段；先读 selectors 的 memberPointers，不能猜字段。",
                            {'field': 'memberPointer', 'pointer': path, 'nextSection': 'selectors'}) from exc
    return value


def _step_request(capture, step_id):
    steps = capture["request"].get("plan", {}).get("steps", [])
    selected = [s for s in steps if s.get("id") == step_id]
    if len(selected) != 1:
        raise AnalysisError("REQUEST_TARGET_UNPROVEN", "无法把表结果对应到唯一的原请求步骤。")
    return selected[0], {s["id"]: s for s in steps}


def _bindings(step, steps, visited=None):
    visited = set() if visited is None else visited
    sid = step.get("id")
    if sid in visited:
        raise AnalysisError("CYCLIC_PLAN", "原请求步骤存在循环。")
    visited = visited | {sid}
    result = set(step.get("resourceIris", []))
    for sid in step.get("inputs", []):
        if sid in steps:
            result.update(_bindings(steps[sid], steps, visited))
    return result


def validate_form_request(capture, receipt, step_id, form):
    runtime = form.get("runtime") or {}
    context = runtime.get("context") or {}
    base = context.get("baseContext") or {}
    for field in CONTEXT_FIELDS:
        if not receipt.get("context", {}).get(field) or base.get(field) != receipt["context"][field]:
            raise AnalysisError("CONTEXT_CONFLICT", "表观察与预算回执不属于同一固定上下文。")
    step, steps = _step_request(capture, step_id)
    query = step.get("formQuery", {})
    progress = period_execution(capture, receipt, step_id, context)
    if query.get("periodCode") and query["periodCode"] != context.get("period"):
        raise AnalysisError("REQUEST_MISMATCH", "表观察时期与原请求不一致。")
    for key, value in query.get("dimensions", {}).items():
        if context.get("effectiveDimensions", {}).get(key) != value:
            raise AnalysisError("REQUEST_MISMATCH", "表观察维度与原请求不一致。")
    binding = context.get("target", {}).get("formBindingKey")
    bindings = _bindings(step, steps)
    if not bindings:
        # Dynamic inputs may resolve a binding in earlier, returned plan rows.
        for previous in receipt.get("data", {}).get("stepResults", []):
            if previous.get("stepId") in step.get("inputs", []):
                bindings.update(row["resourceIri"] for row in previous.get("rows", []) if row.get("resourceIri"))
        if progress:
            bindings.update(progress['bindingIris'])
    if not binding or binding not in bindings:
        raise AnalysisError("BINDING_UNPROVEN", "表结果绑定未由原请求或已返回的上游步骤证明。")
    observation = runtime.get("observation") or {}
    upgraded = (step.get('operation') == 'FORM_VIEW_QUERY'
                and observation.get('entryType') == 'INPUT_VO'
                and query.get('observationId') in form.get('visitedObservationIds', [])
                and observation.get('observationId') in form.get('visitedObservationIds', []))
    if query.get("observationId") and query["observationId"] != observation.get("observationId") and not upgraded:
        raise AnalysisError("OBSERVATION_CONFLICT", "返回的观察与原请求不一致。")
    page = form.get("page")
    if page and (page.get("observationId") != observation.get("observationId") or page.get("contentDigest") != observation.get("contentDigest")):
        raise AnalysisError("OBSERVATION_CONFLICT", "数据页与已声明观察不一致。")
    from ..grid_selection import resolved_form_query, validate_selection
    query = resolved_form_query(query, receipt, step_id)
    validate_selection(form, query, upgraded=upgraded)
    return context, observation


def cell_value(cell, number_format=None):
    if cell.get("issue"):
        return None, "FAILED", "unavailable"
    raw = cell.get("rawValueJson")
    if raw is None:
        display = cell.get("displayValue")
        if display in (None, ""):
            return None, "NO_VALUE", "unavailable"
        try:
            return decimal_text(decimal_value(display)), "PRESENT", "display_only"
        except AnalysisError:
            return None, "NOT_APPLICABLE", "display_only"
    try:
        value = exact_loads(raw) if isinstance(raw, str) else raw
        if value is None:
            return None, "NO_VALUE", "raw_json"
        if isinstance(value, str) and number_format == "grouped-dot" and re.fullmatch(r"-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?", value):
            value = value.replace(",", "")
        return decimal_text(decimal_value(value)), "PRESENT", "raw_json" if isinstance(raw, str) else "typed_value"
    except AnalysisError as exc:
        return None, "FAILED" if exc.code != "NOT_NUMERIC" else "NOT_APPLICABLE", exc.code


def catalog(capture, *, grid_selection=None):
    receipt = receipt_from_capture(capture)
    from ..identity import dimension_models
    items = []
    for step_id, form in form_rows(receipt):
        # 文件有独立读取入口。目录和最终核对不展开全部文件内容。
        if form.get('file') and grid_selection is None:
            continue
        context = (form.get("runtime") or {}).get("context") or {}
        cells = (form.get("page") or {}).get("cells", form.get("cells", []))
        if form.get('file'):
            observation = (form.get('runtime') or {}).get('observation', {})
            if step_id != grid_selection['stepId'] or (grid_selection.get('observationId')
                    and observation.get('observationId') != grid_selection['observationId']):
                continue
            from ..grid_files import captured_cells
            wanted = set(grid_selection.get('cellKeys', []))
            # 表内依赖已由本体提取；仅构建时读本题所选文件，不把文件塞回模型目录。
            if grid_selection.get('includeFormulaDependencies'):
                wanted.update((form.get('formulaGraph') or {}).get('cellKeys', []))
            cells = [cell for cell in captured_cells(capture, form) if cell.get('key') in wanted]
        for cell in cells:
            value, state, precision = cell_value(cell, capture.get("numberFormat"))
            oid = (form.get("runtime") or {}).get("observation", {}).get("observationId")
            source = budget_source_context(capture, receipt)
            items.append({"factId": fact_id(source['sourceContextId'], oid, cell.get('key')),
                          "sourceContextId": source['sourceContextId'],
                          "captureRef": capture['captureRef'], "observationId": oid,
                          "context": receipt.get('context'), "querySessionId": receipt.get('querySessionId'),
                          "dataContext": data_context(receipt['context']),
                          "taskName": form.get('taskName'), "formName": form.get('formName'),
                          "observationExhausted": (form.get('page') or {}).get('observationExhausted'),
                          "executionId": receipt.get('executionId'),
                          "stepId": step_id, "bindingRef": context.get("target", {}).get("formBindingKey"),
                          "dimensionModels": dimension_models(form),
                          "effectiveDimensions": context.get("effectiveDimensions", {}),
                          "period": cell.get('dimensions', {}).get('DATATIME', context.get('period')),
                          "executionPeriod": context.get('period'), "periodResolution": context.get('periodResolution'),
                          "cellKey": cell.get("key"), "row": cell.get("row"), "column": cell.get("column"),
                          "headers": cell.get("headers", []), "modelCode": cell.get("modelCode"), "measureCode": cell.get("measureCode"),
                          "dimensions": cell.get("dimensions", {}), "displayValue": cell.get("displayValue"),
                          "unit": cell.get("unit"), "value": value, "valueState": state, "precision": precision,
                          "formula": cell.get("formula"), "cellAddress": source_address(cell, cells),
                          "formulaStructure": cell.get("formulaStructure"),
                          "formulaTarget": cell.get('key') in (form.get('formulaGraph') or {}).get('targetKeys', [])})
    from .budget_batch import catalog as batch_catalog
    items.extend(batch_catalog(capture))
    return items


def source_address(cell, cells):
    if isinstance(cell.get('formulaStructure'), dict):
        # 新协议已在本体侧确认地址；显式未解析时不能再次猜测坐标。
        return cell['formulaStructure'].get('address')
    # Budget Grid2 reserves row/column zero for indexes. Its formula service
    # converts grid (r,c) to POI CellAddress(r-1,c-1); positive (1,1) is A1.
    # Explicit source index labels take precedence for expanded grids.
    columns = {str(c.get("displayValue", "")).strip().upper() for c in cells
               if c.get("mapType") == "COL_INDEX" and c.get("column") == cell.get("column")}
    rows = {str(c.get("displayValue", "")).strip() for c in cells
            if c.get("mapType") == "ROW_INDEX" and c.get("row") == cell.get("row")}
    if len(columns) == len(rows) == 1:
        column, row = next(iter(columns)), next(iter(rows))
        if re.fullmatch(r"[A-Z]{1,4}", column) and re.fullmatch(r"[1-9][0-9]{0,6}", row):
            return column + row
    if columns or rows:
        return None
    row, column = cell.get("row"), cell.get("column")
    if (type(row) is int and type(column) is int and 1 <= row <= 9999999
            and 1 <= column <= 475254 and cell.get("key") == f"{row}:{column}"):
        name = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            name = chr(65 + remainder) + name
        return name + str(row)
    return None


def resolve_form_requests(captures, receipts=None):
    """沿服务端续查链恢复查询计划，供数值、结构及文件入口共用；不改写原始回执。"""
    receipts = receipts if receipts is not None else {ref: receipt_from_capture(value) for ref, value in captures.items()}
    # 只认可真实上游执行和同一来源/上下文；不能按相似步骤、机构或表样猜计划。
    effective = {}
    def linked(ref):
        path, visiting = [], set()
        current = ref
        while current not in effective:
            if current in visiting:
                raise AnalysisError("CYCLIC_RESUME", "续读链存在循环。")
            visiting.add(current)
            capture, receipt = captures[current], receipts[current]
            resume = capture['request'].get('resumeRef')
            if not resume:
                effective[current] = capture
                break
            parents = [key for key, r in receipts.items()
                       if r.get("executionId") == resume and r.get("resumeRef") == resume and r.get("status") == "PARTIAL"
                       and all(capture.get(k) == captures[key].get(k) for k in ("resourceId", "originalToolName", "serverName", "connectionFingerprint"))
                       and receipt.get("querySessionId") == r.get("querySessionId")
                       and all(receipt.get('context', {}).get(k) == r.get('context', {}).get(k) for k in CONTEXT_FIELDS)]
            if len(parents) != 1 or receipt.get("continuationOf") != resume:
                raise AnalysisError("RESUME_UNPROVEN", "续读缺少唯一的已捕获上游执行或服务端关联。")
            path.append((current, parents[0]))
            current = parents[0]
        for child, parent in reversed(path):
            capture = captures[child]
            if 'plan' in capture['request'] and capture['request']['plan'] != effective[parent]['request'].get('plan'):
                raise AnalysisError("RESUME_UNPROVEN", "续查不能替换已捕获原请求的查询计划。")
            effective[child] = {**capture, 'request': {**effective[parent]['request'], **capture['request']}}
        return effective[ref]
    return {ref: linked(ref) for ref in captures}


def stored_form_chain(store, owner, capture):
    """只加载当前会话内服务端指向的续查上游，普通输入无须搜索。"""
    if not capture['request'].get('resumeRef'):
        return {capture['captureRef']: capture}
    candidates = {capture['captureRef']: capture}
    summaries, offset = {}, 0
    while offset is not None:
        page = store.list_captures(owner, offset=offset, limit=store.limits.page_size)
        for item in page['items']:
            execution = (item.get('receiptSummary') or {}).get('executionId')
            if execution:
                summaries.setdefault(execution, []).append(item['captureRef'])
        offset = page['nextOffset']
    pending = [capture]
    while pending:
        child = pending.pop()
        for reference in summaries.get(child['request'].get('resumeRef'), []):
            if reference not in candidates:
                parent = store.get_capture(owner, reference)
                # 后续共享校验还会验证 querySession、上下文及 continuationOf。
                if all(child.get(key) == parent.get(key) for key in
                       ('resourceId', 'originalToolName', 'serverName', 'connectionFingerprint')):
                    candidates[reference] = parent
                    pending.append(parent)
    return candidates


def stored_form_request(store, owner, capture):
    if not capture['request'].get('resumeRef'):
        return capture
    return resolve_form_requests(stored_form_chain(store, owner, capture))[capture['captureRef']]


def selected_form(capture, receipt, selection):
    """按步骤、表样、时期和观察定位显式单元格选择器，要求恰好命中一次。"""
    forms = []
    for step_id, form in form_rows(receipt):
        runtime = form.get('runtime') or {}
        context = runtime.get('context') or {}
        identifiers = {'observationId': (runtime.get('observation') or {}).get('observationId'),
                       'bindingRef': (context.get('target') or {}).get('formBindingKey'), 'executionPeriod': context.get('period')}
        if step_id == selection.get('stepId') and all(not selection.get(key) or selection[key] == value
                                                      for key, value in identifiers.items()):
            forms.append((step_id, form))
    if len(forms) != 1:
        raise AnalysisError("AMBIGUOUS_FORM", "表结果无法唯一定位，请指定实际步骤、绑定、执行时期或观察标识。")
    step_id, form = forms[0]
    context, observation = validate_form_request(capture, receipt, step_id, form)
    return step_id, form, context, observation


def adapt_forms(captures: dict[str, dict], selections: list[dict], scope_request: dict, max_facts: int) -> dict:
    if not selections:
        raise AnalysisError("EMPTY_SELECTION", "需要选择实际捕获的对象和单元格。")
    receipts = {ref: receipt_from_capture(value) for ref, value in captures.items()}
    captures = resolve_form_requests(captures, receipts)
    from ..identity import dimension_models, names_for
    from ..actions import source_identity
    labels = {}
    from .budget_discovery import entity_rows, selected_rows, rows_complete
    for capture_ref, receipt in receipts.items():
        origin = digest({'source': source_identity(captures[capture_ref]), 'context': data_context(receipt['context'])})
        for row in entity_rows(receipt):
            if row.get("key") and row.get("label"):
                labels.setdefault((origin, row.get("modelCode"), row["key"]), set()).add(row["label"])
    data_refs = {item.get('captureRef') for item in selections}
    data_refs.update(ref for ref, capture in captures.items() if capture.get('originalToolName') != 'validate_evidence')
    contexts = {tuple(receipts[ref].get("context", {}).get(k) for k in CONTEXT_FIELDS) for ref in data_refs if ref in receipts}
    boundaries = {digest(data_context(receipts[ref].get('context', {}))) for ref in data_refs if ref in receipts}
    mode = scope_request.get('contextMode', 'SINGLE_SNAPSHOT')
    if mode not in ('SINGLE_SNAPSHOT', 'COMPOSITE'):
        raise AnalysisError('INVALID_SCOPE', 'contextMode 仅支持 SINGLE_SNAPSHOT 或 COMPOSITE。')
    if any(any(v is None for v in context) for context in contexts) or (mode != 'COMPOSITE' and len(boundaries) != 1):
        raise AnalysisError("CONTEXT_CONFLICT", "所选输入跨查询上下文、来源、快照或读取口径。")
    if any(not r.get("querySessionId") for r in receipts.values()):
        raise AnalysisError("CONTEXT_CONFLICT", "预算输入必须保留实际查询会话。")
    origins = {digest({key: capture.get(key) for key in ('resourceId', 'serverName', 'connectionFingerprint')}) for ref, capture in captures.items() if ref in data_refs}
    if mode != 'COMPOSITE' and len(origins) != 1:
        raise AnalysisError('CONTEXT_CONFLICT', '不同来源连接须显式以 COMPOSITE 分组汇编。')
    source_contexts = {ref: budget_source_context(capture, receipts[ref]) for ref, capture in captures.items()}
    facts_by_observation = {}
    member_models = {}
    pages = defaultdict(list)
    selected_observations = set()
    scope_members = scope_request.get("members", [])
    if not isinstance(scope_members, list) or not scope_members or any(not isinstance(x, str) or not x for x in scope_members) or len(set(scope_members)) != len(scope_members):
        raise AnalysisError("INVALID_SCOPE", "范围必须是无重复的明确对象身份集合。")
    reference_members = scope_request.get('referenceMembers', [])
    if (not isinstance(reference_members, list) or len(reference_members) > 1000
            or any(not isinstance(x, str) or not x for x in reference_members)
            or len(set(reference_members)) != len(reference_members) or set(reference_members) & set(scope_members)):
        raise AnalysisError('INVALID_SCOPE', 'referenceMembers 是不重复且不与 members 重叠的比较对象标识。', {'path': '/scope/referenceMembers'})
    selected_members = set(scope_members) | set(reference_members)
    for excluded in scope_request.get("excludedMembers", []):
        if excluded.get("captureRef") not in captures or excluded.get("objectId") in selected_members or not excluded.get("reason"):
            raise AnalysisError("INVALID_EXCLUSION", "排除对象需要独立的范围说明和已纳入的来源引用。")
    scope_issues = {}
    def scope_issue(details):
        # Group repeated mismatches, while retaining every distinct member or constraint.
        identity = (details['field'], details.get('dimension'), details.get('expected'), details.get('actual'))
        if identity not in scope_issues:
            scope_issues[identity] = {**details, 'path': '/' + details['field'].replace('.', '/'),
                                      'affectedCellCount': 0}
        scope_issues[identity]['affectedCellCount'] += 1
    for selection in selections:
        ref = selection.get("captureRef")
        if ref not in captures:
            raise AnalysisError("UNKNOWN_CAPTURE", "选择器引用了未纳入本次分析的输入。")
        capture, receipt = captures[ref], receipts[ref]
        sid, form, context, observation = selected_form(capture, receipt, selection)
        progress = period_execution(capture, receipt, sid, context)
        if not observation.get("observationId"):
            raise AnalysisError("MISSING_OBSERVATION", "该结果没有可用的运行观察。")
        oid = observation["observationId"]
        observation_key = (source_contexts[ref]['sourceContextId'], oid, (form.get('selection') or {}).get('contentDigest'))
        selected_observations.add(observation_key)
        page = form.get("page")
        if page:
            pages[observation_key].append({**page, "receiptComplete": receipt.get("completeness", {}).get("enumerationExhausted") is True})
        else:
            pages[observation_key].append({"targeted": True, "receiptComplete": receipt.get("completeness", {}).get("enumerationExhausted") is True})
        from ..grid_files import captured_cells
        cells = captured_cells(capture, form)
        keys = selection.get("cellKeys", [])
        if not keys or len(keys) != len(set(keys)):
            raise AnalysisError("INVALID_SELECTOR", "选择器需要无重复的实际 cellKeys。")
        if form.get('file'):
            requested_rows = {key.split(':')[0] for key in keys}
            requested_columns = {key.split(':')[-1] for key in keys}
            chosen = set(keys)
            index_cells, matched = [], {}
            for cell in cells:
                if cell.get('key') in chosen:
                    matched[cell['key']] = cell
                if (cell.get('mapType') == 'ROW_INDEX' and str(cell.get('row')) in requested_rows
                        or cell.get('mapType') == 'COL_INDEX' and str(cell.get('column')) in requested_columns):
                    index_cells.append(cell)
            cells = [*matched.values(), *index_cells]
        else:
            matched = {c["key"]: c for c in cells if c.get("key") in keys}
        if set(keys) != set(matched):
            raise AnalysisError("CELL_NOT_CAPTURED", "尚未完整捕获所选单元格，请补页或检查定位；不要按连续行号补造 cellKeys 或因此删除本题主体。",
                                {'captureRef': ref, 'stepId': sid, 'observationId': oid,
                                 'missingCellKeys': sorted(set(keys) - set(matched)),
                                 'capturedSelectionKeys': sorted(matched),
                                 'selectionRead': {'section': 'selectors', 'captureRef': ref}})
        for key in keys:
            cell = matched[key]
            effective = {**context.get("effectiveDimensions", {}), **cell.get("dimensions", {})}
            # 完整表页包含目标以外的公式依赖。运行条件已由 validate_form_request 校验；
            # 目标约束只约束定位结果，不能错误地把每个同表依赖都限定成目标科目。
            graph = form.get('formulaGraph') or {}
            dependency = key in graph.get('cellKeys', []) and key not in graph.get('targetKeys', [])
            query = {} if page or form.get('file') or dependency else _step_request(capture, sid)[0].get("formQuery", {})
            for request_field, cell_field in (("cellKey", "key"), ("measureCode", "measureCode"), ("modelCode", "modelCode")):
                if query.get(request_field) is not None and cell.get(cell_field) != query[request_field]:
                    raise AnalysisError("REQUEST_TARGET_MISMATCH", "定位单元格与原请求的指标或坐标不一致。")
            if query.get('expression') is not None and cell.get('expression') != query['expression']:
                # Native offset resolution is attested by the source; do not reproduce that engine in Python.
                if (form.get('matchedExpression') != query['expression'] or form.get('status') not in ('VALUE_FOUND', 'CELL_HAS_NO_VALUE')
                        or cell not in form.get('cells', []) or cell.get('issue')):
                    raise AnalysisError('REQUEST_TARGET_MISMATCH', '表达式变化缺少服务端完成时期偏移后的目标匹配证明。')
            for dimension, expected in query.get("targetDimensions", {}).items():
                if effective.get(dimension) != expected:
                    raise AnalysisError("REQUEST_TARGET_MISMATCH", "定位单元格与原请求的目标维度不一致。")
            outside_scope = False
            for dimension, expected in scope_request.get("expectedDimensions", {}).items():
                if effective.get(dimension) != expected:
                    outside_scope = True
                    scope_issue({'field': 'scope.expectedDimensions', 'dimension': dimension,
                                 'expected': expected, 'actual': effective.get(dimension),
                                 'captureRef': ref, 'stepId': sid, 'cellKey': key, 'nextSection': 'inventory'})
            member_dimension = scope_request.get("memberDimension")
            member = effective.get(member_dimension) if member_dimension else context["target"]["formBindingKey"]
            if member not in selected_members:
                outside_scope = True
                scope_issue({'field': 'scope.members', 'memberDimension': member_dimension, 'actual': member,
                             'captureRef': ref, 'stepId': sid, 'cellKey': key, 'nextSection': 'inventory'})
            if outside_scope:
                continue
            value, state, precision = cell_value(cell, capture.get("numberFormat"))
            model = dimension_models(form).get(member_dimension)
            if member in member_models and member_models[member] != model:
                raise AnalysisError('MEMBER_IDENTITY_CONFLICT', '同一对象编码对应不同来源模型，须拆分分析范围，不能合并展示。')
            member_models[member] = model
            origin = digest({'source': source_identity(capture), 'context': data_context(receipt['context'])})
            names = names_for(labels, model, member, origin)
            fid = fact_id(source_contexts[ref]['sourceContextId'], oid, key)
            fact = {"factId": fid, "captureRefs": [ref], "stepId": sid, "objectId": member,
                    "objectLabel": next(iter(names)) if len(names) == 1 else "",
                    "objectLabelState": "RESOLVED" if len(names) == 1 else "CONFLICT" if names else "MISSING",
                    "objectModelCode": model,
                    "executionId": receipt.get("executionId"),
                    "bindingRef": context["target"]["formBindingKey"], "observationId": oid,
                    "context": json_values(receipt["context"]), "contentDigest": observation.get("contentDigest"),
                    "dataContext": data_context(receipt['context']),
                    "sourceContextId": source_contexts[ref]['sourceContextId'], "querySessionId": receipt['querySessionId'],
                    "cellKey": key, "row": cell.get("row"), "column": cell.get("column"),
                    "cellAddress": source_address(cell, cells),
                    "measureCode": cell.get("measureCode"), "headers": cell.get("headers") or [c["displayValue"] for c in cells if c.get("row") == cell.get("row") and c.get("displayValue") and cell_value(c, capture.get("numberFormat"))[1] == "NOT_APPLICABLE"],
                    "dimensions": effective, "dimensionModels": dimension_models(form),
                    "formContext": json_values(context),
                    "period": effective.get("DATATIME", context.get("period")),
                    "executionPeriod": context.get('period'), "periodResolution": context.get('periodResolution'),
                    "periodPlanId": progress['planId'] if progress else None,
                    "periodWorkIndex": progress['workIndex'] if progress else None,
                    "formName": form.get("formName"), "taskName": form.get("taskName"),
                    "rawValueJson": cell.get("rawValueJson"), "displayValue": cell.get("displayValue"),
                    "value": value, "valueState": state, "precision": precision,
                    "unit": cell.get("unit"), "scale": "1" if cell.get("unit") else None,
                    "scaleBasis": "source_unit_label" if cell.get("unit") else None,
                    "formula": cell.get("formula"), "formulaStructure": cell.get("formulaStructure"),
                    "formulaTarget": key in graph.get('targetKeys', []), "readAt": form.get("readAt"),
                    "entryType": observation.get("entryType"),
                    "expression": cell.get("expression"),
                    "formulaStatus": ('CAPTURED' if cell.get('formula') else 'NOT_CONFIGURED'
                                      if observation.get('entryType') == 'INPUT_VO' else 'NOT_CAPTURED'),
                    "evidenceIds": [e["evidenceId"] for e in receipt.get("evidence", []) if e.get("kind") == "FORM_RUNTIME" and e.get("fact", {}).get("observation", {}).get("observationId") == oid]}
            if fid in facts_by_observation:
                previous = facts_by_observation[fid]
                if any(previous[k] != fact[k] for k in ("value", "rawValueJson", "dimensions", "contentDigest", "unit", "objectModelCode")):
                    previous["valueState"] = "CONFLICT"
                previous["captureRefs"] = sorted(set(previous["captureRefs"] + [ref]))
            else:
                facts_by_observation[fid] = fact
            if len(facts_by_observation) > max_facts:
                raise AnalysisError("FACT_LIMIT", "所选事实超过本次分析容量上限。")
    if scope_issues:
        issues = list(scope_issues.values())
        missing = list(dict.fromkeys(i['actual'] for i in issues if i['field'] == 'scope.members' and i['actual'] is not None))
        raise AnalysisError('SCOPE_MISMATCH', '所选单元格实际维度或对象不符合声明范围；一次核对全部 issues 和 missingMembers。主体填 members，额外比较对象填 referenceMembers；保留主体 population，勿删除真实口径绕过。',
                            {**issues[0], 'issues': issues, 'missingMembers': missing})
    facts = list(facts_by_observation.values())
    positions = defaultdict(list)
    for fact in facts:
        positions[(fact['sourceContextId'], fact["bindingRef"], fact["executionPeriod"], fact["objectId"], fact["cellKey"], digest(fact["dimensions"]))].append(fact)
    for group in positions.values():
        if len(group) > 1:
            for fact in group:
                fact["valueState"] = "CONFLICT"
    observed_members = {f["objectId"] for f in facts}
    for member in sorted(selected_members - observed_members):
        facts.append({"factId": "missing_" + digest(member)[:24], "objectId": member,
                      "value": None, "valueState": "NOT_QUERIED", "captureRefs": [], "unit": None, "scale": None})
    # Include continuation pages even when selected facts were on an earlier page.
    pages.clear()
    for ref, receipt in receipts.items():
        for sid, form in form_rows(receipt):
            oid = (form.get("runtime") or {}).get("observation", {}).get("observationId")
            observation_key = (source_contexts[ref]['sourceContextId'], oid, (form.get('selection') or {}).get('contentDigest'))
            if observation_key not in selected_observations:
                continue
            validate_form_request(captures[ref], receipt, sid, form)
            progress = period_execution(captures[ref], receipt, sid, (form.get('runtime') or {}).get('context'))
            page = form.get("page")
            selected_scope = form.get('selection')
            pages[observation_key].append({**({"targeted": True} if selected_scope else page or {"targeted": True}),
                               "periodPlan": progress is not None,
                               "receiptComplete": (progress['periodFinished'] if progress else receipt.get("completeness", {}).get("enumerationExhausted") is True)
                                    and (not form.get('file') or form.get('coverage') == 'COMPLETE')
                                    and (not selected_scope or selected_scope.get('complete') is True)})
    pages_complete = True
    if len(facts) > max_facts:
        raise AnalysisError("FACT_LIMIT", "包含未查询对象后的事实数量超过本次上限。")
    for oid in selected_observations:
        group = pages[oid]
        if all(p.get("targeted") for p in group):
            pages_complete &= all(p["receiptComplete"] for p in group)
            continue
        # A targeted lookup can share an observation with a captured full-grid
        # page. It has no page bounds and cannot prove or invalidate grid coverage.
        group = [p for p in group if not p.get('targeted')]
        if any('startCell' in p or p.get('periodPlan') for p in group):
            if any(type(p.get(k)) is not int for p in group for k in ('startCell', 'endCellExclusive', 'totalCells')):
                raise AnalysisError('INVALID_PAGE_RANGE', '时期网格缺少完整的单元格分页位置。')
            if (len({(p.get('contentDigest'), p['totalCells']) for p in group}) != 1
                    or any(not 0 <= p['startCell'] <= p['endCellExclusive'] <= p['totalCells']
                           or p['endCellExclusive'] - p['startCell'] != len(p.get('cells', [])) for p in group)):
                raise AnalysisError('INVALID_PAGE_RANGE', '单元格分页数量或观察摘要不一致。')
            end, continuous = 0, True
            for p in sorted(group, key=lambda p: (p['startCell'], p['endCellExclusive'])):
                continuous &= p['startCell'] <= end
                end = max(end, p['endCellExclusive'])
            finals = [p for p in group if p['endCellExclusive'] == p['totalCells'] and p.get('observationExhausted') is True
                      and p.get('nextCursor') is None and p['receiptComplete']]
            pages_complete &= continuous and bool(finals) and all(p.get('coverage') == 'COMPLETE' for p in group)
            continue
        if any(type(p.get("startRow")) is not int or type(p.get("endRowExclusive")) is not int or p["startRow"] >= p["endRowExclusive"] for p in group):
            raise AnalysisError("INVALID_PAGE_RANGE", "观察分页范围缺失或不合法。")
        if len({(p.get("contentDigest"), p.get("totalRows"), p.get("totalColumns")) for p in group}) != 1:
            raise AnalysisError("OBSERVATION_CONFLICT", "同一观察的页摘要或范围不一致。")
        unique = {(p.get("startRow"), p.get("endRowExclusive")): p for p in group}
        end = min((p[0] for p in unique), default=2)
        continuous = end in (0, 1)
        for (start, stop), _page in sorted(unique.items()):
            continuous &= start <= end
            end = max(end, stop)
        final = max(group, key=lambda p: p["endRowExclusive"])
        total = final.get("totalRows")
        # Budget GridPage.totalRows is the exclusive grid bound, including
        # header offsets; do not add the first published row to it.
        bounded_end = type(total) is int and final["endRowExclusive"] == total
        pages_complete &= continuous and bounded_end and final.get("observationExhausted") is True and final.get("nextCursor") is None and final["receiptComplete"] and all(p.get("coverage") == "COMPLETE" for p in group)
    periods, periods_complete = period_coverage(facts, captures, receipts, source_contexts)
    pages_complete &= periods_complete
    population_complete = False
    population = scope_request.get("population")
    if population:
        source = receipts.get(population.get("captureRef"))
        if source is None:
            raise AnalysisError("UNKNOWN_POPULATION", "范围来源尚未纳入捕获引用。")
        step = selected_rows(source, population.get('stepId'))
        rows = step.get("rows", [])
        members = [pointer(row, population["memberPointer"]) for row in rows]
        if any(not isinstance(x, str) for x in members) or len(set(members)) != len(members):
            raise AnalysisError("INVALID_POPULATION", "范围来源不是互不重复的明确对象集合。")
        if set(members) != set(scope_members):
            raise AnalysisError("POPULATION_MISMATCH", "主体 members 必须与 population 枚举一致；额外比较对象放入 referenceMembers，不删除范围来源。",
                                {'path': '/scope/members', 'missingMembers': sorted(set(members) - set(scope_members)),
                                 'extraMembers': sorted(set(scope_members) - set(members))})
        population_complete = rows_complete(source, population.get('stepId'), step)
    observed_primary = observed_members & set(scope_members)
    # 表页接收进度与本次选中的事实范围分别记录。未选中的表尾不影响已捕获单元格；
    # 多期查询仍须完成所请求的时期，全集枚举仍由 population 单独证明。
    selected_complete = all(item['selectedDataComplete'] for item in periods) and observed_members == selected_members
    primary_complete = observed_primary == set(scope_members) and all(item['selectedDataComplete'] for item in periods)
    coverage = "COMPLETE" if population_complete and primary_complete else "PARTIAL" if not primary_complete else "UNKNOWN"
    return {"facts": facts, "scope": {**scope_request, "scopeId": "scope_" + digest(scope_request)[:24],
            "coverage": coverage, "populationComplete": population_complete, "selectedDataComplete": selected_complete,
            "sourcePagesComplete": pages_complete,
            "periodCoverage": periods,
            "observedMembers": sorted(observed_primary), "pendingMembers": sorted(set(scope_members) - observed_members),
            "observedReferenceMembers": sorted(observed_members & set(reference_members)),
            "pendingReferenceMembers": sorted(set(reference_members) - observed_members),
            "excludedMembers": scope_request.get("excludedMembers", []), "semanticCompleteness": "UNVERIFIED"},
            "sourceContexts": list({value['sourceContextId']: value for ref, value in source_contexts.items() if ref in data_refs}.values()),
            "context": combined_context([value for ref, value in source_contexts.items() if ref in data_refs])}


def fact_id(context_id, observation_id, cell_key):
    return 'fact_' + digest({'sourceContextId': context_id, 'observation': observation_id, 'cell': cell_key})[:24]


def adapt(captures, selections, scope_request, max_facts):
    """按真实来源形状分派；表格结果与网格事实可共同参与同一分析。"""
    from .budget_batch import adapt as adapt_batch
    table_selections = [selection for selection in selections if 'batchId' in selection]
    form_selections = [selection for selection in selections if 'batchId' not in selection]
    if not table_selections:
        return adapt_forms(captures, selections, scope_request, max_facts)
    if not form_selections:
        return adapt_batch(captures, selections, scope_request, max_facts)
    results = [adapt_forms(captures, form_selections, scope_request, max_facts),
               adapt_batch(captures, table_selections, scope_request, max_facts)]
    facts = [fact for result in results for fact in result['facts'] if fact.get('captureRefs')]
    members, references = set(scope_request['members']), set(scope_request.get('referenceMembers', []))
    observed = {fact['objectId'] for fact in facts}
    for member in sorted((members | references) - observed):
        facts.append({'factId': 'missing_' + digest(member)[:24], 'objectId': member,
                      'value': None, 'valueState': 'NOT_QUERIED', 'captureRefs': [], 'unit': None, 'scale': None})
    if len(facts) > max_facts:
        raise AnalysisError('FACT_LIMIT', '汇编后的事实数量超过上限。')
    contexts = {context['sourceContextId']: context for result in results for context in result['sourceContexts']}
    complete = all(result['scope']['sourcePagesComplete'] for result in results)
    population = any(result['scope']['populationComplete'] for result in results)
    selected_complete = all(result['scope']['selectedDataComplete'] for result in results) and observed == members | references
    return {'facts': facts, 'sourceContexts': list(contexts.values()), 'context': combined_context(list(contexts.values())),
            'scope': {**scope_request, 'scopeId': 'scope_' + digest(scope_request)[:24],
                      'coverage': 'COMPLETE' if population and selected_complete else 'PARTIAL' if not selected_complete else 'UNKNOWN',
                      'populationComplete': population, 'selectedDataComplete': selected_complete,
                      'sourcePagesComplete': complete, 'semanticCompleteness': 'UNVERIFIED',
                      'observedMembers': sorted(members & observed), 'pendingMembers': sorted(members - observed),
                      'observedReferenceMembers': sorted(references & observed), 'pendingReferenceMembers': sorted(references - observed),
                      'periodCoverage': [item for result in results for item in result['scope'].get('periodCoverage', [])],
                      'excludedMembers': scope_request.get('excludedMembers', [])}}


class BudgetV2Adapter(SourceAdapter):
    """First-class budget contract with its original request/evidence checks."""
    adapter_id = 'budget-v2'
    automatic = True

    def __init__(self, options=None):
        super().__init__(options)
        if self.options:
            raise AnalysisError('INVALID_SOURCE_CONFIG', '预算 v2 使用完整原协议，不接受覆盖协议校验的选项。')

    def matches(self, tool, origin):
        inner = tool._tool
        metadata = inner._tool.meta or {}
        return (declares_v2(inner.input_schema) and metadata.get('version') == '2.0'
                and metadata.get('exactContractResource') == 'semantic://contracts/external-query')

    raw_receipt = staticmethod(budget_receipt)
    data_context = staticmethod(data_context)
    receipt = staticmethod(receipt_from_capture)
    catalog = staticmethod(catalog)
    adapt = staticmethod(adapt)

    def summary(self, receipt):
        return {'status': receipt.get('status'), 'executionId': receipt.get('executionId'),
                'context': json_values(receipt.get('context')), 'querySessionId': receipt.get('querySessionId'),
                'stepIds': [step.get('stepId') for step in receipt.get('data', {}).get('stepResults', [])]}

    def compact(self, receipt, ref, source_chars):
        from ..query_results import compact_receipt
        return compact_receipt(receipt, ref, source_chars)

    def is_data(self, capture):
        raw = budget_receipt(capture['response'])
        return not (str(capture.get('captureState', '')).upper() in ('SUCCESS', 'SUCCEEDED')
                    and raw.get('status') == 'SUCCEEDED' and not raw.get('data'))

    def entities(self, capture):
        from .budget_discovery import entity_rows
        return entity_rows(receipt_from_capture(capture))

    def selectors(self, capture):
        from .budget_discovery import selectors
        from .budget_batch import selectors as batch_selectors
        return selectors(capture) + batch_selectors(capture)

    def rows(self, capture, step_id):
        from .budget_discovery import selected_rows
        return selected_rows(receipt_from_capture(capture), step_id)['rows']

    def population(self, capture, selector, members):
        from .budget_discovery import selected_rows, rows_complete
        receipt = receipt_from_capture(capture)
        output = selected_rows(receipt, selector.get('stepId'))
        actual = [pointer(row, selector['memberPointer']) for row in output['rows']]
        if len(set(actual)) != len(actual) or set(actual) != set(members):
            raise AnalysisError('POPULATION_MISMATCH', '主体 members 必须与原始枚举一致。')
        return rows_complete(receipt, selector.get('stepId'), output)

    def relations(self, captures):
        from .budget_relations import captured_relations
        return captured_relations(captures)

    def relation_diagnostics(self, captures):
        from .budget_relations import captured_relations
        diagnostics = []
        captured_relations(captures, diagnostics)
        return diagnostics

    def verify(self, facts, captures, verifier_refs=None):
        from .budget_verification import verification
        return verification(facts, captures, verifier_refs)

    def receipt_info(self, capture):
        receipt = receipt_from_capture(capture)
        from ..grid_files import file_summary
        return {'context': receipt.get('context'), 'querySessionId': receipt.get('querySessionId'),
                'status': receipt.get('status'), 'error': receipt.get('error'),
                'branches': receipt.get('branches', []), 'completeness': receipt.get('completeness'),
                'evidenceIds': [e.get('evidenceId') for e in receipt.get('evidence', [])], 'resumeRef': receipt.get('resumeRef'),
                'batchQueries': batch_query_context(capture, receipt), 'gridFiles': file_summary(capture),
                'gridFileErrors': capture.get('gridFileErrors', [])}

    def member_namespace(self, capture, dimension):
        if capture.get('originalToolName') == 'validate_evidence':
            receipt_from_capture(capture)
            return None
        return super().member_namespace(capture, dimension)

    def readonly(self, tool, origin):
        if tool.is_read_only:
            return 'mcp-readonly-annotation'
        metadata = tool._tool._tool.meta or {}
        if (origin['originalToolName'] in {'execute_semantic_plan', 'validate_evidence'}
                and self.matches(tool, origin) and metadata.get('risk') == 'L1'
                and metadata.get('dataBoundary') == 'PINNED_SOURCE_SNAPSHOT_ASOF'
                and metadata.get('evidencePolicy') == 'EVIDENCE_REQUIRED'):
            return 'budget-v2-business-readonly-contract'
        return None

    def roles(self, tool, origin):
        return {'execute_semantic_plan': ['query'], 'validate_evidence': ['verify']}.get(origin['originalToolName'], [])

    def verification_task(self, fact, capture):
        receipt = receipt_from_capture(capture)
        return {'resourceId': capture['resourceId'], 'executionId': fact.get('executionId'),
                'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId'],
                'sourceContextId': budget_source_context(capture, receipt)['sourceContextId'],
                'evidenceIds': list(fact.get('evidenceIds', [])),
                'toolName': 'validate_evidence', 'action': 'verify'}
