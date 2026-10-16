"""Readonly follow-up proposals, host-owned run lifecycle and intent fences."""
from copy import deepcopy
import re

from .findings import signature, subject
from .models import AnalysisError, canonical, digest

ACTION_REF = re.compile(r"(ana_[0-9a-f]{32}:[1-9][0-9]{0,8})#(action_[0-9a-f]{24})")
RUN_STATES = {"PROPOSED", "RUNNING", "AWAITING_INPUT", "PARTIAL", "COMPLETED", "FAILED", "CANCELLED"}


def source_identity(capture):
    return tuple(capture.get(k) for k in ("resourceId", "serverName", "connectionFingerprint"))


def readonly_capability(tool, origin, sources=None):
    """Only an authorized captured source can propose follow-up; no permission override."""
    from .adapters import default_sources
    adapter = (sources or default_sources()).for_tool(tool, origin)
    basis = adapter.readonly(tool, origin) if adapter else None
    if basis is None:
        return None
    return {**origin, "toolName": tool.name, "readOnly": True, "readOnlyBasis": basis,
            "roles": adapter.roles(tool, origin),
            "permission": "CHECK_ON_EACH_CALL", "capabilityRef": "capability_" + digest([origin, basis])[:24]}


def resolve(store, owner, ref):
    match = ACTION_REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise AnalysisError("INVALID_ACTION", "请使用已发布分析中的完整行动引用。")
    artifact = store.get_analysis(owner, match[1])
    if artifact["publicationState"] != "PUBLISHED":
        raise AnalysisError("NOT_PUBLISHED", "行动必须属于已发布版本。")
    action = next((a for a in artifact["actions"] if a["actionId"] == match[2]), None)
    if not action or action.get("actionRef") != ref:
        raise AnalysisError("NOT_FOUND", "本会话没有该版本的行动。")
    return artifact, action


def followup_text(action):
    return f"请继续核对：{action['goal']}\n范围：{action['scopeLabel']}\n判断用途：{action['expectedDecision']}\n行动引用：{action['actionRef']}"


ACTION_CHECKS = ("VERIFY_VALUE", "CONFIRM_UNIT", "COMPLETE_COVERAGE", "VERIFY_COMPOSITION", "REQUEST_EVIDENCE")


def _expression_digest(fact):
    from .formulas import Expression, source_expression
    expression = source_expression(fact.get("formula"))
    if not expression:
        return None
    return digest(Expression(expression).tree)


def _formula_targets(artifact, selected_findings):
    """Freeze source-proven targets, never model-provided formula text."""
    facts = {f["factId"]: f for f in artifact["facts"]}
    values = {v["derivedId"]: v for v in artifact["derivedValues"]}
    targets, complete = {}, True
    for finding in selected_findings:
        for ref in finding["derivedRefs"]:
            value = values[ref]
            if value["operator"] != "FORMULA_CHECK": continue
            trace = value.get("formulaTrace")
            if not trace:
                complete = False
                continue
            target = facts[trace["targetFactRef"]]
            try:
                expression_digest = _expression_digest(target)
                if expression_digest is None:
                    raise AnalysisError("FORMULA_INCOMPLETE", "未取得可绑定的来源公式。")
                formulas = [{"signature": signature(target), "expressionDigest": expression_digest}]
                for fid in trace["sourceFactIds"]:
                    fact = facts[fid]
                    if fid != target["factId"]:
                        formulas.append({"signature": signature(fact), "expressionDigest": _expression_digest(fact)})
            except AnalysisError:
                complete = False
                continue
            targets[digest(signature(target))] = {"target": signature(target),
                "sourceOrigins": target["sourceOrigins"], "formulas": formulas}
    return list(targets.values()), complete


