"""Explicitly selected checks; scenario and next-step choices belong to prompts."""
from copy import deepcopy
import operator

from .engine import _field
from .models import AnalysisError, decimal_value, digest

FINDING_VERSION = "1.1.0"
CHECKS = ("SIGN", "UNIT_GAP", "MISSING_VALUE", "COVERAGE_GAP", "CALCULATION", "THRESHOLD", "RECHECK")
COMPARISONS = {"lt": operator.lt, "eq": operator.eq, "gt": operator.gt}
BUILTINS = {
    "sign": ("明确选择的数值符号检查", "仅检查被选择的正值、负值或零值条件"),
    "unit": ("来源未提供单位或缩放", "只描述计量缺口"),
    "coverage": ("所选范围或数值尚不完整", "不把部分结果视为全集"),
    "composition": ("已登记 LINEAR 公式及完整输入", "只说明算术构成，不证明业务原因"),
    "comparison": ("同口径 DIFFERENCE 的两个实际输入", "只说明显式对照差额，不推断趋势"),
    "formula_gap": ("所选计算的必需项或口径检查受阻", "只说明该计算的缺口"),
    "calculation": ("明确选择的计算结果", "只支持该计算关系"),
}


def rule_record(kind):
    description, applicability = BUILTINS[kind]
    return {"ruleRef": "observation:" + kind, "version": FINDING_VERSION,
            "source": "AnalysisArtifact v1 / findings", "sourceType": "observation",
            "applicability": applicability, "description": description}


def metric(fact):
    return " / ".join(fact.get("headers", [])) or fact.get("measureCode") or "所选指标"


def subject(fact):
    label = fact.get('objectLabel') or fact['objectId']
    if any(marker in label.lower() for marker in ('http://', 'https://', 'urn:', 'iri:')):
        label = '所选对象'
    return f"{label}（{fact.get('period') or '来源时期未明'}，{metric(fact)}）"


def signature(fact):
    return {k: fact.get(k) for k in ("objectId", "bindingRef", "cellKey", "measureCode", "period", "dimensions")}


def validate_threshold(rule):
    if (not isinstance(rule, dict) or any(not isinstance(rule.get(k), str) or not rule[k].strip() for k in ("source", "version"))
            or not isinstance(rule.get("appliesTo"), dict) or not rule["appliesTo"]):
        raise AnalysisError("INVALID_FINDING_RULE", "阈值规则需要来源、版本和明确适用条件。")
    if rule.get("sourceType") not in ("user_assumption", "policy") or rule.get("operator") not in ("lt", "gt"):
        raise AnalysisError("INVALID_FINDING_RULE", "阈值来源必须区分用户假设与已登记制度，比较仅支持 lt/gt。")
    decimal_value(rule.get("threshold"))
    if not isinstance(rule.get("unit"), str) or not rule["unit"].strip() or rule.get("scale") != "1":
        raise AnalysisError("INVALID_FINDING_RULE", "阈值需要明确原单位及缩放；首版不作隐式换算。")


