"""Application operations shared by the three host tools."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from time import perf_counter

from .adapters import default_sources
from .engine import RuleRegistry, calculate, _comparable
from .models import AnalysisError, ENGINE_VERSION, SCHEMA_VERSION, Owner, bounded_page, digest, json_values, now
from .store import AnalysisStore
from .validation import public_states, summary, validate_claims, validate_views
from . import findings
from .actions import ACTION_CHECKS, ActionRuntime, proposals, validate_plan, source_identity
from .python_libraries import capability as python_capability


def verification(facts, captures, verifier_refs=None, sources=None):
    from .adapters import default_sources
    return (sources or default_sources()).verify(facts, captures, verifier_refs)


def compact_derived(values, limit=100, analysis_ref=None):
    result = []
    payload_budget = 12000
    for value in values:
        compact = dict(value)
        if value.get('outputKind') in ('TABLE', 'CHART_DATA', 'DIAGNOSTIC', 'FILE'):
            # 小产物直接返回，避免刚算完又逐项 Inspect；大产物仍保持可恢复的完整引用。
            from .query_results import encode
            payload = {field: value[field] for field in ('rows', 'data', 'text') if field in value}
            size = len(encode(payload))
            complete = size <= min(6000, payload_budget)
            if complete:
                payload_budget -= size
            else:
                for field in payload:
                    compact.pop(field, None)
            compact['payloadComplete'] = complete
            if analysis_ref:
                compact['nextRead'] = {'section': 'products', 'analysisRef': analysis_ref,
                                       'derivedId': value['derivedId']}
            elif value.get('pythonRunRef'):
                compact['nextRead'] = {'section': 'python', 'pythonRunRef': value['pythonRunRef']}
        if analysis_ref and value.get('operator') == 'FORMULA_CHECK' and len(value.get('inputRefs', [])) == 1:
            compact['nextRead'] = {'section': 'formulas', 'analysisRef': analysis_ref, 'factRef': value['inputRefs'][0]}
        for key in ("inputRefs", "sourceFactIds", "orderedFactIds", "unavailableFactIds"):
            if key in compact and len(compact[key]) > limit:
                compact[key + "Total"] = len(compact[key])
                compact[key] = compact[key][:limit]
                compact["hasMore"] = True
        if "formulaTrace" in compact:
            trace = dict(compact["formulaTrace"])
            for key in ("nodes", "pendingDependencies", "sourceFactIds", "differenceOriginRefs", "externalBoundaries"):
                trace[key + "Total"] = len(trace[key])
                trace[key] = trace[key][:min(limit, 10)]
            all_nodes = value['formulaTrace']['nodes']
            # 先展示目标和真正影响判断的差额/缺数，不按遍历顺序重复回传整条链。
            priority = all_nodes[:1] + [node for node in all_nodes[1:] if
                        node.get('localCheck', {}).get('status') == 'MISMATCH' or node.get('valueState') != 'PRESENT']
            ordered = {node['factRef']: node for node in priority + all_nodes}
            trace["nodes"] = [{k: node.get(k) for k in ("factRef", "headers", "address", "expression", "status", "observed", "recalculated", "difference", "localCheck", "propagatedDifference", "differenceOriginRefs")}
                              for node in list(ordered.values())[:min(limit, 4)]]
            for node in trace['nodes']:
                node['differenceOriginRefsTotal'] = len(node['differenceOriginRefs'])
                node['differenceOriginRefs'] = node['differenceOriginRefs'][:10]
            trace["detailsSection"] = "formulas"
            if trace.get('recovery'):
                recovery = dict(trace['recovery'])
                recovery['blockers'] = recovery.get('blockers', [])[:3]
                recovery['pendingDependencies'] = recovery.get('pendingDependencies', [])[:3]
                trace['recovery'] = recovery
            compact["formulaTrace"] = trace
            # 完整诊断仍在冻结产物中；返回值避免再复制一次相同公式追溯。
            compact['diagnostics'] = [{key: item[key] for key in ('code', 'message') if key in item}
                                      for item in compact.get('diagnostics', [])]
        result.append(compact)
    return result


def bind_source_origins(facts, captures):
    for fact in facts:
        fact["sourceOrigins"] = sorted({digest(source_identity(captures[ref])) for ref in fact.get("captureRefs", [])})
    return sorted({origin for fact in facts for origin in fact["sourceOrigins"]})


class AnalysisService:
    def __init__(self, store: AnalysisStore, owner: Owner, authorize, rules: RuleRegistry | None = None, skill_versions: dict | None = None, delivery=None, actions_enabled=False, finding_rules=None, render_checker=None, sources=None):
        from .render_client import RenderChecker
        self.render_checker = render_checker or RenderChecker.from_environment()
        self.store, self.owner, self.authorize = store, owner, authorize
        self.rules = rules or RuleRegistry()
        self.skill_versions = skill_versions or {}
        self.delivery = delivery
        self.actions_enabled = actions_enabled
        self.sources = sources or default_sources()
        self.finding_rules = finding_rules or {}
        for rule in self.finding_rules.values():
            findings.validate_threshold(rule)
        self.actions = ActionRuntime(store, owner)
        self.turn = None
        self.web_ui_url = None
        self.preview_reviewer = None
        self.verification_runner = None
        from .budget_preparation import BudgetPreparation
        self.budget_preparation = BudgetPreparation(self)
        self._evidence_cache = {}

    async def task(self, **kwargs):
        await self.authorize()
        if self.turn is None:
            raise AnalysisError('NO_ACTIVE_USER_TURN', '当前没有可登记的真实用户轮。')
        subjects = kwargs.pop('formulaSubjects', None)
        if subjects is not None and (kwargs.get('action', 'declare') != 'declare' or kwargs.get('requiredEvidence') != 'FORMULAS'
                                     or kwargs.get('mode') not in ('ANALYSIS', 'REPORT')):
            raise AnalysisError('INVALID_FORMULA_SUBJECTS', '按主体准备表展开适用于声明 FORMULAS 的数据分析。')
        turn = self.turn
        result = turn.task(**kwargs)
        if subjects is not None:
            from .budget_preparation import formula_subjects
            targets = formula_subjects(turn, subjects)
            previous = turn.state.get('budgetFormulaPreparation', [])
            if previous and {item['targetKey'] for item in previous} != {item['key'] for item in targets}:
                raise AnalysisError('PREPARATION_SCOPE_CONFLICT', '本轮已登记的预期主体与此次声明不一致，请沿用原目标。')
            records = await self.budget_preparation.ensure(turn, targets, 'PRIMARY')
            turn.state['budgetFormulaTargets'] = targets
            turn.state['budgetFormulaPreparation'] = records
            result['preparedFormulaInputs'] = {
                'populationCaptureRef': targets[0]['source'], 'expected': len(targets),
                'targets': [{'subject': item['subject'], **({'captureRef': item['selection']['captureRef']} if 'selection' in item else {'error': item['error']})}
                            for item in records],
                'next': ('构建时选择本题的总体数据或已准备目标回执，宿主自动附上各对象的公式材料；额外统计放在 operations。'
                         if all('selection' in item for item in records) else
                         '已登记各预期主体及缺口。仅补失败目标；提交后续实际回执构建时，宿主会重新核对并更新准备状态。')}
        return result

    async def python(self, **kwargs):
        from .python_analysis import analyze
        return await analyze(self, **kwargs)

    async def read_query_result(self, captureRef=None, pointer='', offset=0, limit=20, search=None, fields=None,
                                requests=None, allPages=False):
        from .query_results import read_receipt
        await self.authorize()
        if requests is not None:
            if captureRef is not None or pointer or offset or limit != 20 or search is not None or fields is not None:
                raise AnalysisError('INVALID_READ_BATCH', 'requests 与顶层单次读取条件不能混用。')
            from .batch_reads import read_batch
            receipts = {}
            async def read_page(**request):
                reference = request.get('captureRef')
                if reference not in receipts:
                    capture = self.store.get_capture(self.owner, reference)
                    receipts[reference] = self.sources.for_capture(capture).raw_receipt(capture['response'])
                return read_receipt(receipts[reference], reference,
                                    **{key: value for key, value in request.items() if key != 'captureRef'})
            return await read_batch(requests, allPages, read_page,
                                    allowed_fields={'captureRef', 'pointer', 'offset', 'limit', 'search', 'fields'})
        if allPages:
            raise AnalysisError('INVALID_READ_BATCH', 'allPages 仅用于 requests 批量读取。')
        capture = self.store.get_capture(self.owner, captureRef)
        return read_receipt(self.sources.for_capture(capture).raw_receipt(capture['response']), captureRef, pointer, offset, limit, search, fields)

    async def build(self, captureRefs=None, selections=None, scope=None, operations=None, requestKey=None,
                    baseAnalysisRef=None, actionRunRef=None, workPlan=None):
        started = perf_counter()
        await self.authorize()
        # 首建可由已明确的回执和范围推导选择器；三个字段均省略专用于续建。
        supplied_inputs = [value is not None for value in (captureRefs, selections, scope)]
        incremental = not any(supplied_inputs)
        if (incremental and not baseAnalysisRef) or (not incremental and (captureRefs is None or scope is None)):
            raise AnalysisError('INVALID_BUILD_INPUT', '首次构建提供 captureRefs 和 scope。需选用子集或多种用途时传 selections；续建提供 baseAnalysisRef。')
        if not isinstance(operations, list) or not isinstance(requestKey, str) or not requestKey:
            raise AnalysisError('INVALID_BUILD_INPUT', 'operations 必须为数组，requestKey 必须为非空字符串。')
        preparation = None
        if not incremental:
            if not captureRefs or len(captureRefs) > self.store.limits.max_selections or len(captureRefs) != len(set(captureRefs)):
                raise AnalysisError('CAPTURE_LIMIT', '输入引用需要非空、无重复且不超过配置数量。')
            captureRefs, selections, preparation = await self.budget_preparation.for_build(captureRefs, selections, scope)
        request = {"captureRefs": captureRefs, "selections": selections, "scope": scope,
                   "operations": operations, "baseAnalysisRef": baseAnalysisRef, "engineVersion": ENGINE_VERSION,
                   "ruleVersion": self.rules.version, "skillVersions": self.skill_versions}
        from .native_analysis import CAPABILITY_VERSION
        request['capabilityVersion'] = CAPABILITY_VERSION
        request['workPlan'] = deepcopy([] if workPlan is None else workPlan)
        if actionRunRef:
            request["actionRunRef"] = actionRunRef
        reused = self.store.find_request(self.owner, "build", requestKey, digest(request))
        if reused:
            artifact = self.store.get_analysis(self.owner, reused)
            if self.turn is not None:
                from .followup_scope import validate_primary
                validate_primary(self.turn, artifact)
            return self._built(artifact)
        # 同一用户轮的后续构建延续已有工作目标；幂等检查使用调用方原始参数。
        implicit_base = baseAnalysisRef is None and self.turn is not None
        if implicit_base:
            baseAnalysisRef = self.turn.state.get('analysisRef')
        base = self.store.get_analysis(self.owner, baseAnalysisRef) if baseAnalysisRef else None
        if base:
            self.store.continuation_revision(self.owner, baseAnalysisRef)
        if incremental:
            captureRefs = deepcopy(base['captureRefs'])
            selections = deepcopy(base['plan']['selections'])
            scope = deepcopy(base['scope'])
            preparation = base.get('inputPreparation')
        if base and implicit_base:
            # 同轮只补输入时保留已取得的证据。显式 base 仍按调用方提交的完整选择器重建。
            # 同一来源步骤的选择器由本次输入替换，避免把旧筛选与新粒度重复拼入。
            def input_key(selection):
                return tuple(selection.get(key) for key in ('captureRef', 'stepId', 'batchId', 'observationId'))
            incoming = {input_key(selection) for selection in selections}
            inherited = [deepcopy(selection) for selection in base['plan']['selections']
                         if input_key(selection) not in incoming]
            selections = inherited + selections
            captureRefs = list(dict.fromkeys(base['captureRefs'] + captureRefs))
        # 运算引用在构建过程中会规范化，幂等请求保留调用时的原文。
        effective_work = deepcopy(request['workPlan'])
        if not captureRefs or len(captureRefs) > self.store.limits.max_selections or len(captureRefs) != len(set(captureRefs)):
            raise AnalysisError("CAPTURE_LIMIT", "输入引用需要非空、无重复且不超过配置数量。")
        if len(selections) > self.store.limits.max_selections or len(operations) > self.store.limits.max_operations:
            raise AnalysisError("PLAN_LIMIT", "分析计划超过本次容量上限。")
        captures = {ref: self.store.get_capture(self.owner, ref) for ref in captureRefs}
        from .formula_inputs import expand_selections, add_checks
        closure_started = perf_counter()
        prepared_input_count = sum(bool(item.get('preparedTargets')) for item in selections)
        selections, formula_targets = expand_selections(self.sources, captures, selections)
        if formula_targets or prepared_input_count:
            from .timing import record_timing
            record_timing(self.turn, 'formula_input_closure', (perf_counter() - closure_started) * 1000,
                          targetCount=len(formula_targets), preparedInputCount=prepared_input_count,
                          selectedCellCount=sum(len(item.get('cellKeys', [])) for item in selections))
        from .batch_scope import resolve_members
        typed_inputs = any('role' in selection or 'grain' in selection for selection in selections)
        if not typed_inputs and not incremental:
            scope = resolve_members(self.sources, captures, selections, scope, self.store.limits.max_facts)
        if baseAnalysisRef:
            from .completion import continue_plan
            scope, effective_work = continue_plan(base, scope, workPlan)
        loaded = perf_counter()
        if incremental:
            # 直接保留真实事实、来源身份、角色及粒度，避免重适配使有效 Python 输入失效。
            data = {key: deepcopy(base[key]) for key in
                    ('facts', 'scope', 'structureEvidence', 'sourceContexts', 'context') if key in base}
            data['scope'] = deepcopy(scope)
        elif typed_inputs:
            from .analysis_inputs import adapt_inputs
            data = adapt_inputs(self.sources, captures, selections, scope, self.store.limits.max_facts)
        else:
            data = self.sources.adapt(captures, selections, scope, self.store.limits.max_facts)
        adapted = perf_counter()
        from .validation import validate_report_topics
        validate_report_topics(data)
        origins = bind_source_origins(data["facts"], captures)
        if self.turn is not None:
            from .followup_scope import validate_primary
            validate_primary(self.turn, data)
        from .relations import captured_relations
        data['relations'] = deepcopy(base.get('relations', [])) if incremental else captured_relations(captures, self.sources, data['scope'])
        if baseAnalysisRef:
            from .operation_plan import merge_operations
            operations = merge_operations(base, operations, effective_work,
                                          [fact['factId'] for fact in data['facts']])
            if len(operations) > self.store.limits.max_operations:
                raise AnalysisError('PLAN_LIMIT', '包含继承运算后的分析计划超过本次容量上限。')
        verifier_refs = None
        if actionRunRef:
            if not self.actions_enabled or self.actions.active_ref != actionRunRef:
                raise AnalysisError("ACTION_NOT_RUNNING", "本回合尚未通过 AnalysisFollowUp 恢复该行动。")
            verifier_refs = set(self.store.get_action_run(self.owner, actionRunRef)["newCaptureRefs"])
        # 核验凭据与数值选择器独立登记，续建不需要模型再次选入核验回执。
        captures = self._verification_captures(data['facts'], captures, verifier_refs)
        captureRefs = list(captures)
        verified, checked = verification(data["facts"], captures, verifier_refs, self.sources)
        if actionRunRef:
            validate_plan(self.store, self.owner, actionRunRef, data, captures, baseAnalysisRef, verified == "VALID")
        operations = add_checks(operations, formula_targets, data['facts'], data['scope'])
        if len(operations) > self.store.limits.max_operations:
            raise AnalysisError('PLAN_LIMIT', '包含目标公式检查后的分析计划超过本次容量上限。')
        failure_cache = self.turn.state.setdefault('operationInputFailures', {}) if self.turn is not None else None
        derived = calculate(data["facts"], data["scope"], operations, self.rules, data["relations"], failure_cache=failure_cache)
        if workPlan is None and not effective_work and formula_targets:
            from .prepared_inputs import formula_work
            effective_work = formula_work(formula_targets, data['facts'], derived)
        elif formula_targets and any(task.get('operator') == 'FORMULA_CHECK' for task in effective_work):
            from .budget_preparation import formula_fact_links
            from .prepared_inputs import bind_formula_work
            links = formula_fact_links(self.turn, data['facts'], formula_targets)
            bind_formula_work(effective_work, formula_targets, derived, links)
        # 结果引用由宿主确定；模型可使用本计划运算 id，无须手工维护第二套前缀。
        operation_refs = {value['derivedId'].removeprefix('derived_'): value['derivedId'] for value in derived}
        for task in effective_work:
            if task.get('kind') == 'COMPUTATION':
                from .scalar_references import normalize_reference
                task['refs'] = [normalize_reference(ref, operation_refs) for ref in task.get('refs', [])]
        carried_python = []
        changed_python_inputs = {}
        if baseAnalysisRef:
            from .python_analysis import input_record
            known = {value.get('factId', value.get('derivedId')): value for value in data['facts'] + derived}
            old_known = {value.get('factId', value.get('derivedId')): value for value in base['facts'] + base['derivedValues']}
            for run in base.get('pythonRuns', []):
                for file in run.get('fileInputs', []):
                    known[file['ref']] = old_known[file['ref']] = file
            for value in base['derivedValues']:
                if value.get('operator') != 'PYTHON':
                    continue
                changed_inputs = []
                for ref in value['inputRefs']:
                    if ref not in known or ref not in old_known:
                        changed_inputs.append({'inputRef': ref, 'missing': True})
                        continue
                    current_input, previous_input = input_record(ref, known[ref]), input_record(ref, old_known[ref])
                    fields = sorted(key for key in current_input.keys() | previous_input.keys()
                                    if current_input.get(key) != previous_input.get(key))
                    if fields:
                        changed_inputs.append({'inputRef': ref, 'changedFields': fields})
                if not changed_inputs:
                    derived.append(deepcopy(value))
                    known[value['derivedId']] = value
                    carried_python.append(value['pythonRunRef'])
                else:
                    changed_python_inputs[value['derivedId']] = {'inputs': changed_inputs[:10], 'count': len(changed_inputs)}
        calculated = perf_counter()
        try:
            _comparable(data["facts"], data["scope"])
            comparability = "CONFIRMED"
        except AnalysisError as exc:
            comparability = "CONFLICT" if exc.code in ("UNIT_CONFLICT", "METRIC_CONFLICT") else "UNCONFIRMED"
        artifact = {"schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION, "ruleVersion": self.rules.version,
                    **({'inputPreparation': preparation} if preparation else {}),
                    "skillVersions": self.skill_versions, "captureRefs": captureRefs, **data,
                    "inputDigest": digest({ref: digest(value) for ref, value in captures.items()}),
                    "planDigest": digest({"selections": selections, "scope": scope, "operations": operations, 'workPlan': effective_work}),
                    "plan": {"selections": selections, "scope": scope, "operations": operations, 'workPlan': effective_work},
                    "derivedValues": derived, "coverage": data["scope"]["coverage"], "receiptVerification": verified,
                    "verificationRecords": checked, "verificationRecordedAt": now(), "comparability": comparability,
                    "calculationValidation": "NOT_APPLICABLE" if not derived else "BLOCKED" if any(v["calculationValidation"] != "PASS" for v in derived) else "PASS",
                    "claims": [], "viewPlan": [], "findings": [], "actions": [], "presentationPolicy": "explicit", "technicalDetails": False}
        if self.turn is not None:
            from .grouping_policy import grouping_requirements
            artifact['declaredGoals'] = deepcopy(self.turn.state.get('declaredGoals', []))
            artifact['groupingRequirements'] = deepcopy(grouping_requirements(self.turn))
        artifact["findingRules"] = self.finding_rules
        artifact["findingVersion"] = findings.FINDING_VERSION
        artifact["sourceResourceIds"] = sorted({captures[ref]["resourceId"] for fact in data["facts"] for ref in fact.get("captureRefs", [])})
        artifact["sourceOrigins"] = origins
        if baseAnalysisRef:
            artifact["baseAnalysisRef"] = baseAnalysisRef
            artifact['pythonRuns'] = [deepcopy(run) for run in base.get('pythonRuns', []) if run['runRef'] in carried_python]
        if actionRunRef:
            artifact["actionRunRef"] = actionRunRef
            artifact["sourceTransition"] = "SAME_CONTEXT" if data["context"] == self.store.get_analysis(self.owner, baseAnalysisRef)["context"] else "NEW_CONTEXT_INDEPENDENTLY_VERIFIED"
        from .completion import completion_summary
        try:
            completion_summary(artifact)
        except AnalysisError as error:
            invalidated = changed_python_inputs.get((error.details or {}).get('ref'))
            if error.code == 'INVALID_WORK_REF' and invalidated:
                error.details.update(reason='PYTHON_INPUT_CHANGED', baseAnalysisRef=baseAnalysisRef,
                                     changedInputs=invalidated['inputs'], changedInputCount=invalidated['count'],
                                     hint='代码输入的粒度、名称、数值或用途已改变，旧计算不能证明新输入。若仅补工作项或展示，使用原 analysisRef 的计算结果；确需改变输入则在新输入上重新执行代码，不必重查来源。')
            raise
        if baseAnalysisRef:
            from .completion import validate_target_continuity
            validate_target_continuity(base, artifact)
        artifact = self.store.build(self.owner, request, artifact, requestKey, baseAnalysisRef)
        logging.getLogger(__name__).info(
            '分析构建 版本=%s 输入方式=%s 事实=%d 来源=%d 读取毫秒=%.2f 范围毫秒=%.2f 核验计算毫秒=%.2f 总耗时毫秒=%.2f',
            artifact['analysisRef'], '沿用冻结输入' if incremental else '适配来源输入', len(data['facts']), len(captures), (loaded-started)*1000,
            (adapted-loaded)*1000, (calculated-adapted)*1000, (perf_counter()-started)*1000)
        return self._built(artifact)

    def _built(self, artifact):
        from .completion import completion_summary
        from .native_analysis import document_inputs
        from .result_projection import project_results
        artifact = self.effective_evidence(artifact)
        previous = self.store.get_analysis(self.owner, artifact['baseAnalysisRef']) if artifact.get('baseAnalysisRef') else None
        values, changes, calculation_summary = project_results(
            artifact['derivedValues'], compact_derived(artifact['derivedValues'], analysis_ref=artifact['analysisRef']),
            artifact['analysisRef'], previous.get('derivedValues', []) if previous else [])
        verification_tasks = self.pending_verifications(artifact)
        scope = {key: value for key, value in artifact['scope'].items() if key != 'sourceScopes'}
        omitted_lists = []
        for key, value in list(scope.items()):
            if isinstance(value, list) and len(value) > 50:
                omitted_lists.append({'field': key, 'count': len(value)})
                scope.pop(key)
        if omitted_lists:
            scope['omittedLists'] = omitted_lists
            scope['detailRead'] = {'section': 'summary', 'analysisRef': artifact['analysisRef']}
        scope['sourceRangeSummary'] = [{key: value for key, value in item.items()
                                       if key in ('captureRef', 'stepId', 'batchId', 'returnedRowCount', 'selectedRowCount', 'missingSelectedCellCount', 'columnSelection',
                                                  'rangeSummary', 'aggregationSummary')}
                                      for item in artifact['scope'].get('sourceScopes', [])]
        return {"schemaVersion": SCHEMA_VERSION, "analysisRef": artifact["analysisRef"],
                **({'inputPreparation': artifact['inputPreparation']} if artifact.get('inputPreparation') else {}),
                "revision": artifact["revision"], "scope": scope, **public_states(artifact),
                "factCount": len(artifact["facts"]), "derivedValues": values,
                'resultChanges': changes, 'calculationSummary': calculation_summary,
                'documentInputs': document_inputs(artifact),
                'sourceContexts': artifact.get('sourceContexts', []),
                'verificationTasks': verification_tasks[:100], 'verificationTaskCount': len(verification_tasks),
                'relationCount': len(artifact['relations']),
                'completion': completion_summary(artifact),
                "next": ("剩余来源核验由宿主在发布前增量执行，成功凭据自动登记，无须重建或重算；" if verification_tasks else "") +
                        "已返回的 derivedValues 可直接使用，缺具体字段才定向 Inspect。仅追加计算或工作项时传 baseAnalysisRef+operations+requestKey，省略 captureRefs/selections/scope，沿用已冻结输入；已有计算仅补完成引用可在发布时传 workRefs。换来源或范围时才同时提供三个完整输入字段。同轮省略 baseAnalysisRef 时补入新来源并延续已有输入。分析、构成查询首次用 DOCUMENT 交付图文；只改展示无需重建或重新查询。"}

    def pending_verifications(self, artifact):
        """以来源、快照、查询会话、执行和证据标识计算差集，不整批重发。"""
        tasks = {}
        if artifact['receiptVerification'] != 'VALID':
            captures = {ref: self.store.get_capture(self.owner, ref) for ref in artifact['captureRefs']}
            budget = {ref: capture for ref, capture in captures.items()
                      if capture.get('sourceAdapter', {}).get('id') == 'budget-v2'}
            from .adapters.budget_verification import verification_details, evidence_scope
            budget_facts = [fact for fact in artifact['facts'] if set(fact.get('captureRefs', [])) & budget.keys()]
            _, _, pending = verification_details(deepcopy(budget_facts), budget, self._verifier_refs(artifact)) if budget else (None, [], set())
            scopes = {ref: evidence_scope(capture) for ref, capture in budget.items()}
            for fact in artifact['facts']:
                for ref in fact.get('captureRefs', []):
                    capture = captures[ref]
                    task = self.sources.for_capture(capture).verification_task(fact, capture)
                    if task is None:
                        continue
                    evidence_ids = task['evidenceIds']
                    if ref in budget:
                        evidence_ids = [eid for eid in evidence_ids if (scopes[ref], task['executionId'], eid) in pending]
                        if not evidence_ids:
                            continue
                    key = (source_identity(capture), task.get('sourceContextId'), task.get('querySessionId'), task['executionId'])
                    saved = tasks.setdefault(key, {**task, 'sourceCaptureRef': ref, 'evidenceIds': set()})
                    saved['evidenceIds'].update(evidence_ids)
        return [{**task, 'evidenceIds': sorted(task['evidenceIds'])} for task in tasks.values()]

    def _annotate_formula_dependencies(self, artifact, trace):
        """只检查当前分析已登记的回执，区别未选入依赖和尚未捕获依赖。"""
        if not trace['pendingDependencies']:
            return
        known = {fact['factId']: fact for fact in artifact['facts']}
        catalog = []
        for capture_ref in artifact['captureRefs']:
            capture = self.store.get_capture(self.owner, capture_ref)
            catalog.extend(self.sources.catalog(capture))
        for dependency in trace['pendingDependencies']:
            target = known[dependency['targetFactRef']]
            candidates = [item for item in catalog
                          if item.get('cellAddress') == dependency['address']
                          and all(target.get(key) and item.get(key) == target[key]
                                  for key in ('sourceContextId', 'bindingRef', 'observationId'))]
            if dependency['reason'] == 'AMBIGUOUS_CELL':
                dependency['captureState'] = 'AMBIGUOUS'
            elif candidates:
                dependency['captureState'] = 'CAPTURED_NOT_SELECTED'
                dependency['requiresSourceQuery'] = False
                dependency['selections'] = [{key: item[key] for key in
                    ('captureRef', 'stepId', 'bindingRef', 'observationId')} | {'cellKeys': [item['cellKey']]}
                    for item in candidates]
                dependency['hint'] = '合并到对应输入的 cellKeys，沿用该输入用途与粒度；依赖不新增为业务 targetRefs。'
            else:
                dependency['captureState'] = 'NOT_CAPTURED_OR_UNMAPPED'
                dependency['hint'] = '当前回执未找到同表观察的来源坐标；先核对分页与坐标映射，再决定是否补查。'

    async def _review_publication(self, artifact):
        """审查异常保留已冻结的草稿与重试入口，不将服务故障当作通过。"""
        if self.turn is not None:
            from .followup_scope import validate_primary
            validate_primary(self.turn, artifact)
            from .review_control import closeout_reason
            if closeout_reason(self.turn):
                # 到限后的最后登记只能复用精确匹配的审查，不开启新一轮模型调用。
                from .turn_completion import document_review
                _, verdict = document_review(self.turn, artifact)
                if verdict is not None:
                    return verdict
                raise AnalysisError('ANALYSIS_RETRY_LIMIT', '当前内容未有可复用的审查结果，保留已有有效数据。')
        for attempt in range(2):
            try:
                return await self.preview_reviewer(artifact)
            except AnalysisError:
                raise
            except Exception as exc:
                if attempt == 0:
                    continue
                import logging
                logging.getLogger(__name__).exception('分析页面审查暂不可用，原材料重试后仍未完成')
                raise AnalysisError('REVIEW_UNAVAILABLE', '分析核对暂未完成，原数据和页面已保存。',
                                    {'requiresBuild': False, 'recoveryAction': 'HOST_CLOSEOUT'}) from exc

    async def publish_reviewed_preview(self, analysis_ref):
        """补做结束时提交已通过的冻结预览，沿用正常发布校验和幂等请求键。"""
        from .closeout_content import reviewed_publication
        artifact = self.store.get_analysis(self.owner, analysis_ref)
        reviewed = reviewed_publication(self, artifact)
        if reviewed is None or reviewed['publicationCheckCode']:
            return None
        return await self.publish(analysisRef=analysis_ref, repairRef=reviewed['previewRef'],
                                  repairs=[], requestKey='confirm-' + reviewed['previewRef'])

    async def publish_available_facts(self, analysis_ref, reason):
        """补做到限后复用既有组件，保留已核对说明与数据，不发布失败图形。"""
        await self.authorize()
        artifact = self.store.get_analysis(self.owner, analysis_ref)
        artifact = await self.ensure_verification(artifact)
        artifact = self._with_captured_verification(artifact)
        analysis_ref = artifact['analysisRef']
        if self.turn is not None:
            from .followup_scope import validate_primary
            validate_primary(self.turn, artifact)
        built = self._built(artifact)
        if artifact['receiptVerification'] == 'INVALID':
            raise AnalysisError('EVIDENCE_INVALID', '现有数据的来源核验未通过，已保留具体诊断与原始回执。',
                                {'verificationRecords': artifact.get('verificationRecords', [])})
        if built['verificationTaskCount']:
            raise AnalysisError('EVIDENCE_UNVERIFIED', '现有数据的来源核验尚未完成，已保留原始回执。')
        facts = [fact for fact in artifact['facts'] if fact.get('inputRole') != 'STRUCTURE']
        if not facts:
            raise AnalysisError('NO_AVAILABLE_FACTS', '当前没有可交付的已查得数据。')
        from .closeout_content import reviewed_preview, preview_review_status
        reviewed = reviewed_preview(self, artifact)
        review_status = preview_review_status(self, artifact, reason)
        # 每份事实摘要是独立草稿，保留之前页面，且不重复取数或执行计算。
        draft = deepcopy(artifact)
        for field in ('publishedAt', 'freeDocument', 'presentation', 'workRefs', 'partialReason',
                      'renderCheckUnavailableReason', 'actionRunRef', 'report'):
            draft.pop(field, None)
        draft.update(claims=[], viewPlan=[], findings=[], actions=[], technicalDetails=False,
                     baseAnalysisRef=analysis_ref)
        draft['hostSummary'] = {'reason': reason, 'content': 'REVIEWED_ANALYSIS' if reviewed else 'SOURCE_FACTS_ONLY',
                                'semanticReview': review_status}
        if reviewed:
            draft['hostSummary'].update(reviewedParagraphs=reviewed['paragraphs'],
                                        previewRef=reviewed['previewRef'], contentDigest=reviewed['contentDigest'])
            if reviewed['workRefs'] is not None:
                draft['workRefs'] = reviewed['workRefs']
        request = {'kind': 'AVAILABLE_FACTS', 'baseAnalysisRef': analysis_ref, 'reason': reason}
        draft = self.store.build(self.owner, request, draft, 'available-' + digest(request), analysis_ref)
        scope_id = draft['scope']['scopeId']
        references = set(draft['scope'].get('referenceMembers', []))
        primary = [fact for fact in facts if fact.get('inputRole') != 'REFERENCE' and fact['objectId'] not in references]
        primary_refs = {fact['factId'] for fact in primary}
        comparison = [fact for fact in facts if fact['factId'] not in primary_refs]
        views = []
        for name, title, selected in (('primary', '分析主体数据', primary), ('reference', '参考数据', comparison)):
            for start in range(0, len(selected), 1000):
                views.append({'viewId': 'available-' + name + '-' + str(start), 'scopeId': scope_id, 'type': 'TABLE',
                              'title': title, 'valueRefs': [fact['factId'] for fact in selected[start:start + 1000]],
                              'showUnit': True, 'pageSize': 20})
        if reviewed:
            scalars = [value['derivedId'] for value in draft['derivedValues']
                       if value.get('calculationValidation') == 'PASS' and value.get('value') is not None]
            if scalars:
                views.append({'viewId': 'available-computed', 'scopeId': scope_id, 'type': 'TABLE',
                              'title': '已完成的计算', 'valueRefs': scalars, 'showUnit': True, 'pageSize': 20})
        # 只用主体同粒度原值绘图；总体对照的不同粒度不应阻止主体图形。
        # 原值图不声称汇总贡献，仍校验单位、时期、来源及指标一致。
        from .validation import validate_source_chart
        numeric = [fact for fact in primary if fact['valueState'] == 'PRESENT'][:20]
        if len(numeric) > 1:
            try:
                validate_source_chart(numeric, draft['scope'])
            except AnalysisError:
                pass  # 跨指标或缺少图形所需身份时仍交付完整数据表。
            else:
                views.insert(0, {'viewId': 'available-chart', 'scopeId': scope_id, 'type': 'BAR_VALUES',
                                 'title': '已取得数据对照（最多20项）',
                                 'valueRefs': [fact['factId'] for fact in numeric], 'showUnit': True})
        partial_reason = '本次展示已取得的数据；进一步的分析解释尚未完成。'
        if reviewed:
            unfinished = ('原图表尚未完成展示检查' if reviewed['publicationCheckCode'] in
                          ('DOCUMENT_RENDER_ERRORS', 'DOCUMENT_CHECK_COVERAGE_LOST') else '原页面尚未完成展示交付')
            partial_reason = unfinished + '，本次保留已核对的分析说明、计算结果和数据明细。'
        if reviewed and reviewed['partialReason']:
            partial_reason += ' ' + reviewed['partialReason']
        return await self._publish_content(draft['analysisRef'], [], views, 'available-publish-' + digest(request),
            draft['baseRevision'], partialReason=partial_reason,
            host_summary=reason)

    async def inspect(self, *args, **request):
        """续读原样保留调用条件；不因分页扩大捕获、对象或字段范围。"""
        if 'requests' in request:
            if args or set(request) - {'requests', 'allPages', 'analysisRef'}:
                raise AnalysisError('INVALID_READ_BATCH', 'requests 与顶层单次读取条件不能混用。')
            from inspect import signature
            from .batch_reads import read_batch, validate_inspect_requests
            await self.authorize()
            requests = deepcopy(request['requests'])
            if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
                raise AnalysisError('INVALID_READ_BATCH', 'requests 必须是读取条件对象的列表。')
            if request.get('analysisRef'):
                for item in requests:
                    if not item.get('captureRef') and not item.get('pythonRunRef'):
                        item.setdefault('analysisRef', request['analysisRef'])
            validate_inspect_requests(requests)
            return await read_batch(requests, request.get('allPages', False), self.inspect,
                                    allowed_fields=set(signature(self._inspect).parameters))
        if 'allPages' in request:
            raise AnalysisError('INVALID_READ_BATCH', 'allPages 仅用于 requests 批量读取。')
        if args:
            from inspect import signature
            request = dict(signature(self._inspect).bind(*args, **request).arguments)
        result = await self._inspect(**request)
        if isinstance(result, dict) and result.get('nextOffset') is not None:
            next_read = {**{key: value for key, value in request.items() if value is not None},
                         'section': request.get('section', 'captures'), 'offset': result['nextOffset']}
            candidate = {**result, 'nextRead': next_read}
            # 可执行提示不挤占真实数据；容量刚好用满时仍保留原 nextOffset 供续读。
            if 'characterBudget' not in result or len(json.dumps(candidate, ensure_ascii=False)) <= result['characterBudget']:
                result = candidate
        return result

    async def _inspect(self, section="captures", analysisRef=None, captureRef=None, offset=0, limit=50, derivedId=None, stepId=None, search=None, findingPlan=None, thresholds=None, factRef=None, memberDimension=None, objectId=None, bindingRef=None, period=None, measureCode=None, cellKey=None, detail="compact", fields=None, previewRef=None, pythonRunRef=None):
        await self.authorize()
        bounded_page([], offset, limit, self.store.limits.page_size)
        if section == 'materials':
            if not captureRef:
                raise AnalysisError('CAPTURE_REQUIRED', '资料入口需要实际 captureRef。')
            if any(value is not None for value in (analysisRef, objectId, memberDimension, factRef, fields, search,
                                                   stepId, bindingRef, period, measureCode, cellKey, derivedId)):
                raise AnalysisError('INVALID_MATERIAL_FILTER', 'materials 读取本回执资料；按对象或字段筛选请使用 inventory/facts。')
            from .analysis_materials import materials
            return materials(self.store.get_capture(self.owner, captureRef), self.sources, offset=offset, limit=limit,
                             store=self.store, owner=self.owner)
        if section == 'gridFiles':
            if not captureRef:
                raise AnalysisError('CAPTURE_REQUIRED', '读取网格文件信息需要 captureRef。')
            from .grid_files import file_summary
            capture = self.store.get_capture(self.owner, captureRef)
            return {'captureRef': captureRef, **bounded_page(file_summary(capture), offset, limit),
                    'errors': capture.get('gridFileErrors', []), 'tool': 'AnalysisPython',
                    'input': 'gridInputs[{captureRef,fileRef}]',
                    'reader': 'grid_rows(ref, where)', 'previewIsCompleteData': False}
        if section == 'python':
            if pythonRunRef:
                return self.store.get_python_run(self.owner, pythonRunRef)
            artifact = self.store.get_analysis(self.owner, analysisRef)
            return bounded_page(artifact.get('pythonRuns', []), offset, limit)
        if pythonRunRef is not None:
            raise AnalysisError('INVALID_SECTION', 'pythonRunRef 仅用于 section=python。')
        if fields is not None and (section != "facts" or not isinstance(fields, list) or not 1 <= len(fields) <= 30 or any(not isinstance(k, str) for k in fields)):
            raise AnalysisError("INVALID_FIELDS", "fields 仅用于 facts，选择最多 30 个实际字段。",
                                {'path': '/fields', 'nextAction': 'REMOVE_FIELDS' if section != 'facts' else 'SELECT_FACT_FIELDS',
                                 'hint': 'inventory 使用 detail=compact 和现有筛选条件；移除 fields，无需重新查询预算数据。' if section == 'inventory' else '按当前 section 的契约修正 fields。'})
        if section != "findings" and (findingPlan is not None or thresholds is not None):
            raise AnalysisError("INVALID_SECTION", "检查预览参数仅用于 section=findings。")
        if section == 'preview':
            result = await self.preview_content(previewRef)
            return {'analysisRef': result['analysisRef'], 'previewRef': previewRef, 'diagnostics': result['diagnostics']}
        if previewRef is not None:
            raise AnalysisError('INVALID_SECTION', 'previewRef 仅用于 section=preview。')
        if section == "protocol":
            from .presentation import RENDERER_VERSION
            from .native_analysis import capabilities
            return {"schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION, "rendererVersion": RENDERER_VERSION,
                    'nativeAnalysis': capabilities(),
                    "skillVersions": self.skill_versions, "rules": self.rules.rules,
                    "sourceAdapters": sorted(self.sources.factories),
                    "contextModes": ['SINGLE_SNAPSHOT', 'COMPOSITE'],
                    "capabilityPolicy": 'HOST_CONFIGURED_SKILL_GUIDED',
                    "turnCompletion": {'tool': 'AnalysisTask', 'modes': ['DIRECT', 'ANALYSIS', 'REPORT'],
                                       'scope': 'real_user_turn', 'resumePreservesContract': True,
                                       'analysisRequiresDeclaredWork': True, 'checksActualSourceVerification': True,
                                       'endGuard': 'bounded_continuation', 'semanticReview': 'evidence_based_model_review',
                                       'retryLimits': {'reviewAttempts': self.store.limits.max_review_attempts,
                                                       'reviewSeconds': self.store.limits.review_budget_seconds,
                                                       'analysisErrors': self.store.limits.max_analysis_repairs,
                                                       'repairSeconds': self.store.limits.repair_budget_seconds},
                                       'closeoutAction': 'HOST_CLOSEOUT',
                                       'noImplicitBusinessMethods': True},
                    'formulaCheck': {'checkScope': ['LOCAL', 'RECURSIVE'], 'default': 'LOCAL',
                                     'ordinaryComposition': 'LOCAL', 'missingDeeperValuesDoNotInvalidateLocal': True},
                    "operators": ["SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC", "DESCRIBE"],
                    "secondaryAnalysis": {'tool': 'AnalysisPython', 'runtime': 'HOST_MANAGED_PYTHON',
                                          'requires': ['fallbackReason', 'purpose'],
                                          'selectionPolicy': 'native_first_then_necessary_capability_gap',
                                          **python_capability(),
                                          'inputs': ['owned_fact_or_product_refs', 'captured_query_selectors', 'workspace_data_files', 'captured_grid_files'],
                                          'outputs': ['SCALAR', 'TABLE', 'CHART_DATA', 'DIAGNOSTIC', 'FILE'],
                                          'outputPrecision': {'field': 'decimalPlaces', 'range': [0, 16],
                                                              'rounding': 'ROUND_HALF_EVEN', 'intermediatesUnchanged': True},
                                          'inspectSection': 'python', 'productSection': 'products',
                                          'maximumSeconds': 60, 'memoryLimitBytes': {'standard': 256 * 1024 * 1024, 'libraries': 1024 * 1024 * 1024},
                                          'gridFiles': {'input': 'gridInputs', 'reader': 'grid_rows', 'maximumFileBytes': 512 * 1024 * 1024},
                                          'businessValidation': 'separate_semantic_review', 'nativeGroupingRequired': True},
                    "viewTypes": ["METRIC", "TABLE", "BAR_VALUES", "BAR_RANK", "BRIDGE", "ORDER", "FORMULA", "HIERARCHY", "WATERFALL", "RECONCILIATION"],
                    "findingRules": self.finding_rules, "findingVersion": findings.FINDING_VERSION,
                    "findingChecks": list(findings.CHECKS), "actionChecks": list(ACTION_CHECKS),
                    "planPolicy": "EXPLICIT_ONLY",
                    "viewOptions": ["title", "description", "showUnit", "includeDetails", "nodeRefs", "collapsed", "pageSize", "numberFormat", "labels", "comparisonRef", "descriptionParts", "maxInputsPerScene", "groupId", "orderRef", "nodeDescriptions", "relationLayout", "showChecks", "emphasisRefs", "orientation"],
                    "documentPresentation": {"version":"free-html-1", "publishField":"document", "source":["path","html"],
                        "bindings":"key/valueRef/field/nodeRef/format", "textSlot":"{{key}}", "scriptData":"window.analysisData[key]",
                        "datasets":"key/valueRef/nodeRefs/format", "scriptDatasets":"window.analysisSets[key]",
                        "datasetKinds":["ORDERED", "FORMULA", "RECONCILIATION", "TABULAR"],
                        "previewChecks":"previewPage 的运行页面检查；AnalysisInspect(section=preview,previewRef=repairRef) 读取同一文档摘要记录",
                        "requiredPreviewWidths":2, "previewUnavailableField":"renderCheckUnavailableReason",
                        "automaticPreviewChecks": bool(self.render_checker),
                        "optionalConsistencyMarkers": {"visibleCount":"data-analysis-count-for",
                            "linearScale":"data-analysis-scale", "boundRow":["data-analysis-bar-set","data-analysis-bar-ref"],
                            "boundScalar":"data-analysis-bar-value", "axis":"data-analysis-bar-axis"},
                        "declaredSortRequiresDataset":True, "partialReasonSlot":"{{analysisPartialReason}}",
                        "layout":"model_authored", "externalResources":False, "frozenPreview":True,
                        "sameContentForCardAndReport":True},
                    "summaryFormat": "structured", "technicalDetailsDefault": False,
                    "presentationOptions": ["labels", "groups", "claimsCollapsed", "summaryPosition", "summaryTitle", "reportTitle"],
                    "groupOptions": ["groupId", "title", "description", "descriptionParts", "width", "claimRefs", "claimsPosition"],
                    'publicationRepair': {'input': ['repairRef', 'repairs'], 'errorPaths': 'JSON Pointer',
                                          'preservesUnchangedContent': True, 'draftOnly': True, 'schemaErrorsRepairable': True, 'previewOnly': True, 'previewHasVisualInspection': False,
                                          'versionManagement':'HOST', 'versionRecovery':'retryPreview',
                                          'finalizeAfterChecks': True, 'repairAndFinalize': True,
                                          'batchTextRepair': {'op': 'replaceText', 'path': '/document/html',
                                              'value': ['oldText', 'newText', 'textRef(optional)'],
                                              'basis': 'FROZEN_SOURCE', 'atomic': True,
                                              'latestEditSource': 'documentEditSource.path'}},
                    'presentationRevision': {'publishedPreviewCreatesDraft': True, 'reusesFactsAndCalculations': True,
                                             'frozenSourceRefresh': '/document/path', 'prepublicationSemanticReview': bool(self.preview_reviewer)},
                    'calculationReferences': {'previousOperationIdAccepted': True, 'storedReference': 'derivedId',
                                              'scalarFields': {'shape': ['valueRef', 'field', 'nodeRef(optional)'],
                                                               'arithmeticVariables': 'v0,v1,... in inputRefs order', 'rawVariablesAccepted': False},
                                              'nativeSum': 'same batch, metric, disjoint groups and source CalcType=SUM'},
                    'incrementalBuild': {'required': ['baseAnalysisRef', 'operations', 'requestKey'],
                                         'response': 'resultChanges + compact current results; detailRead restores full frozen data',
                                         'omitTogether': ['captureRefs', 'selections', 'scope'],
                                         'preservesFrozenInputs': True, 'workPlanOptional': True,
                                         'replaceInputsTogether': True},
                    'scopeMembers': {'subjects': 'members', 'comparators': 'referenceMembers', 'populationAppliesTo': 'members',
                                     'sourceQualifiedIdentity': 'inventory.memberRef', 'originalSourceId': 'sourceObjectId',
                                     'operationSelector': 'Use objectId returned by Build, or actual fact inputRefs'},
                    'workCompletion': {'updates': 'workRefs', 'rebuildFrom': 'baseAnalysisRef', 'draftBaseSupported': True, 'preservesDeclaredTasks': True, 'incompletePublicationRequires': 'partialReason',
                                       'scalarReferences': True, 'explicitOrdinaryMethodCorrection': True,
                                       'computationScope':['operator','targetRefs'], 'missingTargets':'missingTargetRefs',
                                       'readyDoesNotMeanSourceConsistency': True},
                    'tablePagination': 'independent_per_table',
                    "deliveryCapabilities": {"cards": bool(self.delivery and self.delivery.cards_enabled),
                                             "generateReports": bool(self.delivery and self.delivery.allow_reports),
                                             "followUp": self.actions_enabled},
                    "calculationValidationMeaning": "execution_conditions_only_not_source_consistency",
                    "formulaChecks": ["localCheck", "sourceConsistency", "differenceOriginRefs", "propagatedDifference"],
                    "formulaInspection": {"version": 2, "noCapturedFormula": "COMPLETED_NOT_APPLICABLE",
                                          "noCapturedFormulaDoesNotProveSourceAbsence": True,
                                          "blockedRecovery": ["nextRead", "diagnostics.details.blockers", "diagnostics.details.pendingDependencies"],
                                          "implicitBusinessPolicy": None},
                    "reconciliation": {"operator": "RECONCILE", "aggregation": "SUM", "requires": ["relationRef", "parent_then_all_direct_children"], "defaultExpectation": None},
                    "inventory": {"section": "inventory", "scope": "current_session", "preservesObservations": True, "defaultDetail": "compact", "details": ["compact", "full"], "filters": ["captureRef", "stepId", "cellKey", "objectId", "bindingRef", "period", "measureCode"]},
                    "capabilities": await self.actions.capabilities() if self.actions_enabled else []}
        if section == "inventory":
            from .inventory import inventory
            return inventory(self.store, self.owner, offset, limit, memberDimension, search, sources=self.sources,
                             objectId=objectId, bindingRef=bindingRef, period=period, measureCode=measureCode, cellKey=cellKey, captureRef=captureRef, stepId=stepId, detail=detail)
        if section == 'inventoryIssues':
            from .inventory import session_inputs
            _, issues, counts = session_inputs(self.store, self.owner, self.sources)
            return {**bounded_page(issues, offset, limit, self.store.limits.page_size), **counts}
        if section == 'entities':
            from .inventory import entities, session_inputs
            captures = [self.store.get_capture(self.owner, captureRef)] if captureRef else session_inputs(self.store, self.owner, self.sources)[0]
            return entities(captures, offset, limit, self.store.limits.page_size, self.store.limits.max_facts, objectId, search, self.sources)
        if section == 'results':
            return self.store.list_results(self.owner, offset, limit)
        if section == 'selectors':
            from .analysis_materials import selection_candidates, material_capture
            from .inspect_pages import character_page
            capture = material_capture(self.store.get_capture(self.owner, captureRef), self.sources, self.store, self.owner)
            return character_page(selection_candidates(capture, self.sources),
                                  offset, limit, self.store.limits.page_size, fields_supported=False)
        if section == 'relations' and not analysisRef:
            from .inventory import session_inputs
            from .relations import captured_relations
            captures, _, counts = session_inputs(self.store, self.owner, self.sources)
            capture_map = {c['captureRef']: c for c in captures}
            diagnostics = [item for adapter, group in self.sources.groups(capture_map)
                           for item in (adapter.relation_diagnostics(group) if hasattr(adapter, 'relation_diagnostics') else [])]
            return {**bounded_page(captured_relations(capture_map, self.sources), offset, limit, self.store.limits.page_size),
                    **counts, 'diagnostics': bounded_page(diagnostics, offset, limit, self.store.limits.page_size)}
        if section == "captures":
            return self.store.list_captures(self.owner, offset, limit)
        if section == 'queryContexts':
            # 只读当前会话的已有捕获；按最新优先分页，不枚举远端任务或成员。
            import json
            page = self.store.list_captures(self.owner, offset, min(limit, 10), newest_first=True)
            items = []
            for item in page['items']:
                capture = self.store.get_capture(self.owner, item['captureRef'])
                adapter = self.sources.for_capture(capture)
                try:
                    context = adapter.receipt_info(capture)
                except AnalysisError as exc:
                    context = {'error': exc.as_dict()}
                entry = {**item, 'queryContext': context}
                if len(json.dumps(json_values(entry), ensure_ascii=False)) > 12000:
                    entry = {**item, 'detailRead': {'section': 'receipt', 'captureRef': item['captureRef']},
                             'message': '该回执的完整查询条件较大，按引用读取；未裁剪条件或成员范围。'}
                items.append(entry)
            return {**page, 'items': items, 'order': 'NEWEST_FIRST'}
        if section == "capture":
            capture = self.store.get_capture(self.owner, captureRef)
            items = self.sources.catalog(capture, memberDimension)
            if stepId:
                items = [item for item in items if item["stepId"] == stepId]
            if search:
                import json
                def source_row(item):
                    # TABLE 行和网格行使用各自定位信息；不为聚合结果捏造网格坐标。
                    return (item.get('stepId'), item.get('bindingRef'), item.get('batchId'),
                            item.get('rowIndex') if item.get('batchId') else item.get('row'))
                rows = {source_row(item) for item in items
                        if search in json.dumps(item, ensure_ascii=False)}
                items = [item for item in items if source_row(item) in rows]
            return bounded_page(items, offset, limit, self.store.limits.page_size)
        if section == "receipt":
            value = self.store.get_capture(self.owner, captureRef)
            receipt = self.sources.for_capture(value).receipt_info(value)
            # Request is included for context/recovery, not full raw responses.
            return {"captureRef": captureRef, "request": value["request"], "receiptSummary": value.get("receiptSummary"),
                    **receipt}
        if section == "rows":
            capture = self.store.get_capture(self.owner, captureRef)
            return bounded_page(self.sources.for_capture(capture).rows(capture, stepId), offset, limit, self.store.limits.page_size)
        artifact = self.store.get_analysis(self.owner, analysisRef)
        if artifact.get("schemaVersion") != SCHEMA_VERSION:
            raise AnalysisError("UNSUPPORTED_VERSION", "不支持该分析协议版本。")
        if section == "delivery":
            if self.delivery is None:
                raise AnalysisError("DELIVERY_UNAVAILABLE", "当前宿主没有装配展示能力。")
            return self.delivery.ensure(analysisRef)
        if section == "summary":
            document = None
            if artifact['publicationState'] == 'PUBLISHED':
                from .presentation import project
                document = project(artifact, self.rules)
            return {"analysisRef": analysisRef, **public_states(artifact), "scope": artifact["scope"], "summary": summary(artifact, document)}
        if section == "findings":
            self._check_draft_rules(artifact)
            if artifact["publicationState"] == "PUBLISHED" and (findingPlan is not None or thresholds is not None):
                raise AnalysisError("ALREADY_PUBLISHED", "已发布发现保持固定；新检查请创建关联分析。")
            items = artifact["findings"] if artifact["publicationState"] == "PUBLISHED" else findings.generate(artifact, self.rules, thresholds=thresholds, configured=artifact["findingRules"], base=self._finding_base(artifact), plan=findingPlan)
            return bounded_page(items, offset, limit, self.store.limits.page_size)
        if section == "actions":
            return {"proposals": bounded_page(artifact["actions"], offset, limit, self.store.limits.page_size),
                    "runs": bounded_page(self.store.list_action_runs(self.owner, analysisRef), offset, limit, self.store.limits.page_size)}
        if section == "derived":
            values = artifact['derivedValues']
            if search:
                values = [value for value in values if search in value['derivedId']]
            return bounded_page(compact_derived(values, analysis_ref=analysisRef), offset, limit, self.store.limits.page_size)
        if section == 'products':
            value = next((item for item in artifact['derivedValues'] if item['derivedId'] == derivedId), None)
            if value is None or value.get('operator') not in ('PYTHON', 'DESCRIBE'):
                raise AnalysisError('UNKNOWN_VALUE', 'products 使用当前分析的 Python 产物或 DESCRIBE 表格 derivedId。')
            if 'rows' in value:
                return {**{key: item for key, item in value.items() if key != 'rows'},
                        'rows': bounded_page(value['rows'], offset, limit, self.store.limits.page_size)}
            if 'text' in value:
                text = value['text']
                return {**{key: item for key, item in value.items() if key != 'text'},
                        'text': text[offset:offset + limit * 100], 'nextOffset': offset + limit * 100 if len(text) > offset + limit * 100 else None}
            return value
        if section == "formulas":
            if derivedId is not None or factRef is None:
                details = {'path': '/derivedId' if derivedId is not None else '/factRef',
                           'section': section, 'requiredField': 'factRef', 'nextAction': 'CORRECT_INSPECT_ARGUMENTS',
                           'hint': 'formulas 读取来源事实的表内依赖，传目标 factRef；已完成的计算结果用 section=derived。无需重查来源或重建分析。',
                           'nextRead': {'section': 'facts', 'analysisRef': analysisRef, 'fields': ['factId', 'objectId', 'headers']}}
                value = next((v for v in artifact['derivedValues'] if v['derivedId'] == derivedId), None)
                candidates = value.get('inputRefs', []) if value and value.get('operator') == 'FORMULA_CHECK' else []
                if factRef is not None or len(candidates) == 1:
                    details['retryInspect'] = {'section': 'formulas', 'analysisRef': analysisRef,
                                               'factRef': factRef if factRef is not None else candidates[0],
                                               'offset': offset, 'limit': limit}
                raise AnalysisError('INVALID_INSPECT_ARGUMENT', 'section=formulas 必须使用目标 factRef，不能使用 derivedId。', details)
            from .formulas import trace_formula
            trace = trace_formula(artifact["facts"], factRef)
            self._annotate_formula_dependencies(artifact, trace)
            return {**{k: v for k, v in trace.items() if k not in ("nodes", "pendingDependencies")},
                    "nodes": bounded_page(trace["nodes"], offset, limit, self.store.limits.page_size),
                    "pendingDependencies": bounded_page(trace["pendingDependencies"], offset, limit, self.store.limits.page_size)}
        if section == "order":
            if derivedId is None:
                raise AnalysisError('INVALID_INSPECT_ARGUMENT', 'section=order 必须指定 SORT/RANK 的 derivedId。',
                                    {'path': '/derivedId', 'section': section, 'requiredField': 'derivedId',
                                     'nextRead': {'section': 'derived', 'analysisRef': analysisRef}})
            value = next((v for v in artifact["derivedValues"] if v["derivedId"] == derivedId), None)
            if value is None:
                raise AnalysisError("UNKNOWN_VALUE", "不存在该计算结果。")
            return bounded_page(value.get("orderedFactIds", []) + value.get("unavailableFactIds", []), offset, limit, self.store.limits.page_size)
        if section in ("facts", "claims", "verificationRecords", "relations"):
            items = artifact[section]
            if section == "facts":
                items = [item for item in items if all(expected is None or item.get(key) == expected
                         for key, expected in {'objectId': objectId, 'bindingRef': bindingRef, 'period': period, 'measureCode': measureCode}.items())]
            if search:
                import json
                items = [item for item in items if search in json.dumps(item, ensure_ascii=False)]
            if section == "facts":
                from .inspect_pages import character_page, compact_facts
                if factRef is not None: items = [item for item in items if item['factId'] == factRef]
                if fields is not None:
                    available = {key for item in artifact['facts'] for key in item}
                    unknown = set(fields) - available
                    if unknown:
                        raise AnalysisError('INVALID_FIELDS', 'fields 包含不存在的事实字段；按 availableFields 选择实际字段。',
                                            {'path': '/fields', 'fields': sorted(unknown), 'availableFields': sorted(available)})
                    items = [{k: v for k, v in item.items() if k in set(fields) | {'factId'}} for item in items]
                return character_page(items, offset, limit, self.store.limits.page_size,
                                      compact_facts if detail == 'compact' and fields is None else None,
                                      {'detail': detail, 'fullRead': {'section': 'facts', 'analysisRef': analysisRef, 'detail': 'full'}})
            return bounded_page(items, offset, limit, self.store.limits.page_size)
        raise AnalysisError("INVALID_SECTION", "不支持该分析读取区段。")

    async def validate_publication_input(self, request):
        from .publication import schema_issues
        await self.authorize()
        issues = schema_issues(request)
        if not issues: return
        details = {'issues': issues, 'path': issues[0]['path']}
        # Only save a repair proposal when its immutable envelope is valid.
        ref = request.get('analysisRef')
        if (isinstance(ref, str) and not request.get('repairRef')
                and not set(request) - {'analysisRef', 'requestKey', 'previewOnly',
                                       'document', 'claims', 'viewPlan', 'presentation', 'workRefs', 'partialReason',
                                       'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses', 'renderCheckUnavailableReason'}):
            artifact = self.store.get_analysis(self.owner, ref)
            if artifact['publicationState'] == 'DRAFT':
                saved = {k: v for k, v in request.items() if k not in ('requestKey', 'previewOnly')}
                details['repairRef'] = self.store.save_publication_proposal(self.owner, ref, saved)
                details['repairHint'] = '按 issues/path 修改，未修改的图表、简称和结论保持原样。'
        raise AnalysisError('INVALID_PUBLICATION', '发布参数校验未通过。', details)

    def _verifier_refs(self, artifact):
        """追查行动只接受该行动新取得的核验；普通分析使用已保存凭据。"""
        action_ref = artifact.get('actionRunRef')
        if action_ref:
            if not self.actions_enabled or self.actions.active_ref != action_ref:
                raise AnalysisError('ACTION_NOT_RUNNING', '本回合尚未通过 AnalysisFollowUp 恢复该行动。')
            return set(self.store.get_action_run(self.owner, action_ref)['newCaptureRefs'])
        return None

    def _verification_captures(self, facts, captures, verifier_refs=None):
        from .adapters.budget_verification import matches_selected_execution
        captures = dict(captures)
        for ref in self.turn.state.get('queryCaptureRefs', []) if self.turn else []:
            if ref in captures or verifier_refs is not None and ref not in verifier_refs:
                continue
            candidate = self.store.get_capture(self.owner, ref)
            if (candidate.get('sourceAdapter', {}).get('id') != 'budget-v2'
                    or str(candidate.get('captureState', '')).upper() not in ('SUCCESS', 'SUCCEEDED')):
                continue
            if matches_selected_execution(facts, captures, candidate):
                captures[ref] = candidate
        return captures

    def _captured_verification_updates(self, artifact):
        """保留每次成功的增量凭据；未全部核验时也不能丢掉已完成部分。"""
        verifier_refs = self._verifier_refs(artifact)
        captures = {ref: self.store.get_capture(self.owner, ref) for ref in artifact['captureRefs']}
        captures = self._verification_captures(artifact['facts'], captures, verifier_refs)
        facts = deepcopy(artifact['facts'])
        state, records = verification(facts, captures, verifier_refs, self.sources)
        if (list(captures) == artifact['captureRefs'] and state == artifact['receiptVerification']
                and records == artifact.get('verificationRecords', [])):
            return {}
        return {'captureRefs': list(captures), 'receiptVerification': state,
                'verificationRecords': records, 'verificationRecordedAt': now(),
                'facts': facts,
                'inputDigest': digest({ref: digest(value) for ref, value in captures.items()})}

    def effective_evidence(self, artifact):
        """发布版本不可改写；完成检查按相同来源读取后续核验，不另造业务版本。"""
        refs = tuple(self.turn.state.get('queryCaptureRefs', [])) if self.turn else ()
        verifier_refs = self._verifier_refs(artifact)
        key = (artifact['analysisRef'], artifact.get('inputDigest'), refs,
               None if verifier_refs is None else tuple(sorted(verifier_refs)))
        if key not in self._evidence_cache:
            updates = self._captured_verification_updates(artifact)
            self._evidence_cache[key] = updates
            while len(self._evidence_cache) > 32:
                self._evidence_cache.pop(next(iter(self._evidence_cache)))
            if updates and self.turn:
                # 回执本身已持久化；进度索引供后台恢复和排查查看累计状态。
                def record(progress):
                    records = progress.setdefault('evidenceRegistration', {})
                    records[artifact['analysisRef']] = {field: updates[field] for field in
                        ('receiptVerification', 'verificationRecords', 'verificationRecordedAt')}
                    while len(records) > 32:
                        records.pop(next(iter(records)))
                self.store.turn_progress(self.owner, self.turn.state['turnKey'], record)
        return {**artifact, **self._evidence_cache[key]}

    async def ensure_verification(self, artifact):
        """确需远程核验时由宿主执行差集，不让模型承担凭据登记循环。"""
        artifact = self.effective_evidence(artifact)
        if artifact['receiptVerification'] != 'INVALID' and self.verification_runner:
            tasks = self.pending_verifications(artifact)
            if tasks:
                await self.verification_runner(tasks)
                artifact = self.effective_evidence(artifact)
        return artifact

    def _with_captured_verification(self, artifact):
        """把本轮已取得的核验接入新草稿；原版本、事实、计算和页面均不改写。"""
        if artifact['publicationState'] != 'DRAFT':
            return artifact
        updates = self._captured_verification_updates(artifact)
        if not updates:
            return artifact
        if updates['receiptVerification'] == 'INVALID':
            raise AnalysisError('EVIDENCE_INVALID', '当前事实的来源核验未通过，不能发布为有效结果。',
                                {'verificationRecords': updates['verificationRecords']})
        # 发布不允许就地修改输入。新增关联草稿仅登记凭据，不重跑查询、计算或 Python。
        draft = deepcopy(artifact)
        draft.update(updates, baseAnalysisRef=artifact['analysisRef'])
        request = {'kind': 'CAPTURED_VERIFICATION', 'baseAnalysisRef': artifact['analysisRef'],
                   'verificationCaptureRefs': [ref for ref in updates['captureRefs'] if ref not in artifact['captureRefs']]}
        draft = self.store.build(self.owner, request, draft, 'verification-' + digest(request), artifact['analysisRef'])
        self._record_presentation_revision(draft)
        return draft

    def _record_presentation_revision(self, artifact, created=False):
        """登记本轮展示修订，并让宿主的核验或恢复草稿延续该身份。"""
        if self.turn is None:
            return
        from .review_control import progress
        def record(state):
            revisions = state.setdefault('presentationRevisionRefs', {})
            base_ref = artifact.get('baseAnalysisRef')
            if created or base_ref in revisions:
                revisions[artifact['analysisRef']] = base_ref
        progress(self.turn, record)

    async def publish(self, analysisRef, claims=None, viewPlan=None, requestKey=None,
                      thresholds=None, hypotheses=None, findingPlan=None, actionPlan=None, technicalDetails=None,
                      presentation=None, workRefs=None, partialReason=None, repairRef=None, repairs=None, previewOnly=False, document=None, renderCheckUnavailableReason=None, finalize=False):
        from .publication import repair_request
        if finalize:
            if previewOnly:
                raise AnalysisError('INVALID_PUBLICATION', 'finalize 与 previewOnly 不能同时为 true。')
            # 将机械确认合并为一次调用；审查和页面检查仍走原有链路及缓存。
            # 有错误、审查未执行或不可用时只返回保存草稿，不自动宣称完成。
            preview = await self.publish(analysisRef=analysisRef, requestKey=requestKey,
                claims=claims, viewPlan=viewPlan, thresholds=thresholds, hypotheses=hypotheses,
                findingPlan=findingPlan, actionPlan=actionPlan, technicalDetails=technicalDetails,
                presentation=presentation, workRefs=workRefs, partialReason=partialReason,
                repairRef=repairRef, repairs=repairs, document=document,
                renderCheckUnavailableReason=renderCheckUnavailableReason, previewOnly=True)
            diagnostics = preview.get('diagnostics', {})
            if (diagnostics.get('semanticReview', {}).get('status') == 'PASS'
                    and not diagnostics.get('publicationCheck')
                    and preview.get('nextAction') == 'REVIEW_AND_CONFIRM'):
                published = await self.publish(**preview['confirmPublish'])
                published['automaticFinalization'] = {'status': 'PUBLISHED',
                    'semanticReview': 'PASS', 'previewRef': preview['repairRef'],
                    'renderChecks': diagnostics.get('renderChecks', {}).get('status')}
                return published
            preview['automaticFinalization'] = {'status': 'AWAITING_REPAIR_OR_REVIEW'}
            preview['next'] = ('尚未发布。根据 diagnostics 修复实质问题，复用 repairAndFinalize 提交；'
                               'NOT_REVIEWED/UNAVAILABLE 不是通过，按实际未执行原因恢复检查，不把确认发布当作修复。')
            return preview
        await self.authorize()
        artifact = self.store.get_analysis(self.owner, analysisRef)
        options = dict(document=document, claims=claims, viewPlan=viewPlan, thresholds=thresholds, hypotheses=hypotheses,
                       findingPlan=findingPlan, actionPlan=actionPlan, technicalDetails=technicalDetails,
                       presentation=presentation, workRefs=workRefs, partialReason=partialReason, renderCheckUnavailableReason=renderCheckUnavailableReason)
        if repairRef is not None:
            repair_started = perf_counter()
            if any(v is not None for v in options.values()):
                raise AnalysisError('INVALID_REPAIR', '修复时只传 analysisRef、requestKey、repairRef、repairs，并用 previewOnly=true 继续检查；其他内容沿用保存的草案，版本由服务管理。')
            saved = self.store.get_publication_proposal(self.owner, repairRef)
            # 冻结草案保存的是 HTML。允许用工作区文件更新该来源，仍按原有
            # 路径边界读取，并冻结成新 HTML；不要求模型重传整页与绑定。
            if repairs and any(change.get('path') == '/document/path' for change in repairs):
                from .free_document import freeze_source
                normalized = []
                for change in repairs:
                    if change.get('path') == '/document/path' and change.get('op') in ('add', 'replace'):
                        updated = {**saved['document'], 'path': change.get('value')}
                        updated.pop('html', None)
                        frozen = freeze_source(updated, self.delivery.root if self.delivery else None)
                        change = {'op': 'replace', 'path': '/document/html', 'value': frozen['html']}
                    normalized.append(change)
                repairs = normalized
            request = repair_request(saved, analysisRef, repairs)
            if repairs:
                from .timing import record_timing
                record_timing(self.turn, 'publicationRepair', (perf_counter() - repair_started) * 1000, includedIn='publication',
                              repairCount=len(repairs), textRepairCount=sum(
                                  change.get('op') == 'replaceText' for change in repairs),
                              evidenceRebuilt=False)
        else:
            if repairs is not None: raise AnalysisError('INVALID_REPAIR', 'repairs 必须配合 repairRef。')
            request = {**options, 'analysisRef': analysisRef}
            request['technicalDetails'] = technicalDetails if technicalDetails is not None else False
        try:
            await self.validate_publication_input({k: v for k, v in {**request, 'requestKey': requestKey}.items() if v is not None})
            if previewOnly and self.turn is not None:
                from .grouping_policy import grouping_issues
                issues = grouping_issues(self.turn, artifact)
                if issues:
                    # 复用现有完成条件，提前反馈取数缺口，避免先审整页再发现缺聚合数据。
                    raise AnalysisError('GROUPING_RESULT_REQUIRED', issues[0]['message'], {'issues': issues})
            if request.get('document') is not None:
                from .free_document import freeze_source
                request['document'] = freeze_source(request['document'], self.delivery.root if self.delivery else None)
            if previewOnly and artifact['publicationState'] == 'DRAFT':
                continuation = self.store.continuation_revision(self.owner, analysisRef)
                if continuation != artifact['baseRevision']:
                    # 摘要发布后继续旧草稿：保留事实、计算和当前页面，仅新建版本基准。
                    draft = deepcopy(artifact)
                    draft.pop('hostSummary', None)
                    draft['baseAnalysisRef'] = analysisRef
                    resume_request = {'kind': 'CONTINUE_AFTER_SUMMARY', 'baseAnalysisRef': analysisRef,
                                      'baseRevision': continuation}
                    artifact = self.store.build(self.owner, resume_request, draft,
                                                'resume-' + digest(resume_request), analysisRef)
                    self._record_presentation_revision(artifact)
                    analysisRef = artifact['analysisRef']
                    request['analysisRef'] = analysisRef
            if artifact['publicationState'] == 'DRAFT':
                # 基准来自不可变的服务端版本；模型不提交或推算版本号。
                self.store.check_publication_base(self.owner, analysisRef, artifact['baseRevision'])
            await self.ensure_verification(artifact)
            if previewOnly:
                artifact = self._with_captured_verification(artifact)
                analysisRef = artifact['analysisRef']
                request['analysisRef'] = analysisRef
            if previewOnly and artifact['publicationState'] == 'PUBLISHED':
                # 已发布版本不可覆盖。仅修展示时由宿主创建数据完全相同的草稿，
                # 不重新适配、取数或执行分析代码；发布时仍检查当前 head。
                draft = deepcopy(artifact)
                for field in ('publishedAt', 'freeDocument', 'presentation', 'workRefs', 'partialReason',
                              'renderCheckUnavailableReason', 'actionRunRef', 'hostSummary'):
                    draft.pop(field, None)
                draft.update(claims=[], viewPlan=[], findings=[], actions=[], technicalDetails=False)
                draft['baseAnalysisRef'] = analysisRef
                # 已发布版本先创建展示修订时，也将同轮新核验一并登记；不多建中间草稿。
                draft.update(self._captured_verification_updates(draft))
                for field in ('workRefs', 'partialReason'):
                    if request.get(field) is None and field in artifact:
                        request[field] = deepcopy(artifact[field])
                fork_request = {'kind': 'PRESENTATION', 'baseAnalysisRef': analysisRef, 'content': request}
                draft = self.store.build(self.owner, fork_request, draft, 'presentation-' + digest(fork_request), analysisRef)
                analysisRef, artifact = draft['analysisRef'], draft
                request['analysisRef'] = analysisRef
                # 展示修订由宿主实际创建并登记，不能再要求模型空 Build 一次。
                self._record_presentation_revision(draft, created=True)
            result = await self._publish_content(requestKey=requestKey, previewOnly=previewOnly,
                                                 expectedBaseRevision=artifact['baseRevision'], **request)
            if previewOnly:
                proposed = result.pop('_reviewArtifact')
                result.pop('document')
                # 对外展示投影省略了数据集；排序覆盖校验必须使用完整的冻结文档，
                # 否则预览会漏报缺少标记，直到正式发布才返回同一个问题。
                preview_document = proposed.get('freeDocument')
                preview_ref = self.store.save_publication_proposal(self.owner, analysisRef, request)
                from .publication import repair_fields
                result['repairFields'] = repair_fields(request)
                async def review_preview():
                    if self.preview_reviewer:
                        try:
                            verdict = await self._review_publication(proposed)
                            if verdict['issues']:
                                from .document_repair import attach_text_targets
                                verdict = {**verdict, 'issues': attach_text_targets(verdict['issues'], request.get('document'), preview_document)}
                            result['diagnostics']['semanticReview'] = {'status': 'REVISE' if verdict['issues'] else 'PASS', **verdict}
                        except AnalysisError as review_error:
                            result['diagnostics']['semanticReview'] = {'status': 'UNAVAILABLE',
                                'error': review_error.as_dict()}
                            if self.turn and review_error.code in ('REVIEW_CAPACITY', 'REVIEW_UNAVAILABLE', 'ANALYSIS_RETRY_LIMIT'):
                                from .review_control import request_closeout
                                request_closeout(self.turn, review_error.code)
                if preview_document:
                    # 页面检查可直接发现脚本、数值比例和排序标记问题。先反馈这些
                    # 可确定的问题，避免对尚不能发布的页面消耗昂贵的内容审查额度。
                    await self._automatic_preview_checks(result, request, preview_ref, preview_document)
                render_state = result['diagnostics'].get('renderService', {}).get('status')
                if preview_document and render_state in ('NOT_CONFIGURED', 'UNAVAILABLE') and not request.get('renderCheckUnavailableReason'):
                    # 宿主实际探测到不可用，不把禁用的浏览器 MCP 作为必经后备路径。
                    request['renderCheckUnavailableReason'] = '宿主页面测量未配置或暂不可用，本次未完成视觉测量。'
                    result['diagnostics']['renderChecks']['unavailableReason'] = request['renderCheckUnavailableReason']
                    preview_ref = self.store.save_publication_proposal(self.owner, analysisRef, request)
                if preview_document:
                    from .document_checks import require_publication_checks
                    try:
                        require_publication_checks(preview_document,
                            self.store.document_checks(self.owner, analysisRef, preview_document['contentDigest']),
                            request.get('renderCheckUnavailableReason'),
                            self.store.document_check_requirements(self.owner, analysisRef))
                    except AnalysisError as check_error:
                        result['diagnostics']['publicationCheck'] = check_error.as_dict()
                check_error = result['diagnostics'].get('publicationCheck')
                if check_error:
                    # 内容曾经通过、随后只改展示时，继续保留已有审查结论。
                    # 只查当前证据、文字与绑定的缓存，不能为错误页面再请求模型。
                    verdict = None
                    if self.turn is not None:
                        from .turn_completion import document_review
                        _, verdict = document_review(self.turn, proposed)
                    if verdict is not None:
                        result['diagnostics']['semanticReview'] = {
                            'status': 'REVISE' if verdict['issues'] else 'PASS', **verdict,
                        }
                    else:
                        result['diagnostics']['semanticReview'] = {
                            'status': 'NOT_REVIEWED', 'reason': 'PUBLICATION_CHECK_REQUIRED',
                            'message': '先修复页面检查问题，再核对分析内容；本次未消耗内容审查额度。',
                        }
                else:
                    await review_preview()
                from urllib.parse import urlencode, quote
                result.update(repairRef=preview_ref, previewUrl='/workspace/analyses/preview?' + urlencode({
                    'agent_id': self.owner.agent_id, 'session_id': self.owner.session_id, 'preview_ref': preview_ref}),
                    previewPage='/analysis-preview/' + '/'.join(quote(v, safe='') for v in (self.owner.agent_id, self.owner.session_id, preview_ref)),
                    confirmPublish={'analysisRef': analysisRef,
                                    'repairRef': preview_ref, 'repairs': [], 'requestKey': 'confirm-' + preview_ref},
                    next='查看实际预览；如无修改，直接使用 confirmPublish 作为完整参数正式发布，不再附 document/workRefs 等内容；改动后再次预览。',
                    repairAndFinalize={'analysisRef': analysisRef, 'repairRef': preview_ref,
                        'repairs': [], 'requestKey': 'repair-' + preview_ref, 'finalize': True},
                    repairHint='textRepairable=true 的文字问题优先把所有相关修改一次放入 repairs：{op:"replaceText",path:"/document/html",value:{oldText:textTargets原文片段,newText:修正文字,textRef:textTargets引用}}。使用 textTargets 的 oldText/textRef；原句跨标签时只修改必要片段并保留其中占位符；可补充引用页面已有绑定。正文、摘要和图注一起修正；依据 semanticReview.repairEvidence，不重查重算。textRepairable=false 不试猜跨标签或动态脚本文字，直接编辑当前文件，用 /document/path 刷新；已有 documentEditSource 时以该源为准。bindings/datasets 沿用。')
                if self.delivery and repairs and any(change.get('op') == 'replaceText' for change in repairs):
                    result['documentEditSource'] = self.delivery.save_edit_source(request['document']['html'])
                if self.web_ui_url:
                    result['previewPage'] = self.web_ui_url.rstrip('/') + result['previewPage']
                checks = result['diagnostics'].get('renderChecks')
                result['nextAction'] = ('REPAIR_DOCUMENT' if checks and checks['status'] == 'ERRORS' else
                                       'RUN_RENDER_CHECKS' if checks and not checks['measurementRequirementMet']
                                        and not checks.get('unavailableReason') else 'REVIEW_AND_CONFIRM')
                if (result['diagnostics'].get('semanticReview', {}).get('status') == 'REVISE'
                        or result['diagnostics'].get('publicationCheck', {}).get('code') not in (None, 'DOCUMENT_NOT_CHECKED')):
                    result['nextAction'] = 'REPAIR_DOCUMENT'
                elif result['diagnostics'].get('semanticReview', {}).get('status') == 'UNAVAILABLE':
                    from .review_control import closeout_reason
                    result['nextAction'] = 'HOST_CLOSEOUT' if self.turn and closeout_reason(self.turn) else 'RETRY_SAVED_PREVIEW'
                if checks and checks.get('measurementRequirementMet'):
                    result['next'] = '宿主已完成页面测量；如无实质问题，将 confirmPublish 的对象内容直接作为 AnalysisPublish 的完整参数（不要增加 confirmPublish 外层字段）。有实质问题时局部修复；无需再调用浏览器 MCP 重复测量。'
                elif checks and checks.get('unavailableReason'):
                    result['next'] = '页面测量保留未检查状态；不要启用禁用的浏览器或重写页面。存在已发现的实质错误时修复，否则按 confirmPublish 交付。'
                if self.turn is not None:
                    from .closeout_content import evidence_digest
                    from .review_control import progress
                    proof = {'analysisRef': analysisRef, 'previewRef': preview_ref,
                             'evidenceDigest': evidence_digest(artifact),
                             'contentDigest': (preview_document or {}).get('contentDigest'),
                             'publicationCheckCode': result['diagnostics'].get('publicationCheck', {}).get('code'),
                             'semanticReview': result['diagnostics'].get('semanticReview', {}).get('status', 'NOT_REVIEWED')}
                    progress(self.turn, lambda state: state.update(lastPublicationPreview=proof))
            return result
        except AnalysisError as exc:
            if exc.code in ('ACCESS_DENIED', 'OWNER_MISMATCH'): raise
            if self.store.get_analysis(self.owner, analysisRef)['publicationState'] != 'DRAFT': raise
            details = dict(exc.details or {})
            try: proposal_ref = self.store.save_publication_proposal(self.owner, analysisRef, request)
            except AnalysisError as storage_error:
                details['repairUnavailable'] = storage_error.as_dict()
                raise AnalysisError(exc.code, str(exc), details) from exc
            details.update(repairRef=proposal_ref, analysisRef=analysisRef,
                           preservedViewIds=[v.get('viewId') for v in request.get('viewPlan') or [] if isinstance(v, dict)])
            from .publication import repair_fields, recovery_details
            details['repairFields'] = repair_fields(request)
            details = recovery_details(exc.code, details)
            if exc.code == 'VERSION_CONFLICT' and details['nextAction'] == 'RETRY_PREVIEW':
                details['retryPreview'] = {'analysisRef': analysisRef,
                                          'repairRef': proposal_ref, 'repairs': [], 'previewOnly': True,
                                          'requestKey': 'resume-preview-' + proposal_ref}
            if exc.code == 'DOCUMENT_NOT_CHECKED':
                from urllib.parse import quote
                details.update(previewPage='/analysis-preview/' + '/'.join(quote(v, safe='') for v in (self.owner.agent_id, self.owner.session_id, proposal_ref)),
                               confirmPublish={'analysisRef': analysisRef,
                                               'repairRef': proposal_ref, 'repairs': [], 'requestKey': 'confirm-' + proposal_ref},
                               repairHint='页面内容已保留；先完成实际浏览器检查，再复用 confirmPublish，无需重写页面或重建数据。')
                if self.web_ui_url:
                    details['previewPage'] = self.web_ui_url.rstrip('/') + details['previewPage']
            raise AnalysisError(exc.code, str(exc), details) from exc

    async def _publish_content(self, analysisRef, claims=None, viewPlan=None, requestKey=None, expectedBaseRevision=None, thresholds=None,
                               hypotheses=None, findingPlan=None, actionPlan=None, technicalDetails=False,
                               presentation=None, workRefs=None, partialReason=None, previewOnly=False, document=None, renderCheckUnavailableReason=None, host_summary=None):
        from .publication import validate_content, located, raise_issues
        from .completion import completion_summary
        artifact = self.store.get_analysis(self.owner, analysisRef)
        if expectedBaseRevision is None:
            expectedBaseRevision = artifact['baseRevision']
        if self.turn is not None:
            from .followup_scope import validate_primary
            validate_primary(self.turn, artifact)
        if artifact.get("schemaVersion") != SCHEMA_VERSION:
            raise AnalysisError("UNSUPPORTED_VERSION", "不支持该分析协议版本。")
        self._check_draft_rules(artifact)
        if previewOnly and (artifact['publicationState'] != 'DRAFT' or expectedBaseRevision != artifact['baseRevision']):
            raise AnalysisError('PREVIEW_SCOPE', '预览须引用当前草稿及其基础版本。')
        if artifact['publicationState'] == 'DRAFT':
            self.store.check_publication_base(self.owner, analysisRef, expectedBaseRevision)
        if type(technicalDetails) is not bool:
            raise AnalysisError('INVALID_VIEW', '技术详情开关必须为布尔值。')
        proposed = deepcopy(artifact)
        if host_summary:
            proposed.setdefault('hostSummary', {'reason': host_summary, 'content': 'SOURCE_FACTS_ONLY',
                                                'semanticReview': 'UNAVAILABLE'})
        else:
            # 对摘要进行新的人工/模型修订时，必须重新审查当前内容，不能
            # 沿用此前兜底正文及其通过状态。
            proposed.pop('hostSummary', None)
        proposed['technicalDetails'] = technicalDetails
        issues = []
        if renderCheckUnavailableReason is not None:
            if document is None or not isinstance(renderCheckUnavailableReason, str) or not renderCheckUnavailableReason.strip() or len(renderCheckUnavailableReason) > 1000:
                issues.append({'code': 'INVALID_PREVIEW_CHECK', 'message': 'renderCheckUnavailableReason 仅用于自由页面，说明浏览器检查确实不可用的具体原因。', 'path': '/renderCheckUnavailableReason'})
            else: proposed['renderCheckUnavailableReason'] = renderCheckUnavailableReason
        if workRefs is not None: proposed['workRefs'] = deepcopy(workRefs)
        if partialReason is not None:
            if not isinstance(partialReason, str) or not partialReason.strip() or len(partialReason) > 2000:
                issues.append({'code': 'INVALID_WORK_PLAN', 'message': 'partialReason 必须说明本次仍未完成的具体内容。', 'path': '/partialReason'})
            else: proposed['partialReason'] = partialReason
        # Validate task references before the free document consumes them.
        try: completion_summary(proposed)
        except AnalysisError as exc: issues.append(located(exc, '/workRefs'))
        if self.turn is not None and not issues and not host_summary:
            # 先反馈真实数据缺口，不能昂贵地审完页面后才发现同一完成条件不满足。
            _, data_issues, _ = self.turn.data_issues(proposed)
            issues.extend(data_issues)
        raise_issues(issues)
        if any(not f.get("objectLabel") or f.get("objectLabelState") == "CONFLICT" for f in artifact["facts"] if f["valueState"] != "NOT_QUERIED"):
            issues.append({'code': 'OBJECT_LABEL_REQUIRED', 'message': '部分对象缺少唯一名称；从 entities 或 inventory.objectLabelCaptureRefs 选择同源名称捕获后重新构建。',
                           'path': '/captureRefs', 'nextSection': 'entities', 'requiresBuild': True,
                           'objectIds': sorted({f['objectId'] for f in artifact['facts'] if f.get('objectLabelState') != 'RESOLVED'})[:100]})
        try:
            if document is not None:
                if viewPlan is not None or presentation is not None:
                    raise AnalysisError('INVALID_DOCUMENT', '自由页面与固定组件展示参数不能混传。', {'path':'/document'})
                from .free_document import compile_document
                proposed.update(validate_content(artifact, claims or [], [], None))
                proposed['freeDocument'] = compile_document(proposed, document)
            else:
                proposed.update(validate_content(artifact, claims, viewPlan, presentation))
        except AnalysisError as exc: issues.extend((exc.details or {}).get('issues', [located(exc, '')]))
        try:
            completion = completion_summary(proposed)
            if completion['state'] == 'INCOMPLETE' and not proposed.get('partialReason'):
                issues.append({'code': 'INCOMPLETE_WORK_PLAN', 'message': '已声明工作项尚未完成；补齐真实引用，或以 partialReason 明确本次只交付部分结果。',
                               'path': '/workRefs', 'pendingTasks': [t for t in completion['items'] if t['state'] != 'READY'],
                               'hint': '已有计算用 derivedId；枚举关系用 relationRef。用 workRefs 更新现有 taskId 的 refs，无需重新 Build；不得删除待办来宣布完成。'})
        except AnalysisError as exc: issues.append(located(exc, '/workRefs'))
        raise_issues(issues)
        from .validation import validate_report_topics
        from .presentation import project, page
        if self.actions_enabled and not host_summary:
            # Use frozen output on a retry. Capability churn must not change
            # a previously published proposal or report.
            if artifact["publicationState"] != "PUBLISHED":
                base = self._finding_base(artifact)
                proposed["findings"] = findings.generate(artifact, self.rules, thresholds, artifact["findingRules"], hypotheses, base, plan=findingPlan)
                proposed["actions"] = proposals(proposed, await self.actions.capabilities(), plan=actionPlan)
                proposed["findingPlan"] = deepcopy(findingPlan or [])
                proposed["actionPlan"] = deepcopy(actionPlan or [])
        elif thresholds or hypotheses or findingPlan or actionPlan:
            raise AnalysisError("ACTIONS_DISABLED", "发现与追查功能尚未启用。")
        publication_view = project({**proposed, 'publicationState': 'PUBLISHED', 'publishedAt': '预览（尚未发布）' if previewOnly else now()}, self.rules)
        validate_report_topics(proposed, published=True, document=publication_view)
        if artifact["publicationState"] != "PUBLISHED":
            # Validate the content before publishing; only transient file delivery
            # failures may leave a published result awaiting registration.
            page(publication_view)
        from .display_review import review
        if document is not None:
            from .free_document import diagnostics as document_diagnostics
            from .document_checks import summary as check_summary, require_publication_checks
            diagnostics = document_diagnostics(proposed['freeDocument'])
            records = self.store.document_checks(self.owner, analysisRef, proposed['freeDocument']['contentDigest'])
            reason = proposed.get('renderCheckUnavailableReason')
            diagnostics['renderChecks'] = check_summary(records, reason)
            if not previewOnly and artifact['publicationState'] == 'DRAFT':
                require_publication_checks(proposed['freeDocument'], records, reason,
                                           self.store.document_check_requirements(self.owner, analysisRef))
        else:
            diagnostics = review(proposed, publication_view)
        if self.turn is not None:
            from .review_control import progress
            def record_wait(state):
                waits = state.setdefault('publicationWaits', {})
                waits[requestKey] = {'analysisRef': analysisRef,
                                    'workReady': completion['state'] in ('READY', 'UNDECLARED')
                                    and proposed['scope'].get('selectedDataComplete', False)}
                while len(waits) > 32:
                    waits.pop(next(iter(waits)))
            progress(self.turn, record_wait)
        if previewOnly:
            return {'schemaVersion': SCHEMA_VERSION, 'analysisRef': analysisRef, 'previewOnly': True,
                    'publicationState': 'DRAFT', 'document': publication_view,
                    '_reviewArtifact': proposed, 'diagnostics': diagnostics, 'summary': summary(proposed, publication_view)}
        if self.preview_reviewer and artifact['publicationState'] == 'DRAFT' and not host_summary:
            verdict = await self._review_publication(proposed)
            if verdict['issues']:
                from .document_repair import attach_text_targets
                verdict = {**verdict, 'issues': attach_text_targets(verdict['issues'], document, proposed.get('freeDocument'))}
                raise AnalysisError('DOCUMENT_SEMANTIC_ERRORS', '页面内容与实际证据存在实质不一致，须先修正再发布。',
                                    {'path': '/document', **verdict, 'requiresBuild': False})
        request = {"analysisRef": analysisRef, "claims": claims, "viewPlan": viewPlan, "expectedBaseRevision": expectedBaseRevision}
        if thresholds is not None or hypotheses is not None:
            request.update(thresholds=thresholds, hypotheses=hypotheses)
        if findingPlan is not None or actionPlan is not None:
            request.update(findingPlan=findingPlan, actionPlan=actionPlan)
        request['technicalDetails'] = technicalDetails
        request['presentation'] = presentation
        if document is not None: request['document'] = document
        if workRefs is not None: request['workRefs'] = workRefs
        if partialReason is not None: request['partialReason'] = partialReason
        if renderCheckUnavailableReason is not None: request['renderCheckUnavailableReason'] = renderCheckUnavailableReason
        from .review_control import measure_stage
        with measure_stage(self.turn, 'artifactStorage'):
            result = self.store.publish(self.owner, analysisRef, proposed, request, requestKey, expectedBaseRevision)
        delivery = {"type": "tool_markdown", "registered": True, "chartOrReportCreated": False}
        if self.delivery is not None:
            try:
                with measure_stage(self.turn, 'deliveryRegistration'):
                    delivery = self.delivery.ensure(result["analysisRef"])
            except (AnalysisError, OSError) as exc:
                delivery = {"type": "workspace_analysis", "registered": False, "error": exc.as_dict() if isinstance(exc, AnalysisError) else {"code": "DELIVERY_IO", "message": "展示文件暂不可用，请重试交付。"}}
        return {"schemaVersion": SCHEMA_VERSION, "analysisRef": result["analysisRef"], "revision": result["revision"],
                **public_states(result), "summary": summary(result, publication_view),
                "delivery": delivery, "diagnostics": diagnostics}

    async def _automatic_preview_checks(self, result, request, preview_ref, document):
        from .review_control import measure_stage
        with measure_stage(self.turn, 'pageChecks'):
            return await self._run_preview_checks(result, request, preview_ref, document)

    async def _run_preview_checks(self, result, request, preview_ref, document):
        """Measure a frozen preview once; preserve errors without changing content."""
        from .document_checks import evaluate, summary as check_summary
        from .free_document import compile_document
        checks = result['diagnostics'].get('renderChecks', {})
        started = perf_counter()
        if not self.render_checker:
            result['diagnostics']['renderService'] = {'status': 'NOT_CONFIGURED'}
            return
        if checks.get('measurementRequirementMet'):
            result['diagnostics']['renderService'] = {'status': 'REUSED'}
            return
        try:
            observations = await self.render_checker.check(document)
            # Recheck ownership after the external wait; only this owner's frozen
            # draft can receive observations. Validate all records before writing.
            await self.authorize()
            proposed = deepcopy(self.store.get_analysis(self.owner, request['analysisRef']))
            proposed.update({key: request[key] for key in ('workRefs', 'partialReason') if request.get(key) is not None})
            full = compile_document(proposed, request['document'])
            if full['contentDigest'] != document['contentDigest']:
                raise AnalysisError('STALE_PREVIEW_CHECK', '预览已变化，请重新检查。')
            records = []
            for sample in observations:
                record = evaluate(full, {key: value for key, value in sample.items() if key != 'hostRenderedText'})
                if 'hostRenderedText' in sample:
                    from .render_evidence import validate_snapshot
                    record['hostRenderedText'] = validate_snapshot(sample['hostRenderedText'])
                records.append(record)
            if len({r['viewport']['width'] for r in records}) < 2:
                raise AnalysisError('INVALID_PREVIEW_CHECK', '自动检查没有取得两个不同宽度的有效记录。')
            for record in records:
                record.update(previewRef=preview_ref, runner='headless-service')
                self.store.save_document_check(self.owner, request['analysisRef'], record)
            result['diagnostics']['renderChecks'] = check_summary(
                self.store.document_checks(self.owner, request['analysisRef'], full['contentDigest']),
                request.get('renderCheckUnavailableReason'))
            result['diagnostics']['renderService'] = {'status': 'COMPLETED',
                                                      'elapsedMs': round((perf_counter() - started) * 1000, 2)}
        except AnalysisError as exc:
            if exc.code in ('ACCESS_DENIED', 'OWNER_MISMATCH'): raise
            result['diagnostics']['renderService'] = {'status': 'UNAVAILABLE', 'error': exc.as_dict()}

    async def preview_content(self, preview_ref):
        from .publication import schema_issues
        saved = self.store.get_publication_proposal(self.owner, preview_ref)
        issues = schema_issues({k: v for k, v in {**saved, 'requestKey': 'preview-read'}.items() if v is not None})
        if issues:
            raise AnalysisError('INVALID_PUBLICATION', '该草案仍需按错误路径修正后预览。', {'issues': issues})
        result = await self._publish_content(requestKey='preview-read', previewOnly=True, **saved)
        result.pop('_reviewArtifact', None)
        return result

    async def record_preview_check(self, preview_ref, content_digest, frame_key, measurements):
        await self.authorize()
        from .document_checks import evaluate
        result = await self.preview_content(preview_ref)
        doc = result['document'].get('freeDocument')
        if not doc or doc['contentDigest'] != content_digest or doc['frameKey'] != frame_key:
            raise AnalysisError('STALE_PREVIEW_CHECK', '检查记录与当前预览内容不一致；重新加载并检查。')
        saved = self.store.get_publication_proposal(self.owner, preview_ref)
        from .free_document import compile_document
        proposed = deepcopy(self.store.get_analysis(self.owner, result['analysisRef']))
        proposed.update({key: saved[key] for key in ('workRefs', 'partialReason') if saved.get(key) is not None})
        full = compile_document(proposed, saved['document'])
        record = evaluate(full, measurements)
        record['previewRef'] = preview_ref
        self.store.save_document_check(self.owner, result['analysisRef'], record)
        return record

    def _finding_base(self, artifact):
        return self.store.get_analysis(self.owner, artifact["baseAnalysisRef"]) if artifact.get("baseAnalysisRef") else None

    def _check_draft_rules(self, artifact):
        if artifact["publicationState"] != "PUBLISHED" and artifact["ruleVersion"] != self.rules.version:
            raise AnalysisError("RULE_VERSION_UNAVAILABLE", "草稿计算时的规则已变化；请按当前规则重新 Build，不能用新规则解释旧计算。")

    async def follow_up(self, actionRef, requestKey):
        await self.authorize()
        if not self.actions_enabled:
            raise AnalysisError("ACTIONS_DISABLED", "当前会话未启用追查能力。")
        return await self.actions.follow_up(actionRef, requestKey)

    async def render_report(self, analysisRef, requestKey, viewParameters=None, layout="standard"):
        await self.authorize()
        if self.delivery is None:
            raise AnalysisError("DELIVERY_UNAVAILABLE", "当前宿主没有装配展示能力。")
        self.delivery.ensure(analysisRef)
        return self.delivery.render_report(analysisRef, requestKey, viewParameters, layout)