def _formula_footprint(facts, targets):
    """Permit only reachable new dependencies on the frozen target's source."""
    from .formulas import trace_formula
    by_signature = {}
    for fact in facts:
        by_signature.setdefault(digest(signature(fact)), []).append(fact)
    allowed = {}
    for target in targets:
        for frozen in target["formulas"]:
            candidates = by_signature.get(digest(frozen["signature"]), [])
            for fact in candidates:
                try:
                    unchanged = _expression_digest(fact) == frozen["expressionDigest"]
                except AnalysisError:
                    unchanged = False
                if not unchanged:
                    raise AnalysisError("ACTION_FORMULA_CHANGED", "来源公式已变化，请按新目标创建关联分析，不能完成原公式行动。")
        candidates = by_signature.get(digest(target["target"]), [])
        if not candidates: continue
        if len(candidates) != 1:
            raise AnalysisError("ACTION_FORMULA_AMBIGUOUS", "本次无法定位唯一的原公式目标。")
        root = candidates[0]
        if set(root["sourceOrigins"]) != set(target["sourceOrigins"]):
            raise AnalysisError("ACTION_SOURCE_CHANGED", "公式目标的来源已改变。")
        trace = trace_formula(facts, root["factId"])
        for ref in trace["sourceFactIds"]:
            allowed.setdefault(ref, set()).update(target["sourceOrigins"])
    return allowed


def proposals(artifact, capabilities, plan=None):
    """Validate prompt-chosen actions; never infer advice from a finding type."""
    if plan is not None and (not isinstance(plan, list) or len(plan) > 300):
        raise AnalysisError("ACTION_LIMIT", "行动计划必须是至多 300 项的列表。")
    facts = {f["factId"]: f for f in artifact["facts"]}
    findings = {f["findingId"]: f for f in artifact["findings"]}
    result = []
    source_resources = set(artifact.get("sourceResourceIds", []))
    source_origins = set(artifact["sourceOrigins"])
    matching = [c for c in capabilities if c["readOnly"] and c["resourceId"] in source_resources
                and digest(source_identity(c)) in source_origins]
    available = [c for c in matching if 'query' in c.get('roles', [])]
    verifiers = [c for c in matching if 'verify' in c.get('roles', [])]
    paired = {digest(source_identity(c)) for c in available} & {digest(source_identity(c)) for c in verifiers}
    for proposal in plan or []:
        if not isinstance(proposal, dict) or set(proposal) != {"findingRefs", "check", "goal", "requiredEvidence", "expectedDecision", "priorityBasis"}:
            raise AnalysisError("INVALID_ACTION_PLAN", "行动需要明确发现、检查能力、目标、所需资料、判断用途及顺序依据；不能指定可用性或完成状态。")
        kind, refs, required = proposal["check"], proposal["findingRefs"], proposal["requiredEvidence"]
        if not isinstance(kind, str) or kind not in ACTION_CHECKS:
            raise AnalysisError("INVALID_ACTION_PLAN", "未知的检查能力。")
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 100
                or any(not isinstance(ref, str) or ref not in findings for ref in refs) or len(set(refs)) != len(refs)):
            raise AnalysisError("INVALID_ACTION_PLAN", "行动必须绑定本次发布检查中存在且不重复的发现。")
        if any(not isinstance(proposal[k], str) or not proposal[k].strip() or len(proposal[k]) > 2000 for k in ("goal", "expectedDecision", "priorityBasis")):
            raise AnalysisError("INVALID_ACTION_PLAN", "目标、用途和顺序依据必须是有界的非空文字。")
        if not isinstance(required, list) or not 1 <= len(required) <= 20 or any(not isinstance(s, str) or not s.strip() or len(s) > 1000 for s in required):
            raise AnalysisError("INVALID_ACTION_PLAN", "所需资料必须是具体、非空且有界的文字列表。")
        fact_refs = list(dict.fromkeys(ref for fid in refs for ref in findings[fid]["factRefs"]))
        selected = [facts[ref] for ref in fact_refs]
        selected_contexts = {digest(f.get('dataContext', f.get('context'))): f.get('context', {}) for f in selected}
        action_context = next(iter(selected_contexts.values()), {}) if len(selected_contexts) == 1 else {'sourceId': None}
        required_origins = {origin for fact in selected for origin in fact["sourceOrigins"]}
        source_ready = bool(required_origins) and required_origins <= paired
        execution = [c for c in available if digest(source_identity(c)) in required_origins] if source_ready else []
        verification = [c for c in verifiers if digest(source_identity(c)) in required_origins] if source_ready else []
        formula_refs = list(dict.fromkeys(findings[fid]["ruleRef"] for fid in refs if findings[fid]["type"] == "composition"))
        formula_targets, targets_complete = _formula_targets(artifact, [findings[fid] for fid in refs]) if kind == "VERIFY_COMPOSITION" else ([], True)
        specified = kind != "REQUEST_EVIDENCE" and (kind != "VERIFY_COMPOSITION" or (targets_complete and bool(formula_refs or formula_targets)))
        runnable = specified and source_ready and len(selected_contexts) == 1 and all(f.get("bindingRef") and f.get("period") for f in selected)
        availability = "AVAILABLE" if runnable else "NEEDS_INPUT" if not specified else "UNAVAILABLE"
        labels = "、".join(subject(f) for f in selected)
        scope_label = labels if len(labels) < 1800 else artifact["scope"]["label"] + "（对象清单见来源明细）"
        aid = "action_" + digest(proposal)[:24]
        if any(a["actionId"] == aid for a in result):
            raise AnalysisError("INVALID_ACTION_PLAN", "行动计划不能重复。")
        result.append({"actionId": aid, "actionRef": artifact["analysisRef"] + "#" + aid,
            "findingRefs": list(refs), "goal": proposal["goal"], "goalType": kind,
            "scopeId": artifact["scope"]["scopeId"], "scopeLabel": scope_label,
            "objectRefs": list(dict.fromkeys(f["objectId"] for f in selected)),
            "requiredEvidence": list(required), "expectedDecision": proposal["expectedDecision"],
            "capabilityRefs": [c["capabilityRef"] for c in execution + verification] if runnable else [],
            "availability": availability,
            "availabilityReason": ("当前已发现同来源执行与核验能力；只判定所选检查是否完成，不自动证明目标文字中的其他结论。"
                if runnable else "所选检查缺少已确认的输入或来源能力；需补充所列资料。"),
            "priorityBasis": proposal["priorityBasis"],
            "policy": {"sourceId": action_context.get('sourceId'), "signatures": [signature(f) for f in selected],
                "formulaRefs": formula_refs if kind == "VERIFY_COMPOSITION" else [],
                "formulaTargets": formula_targets,
                "memberDimension": artifact["scope"].get("memberDimension"),
                "resourceIds": sorted(source_resources), "context": action_context}})
    return result


