"""Keep grouping evidence tied to the selected source, task and form."""
from .models import AnalysisError, digest


def remember_grouping(turn, capture):
    if capture.get('originalToolName') not in ('discover_query_targets', 'execute_budget_query'):
        return
    adapter = turn.service.sources.for_capture(capture)
    if adapter.adapter_id != 'budget-v2':
        return
    try:
        receipt = adapter.raw_receipt(capture['response'])
    except AnalysisError as error:
        turn.state.setdefault('groupingDiagnostics', {})[capture['captureRef']] = error.as_dict()
        return
    data = receipt.get('data') or {}
    findings = data.get('grouping', []) if capture.get('originalToolName') == 'execute_budget_query' else [data]
    from .actions import source_identity
    from .adapters.budget_v2 import data_context
    requirements = turn.state.setdefault('groupingRequirements', {})
    matched_bindings = {item.get('context', {}).get('target', {}).get('formBindingKey')
                        for item in data.get('resultSets', [])}
    requested = capture.get('request', {}).get('groupBy', [])
    if not isinstance(requested, list):
        # New captures normalize against the tool schema. A malformed historical
        # request must never create one requirement per character.
        from .models import exact_loads
        try:
            decoded = exact_loads(requested) if isinstance(requested, str) else None
        except (AnalysisError, ValueError, TypeError):
            decoded = None
        requested = decoded if isinstance(decoded, list) else [requested] if isinstance(requested, str) else []
    # Keep failed grouping evidence too. grouping_requirements decides whether
    # it belongs to the declared/adopted task or only to an unused exploration.
    for keyword in requested:
        if not isinstance(keyword, str):
            continue
        matching = [item for item in findings if item.get('groupingKeyword') == keyword]
        if matching:
            for key, item in list(requirements.items()):
                if item.get('keyword') == keyword and item.get('bindingRef') is None:
                    del requirements[key]
            continue
        boundary = {'source': list(source_identity(capture)), 'context': data_context(receipt.get('context')), 'bindingRef': None}
        requirements[digest([boundary, keyword])] = {**boundary, 'captureRef': capture['captureRef'],
            'keyword': keyword, 'requested': True, 'status': 'UNRESOLVED', 'metadataComplete': False,
            'message': '本轮分组尚未取得成功的原生分组结果', 'organizationType': None,
            'diagnostics': [receipt.get('error')] if receipt.get('error') else [], 'candidates': []}
    for finding in findings:
        binding = finding.get('bindingRef') or finding.get('bindingIri') or finding.get('binding')
        if not binding:
            continue
        if (capture.get('originalToolName') == 'execute_budget_query' and receipt.get('status') == 'SUCCEEDED'
                and data.get('complete') is True and not data.get('unresolvedGroups')
                and not data.get('failureSummary', {}).get('issueCount') and finding.get('status') == 'SUPPORTED'
                and matched_bindings and binding not in matched_bindings):
            # Design candidates with no matching runtime cell are exploration,
            # not eight independent grouping obligations for two actual tables.
            continue
        boundary = {'source': list(source_identity(capture)), 'context': data_context(receipt.get('context')),
                    'bindingRef': binding}
        keyword = finding.get('groupingKeyword') or capture.get('request', {}).get('groupingKeyword')
        key = digest([boundary, keyword])
        turn.state.setdefault('groupingRequirements', {})[key] = {
            **boundary, 'captureRef': capture['captureRef'], 'keyword': keyword,
            'requested': capture.get('originalToolName') == 'execute_budget_query' and keyword in requested,
            'status': finding.get('status'), 'metadataComplete': finding.get('metadataComplete'), 'message': finding.get('message'),
            'organizationType': finding.get('organizationType'), 'diagnostics': finding.get('diagnostics', []),
            'candidates': ([finding['groupBy']] if finding.get('groupBy') else
                           [candidate['groupBy'] for candidate in finding.get('candidates', []) if candidate.get('supported')])}


def validate_grouping_selection(turn, capture_refs):
    """明确选用的口径必须来自本轮真实发现；仅发现候选不会增加业务要求。"""
    if capture_refs is None:
        return
    known = {item['captureRef'] for item in turn.state.get('groupingRequirements', {}).values()}
    if (not isinstance(capture_refs, list) or len(capture_refs) > 20
            or any(not isinstance(ref, str) or ref not in known for ref in capture_refs)
            or len(capture_refs) != len(set(capture_refs))):
        raise AnalysisError('INVALID_GROUPING_SELECTION', 'groupingCaptureRefs 只能选择本轮真实分组发现的 captureRef，不能填机构或属性标识。')


