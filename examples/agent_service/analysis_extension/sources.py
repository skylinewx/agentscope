"""Host-owned source adapters. Captures pin the adapter and its interpretation."""
from __future__ import annotations

from copy import deepcopy

from .models import AnalysisError, digest, json_values


class SourceAdapter:
    """Implement source semantics here; the host owns storage and execution.

    catalog/adapt expose facts with immutable source locators, never supplied
    replacement values. Receipt parsing, completeness, evidence and relations
    remain the source adapter's responsibility.
    """
    adapter_id = ""
    version = "1"

    def __init__(self, options=None):
        if options is not None and not isinstance(options, dict):
            raise AnalysisError("INVALID_SOURCE_CONFIG", "来源适配参数必须是对象。")
        self.options = deepcopy({} if options is None else options)

    def snapshot(self):
        return {"id": self.adapter_id, "version": self.version, "options": self.options}

    def matches(self, tool, origin):
        return False

    def summary(self, receipt):
        return {}

    def compact(self, receipt, ref, source_chars):
        return None

    def is_data(self, capture):
        return True

    def entities(self, capture):
        return []

    def relations(self, captures):
        return []

    def verify(self, facts, captures, verifier_refs=None):
        return "UNVERIFIED", []

    def receipt_info(self, capture):
        return {"sourceAdapter": self.snapshot()}

    def data_context(self, context):
        return context

    def member_namespace(self, capture, dimension):
        """Return a member namespace, or None for auxiliary verification receipts."""
        context = self.receipt_info(capture).get('context') or {}
        return {'adapter': self.adapter_id,
                'origin': [capture.get(k) for k in ('resourceId', 'serverName', 'connectionFingerprint')],
                'sourceId': context.get('sourceId'), 'dimension': dimension}

    def readonly(self, tool, origin):
        return "mcp-readonly-annotation" if tool.is_read_only else None

    def roles(self, tool, origin):
        return []

    def verification_task(self, fact, capture):
        return None


def source_context(capture, context, query_session=None, data_context=None):
    """Namespace source IDs by the real connection and query context."""
    value = {"source": {key: capture.get(key) for key in
                        ("resourceId", "serverName", "connectionFingerprint")},
             "context": json_values(context), "querySessionId": query_session}
    return {"sourceContextId": "source_" + digest(value)[:24], **value,
            'dataContext': json_values(context if data_context is None else data_context)}


def combined_context(contexts):
    unique = {digest(item["context"]): item["context"] for item in contexts}
    if len(unique) == 1 and len({digest(item['source']) for item in contexts}) == 1:
        return next(iter(unique.values()))
    boundaries = {digest([item['source'], item.get('dataContext', item['context'])]) for item in contexts}
    if len(boundaries) == 1:
        return {**contexts[0]['dataContext'], 'kind': 'SINGLE_SNAPSHOT',
                'sourceContextIds': sorted({item['sourceContextId'] for item in contexts})}
    return {"kind": "COMPOSITE", "sourceId": None,
            "sourceContextIds": sorted(item["sourceContextId"] for item in contexts)}