def validate_plan(store, owner, run_ref, data, captures, base_ref, verified_new=False):
    run = store.get_action_run(owner, run_ref)
    origin, action = resolve(store, owner, run["actionRef"])
    if run["status"] != "RUNNING" or base_ref != origin["analysisRef"]:
        raise AnalysisError("ACTION_NOT_RUNNING", "追查需要正在执行的行动及其原版本；目标变化时请仅用 baseAnalysisRef 新建分析。")
    if data["context"]["sourceId"] != action["policy"]["sourceId"] or set(data["scope"]["members"]) != set(action["objectRefs"]):
        raise AnalysisError("ACTION_SCOPE_CHANGED", "来源或对象集合已改变，请新建分析，不挂到原行动。")
    expected = action["policy"]["signatures"]
    try:
        formula_facts = _formula_footprint(data["facts"], action["policy"]["formulaTargets"])
    except AnalysisError as exc:
        store.update_action_run(owner, run_ref, "PARTIAL", exc.code)
        raise
    for fact in data["facts"]:
        if signature(fact) not in expected and fact["factId"] not in formula_facts:
            raise AnalysisError("ACTION_INTENT_CHANGED", "时期、指标或维度已改变，请以新计划保留 baseAnalysisRef，不完成旧行动。")
    fresh = {r for r in captures if r in run["newCaptureRefs"]}
    selected_refs = {r for f in data["facts"] for r in f.get("captureRefs", [])}
    if not fresh or not selected_refs or not selected_refs <= fresh:
        raise AnalysisError("ACTION_NEW_EVIDENCE_REQUIRED", "追查结果的每项事实均须来自本轮宿主登记的新捕获；旧快照仅用作比较依据。")
    original_sources = {}
    for fact in origin["facts"]:
        if signature(fact) in expected:
            original_sources.setdefault(digest(signature(fact)), set()).update(
                source_identity(store.get_capture(owner, ref)) for ref in fact.get("captureRefs", []))
    for fact in data["facts"]:
        for ref in fact.get("captureRefs", []):
            actual_source = source_identity(captures[ref])
            originals = original_sources.get(digest(signature(fact)))
            source_matches = actual_source in originals if originals else digest(actual_source) in formula_facts.get(fact["factId"], set())
            if (captures[ref].get("resourceId") not in action["policy"]["resourceIds"]
                    or not source_matches):
                raise AnalysisError("ACTION_SOURCE_CHANGED", "追查事实的来源资源或连接与原行动不一致。")
    # Repeated request/receipt with no new source observation cannot finish a run.
    old = {(digest(signature(f)), f.get("contentDigest"), digest(f.get("context")), f.get("value"), f.get("unit")) for f in origin["facts"]}
    current = {(digest(signature(f)), f.get("contentDigest"), digest(f.get("context")), f.get("value"), f.get("unit")) for f in data["facts"]}
    if current <= old and not verified_new:
        store.update_action_run(owner, run_ref, "PARTIAL", "NO_NEW_EVIDENCE")
        raise AnalysisError("NO_NEW_EVIDENCE", "本次未取得新观察或新核验证据，已停止重复追查。")
    return run