def grouping_requirements(turn, artifact=None):
    """分组要求来自明确声明或实际采用的数据，探索请求本身不改变任务。"""
    from .actions import source_identity
    from .adapters.budget_v2 import data_context, _bindings, _step_request
    candidates = list(turn.state.get('groupingRequirements', {}).values())
    if not candidates:
        return []
    explicit = turn.state.get('selectedGroupingCaptureRefs')
    if explicit is not None:
        selected = set(explicit)
    else:
        bound_refs = set(artifact['captureRefs'] if artifact is not None else
                         (item['selection']['captureRef'] for item in turn.state.get('adoptedInputs', [])))
        # 这里只做已声明概念的精确对应，不从用户原话猜业务字段或机构类型。
        intent = turn.state.get('queryIntent', {}).get('grouping')
        selected = {item['captureRef'] for item in candidates
                    if (item.get('requested') and item['captureRef'] in bound_refs)
                    or (intent and item.get('keyword') == intent)}
        for ref in bound_refs:
            capture = turn.service.store.get_capture(turn.service.owner, ref)
            if capture.get('originalToolName') not in ('execute_semantic_plan', 'execute_budget_query'):
                continue
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            for step in (receipt.get('data', {}).get('runtimeRequests', []) if capture.get('originalToolName') == 'execute_budget_query' else capture.get('request', {}).get('plan', {}).get('steps', [])):
                groups = step.get('batchQuery', {}).get('aggregation', {}).get('groupBy', [])
                if not groups:
                    continue
                target, steps = _step_request(capture, step['id'])
                bindings = _bindings(target, steps, results=receipt.get("data", {}).get("stepResults", []))
                bindings.update(metric['bindingRef'] for metric in step.get('batchQuery', {}).get('metrics', [])
                                if metric.get('bindingRef'))
                for item in candidates:
                    if (list(item['source']) == list(source_identity(capture))
                            and item['context'] == data_context(receipt.get('context'))
                            and item['bindingRef'] in bindings
                            and any(group in groups for group in item['candidates'])):
                        selected.add(item['captureRef'])
    return [{**item, 'required': item['captureRef'] in selected} for item in candidates]


def grouping_issues(turn, artifact=None):
    """选定的预算分组须有原生聚合；未采用的探索候选不阻断交付。"""
    from .actions import source_identity
    from .adapters.budget_v2 import data_context
    from .adapters import budget_batch
    requirements = [item for item in grouping_requirements(turn, artifact) if item['required']]
    if not requirements:
        return []
    matched = set()
    refs = artifact['captureRefs'] if artifact is not None else turn.state.get('queryCaptureRefs', [])
    for ref in refs:
        capture = turn.service.store.get_capture(turn.service.owner, ref)
        if capture.get('originalToolName') not in ('execute_semantic_plan', 'execute_budget_query'):
            continue
        receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        if receipt.get('status') not in ('SUCCEEDED', 'PARTIAL'):
            continue
        for step_id, table, _ in budget_batch.tables(receipt):
            if table.get('disposition') != 'SUCCEEDED' or not table.get('request', {}).get('aggregation'):
                continue
            budget_batch.validate(capture, receipt, step_id, table)
            groups = table['request']['aggregation'].get('groupBy', [])
            bindings = {metric.get('context', {}).get('target', {}).get('formBindingKey') for metric in table.get('metrics', [])}
            for index, requirement in enumerate(requirements):
                # 会话经 JSON 保存后 tuple 会成为 list；按规范化后的值比较来源身份。
                if (list(requirement['source']) == list(source_identity(capture))
                        and requirement['context'] == data_context(receipt.get('context'))
                        and requirement['bindingRef'] in bindings
                        and any(group in groups for group in requirement['candidates'])):
                    matched.add(index)
    completed_keywords = {requirements[index]['keyword'] for index in matched}
    return [{'code': 'GROUPING_RESULT_REQUIRED',
             'message': '本轮要求的分组尚未完成。已有单位明细只能作为参考，不能标记分组分析成功；未识别、元数据不完整与明确不支持须分别说明。',
             'groupingStatus': item['status'], 'groupingKeyword': item['keyword'],
             'discoveryCaptureRef': item['captureRef'], 'bindingRef': item['bindingRef'],
             'organizationType': item['organizationType'], 'groupByCandidates': item['candidates']}
            for index, item in enumerate(requirements) if index not in matched
            and (item['status'] == 'SUPPORTED' or item['keyword'] not in completed_keywords)]