class SourceRegistry:
    def __init__(self, factories, bindings=None):
        self.factories = {factory.adapter_id: factory for factory in factories}
        if len(self.factories) != len(factories) or "" in self.factories:
            raise AnalysisError("INVALID_SOURCE_CONFIG", "来源适配器标识必须唯一。")
        self.bindings = deepcopy([] if bindings is None else bindings)
        if not isinstance(self.bindings, list):
            raise AnalysisError("INVALID_SOURCE_CONFIG", "sources 必须是来源绑定列表。")
        seen = set()
        for binding in self.bindings:
            if (not isinstance(binding, dict) or set(binding) - {"resourceId", "toolName", "adapter", "options"}
                    or not isinstance(binding.get("resourceId"), str) or not binding["resourceId"]
                    or not isinstance(binding.get("toolName"), str) or not binding["toolName"]):
                raise AnalysisError("INVALID_SOURCE_CONFIG", "来源适配绑定需要实际资源 ID 和原始工具名。")
            key = (binding["resourceId"], binding["toolName"])
            if key in seen:
                raise AnalysisError("INVALID_SOURCE_CONFIG", "同一来源工具只能绑定一个适配器。")
            seen.add(key)
            self.create(binding.get("adapter"), binding.get("options", {}))

    def create(self, adapter_id, options=None, version=None):
        factory = self.factories.get(adapter_id)
        if factory is None or (version is not None and version != factory.version):
            raise AnalysisError("SOURCE_ADAPTER_UNAVAILABLE", "捕获所需的来源适配器版本不可用。")
        return factory(options)

    def for_tool(self, tool, origin):
        if origin is None:
            return None
        # Native contracts take precedence; a generic binding cannot weaken them.
        for factory in self.factories.values():
            if getattr(factory, "automatic", False):
                adapter = factory()
                if adapter.matches(tool, origin):
                    return adapter
        for binding in self.bindings:
            if (binding["resourceId"] == origin["resourceId"]
                    and binding["toolName"] == origin["originalToolName"]):
                return self.create(binding["adapter"], binding.get("options", {}))
        return None

    def for_capture(self, capture):
        saved = capture.get("sourceAdapter")
        if saved is None:
            raise AnalysisError("SOURCE_ADAPTER_UNAVAILABLE", "输入未保存来源适配器登记，请重新查询取得当前协议回执。")
        if (not isinstance(saved, dict) or set(saved) != {"id", "version", "options"}
                or not isinstance(saved["id"], str) or not saved["id"]
                or not isinstance(saved["version"], str) or not saved["version"]
                or not isinstance(saved["options"], dict)):
            raise AnalysisError("INVALID_SOURCE_ADAPTER", "捕获的适配器登记不完整。")
        return self.create(saved["id"], saved["options"], saved["version"])

    def groups(self, captures):
        groups = {}
        for ref, capture in captures.items():
            adapter = self.for_capture(capture)
            key = digest(adapter.snapshot())
            groups.setdefault(key, (adapter, {}))[1][ref] = capture
        return list(groups.values())

    def adapt(self, captures, selections, scope, max_facts):
        from .member_scopes import adapt
        return adapt(self, captures, selections, scope, max_facts)

    def catalog(self, capture, member_dimension=None):
        from .member_scopes import catalog
        return catalog(self, capture, member_dimension)

    def _adapt_unscoped(self, captures, selections, scope, max_facts):
        groups = self.groups(captures)
        if len(groups) == 1:
            adapter, selected = groups[0]
            return adapter.adapt(selected, selections, scope, max_facts)
        if scope.get("contextMode") != "COMPOSITE":
            raise AnalysisError("CONTEXT_CONFLICT", "多个来源适配结果须显式使用 scope.contextMode=COMPOSITE 分组汇编。")
        if not selections or any(item.get("captureRef") not in captures for item in selections):
            raise AnalysisError("UNKNOWN_CAPTURE", "选择器引用未纳入本次构建的来源。")
        results = []
        for adapter, selected in groups:
            own = [item for item in selections if item.get("captureRef") in selected]
            if not own:
                continue
            local_scope = deepcopy(scope)
            if local_scope.get("population", {}).get("captureRef") not in selected:
                local_scope.pop("population", None)
            local_scope["excludedMembers"] = [item for item in scope.get("excludedMembers", []) if item.get("captureRef") in selected]
            results.append(adapter.adapt(selected, own, local_scope, max_facts))
        facts = [fact for result in results for fact in result["facts"] if fact.get("captureRefs")]
        if len({fact["factId"] for fact in facts}) != len(facts):
            raise AnalysisError("SOURCE_IDENTITY_CONFLICT", "来源适配器产生了重复的事实标识。")
        members = set(scope["members"])
        reference = set(scope.get("referenceMembers", []))
        observed = {fact["objectId"] for fact in facts}
        for member in sorted((members | reference) - observed):
            facts.append({"factId": "missing_" + digest(member)[:24], "objectId": member,
                          "value": None, "valueState": "NOT_QUERIED", "captureRefs": [], "unit": None, "scale": None})
        if len(facts) > max_facts:
            raise AnalysisError("FACT_LIMIT", "汇编后的事实数量超过上限。")
        contexts = {item["sourceContextId"]: item for result in results for item in result.get("sourceContexts", [])}
        complete = all(result["scope"].get("sourcePagesComplete", False) for result in results)
        population = scope.get("population")
        population_complete = False
        if population:
            capture = captures.get(population.get("captureRef"))
            if capture is None:
                raise AnalysisError("UNKNOWN_POPULATION", "范围来源未纳入本次捕获。")
            population_complete = self.for_capture(capture).population(capture, population, scope["members"])
        selected_complete = complete and observed == members | reference
        coverage = "COMPLETE" if population_complete and selected_complete else "PARTIAL" if not selected_complete else "UNKNOWN"
        return {"facts": facts, "sourceContexts": list(contexts.values()), "context": combined_context(list(contexts.values())),
                "scope": {**scope, "scopeId": "scope_" + digest(scope)[:24], "coverage": coverage,
                          "excludedMembers": scope.get("excludedMembers", []),
                          "populationComplete": population_complete, "selectedDataComplete": selected_complete,
                          "sourcePagesComplete": complete, "observedMembers": sorted(members & observed),
                          "pendingMembers": sorted(members - observed), "observedReferenceMembers": sorted(reference & observed),
                          "pendingReferenceMembers": sorted(reference - observed), "semanticCompleteness": "UNVERIFIED"}}

    def relations(self, captures, scope=None):
        from .member_scopes import member_ref
        relations = [relation for adapter, group in self.groups(captures) for relation in adapter.relations(group)]
        if scope and scope.get('memberBindings'):
            for relation in relations:
                capture = captures[relation['captureRef']]
                namespace = self.for_capture(capture).member_namespace(capture, scope.get('memberDimension', 'recordId'))
                relation['parentId'] = member_ref(namespace, relation['parentId'])
                relation['childIds'] = [member_ref(namespace, child) for child in relation['childIds']]
                relation['childModelCodes'] = {member_ref(namespace, child): model for child, model in relation['childModelCodes'].items()}
        return relations

    def verify(self, facts, captures, verifier_refs=None):
        states, checked = [], []
        for adapter, group in self.groups(captures):
            selected = [fact for fact in facts if set(fact.get("captureRefs", [])) & group.keys()]
            state, records = adapter.verify(selected, group, verifier_refs)
            if selected:
                states.append(state)
            checked.extend(records)
        missing = any(not fact.get("captureRefs") for fact in facts)
        state = "INVALID" if "INVALID" in states else "VALID" if states and not missing and set(states) == {"VALID"} else "UNVERIFIED"
        return state, checked
