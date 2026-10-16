"""Versioned, allow-listed public projections. Only this layer formats values."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import re

from .engine import RuleRegistry, calculate
from .models import AnalysisError, canonical, decimal_value, digest, precise_sum
from .validation import public_states, validate_source_chart, view_description
from .protocol import validate_artifact
from .formatting import unit_for, number_text, view_label
from .tables import business_tables, completeness_notes, member_key, select_members, ordered_facts

RENDERER_VERSION = "1.11.1"
VIEW_MAX_BYTES = 1024 * 1024
MANIFEST_MAX_BYTES = 64 * 1024
PAGE_SIZE = 12
MAX_PAGE_SIZE = 100
CHART_ROWS = 12
STATES = {"PRESENT": "已取得", "NO_VALUE": "来源无值", "NOT_APPLICABLE": "不适用",
          "FAILED": "读取或解析失败", "NOT_QUERIED": "尚未查询", "CONFLICT": "存在冲突"}
NAMES = {"COUNT": "有效数值", "SUM": "所选范围合计", "DIFFERENCE": "差额", "RATIO": "比值",
         "LINEAR": "已确认公式的算术构成", "CONVERT": "按规则换算"}


def parameters(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"filter", "sort", "topN", "columnKey"}:
        raise AnalysisError("INVALID_VIEW", "视图只接受筛选、排序与 Top N，不能提供替代数值。")
    result = {"filter": value.get("filter", "all"), "sort": value.get("sort", "published"), "topN": value.get("topN"), "columnKey": value.get("columnKey")}
    if result["columnKey"] is not None and (not isinstance(result["columnKey"], str) or not re.fullmatch(r"column_[0-9a-f]{24}", result["columnKey"])):
        raise AnalysisError("INVALID_VIEW_COLUMN", "视图列需要使用当前表头返回的列标识。")
    if result["filter"] not in ("all", "positive", "negative", "zero", "unavailable") or result["sort"] not in ("published", "asc", "desc"):
        raise AnalysisError("INVALID_VIEW", "不支持该筛选或排序方式。")
    if result["topN"] is not None and (type(result["topN"]) is not int or not 1 <= result["topN"] <= 1000):
        raise AnalysisError("INVALID_VIEW", "Top N 必须在 1 至 1000 之间。")
    return result


def opaque(prefix, ref):
    return prefix + digest(ref)[:24]


def label(value, fallback):
    text = str(value or fallback)
    # Source identifiers are not useful business labels in a public projection.
    if re.search(r"(?:https?://|urn:|iri:)", text, re.I):
        return fallback
    return text


def display(value, options=None):
    return number_text(value, options, grouping=True)


def published(artifact):
    validate_artifact(artifact)
    if artifact.get("publicationState") != "PUBLISHED":
        raise AnalysisError("NOT_PUBLISHED", "分析尚未发布。")


def _metrics(artifact, facts, scope, registry, restricted):
    known = {f["factId"]: f for f in artifact["facts"]}
    known.update({v["derivedId"]: v for v in artifact["derivedValues"]})
    selected = {f["factId"] for f in facts}
    cache, derived, result = {}, [], []
    field_names = {"observed": "有效成员", "positive": "正值成员", "negative": "负值成员", "zero": "零值成员", "unavailable": "缺值成员"}
    for view in artifact["viewPlan"]:
        if view["type"] != "METRIC": continue
        field = view.get("field", "value")
        for ref in view["valueRefs"]:
            original = known[ref]
            if ref not in cache:
                item = original
                inputs = set(item.get("sourceFactIds", [ref]))
                if not inputs <= selected:
                    # Recompute only selected direct-fact counts/additive sums.
                    # Derived operands can have other weights and must not be
                    # silently replaced with a sum over their raw source cells.
                    eligible = (restricted and item.get("operator") in ("COUNT", "SUM")
                                and item.get("calculationValidation") == "PASS"
                                and set(item.get("inputRefs", [])) == inputs)
                    if eligible:
                        subset = [f for f in facts if f["factId"] in inputs]
                        operation = {"id": "view" + digest(ref)[:24], "operator": item["operator"], "ruleRef": item.get("ruleRef")}
                        if item.get('comparabilityRef'):
                            operation['comparabilityRef'] = item['comparabilityRef']
                        item = calculate(subset, scope, [operation], registry)[0]
                        derived.append(item)
                    else:
                        item = None
                cache[ref] = item
            item = cache[ref]
            if item is None or item.get("calculationValidation", "PASS") != "PASS" or item.get("valueState") != "PRESENT":
                continue
            number = item["value"] if field == "value" else str(item["counts"][field])
            default_title = (label(item.get("objectLabel"), "所选对象") + " / " + label(" / ".join(item.get("headers", [])), "来源指标")) if "factId" in item else NAMES.get(item["operator"], "计算结果")
            if field != "value": default_title = field_names[field]
            result.append({"valueRef": opaque("value_", [scope["scopeId"], view["viewId"], ref, field]),
                           "label": view_label(view, ref, view.get("title") or default_title), "display": display(number, view.get("numberFormat")),
                           "unit": unit_for(item, view), "scopeId": scope["scopeId"], "viewId": view["viewId"]})
    return result, derived


def _coordinate(number, low, high):
    with localcontext() as ctx:
        ctx.prec = 512
        # Floats are permitted only for bounded geometric coordinates.
        return round(float(Decimal(24) + (number - low) / (high - low or Decimal(1)) * 552), 6)


def scene(rows, kind, note, steps=None):
    """A finite collection of text, line and rectangle primitives; no markup."""
    if not rows:
        return None
    numbers = [decimal_value(r["exact"]) for r in rows]
    if steps is None:
        spans = [(Decimal(0), n) for n in numbers]
    else:
        spans = steps
    low = min(Decimal(0), *(min(a, b) for a, b in spans))
    high = max(Decimal(0), *(max(a, b) for a, b in spans))
    primitives = []
    zero = _coordinate(Decimal(0), low, high)
    primitives.append({"kind": "line", "x1": zero, "x2": zero, "y1": 8, "y2": 8, "tone": "axis"})
    y = 20
    small_value_marker = False
    for row, (start, end) in zip(rows, spans):
        text = row["label"]
        # Preserve the full label as accessible text; visual lines wrap deterministically.
        lines = [text[i:i + 23] for i in range(0, len(text), 23)] or [""]
        short = lines
        for offset, part in enumerate(short):
            primitives.append({"kind": "text", "x": 24, "y": y + offset * 16, "text": part, "tone": "label"})
        unit = row["unit"]
        value_text = row["display"] + (" " + unit if unit else "")
        value_lines = [value_text[i:i + 18] for i in range(0, len(value_text), 18)]
        for offset, part in enumerate(value_lines):
            primitives.append({"kind": "text", "x": 360, "y": y + offset * 16, "text": part, "tone": "value"})
        bar_y = y + max(len(short), len(value_lines)) * 16 + 4
        a, b = _coordinate(start, low, high), _coordinate(end, low, high)
        primitives.append({"kind": "rect", "x": min(a, b), "y": bar_y, "width": abs(b - a), "height": 10,
                           "tone": "negative" if end < start else "positive", "rowKey": row["rowKey"]})
        if kind == "BAR_VALUES" and start != end and abs(b - a) < 1:
            # Mark the actual endpoint; never enlarge a tiny nonzero bar.
            primitives.append({"kind": "line", "x1": b, "x2": b, "y1": bar_y - 3, "y2": bar_y + 13,
                               "tone": "negative" if end < start else "positive"})
            small_value_marker = True
        y = bar_y + 38
        if len(primitives) > 1000 or y > 10000:
            raise AnalysisError('DIAGRAM_LIMIT', '图表文字或对象数量超过展示容量，请缩小单图范围。')
    primitives[0]["y2"] = y - 20
    marker_note = ("；" if note else "") + "极小非零值的竖线标记仅标示真实数值位置，不代表柱形长度" if small_value_marker else ""
    return {"type": kind, "width": 600, "height": y - 12, "zeroBaseline": zero,
            "description": note + marker_note, "rowKeys": [r["rowKey"] for r in rows],
            "primitives": primitives}


def project(artifact, registry: RuleRegistry, view_parameters=None):
    from .actions import projection
    published(artifact)
    if artifact.get('freeDocument'):
        params = parameters(view_parameters)
        if params != parameters():
            raise AnalysisError('DOCUMENT_SCOPE_FIXED', '自由页面范围已冻结；更改范围请创建并发布新版本。')
        base = deepcopy(artifact)
        saved = base.pop('freeDocument')
        result = project(base, registry, params)
        result['freeDocument'] = {k: saved[k] for k in ('version','title','html','contentDigest','frameKey','bindingCount')}
        if 'datasetCount' in saved: result['freeDocument']['datasetCount'] = saved['datasetCount']
        result['reportTitle'] = saved['title']
        result['originalTotal'] = len(artifact['facts'])
        result['originalObjectCount'] = len({f['objectId'] for f in artifact['facts']})
        result['dataDigest'] = digest(artifact)
        result['viewId'] = opaque('view_', [digest(artifact), RENDERER_VERSION])
        return result
    from .presentation_options import prepare_views, sections, ordered_for_view
    artifact = prepare_views(artifact)
    params = parameters(view_parameters)
    requested = {kind: {ref for view in artifact["viewPlan"] if view["type"] == kind for ref in view["valueRefs"]}
                 for kind in ("METRIC", "BAR_VALUES", "BAR_RANK", "BRIDGE", "FORMULA", "HIERARCHY", "WATERFALL", "RECONCILIATION")}
    known = {f['factId']: f for f in artifact['facts']}
    known.update({v['derivedId']: v for v in artifact['derivedValues']})
    displayed = set()
    for view in artifact['viewPlan']:
        if view['type'] == 'ORDER': continue
        for ref in view['valueRefs']:
            value = known[ref]
            displayed.update(value.get('sourceFactIds', [ref]))
    original = [f for f in ordered_facts(artifact) if f['factId'] in displayed]
    facts, members, view_columns, active_column = select_members(original, artifact, params, requested["BAR_VALUES"])
    which = params["filter"]
    restricted = which != "all" or params["topN"] is not None
    scope_id = opaque("scope_", [artifact["analysisRef"], params["filter"], params["sort"], params["topN"], [f["factId"] for f in facts]]) if restricted else artifact["scope"]["scopeId"]
    scope = {**artifact["scope"], "scopeId": scope_id, "members": members}
    scope_label = label(scope.get("label"), "发布范围")
    filter_names = {"all": "全部所选对象", "positive": "正值对象", "negative": "负值对象", "zero": "零值对象", "unavailable": "无可用数值对象"}
    scope_text = scope_label if which == "all" else f"{scope_label} · {filter_names[which]}"
    if params["topN"] is not None:
        scope_text += f" · 当前顺序前 {params['topN']} 项"
    original_objects = len({f["objectId"] for f in original})
    if restricted:
        scope_text += f"（所选 {len(members)} / 共 {original_objects} 个对象）"
    metrics, derived = _metrics(artifact, facts, scope, registry, restricted)
    rows = []
    for index, fact in enumerate(facts):
        present = fact["valueState"] == "PRESENT"
        rows.append({"rowKey": opaque("row_", [artifact["analysisRef"], fact["factId"]]),
                     "memberKey": member_key(fact),
                     "valueRef": opaque("value_", [artifact["analysisRef"], fact["factId"]]),
                     "label": label(fact.get("objectLabel"), f"对象 {index + 1}"),
                     "measure": label(" / ".join(fact.get("headers", [])), "来源指标"),
                     "exact": fact.get("value") if present else None, "display": display(fact["value"]) if present else "—",
                     "unit": label(fact.get("unit"), ""), "state": STATES.get(fact["valueState"], "不可用")})
    limitations = completeness_notes(artifact)
    if artifact["comparability"] != "CONFIRMED" and (requested['BAR_VALUES'] or requested['BAR_RANK']):
        limitations.append("原值排序不代表已确认的业务排名。")
    if artifact["receiptVerification"] != "VALID":
        limitations.append("发布时的来源证据尚未通过完整核验。")
    for value in artifact["derivedValues"]:
        if value.get("formulaTrace", {}).get("status") == "MISMATCH":
            limitations.append("表内公式复算发现差额，运算完成不代表公式核对一致；详见公式核对明细。")
        if value["calculationValidation"] != "PASS":
            limitations.extend(label(d["message"], "该计算条件不足。") for d in value.get("diagnostics", []))
    from .object_charts import object_charts
    comparisons, unavailable = object_charts(artifact, facts, rows, registry, params)
    comparison_requested = bool(requested["BAR_RANK"] or requested["BAR_VALUES"])
    bridges = []
    if not restricted:
        known = {f["factId"]: f for f in artifact["facts"]}
        known.update({v["derivedId"]: v for v in artifact["derivedValues"]})
        for value in artifact["derivedValues"]:
            if value["operator"] != "LINEAR" or value["calculationValidation"] != "PASS" or value["derivedId"] not in requested["BRIDGE"]:
                continue
            rule = registry.rules.get(value.get("ruleRef"), {})
            if not rule or len(rule.get("terms", [])) != len(value["inputRefs"]) or len(value["inputRefs"]) > CHART_ROWS:
                limitations.append("构成明细或冻结规则不足，构成图不可用；请核对原发布计算。")
                continue
            plan = next(v for v in artifact['viewPlan'] if v['type'] == 'BRIDGE' and value['derivedId'] in v['valueRefs'])
            total, bridge_rows, steps = Decimal(0), [], []
            with localcontext() as ctx:
                ctx.prec = 512
                for ref, term in zip(value["inputRefs"], rule["terms"]):
                    item = known[ref]
                    change = decimal_value(item["value"]) * decimal_value(term["coefficient"])
                    end = precise_sum([total, change])
                    bridge_rows.append({"rowKey": opaque("row_", [artifact["analysisRef"], ref]), "label": view_label(plan, ref, label(" / ".join(item.get("headers", [])), "构成项")),
                                        "exact": str(change), "display": display(str(change), plan.get("numberFormat")), "unit": unit_for(value, plan)})
                    steps.append((total, end))
                    total = end
            if total != decimal_value(value["value"]):
                raise AnalysisError("COMPOSITION_MISMATCH", "构成规则与发布结果不符，不能生成图表。")
            chart = scene(bridge_rows, 'BRIDGE', '', steps)
            chart['title'] = plan.get('title', '')
            chart['viewId'] = plan['viewId']
            chart['collapsed'] = plan.get('collapsed', False)
            bridges.append(chart)
    if not restricted:
        from .diagrams import relation_scenes
        bridges.extend(relation_scenes(artifact))
        from .comparison_scenes import comparison_scenes
        bridges.extend(comparison_scenes(artifact))
        if len(bridges) > 100: raise AnalysisError("DIAGRAM_LIMIT", "图表超过 100 幅，请缩小展示范围。")
    scenes = comparisons + bridges
    if len(scenes) > 100: raise AnalysisError("DIAGRAM_LIMIT", "图表超过 100 幅，请缩小展示范围。")
    tables = business_tables(artifact, facts, rows, published_order=params['sort'] == 'published')
    groups = sections(artifact, metrics, scenes, tables)
    placed = {i for group in groups for i in group['claimIndexes']}
    options = artifact.get('presentation', {})
    return {"schemaVersion": 1, "rendererVersion": RENDERER_VERSION, "engineVersion": artifact["engineVersion"],
            "analysisRef": artifact["analysisRef"], "dataDigest": digest(artifact), "viewId": opaque("view_", [digest(artifact), params, RENDERER_VERSION]),
            "scopeId": scope_id, "viewParameters": params, "scopeLabel": scope_text, "isFiltered": restricted,
            "technicalDetails": artifact["technicalDetails"],
            "chartsRequested": bool(comparison_requested or requested["BRIDGE"] or requested['FORMULA'] or requested['HIERARCHY'] or requested['WATERFALL'] or requested['RECONCILIATION']),
            "publishedAt": artifact.get("publishedAt"), "verificationRecordedAt": artifact.get("verificationRecordedAt"),
            "statuses": public_states(artifact), "rows": rows, "metrics": metrics,
            "tables": tables, "columns": view_columns, "activeColumnKey": active_column,
            "memberKeys": [member_key({"objectId": member}) for member in members],
            "objectCount": len(members), "originalObjectCount": original_objects,
            "viewDerivedValues": [{"valueRef": opaque("value_", [scope_id, v["derivedId"]]), "scopeId": scope_id,
                                   "operator": v["operator"], "display": display(v["value"]) if v.get("value") is not None else None,
                                   "validation": v["calculationValidation"]} for v in derived],
            "scenes": scenes, "sections": groups, "claimsCollapsed": options.get("claimsCollapsed", False),
            "summaryPosition": options.get('summaryPosition', 'end'), "summaryTitle": options.get('summaryTitle', '分析总结'),
            "reportTitle": options.get('reportTitle', ''),
            "summaryClaimIndexes": [i for i in range(len(artifact['claims'])) if i not in placed],
            "chartUnavailable": '；'.join(unavailable) or None,
            "claims": [c["rendered"] for c in artifact["claims"]],
            "claimsScopeLabel": '；'.join(x for x in ["结论适用于原发布范围" if restricted else '', artifact.get('partialReason', '')] if x), **projection(artifact),
            "sources": sorted({label(" / ".join(str(f.get(k) or "") for k in ("taskName", "formName", "period")), "来源说明未提供") for f in facts}) if artifact["technicalDetails"] else [],
            "limitations": list(dict.fromkeys(limitations)) if artifact["technicalDetails"] else [], "total": len(rows), "originalTotal": len(original)}


def page(document, offset=0, limit=PAGE_SIZE):
    if type(offset) is not int or offset < 0 or offset > document["objectCount"] or type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise AnalysisError("INVALID_PAGE", "分页参数超出允许范围。")
    members = set(document["memberKeys"][offset:offset + limit])
    result = {**document, "rows": [r for r in document["rows"] if r["memberKey"] in members],
              "memberKeys": document["memberKeys"][offset:offset + limit],
              "tables": document["tables"],
              "page": {"offset": offset, "limit": limit, "total": document["objectCount"],
                       "nextOffset": offset + limit if offset + limit < document["objectCount"] else None}}
    # Each explicit table owns its complete, bounded row set and paginates locally.
    # The raw-row API page must never truncate a different table's population.
    if len(canonical(result)) > VIEW_MAX_BYTES:
        raise AnalysisError("VIEW_TOO_LARGE", "视图超过响应上限，请缩小展示范围。")
    return deepcopy(result)
