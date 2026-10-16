"""Application operations shared by the three host tools."""

from __future__ import annotations

from copy import deepcopy

from .adapters import default_sources
from .engine import RuleRegistry, calculate, _comparable
from .models import AnalysisError, ENGINE_VERSION, SCHEMA_VERSION, Owner, bounded_page, digest, now
from .store import AnalysisStore
from .validation import public_states, summary, validate_claims, validate_views
from . import findings
from .actions import ACTION_CHECKS, ActionRuntime, proposals, validate_plan, source_identity


def verification(facts, captures, verifier_refs=None, sources=None):
    from .adapters import default_sources
    return (sources or default_sources()).verify(facts, captures, verifier_refs)


def compact_derived(values, limit=100):
    result = []
    for value in values:
        compact = dict(value)
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
            trace["nodes"] = [{k: node[k] for k in ("factRef", "status", "observed", "recalculated", "difference", "localCheck", "propagatedDifference", "differenceOriginRefs")} for node in trace["nodes"]]
            for node in trace['nodes']:
                node['differenceOriginRefsTotal'] = len(node['differenceOriginRefs'])
                node['differenceOriginRefs'] = node['differenceOriginRefs'][:10]
            trace["detailsSection"] = "formulas"
            compact["formulaTrace"] = trace
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

    async def read_query_result(self, captureRef, pointer='', offset=0, limit=20, search=None, fields=None):
        from .query_results import read_receipt
        await self.authorize()
        capture = self.store.get_capture(self.owner, captureRef)
        return read_receipt(self.sources.for_capture(capture).raw_receipt(capture['response']), captureRef, pointer, offset, limit, search, fields)

    async def build(self, captureRefs, selections, scope, operations, requestKey, baseAnalysisRef=None, actionRunRef=None, workPlan=None):
        await self.authorize()
        request = {"captureRefs": captureRefs, "selections": selections, "scope": scope,
                   "operations": operations, "baseAnalysisRef": baseAnalysisRef, "engineVersion": ENGINE_VERSION,
                   "ruleVersion": self.rules.version, "skillVersions": self.skill_versions}
        request['workPlan'] = deepcopy([] if workPlan is None else workPlan)
        if actionRunRef:
            request["actionRunRef"] = actionRunRef
        reused = self.store.find_request(self.owner, "build", requestKey, digest(request))
        if reused:
            return self._built(self.store.get_analysis(self.owner, reused))
        effective_work = request['workPlan']
        if baseAnalysisRef:
            from .completion import continue_plan
            base = self.store.get_analysis(self.owner, baseAnalysisRef)
            scope, effective_work = continue_plan(base, scope, workPlan)
        if not captureRefs or len(captureRefs) > self.store.limits.max_selections or len(captureRefs) != len(set(captureRefs)):
            raise AnalysisError("CAPTURE_LIMIT", "输入引用需要非空、无重复且不超过配置数量。")
        if len(selections) > self.store.limits.max_selections or len(operations) > self.store.limits.max_operations:
            raise AnalysisError("PLAN_LIMIT", "分析计划超过本次容量上限。")
        captures = {ref: self.store.get_capture(self.owner, ref) for ref in captureRefs}
        data = self.sources.adapt(captures, selections, scope, self.store.limits.max_facts)
        from .validation import validate_report_topics
        validate_report_topics(data)
        origins = bind_source_origins(data["facts"], captures)
        from .relations import captured_relations
        data['relations'] = captured_relations(captures, self.sources, data['scope'])
        verifier_refs = None
        if actionRunRef:
            if not self.actions_enabled or self.actions.active_ref != actionRunRef:
                raise AnalysisError("ACTION_NOT_RUNNING", "本回合尚未通过 AnalysisFollowUp 恢复该行动。")
            verifier_refs = set(self.store.get_action_run(self.owner, actionRunRef)["newCaptureRefs"])
        verified, checked = verification(data["facts"], captures, verifier_refs, self.sources)
        if actionRunRef:
            validate_plan(self.store, self.owner, actionRunRef, data, captures, baseAnalysisRef, verified == "VALID")
        derived = calculate(data["facts"], data["scope"], operations, self.rules, data["relations"])
        try:
            _comparable(data["facts"], data["scope"])
            comparability = "CONFIRMED"
        except AnalysisError as exc:
            comparability = "CONFLICT" if exc.code in ("UNIT_CONFLICT", "METRIC_CONFLICT") else "UNCONFIRMED"
        artifact = {"schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION, "ruleVersion": self.rules.version,
                    "skillVersions": self.skill_versions, "captureRefs": captureRefs, **data,
                    "inputDigest": digest({ref: digest(value) for ref, value in captures.items()}),
                    "planDigest": digest({"selections": selections, "scope": scope, "operations": operations, 'workPlan': effective_work}),
                    "plan": {"selections": selections, "scope": scope, "operations": operations, 'workPlan': effective_work},
                    "derivedValues": derived, "coverage": data["scope"]["coverage"], "receiptVerification": verified,
                    "verificationRecords": checked, "verificationRecordedAt": now(), "comparability": comparability,
                    "calculationValidation": "NOT_APPLICABLE" if not derived else "BLOCKED" if any(v["calculationValidation"] != "PASS" for v in derived) else "PASS",
                    "claims": [], "viewPlan": [], "findings": [], "actions": [], "presentationPolicy": "explicit", "technicalDetails": False}
        artifact["findingRules"] = self.finding_rules
        artifact["findingVersion"] = findings.FINDING_VERSION
        artifact["sourceResourceIds"] = sorted({captures[ref]["resourceId"] for fact in data["facts"] for ref in fact.get("captureRefs", [])})
        artifact["sourceOrigins"] = origins
        if baseAnalysisRef:
            artifact["baseAnalysisRef"] = baseAnalysisRef
        if actionRunRef:
            artifact["actionRunRef"] = actionRunRef
            artifact["sourceTransition"] = "SAME_CONTEXT" if data["context"] == self.store.get_analysis(self.owner, baseAnalysisRef)["context"] else "NEW_CONTEXT_INDEPENDENTLY_VERIFIED"
        from .completion import completion_summary
        completion_summary(artifact)
        if baseAnalysisRef:
            from .completion import validate_target_continuity
            validate_target_continuity(base, artifact)
        artifact = self.store.build(self.owner, request, artifact, requestKey, baseAnalysisRef)
        return self._built(artifact)

    def _built(self, artifact):
        from .completion import completion_summary
        tasks = {}
        if artifact['receiptVerification'] != 'VALID':
            for fact in artifact['facts']:
                for ref in fact.get('captureRefs', []):
                    capture = self.store.get_capture(self.owner, ref)
                    task = self.sources.for_capture(capture).verification_task(fact, capture)
                    if task is None:
                        continue
                    key = (source_identity(capture), task.get('sourceContextId'), task['executionId'])
                    saved = tasks.setdefault(key, {**task, 'evidenceIds': set()})
                    saved['evidenceIds'].update(task['evidenceIds'])
        verification_tasks = [{**t, 'evidenceIds': sorted(t['evidenceIds'])} for t in tasks.values()]
        return {"schemaVersion": SCHEMA_VERSION, "analysisRef": artifact["analysisRef"],
                "baseRevision": artifact["baseRevision"], "scope": artifact["scope"], **public_states(artifact),
                "factCount": len(artifact["facts"]), "derivedValues": compact_derived(artifact["derivedValues"]),
                'sourceContexts': artifact.get('sourceContexts', []),
                'verificationTasks': verification_tasks[:100], 'verificationTaskCount': len(verification_tasks),
                'relationCount': len(artifact['relations']),
                'completion': completion_summary(artifact),
                "next": ("按 verificationTasks 返回的来源、工具和 action 核验，纳入核验捕获后重新 Build；" if verification_tasks else "") +
                        "已有计算而工作项 refs 为空时，Publish 用 workRefs 补真实 derivedId，无需重算。补数据重建须以本 analysisRef 为 baseAnalysisRef，保留工作项和主体 population。facts/formulas/relations 按需读取；模型自由组织 HTML 页面，用 AnalysisPublish.document 绑定事实和计算，预览后发布。"}

    async def inspect(self, section="captures", analysisRef=None, captureRef=None, offset=0, limit=50, derivedId=None, stepId=None, search=None, findingPlan=None, thresholds=None, factRef=None, memberDimension=None, objectId=None, bindingRef=None, period=None, measureCode=None, cellKey=None, detail="compact", fields=None, previewRef=None):
        await self.authorize()
        bounded_page([], offset, limit, self.store.limits.page_size)
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
            return {"schemaVersion": SCHEMA_VERSION, "engineVersion": ENGINE_VERSION, "rendererVersion": RENDERER_VERSION,
                    "skillVersions": self.skill_versions, "rules": self.rules.rules,
                    "sourceAdapters": sorted(self.sources.factories),
                    "contextModes": ['SINGLE_SNAPSHOT', 'COMPOSITE'],
                    "capabilityPolicy": 'HOST_CONFIGURED_SKILL_GUIDED',
                    "operators": ["SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC"],
                    "viewTypes": ["METRIC", "TABLE", "BAR_VALUES", "BAR_RANK", "BRIDGE", "ORDER", "FORMULA", "HIERARCHY", "WATERFALL", "RECONCILIATION"],
                    "findingRules": self.finding_rules, "findingVersion": findings.FINDING_VERSION,
                    "findingChecks": list(findings.CHECKS), "actionChecks": list(ACTION_CHECKS),
                    "planPolicy": "EXPLICIT_ONLY",
                    "viewOptions": ["title", "description", "showUnit", "includeDetails", "nodeRefs", "collapsed", "pageSize", "numberFormat", "labels", "comparisonRef", "descriptionParts", "maxInputsPerScene", "groupId", "orderRef", "nodeDescriptions", "relationLayout", "showChecks", "emphasisRefs", "orientation"],
                    "documentPresentation": {"version":"free-html-1", "publishField":"document", "source":["path","html"],
                        "bindings":"key/valueRef/field/nodeRef/format", "textSlot":"{{key}}", "scriptData":"window.analysisData[key]",
                        "datasets":"key/valueRef/nodeRefs/format", "scriptDatasets":"window.analysisSets[key]",
                        "datasetKinds":["ORDERED", "FORMULA", "RECONCILIATION"],
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
                                          'baseRevisionRecovery':'retryPreview'},
                    'scopeMembers': {'subjects': 'members', 'comparators': 'referenceMembers', 'populationAppliesTo': 'members',
                                     'sourceQualifiedIdentity': 'inventory.memberRef', 'originalSourceId': 'sourceObjectId',
                                     'operationSelector': 'Use objectId returned by Build, or actual fact inputRefs'},
                    'workCompletion': {'updates': 'workRefs', 'rebuildFrom': 'baseAnalysisRef', 'draftBaseSupported': True, 'preservesDeclaredTasks': True, 'incompletePublicationRequires': 'partialReason',
                                       'computationScope':['operator','targetRefs'], 'missingTargets':'missingTargetRefs',
                                       'readyDoesNotMeanSourceConsistency': True},
                    'tablePagination': 'independent_per_table',
                    "deliveryCapabilities": {"cards": bool(self.delivery and self.delivery.cards_enabled),
                                             "generateReports": bool(self.delivery and self.delivery.allow_reports),
                                             "followUp": self.actions_enabled},
                    "calculationValidationMeaning": "execution_conditions_only_not_source_consistency",
                    "formulaChecks": ["localCheck", "sourceConsistency", "differenceOriginRefs", "propagatedDifference"],
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
            from .discovery import selectors
            return bounded_page(selectors(self.store.get_capture(self.owner, captureRef), self.sources), offset, limit, self.store.limits.page_size)
        if section == 'relations' and not analysisRef:
            from .inventory import session_inputs
            from .relations import captured_relations
            captures, _, counts = session_inputs(self.store, self.owner, self.sources)
            return {**bounded_page(captured_relations({c['captureRef']: c for c in captures}, self.sources), offset, limit, self.store.limits.page_size), **counts}
        if section == "captures":
            return self.store.list_captures(self.owner, offset, limit)
        if section == "capture":
            capture = self.store.get_capture(self.owner, captureRef)
            items = self.sources.catalog(capture, memberDimension)
            if stepId:
                items = [item for item in items if item["stepId"] == stepId]
            if search:
                import json
                rows = {(item["stepId"], item["bindingRef"], item["row"]) for item in items
                        if search in json.dumps(item, ensure_ascii=False)}
                items = [item for item in items if (item["stepId"], item["bindingRef"], item["row"]) in rows]
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
            return bounded_page(compact_derived(artifact["derivedValues"]), offset, limit, self.store.limits.page_size)
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
        if (isinstance(ref, str) and type(request.get('expectedBaseRevision')) is int
                and request['expectedBaseRevision'] >= 0 and not request.get('repairRef')
                and not set(request) - {'analysisRef', 'requestKey', 'expectedBaseRevision', 'previewOnly',
                                       'document', 'claims', 'viewPlan', 'presentation', 'workRefs', 'partialReason',
                                       'technicalDetails', 'findingPlan', 'actionPlan', 'thresholds', 'hypotheses', 'renderCheckUnavailableReason'}):
            artifact = self.store.get_analysis(self.owner, ref)
            if artifact['publicationState'] == 'DRAFT':
                saved = {k: v for k, v in request.items() if k not in ('requestKey', 'previewOnly')}
                details['repairRef'] = self.store.save_publication_proposal(self.owner, ref, saved)
                details['repairHint'] = '按 issues/path 修改，未修改的图表、简称和结论保持原样。'
        raise AnalysisError('INVALID_PUBLICATION', '发布参数校验未通过。', details)

    async def publish(self, analysisRef, claims=None, viewPlan=None, requestKey=None, expectedBaseRevision=None,
                      thresholds=None, hypotheses=None, findingPlan=None, actionPlan=None, technicalDetails=None,
                      presentation=None, workRefs=None, partialReason=None, repairRef=None, repairs=None, previewOnly=False, document=None, renderCheckUnavailableReason=None):
        from .publication import repair_request
        await self.authorize()
        artifact = self.store.get_analysis(self.owner, analysisRef)
        options = dict(document=document, claims=claims, viewPlan=viewPlan, thresholds=thresholds, hypotheses=hypotheses,
                       findingPlan=findingPlan, actionPlan=actionPlan, technicalDetails=technicalDetails,
                       presentation=presentation, workRefs=workRefs, partialReason=partialReason, renderCheckUnavailableReason=renderCheckUnavailableReason)
        if repairRef is not None:
            if any(v is not None for v in options.values()):
                raise AnalysisError('INVALID_REPAIR', '修复时只传 analysisRef、requestKey、expectedBaseRevision、repairRef、repairs，并用 previewOnly=true 继续检查；其他内容沿用保存的草案。')
            saved = self.store.get_publication_proposal(self.owner, repairRef)
            request = repair_request(saved, analysisRef, repairs)
            if request['expectedBaseRevision'] != expectedBaseRevision:
                raise AnalysisError('REPAIR_SCOPE', '修复不能更改基础版本。')
        else:
            if repairs is not None: raise AnalysisError('INVALID_REPAIR', 'repairs 必须配合 repairRef。')
            request = {**options, 'analysisRef': analysisRef, 'expectedBaseRevision': expectedBaseRevision}
            request['technicalDetails'] = technicalDetails if technicalDetails is not None else False
        try:
            await self.validate_publication_input({k: v for k, v in {**request, 'requestKey': requestKey}.items() if v is not None})
            if request.get('document') is not None:
                from .free_document import freeze_source
                request['document'] = freeze_source(request['document'], self.delivery.root if self.delivery else None)
            result = await self._publish_content(requestKey=requestKey, previewOnly=previewOnly, **request)
            if previewOnly:
                preview_document = result.pop('document').get('freeDocument')
                preview_ref = self.store.save_publication_proposal(self.owner, analysisRef, request)
                from .publication import repair_fields
                result['repairFields'] = repair_fields(request)
                if preview_document:
                    await self._automatic_preview_checks(result, request, preview_ref, preview_document)
                from urllib.parse import urlencode, quote
                result.update(repairRef=preview_ref, previewUrl='/workspace/analyses/preview?' + urlencode({
                    'agent_id': self.owner.agent_id, 'session_id': self.owner.session_id, 'preview_ref': preview_ref}),
                    previewPage='/analysis-preview/' + '/'.join(quote(v, safe='') for v in (self.owner.agent_id, self.owner.session_id, preview_ref)),
                    confirmPublish={'analysisRef': analysisRef, 'expectedBaseRevision': expectedBaseRevision,
                                    'repairRef': preview_ref, 'repairs': [], 'requestKey': 'confirm-' + preview_ref},
                    next='查看实际预览；如无修改，直接使用 confirmPublish 作为完整参数正式发布，不再附 document/workRefs 等内容；改动后再次预览。')
                checks = result['diagnostics'].get('renderChecks')
                result['nextAction'] = ('REPAIR_DOCUMENT' if checks and checks['status'] == 'ERRORS' else
                                       'RUN_RENDER_CHECKS' if checks and not checks['measurementRequirementMet']
                                        and not checks.get('unavailableReason') else 'REVIEW_AND_CONFIRM')
            return result
        except AnalysisError as exc:
            if exc.code in ('ACCESS_DENIED', 'OWNER_MISMATCH'): raise
            if self.store.get_analysis(self.owner, analysisRef)['publicationState'] != 'DRAFT': raise
            details = dict(exc.details or {})
            if exc.code == 'PREVIEW_SCOPE':
                # Preserve authored content in a NEW proposal with the draft's actual base.
                # Never retarget the analysis or silently publish against another revision.
                request = {**request, 'expectedBaseRevision': artifact['baseRevision']}
            try: proposal_ref = self.store.save_publication_proposal(self.owner, analysisRef, request)
            except AnalysisError as storage_error:
                details['repairUnavailable'] = storage_error.as_dict()
                raise AnalysisError(exc.code, str(exc), details) from exc
            details.update(repairRef=proposal_ref, analysisRef=analysisRef,
                           preservedViewIds=[v.get('viewId') for v in request.get('viewPlan') or [] if isinstance(v, dict)],
                           repairHint='按 issues/path 局部修复：再次调用 AnalysisPublish，仅传身份、幂等键、基础版本、repairRef 和 repairs；未修改的图表、简称与结论保持原样。只有缺数据或名称捕获才重新 Build。')
            from .publication import repair_fields
            details['repairFields'] = repair_fields(request)
            details['requiresBuild'] = any(issue.get('requiresBuild') is True for issue in details.get('issues', [details]))
            details.setdefault('nextAction', 'BUILD_WITH_REQUIRED_INPUTS' if details['requiresBuild'] else 'REPAIR_SAVED_PROPOSAL')
            if exc.code == 'DOCUMENT_NOT_CHECKED':
                from urllib.parse import quote
                details.update(previewPage='/analysis-preview/' + '/'.join(quote(v, safe='') for v in (self.owner.agent_id, self.owner.session_id, proposal_ref)),
                               confirmPublish={'analysisRef': analysisRef, 'expectedBaseRevision': expectedBaseRevision,
                                               'repairRef': proposal_ref, 'repairs': [], 'requestKey': 'confirm-' + proposal_ref},
                               repairHint='页面内容已保留；先完成实际浏览器检查，再复用 confirmPublish，无需重写页面或重建数据。')
            if exc.code == 'PREVIEW_SCOPE':
                details.update(providedBaseRevision=expectedBaseRevision, expectedBaseRevision=artifact['baseRevision'],
                               nextAction='RETRY_PREVIEW',
                               retryPreview={'analysisRef': analysisRef, 'expectedBaseRevision': artifact['baseRevision'],
                                             'repairRef': proposal_ref, 'repairs': [], 'previewOnly': True,
                                             'requestKey': 'retry-preview-' + proposal_ref},
                               repairHint='已保留原页面和绑定，并创建使用当前草稿基础版本的修复草案；直接用 retryPreview 完整参数重试预览。基础版本不等于草稿 revision。')
            raise AnalysisError(exc.code, str(exc), details) from exc

    async def _publish_content(self, analysisRef, claims, viewPlan, requestKey, expectedBaseRevision, thresholds=None,
                               hypotheses=None, findingPlan=None, actionPlan=None, technicalDetails=False,
                               presentation=None, workRefs=None, partialReason=None, previewOnly=False, document=None, renderCheckUnavailableReason=None):
        from .publication import validate_content, located, raise_issues
        from .completion import completion_summary
        artifact = self.store.get_analysis(self.owner, analysisRef)
        if artifact.get("schemaVersion") != SCHEMA_VERSION:
            raise AnalysisError("UNSUPPORTED_VERSION", "不支持该分析协议版本。")
        self._check_draft_rules(artifact)
        if previewOnly and (artifact['publicationState'] != 'DRAFT' or expectedBaseRevision != artifact['baseRevision']):
            raise AnalysisError('PREVIEW_SCOPE', '预览须引用当前草稿及其基础版本。')
        if type(technicalDetails) is not bool:
            raise AnalysisError('INVALID_VIEW', '技术详情开关必须为布尔值。')
        proposed = deepcopy(artifact)
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
        if self.actions_enabled:
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
                require_publication_checks(proposed['freeDocument'], records, reason)
        else:
            diagnostics = review(proposed, publication_view)
        if previewOnly:
            return {'schemaVersion': SCHEMA_VERSION, 'analysisRef': analysisRef, 'previewOnly': True,
                    'publicationState': 'DRAFT', 'document': publication_view,
                    'diagnostics': diagnostics, 'summary': summary(proposed, publication_view)}
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
        result = self.store.publish(self.owner, analysisRef, proposed, request, requestKey, expectedBaseRevision)
        delivery = {"type": "tool_markdown", "registered": True, "chartOrReportCreated": False}
        if self.delivery is not None:
            try:
                delivery = self.delivery.ensure(result["analysisRef"])
            except (AnalysisError, OSError) as exc:
                delivery = {"type": "workspace_analysis", "registered": False, "error": exc.as_dict() if isinstance(exc, AnalysisError) else {"code": "DELIVERY_IO", "message": "展示文件暂不可用，请重试交付。"}}
        return {"schemaVersion": SCHEMA_VERSION, "analysisRef": result["analysisRef"], "revision": result["revision"],
                **public_states(result), "summary": summary(result, publication_view),
                "delivery": delivery, "diagnostics": diagnostics}

    async def _automatic_preview_checks(self, result, request, preview_ref, document):
        """Measure a frozen preview once; preserve errors without changing content."""
        from .document_checks import evaluate, summary as check_summary
        from .free_document import compile_document
        checks = result['diagnostics'].get('renderChecks', {})
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
            records = [evaluate(full, sample) for sample in observations]
            if len({r['viewport']['width'] for r in records}) < 2:
                raise AnalysisError('INVALID_PREVIEW_CHECK', '自动检查没有取得两个不同宽度的有效记录。')
            for record in records:
                record.update(previewRef=preview_ref, runner='headless-service')
                self.store.save_document_check(self.owner, request['analysisRef'], record)
            result['diagnostics']['renderChecks'] = check_summary(
                self.store.document_checks(self.owner, request['analysisRef'], full['contentDigest']),
                request.get('renderCheckUnavailableReason'))
            result['diagnostics']['renderService'] = {'status': 'COMPLETED'}
        except AnalysisError as exc:
            if exc.code in ('ACCESS_DENIED', 'OWNER_MISMATCH'): raise
            result['diagnostics']['renderService'] = {'status': 'UNAVAILABLE', 'error': exc.as_dict()}

    async def preview_content(self, preview_ref):
        from .publication import schema_issues
        saved = self.store.get_publication_proposal(self.owner, preview_ref)
        issues = schema_issues({k: v for k, v in {**saved, 'requestKey': 'preview-read'}.items() if v is not None})
        if issues:
            raise AnalysisError('INVALID_PUBLICATION', '该草案仍需按错误路径修正后预览。', {'issues': issues})
        return await self._publish_content(requestKey='preview-read', previewOnly=True, **saved)

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