def generate(artifact, registry, thresholds=None, configured=None, hypotheses=None, base=None, plan=None):
    """An omitted plan generates no implicit checks or business advice."""
    facts = {f["factId"]: f for f in artifact["facts"]}
    derived = {v["derivedId"]: v for v in artifact["derivedValues"]}
    checks = {kind: [] for kind in CHECKS}
    if plan is not None and (not isinstance(plan, list) or len(plan) > 100):
        raise AnalysisError("INVALID_FINDING_PLAN", "检查计划必须是至多 100 项的列表。")
    for check in plan or []:
        if not isinstance(check, dict) or not isinstance(check.get("kind"), str) or check["kind"] not in checks:
            raise AnalysisError("INVALID_FINDING_PLAN", "检查类型未提供或不受支持。")
        kind = check["kind"]
        fields = {"SIGN": {"factRefs", "sign"}, "UNIT_GAP": {"factRefs"}, "MISSING_VALUE": {"factRefs"},
                  "COVERAGE_GAP": set(), "CALCULATION": {"derivedRefs"}, "THRESHOLD": {"ruleRef"},
                  "RECHECK": {"previousFindingRef", "factRefs"}}[kind]
        if set(check) != fields | {"kind"}:
            raise AnalysisError("INVALID_FINDING_PLAN", "检查参数不符合能力契约，不能提交替代数值或支持状态。")
        for field, known in (("factRefs", facts), ("derivedRefs", derived)):
            if field in check:
                refs = check[field]
                if (not isinstance(refs, list) or not refs or len(refs) > 1000
                        or any(not isinstance(ref, str) or ref not in known for ref in refs) or len(set(refs)) != len(refs)):
                    raise AnalysisError("INVALID_FINDING_PLAN", "检查需要实际存在、非空且不重复的值引用。")
        if kind == "SIGN" and check["sign"] not in ("positive", "negative", "zero"):
            raise AnalysisError("INVALID_FINDING_PLAN", "符号检查只接受 positive、negative、zero。")
        if any(not isinstance(check[field], str) or not check[field].strip() for field in ("ruleRef", "previousFindingRef") if field in check):
            raise AnalysisError("INVALID_FINDING_PLAN", "规则与历史发现引用必须是非空文字。")
        checks[kind].append(check)
    def is_selected(kind, ref):
        return any(ref in check["factRefs"] for check in checks[kind])
    result = []
    def add(kind, selected, statement, *, values=(), gaps=(), support=None, rule=None, previous=None, predicate=None):
        if not selected:
            return
        if len(result) >= 300:
            raise AnalysisError("FINDING_LIMIT", "重要发现超过 300 项，请明确子范围后分别发布。")
        record = deepcopy(rule or rule_record(kind))
        fid = "finding_" + digest([kind, [f["factId"] for f in selected], list(values), record, previous, predicate])[:24]
        if any(f["findingId"] == fid for f in result):
            return
        verified = artifact["receiptVerification"] == "VALID"
        state = support or ("SUPPORTED" if verified else "NEEDS_EVIDENCE")
        missing = list(gaps)  # Shared source verification is displayed once at analysis level.
        explanation_kind = "arithmetic_composition" if kind == "composition" else "observation"
        result.append({"findingId": fid, "analysisRef": artifact["analysisRef"], "scopeId": artifact["scope"]["scopeId"],
                       "type": kind, "objectRefs": list(dict.fromkeys(f["objectId"] for f in selected)),
                       "metricRefs": list(dict.fromkeys(f.get("measureCode") or f.get("cellKey") or "unlocated" for f in selected)),
                       "ruleRef": record["ruleRef"], "rule": record, "factRefs": [f["factId"] for f in selected],
                       "derivedRefs": list(values), "statement": statement, "support": state,
                       "previousFindingRef": previous, "predicate": deepcopy(predicate),
                       "explanations": [{"explanationId": "explanation_" + fid.removeprefix("finding_"), "findingId": fid,
                           "kind": explanation_kind, "inputRefs": [f["factId"] for f in selected] + list(values),
                           "formulaRef": derived[values[0]].get("ruleRef") if kind == "composition" else None,
                           "residualRef": None, "missingEvidence": missing, "statement": statement, "support": state}]})

    for check in checks["SIGN"]:
        op, name = {"positive": ("gt", "正值"), "negative": ("lt", "负值"), "zero": ("eq", "零值")}[check["sign"]]
        for ref in check["factRefs"]:
            fact = facts[ref]
            if fact["valueState"] == "PRESENT" and COMPARISONS[op](decimal_value(fact["value"]), decimal_value("0")):
                precision_label = "来源显示值" if fact.get("precision") == "display_only" else "来源原值"
                unit_suffix = " " + fact["unit"] if fact.get("unit") else ""
                add("sign", [fact], f"{subject(fact)}的{precision_label}为 {fact['value']}{unit_suffix}，是{name}。该检查仅说明数值符号。",
                    predicate={"operator": op, "threshold": "0"})
    for fact in facts.values():
        if is_selected("UNIT_GAP", fact["factId"]) and (not fact.get("unit") or not fact.get("scale")):
            add("unit", [fact], f"{subject(fact)}的单位或缩放未获来源确认，保留原值，不进行换算。", gaps=["该单元格的单位与缩放元数据"])
        if is_selected("MISSING_VALUE", fact["factId"]) and fact["valueState"] != "PRESENT":
            add("coverage", [fact], f"{subject(fact)}当前没有可用于计算的数值，不能按零处理。", gaps=["同一对象、时期和指标的有效来源值"])
    if checks["COVERAGE_GAP"] and artifact["coverage"] != "COMPLETE":
        add("coverage", list(facts.values()), "所选对象或来源覆盖尚未证明完整；本次结果不能代表全部对象。", gaps=["正式成员关系、缺失对象及未读完分支的完整来源"])
    for value in derived.values():
        if not any(value["derivedId"] in check["derivedRefs"] for check in checks["CALCULATION"]):
            continue
        selected = [facts[r] for r in value.get("sourceFactIds", []) if r in facts]
        if value["operator"] == "FORMULA_CHECK":
            from .validation import bound_value
            trace = value.get("formulaTrace", {})
            if value["calculationValidation"] == "PASS":
                add("calculation", selected, bound_value(value), values=[value["derivedId"]])
            else:
                gaps = [d["message"] for node in trace.get("nodes", []) for d in node["diagnostics"]]
                gaps += ["必要依赖 " + d["address"] + " 尚未解析" for d in trace.get("pendingDependencies", [])]
                add("formula_gap", selected, "表内公式仍有未完成的依赖核对。", values=[value["derivedId"]], gaps=list(dict.fromkeys(gaps)) or ["可适用的来源公式"], support="NEEDS_EVIDENCE")
        elif value["operator"] == "LINEAR":
            if value["calculationValidation"] == "PASS":
                formula = registry.resolve(value["ruleRef"], "LINEAR", selected)
                add("composition", selected, f"已登记公式的计算结果为 {value['value']} {value.get('unit') or ''}；该检查仅支持所选计算关系。",
                    values=[value["derivedId"]], rule={"ruleRef": value["ruleRef"], "source": formula["source"], "version": formula["version"],
                    "sourceType": "formula", "applicability": "已核对适用条件、必需项、单位、时期与公式顺序", "appliesTo": formula.get("appliesTo", {})})
            else:
                add("formula_gap", selected, "所选计算的输入或条件未满足。", values=[value["derivedId"]],
                    gaps=[d["message"] for d in value["diagnostics"]], support="NEEDS_EVIDENCE")
        elif value["operator"] == "DIFFERENCE" and value["calculationValidation"] == "PASS":
            add("comparison", selected, f"所选两个同口径输入的差额为 {value['value']} {value.get('unit') or ''}，不据此推断趋势或原因。", values=[value["derivedId"]])
        elif value["calculationValidation"] == "PASS":
            message = f"所选 {value['operator']} 计算已通过条件校验"
            if value.get("value") is not None:
                message += f"，结果为 {value['value']} {value.get('unit') or ''}"
            add("calculation", selected, message + "。", values=[value["derivedId"]])
        else:
            add("formula_gap", selected, "所选计算的输入或条件未满足。", values=[value["derivedId"]],
                gaps=[d["message"] for d in value["diagnostics"]], support="NEEDS_EVIDENCE")

    for rule in (configured or {}).values():
        validate_threshold(rule)
    rules = {}
    for check in checks["THRESHOLD"]:
        if not isinstance(check["ruleRef"], str) or check["ruleRef"] not in (configured or {}):
            raise AnalysisError("INVALID_FINDING_RULE", "检查只能引用已登记阈值规则。")
        rules[check["ruleRef"]] = deepcopy(configured[check["ruleRef"]])
    if thresholds is not None and (not isinstance(thresholds, list) or len(thresholds) > 20):
        raise AnalysisError("FINDING_LIMIT", "单次用户阈值最多 20 项。")
    for index, proposed in enumerate(thresholds or []):
        if not isinstance(proposed, dict) or set(proposed) != {"factRefs", "threshold", "operator", "unit", "source"} or not proposed["source"]:
            raise AnalysisError("INVALID_FINDING_RULE", "用户阈值须提供实际值引用、原单位及假设来源说明。")
        refs = proposed["factRefs"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in facts for ref in refs) or len(set(refs)) != len(refs):
            raise AnalysisError("UNKNOWN_VALUE", "用户阈值引用不存在的事实。")
        if any(facts[ref].get("unit") != proposed["unit"] or not facts[ref].get("scale") for ref in refs):
            raise AnalysisError("UNIT_UNCONFIRMED", "用户阈值的单位与来源未能对齐，不能按假设替来源补单位。")
        rules["assumption:" + str(index)] = {**proposed, "version": digest(proposed), "sourceType": "user_assumption", "scale": "1", "appliesTo": {"factRefs": refs}}
    for key, rule in rules.items():
        validate_threshold(rule)
        for fact in facts.values():
            matches = all(fact["factId"] in expected if field == "factRefs" else _field(fact, field) == expected for field, expected in rule["appliesTo"].items())
            if not matches or fact["valueState"] != "PRESENT" or fact.get("unit") != rule["unit"] or fact.get("scale") != rule["scale"]:
                continue
            if fact.get("precision") == "display_only":
                raise AnalysisError("PRECISION_UNPROVEN", "阈值比较不能使用只有显示精度的数值；请取得原始精确值或去掉该阈值。")
            actual, threshold = decimal_value(fact["value"]), decimal_value(rule["threshold"])
            hit = actual < threshold if rule["operator"] == "lt" else actual > threshold
            if hit:
                basis = "用户假设（非企业制度）" if rule["sourceType"] == "user_assumption" else "已登记制度阈值"
                add("threshold", [fact], f"{subject(fact)}原值 {fact['value']} {rule['unit']} {'低于' if rule['operator'] == 'lt' else '高于'} {basis} {rule['threshold']} {rule['unit']}。",
                    rule={**rule, "ruleRef": key, "applicability": "指标、单位、缩放与登记条件一致"},
                    predicate={"operator": rule["operator"], "threshold": rule["threshold"]})

    same_source = (base and artifact["context"].get("sourceId") == base["context"].get("sourceId")
                   and bool(artifact.get("sourceResourceIds"))
                   and set(artifact["sourceResourceIds"]) == set(base["sourceResourceIds"])
                   and bool(artifact.get("sourceOrigins"))
                   and set(artifact["sourceOrigins"]) == set(base["sourceOrigins"]))
    if same_source and base.get("publicationState") == "PUBLISHED":
        old_facts = {f["factId"]: f for f in base["facts"]}
        old_findings = {base["analysisRef"] + "#" + f["findingId"]: f for f in base["findings"]}
        for check in checks["RECHECK"]:
            old = old_findings.get(check["previousFindingRef"])
            if not old or len(old["factRefs"]) != 1 or len(check["factRefs"]) != 1:
                raise AnalysisError("INVALID_FINDING_PLAN", "条件复核需要原发布版本的一个发现和当前一个事实。")
            predicate = old.get("predicate")
            if not predicate or predicate.get("operator") not in COMPARISONS:
                raise AnalysisError("INVALID_FINDING_PLAN", "该发现没有可复核的数值条件。")
            previous = old_facts[old["factRefs"][0]]
            current = [facts[ref] for ref in check["factRefs"] if signature(facts[ref]) == signature(previous) and facts[ref]["valueState"] == "PRESENT"
                       and facts[ref].get("precision") != "display_only" and previous.get("precision") != "display_only"
                       and bool(facts[ref].get("sourceOrigins")) and set(facts[ref]["sourceOrigins"]) == set(previous["sourceOrigins"])
                       and facts[ref].get('context', {}).get('sourceId') == previous.get('context', {}).get('sourceId')
                       and all(facts[ref].get(k) == previous.get(k) for k in ("unit", "scale"))]
            if current and artifact["receiptVerification"] == "VALID":
                holds = COMPARISONS[predicate["operator"]](decimal_value(current[0]["value"]), decimal_value(predicate["threshold"]))
                add(old["type"], current, f"{subject(current[0])}本次来源原值为 {current[0]['value']}；{'原条件仍成立' if holds else '新证据不再支持原条件'}，原版本保持不变。",
                    support="SUPPORTED" if holds else "CONTRADICTED", previous=check["previousFindingRef"], rule=old["rule"], predicate=predicate)
    elif checks["RECHECK"] and (not base or base.get("publicationState") != "PUBLISHED"):
        raise AnalysisError("INVALID_FINDING_PLAN", "条件复核需要已发布的关联版本。")
    known = {f["findingId"]: f for f in result}
    if hypotheses is not None and (not isinstance(hypotheses, list) or len(hypotheses) > 30):
        raise AnalysisError("FINDING_LIMIT", "原因假设最多 30 项。")
    for hypothesis in hypotheses or []:
        if not isinstance(hypothesis, dict) or set(hypothesis) != {"findingId", "statement", "missingEvidence"} or not isinstance(hypothesis.get("findingId"), str) or hypothesis["findingId"] not in known:
            raise AnalysisError("INVALID_EXPLANATION", "原因假设需要绑定已登记发现，不接收已支持或已完成标记。")
        text, missing = hypothesis["statement"], hypothesis["missingEvidence"]
        if not isinstance(text, str) or not text.strip() or not 1 <= len(text) <= 2000 or not isinstance(missing, list) or not missing or len(missing) > 20 or any(not isinstance(s, str) or not s.strip() or not 1 <= len(s) <= 1000 for s in missing):
            raise AnalysisError("INVALID_EXPLANATION", "原因假设需要具体、非空的待补证据。")
        finding = known[hypothesis["findingId"]]
        finding["explanations"].append({"explanationId": "hypothesis_" + digest(hypothesis)[:24], "findingId": finding["findingId"],
            "kind": "business_cause_hypothesis", "inputRefs": finding["factRefs"], "formulaRef": None, "residualRef": None,
            "statement": text, "missingEvidence": missing, "support": "NEEDS_EVIDENCE"})
    if len(result) > 300:
        raise AnalysisError("FINDING_LIMIT", "重要发现超过 300 项，请明确分析子范围后分别发布，不能静默遗漏。")
    return result