def outcome(artifact, action, origin):
    if action["goalType"] not in ACTION_CHECKS:
        raise AnalysisError("INVALID_ACTION_PLAN", "不支持该行动类型。")
    if action["goalType"] == "REQUEST_EVIDENCE":
        return "PARTIAL", "ADDITIONAL_EVIDENCE_REQUIRED"
    if not {digest(s) for s in action["policy"]["signatures"]} <= {digest(signature(f)) for f in artifact["facts"]}:
        return "PARTIAL", "REQUIRED_METRICS_MISSING"
    if artifact["receiptVerification"] != "VALID" or not artifact["scope"].get("selectedDataComplete") or any(f["valueState"] != "PRESENT" for f in artifact["facts"]):
        return "PARTIAL", "RESULT_HAS_GAPS"
    kind = action["goalType"]
    if kind == "CONFIRM_UNIT" and any(not f.get("unit") or not f.get("scale") for f in artifact["facts"]):
        return "PARTIAL", "UNIT_STILL_MISSING"
    if kind == "COMPLETE_COVERAGE" and artifact["coverage"] != "COMPLETE":
        return "PARTIAL", "COVERAGE_STILL_INCOMPLETE"
    if kind == "VERIFY_COMPOSITION":
        expected = set(action["policy"]["formulaRefs"])
        targets = action["policy"]["formulaTargets"]
        if expected and artifact["ruleVersion"] != origin["ruleVersion"]:
            return "PARTIAL", "FORMULA_RULE_CHANGED"
        actual = {v.get("ruleRef") for v in artifact["derivedValues"] if v["operator"] == "LINEAR" and v["calculationValidation"] == "PASS"}
        if not (expected or targets) or not expected <= actual:
            return "PARTIAL", "FORMULA_EVIDENCE_REQUIRED"
        try:
            _formula_footprint(artifact["facts"], targets)
        except AnalysisError as exc:
            return "PARTIAL", exc.code
        facts = {f["factId"]: f for f in artifact["facts"]}
        mismatch = False
        for target in targets:
            checks = [v for v in artifact["derivedValues"] if v["operator"] == "FORMULA_CHECK"
                      and v.get("formulaTrace") and signature(facts[v["formulaTrace"]["targetFactRef"]]) == target["target"]]
            if not checks or any(v["calculationValidation"] != "PASS" or v["formulaTrace"]["status"] not in ("MATCH", "MISMATCH") for v in checks):
                return "PARTIAL", "FORMULA_EVIDENCE_REQUIRED"
            mismatch = mismatch or any(v["formulaTrace"]["status"] == "MISMATCH" for v in checks)
        if mismatch:
            return "COMPLETED", "FORMULA_CHECKED_WITH_DIFFERENCE"
        if targets:
            return "COMPLETED", "FORMULA_CHECKED_MATCH"
    return "COMPLETED", "VERIFIED_RESULT_PUBLISHED"


class ActionRuntime:
    def __init__(self, store, owner, capability_provider=None):
        self.store, self.owner = store, owner
        self.capability_provider = capability_provider
        self.user_text = ""
        self.turn_ref = None
        self.active_ref = None

    async def capabilities(self):
        return await self.capability_provider() if self.capability_provider else []

    async def follow_up(self, action_ref, key):
        artifact, action = resolve(self.store, self.owner, action_ref)
        if not self.turn_ref or self.user_text.strip() != followup_text(action):
            raise AnalysisError("ACTION_INTENT_CHANGED", f"本轮并非原行动的完整追问或用户已编辑目标。按本轮意图重新计划，以 baseAnalysisRef={artifact['analysisRef']} 保留关联；不要把新目标挂到旧行动。")
        capabilities = await self.capabilities()
        allowed = [c for c in capabilities if c["capabilityRef"] in action["capabilityRefs"]]
        runnable = action["availability"] == "AVAILABLE" and bool(allowed) and {c["capabilityRef"] for c in allowed} == set(action["capabilityRefs"])
        if self.active_ref and self.store.get_action_run(self.owner, self.active_ref)["actionRef"] != action_ref:
            # Check an existing request first to retain same-key conflict semantics.
            identity = digest({"actionRef": action["actionRef"], "goal": action["goal"], "policy": action["policy"]})
            self.store.find_request(self.owner, "followup", key, identity)
            raise AnalysisError("ACTION_IN_PROGRESS", "当前回合已有其他追查在执行。")
        run = self.store.start_action_run(self.owner, action, key, self.turn_ref, runnable)
        if run["status"] == "RUNNING":
            if self.active_ref and self.active_ref != run["actionRunId"]:
                raise AnalysisError("ACTION_IN_PROGRESS", "当前回合已有追查在执行。")
            self.active_ref = run["actionRunId"]
        selected = {ref for f in artifact["findings"] if f["findingId"] in action["findingRefs"] for ref in f["factRefs"]}
        return {"actionRun": run, "originAnalysisRef": artifact["analysisRef"], "goal": action["goal"],
            "scope": action["policy"], "requiredEvidence": action["requiredEvidence"],
            "facts": [{k: f.get(k) for k in ("factId", "objectId", "objectLabel", "period", "measureCode", "cellKey", "cellAddress", "formula", "value", "unit", "dimensions", "bindingRef")} for f in artifact["facts"] if f["factId"] in selected],
            "capabilities": allowed if runnable else [], "historyIntegrity": "VERIFIED_REGISTERED_CONTENT",
            "sourceRequery": "NOT_CHECKED", "next": "仅 RUNNING 时继续本行动。使用既有只读工具取得并核验本轮新证据，再 Build(actionRunRef, baseAnalysisRef)。表内公式可补齐同一目标可达的依赖；公式变化时新建关联分析，不混用新旧快照。其他状态先读取诊断。核对结束与公式一致分别判断。"}

    def capture(self, ref, state):
        if self.active_ref:
            self.store.record_action_capture(self.owner, self.active_ref, ref, state)

    def finish(self, status="PARTIAL", code="TURN_ENDED_WITHOUT_RESULT"):
        if self.active_ref:
            self.store.update_action_run(self.owner, self.active_ref, status, code, only_running=True)
            self.active_ref = None


def projection(artifact):
    # Internal policy and opaque source identifiers stay in the private artifact.
    findings = [{k: deepcopy(f[k]) for k in ("findingId", "statement", "support", "type")} | {
        "basis": f["rule"]["source"], "ruleVersion": f["rule"]["version"], "applicability": f["rule"]["applicability"],
        "sourceType": f["rule"]["sourceType"], "explanations": [{k: deepcopy(e[k]) for k in ("kind", "statement", "support", "missingEvidence")} for e in f["explanations"]],
        "previousAnalysisRef": f["previousFindingRef"].split("#")[0] if f.get("previousFindingRef") else None} for f in artifact["findings"]]
    actions = [{k: deepcopy(a[k]) for k in ("actionRef", "findingRefs", "goal", "scopeLabel", "requiredEvidence", "expectedDecision", "availability", "availabilityReason", "priorityBasis")} for a in artifact["actions"]]
    def oversized_text(value):
        if isinstance(value, str):
            return len(value.encode("utf-16-le")) > 32000  # Same bound as JS String.length.
        if isinstance(value, dict):
            return any(oversized_text(v) for v in value.values())
        return isinstance(value, list) and any(oversized_text(v) for v in value)
    if oversized_text([findings, actions]) or len(canonical([findings, actions])) > 700000:
        raise AnalysisError("VIEW_TOO_LARGE", "发现与建议超过卡片容量，请缩小分析范围。")
    return {"findings": findings, "actions": actions, "findingsScopeLabel": "原发布范围的发现与建议"}
