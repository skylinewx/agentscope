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
        selected = {item['captureRef'] for item in candidates
                    if item.get('requested') and item['captureRef'] in bound_refs}
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


def identity_grouping_matches(capture, receipt, requirements, artifact):
    """原生查询已按目标维度返回明细时，不要求再做会丢掉父节点的恒等聚合。

    与 BudgetTargetExecutionService.requiresAggregation 的粒度边界一致；
    属性分组仍须原生聚合。完整来源可以在代码中筛选；可用数据不等于必须相加的数据。
    """
    from .actions import source_identity
    from .adapters.budget_v2 import data_context
    from .adapters import budget_batch
    data = receipt.get('data', {})
    if (capture.get('originalToolName') != 'execute_budget_query' or receipt.get('status') != 'SUCCEEDED'
            or data.get('complete') is not True or data.get('unresolvedGroups')
            or data.get('failureSummary', {}).get('issueCount')):
        return set()
    selected = None if artifact is None else {fact['factId'] for fact in artifact.get('facts', [])}
    catalog = None
    matched = set()
    for index, requirement in enumerate(requirements):
        if (requirement['status'] != 'SUPPORTED' or not requirement['bindingRef']
                or list(requirement['source']) != list(source_identity(capture))
                or requirement['context'] != data_context(receipt.get('context'))):
            continue
        findings = [item for item in data.get('grouping', [])
                    if (item.get('bindingRef') or item.get('bindingIri') or item.get('binding')) == requirement['bindingRef']]
        groups = []
        valid = bool(findings)
        for finding in findings:
            choices = ([finding['groupBy']] if finding.get('groupBy') else
                       [item['groupBy'] for item in finding.get('candidates', []) if item.get('supported')])
            if finding.get('status') != 'SUPPORTED' or len(choices) != 1 or choices[0].get('field') is not None:
                valid = False
                break
            groups.append(choices[0])
        if not valid or not any(item.get('groupingKeyword') == requirement['keyword'] for item in findings):
            continue
        if not any(group in groups for group in requirement['candidates']):
            continue
        retained = {group.get('dimCode') for group in groups}
        if None in retained:
            continue
        tables = [(step, table) for step, table, _ in budget_batch.tables(receipt)
                  if any(metric.get('context', {}).get('target', {}).get('formBindingKey') == requirement['bindingRef']
                         for metric in table.get('metrics', []))]
        for step, table in tables:
            request = table.get('request', {})
            if table.get('disposition') != 'SUCCEEDED' or request.get('aggregation'):
                valid = False
                break
            budget_batch.validate(capture, receipt, step, table)
        if not valid or not tables:
            continue
        if catalog is None:
            catalog = budget_batch.catalog(capture)
        facts = [fact for fact in catalog if fact.get('bindingRef') == requirement['bindingRef']]
        # This checks provenance and availability of the real grouping dimension,
        # not whether every source row must participate in a calculation. A total
        # row and its regional details must not be forced into the same sum.
        relevant = facts if selected is None else [fact for fact in facts if fact['factId'] in selected]
        if (relevant and all(all({**fact.get('effectiveDimensions', {}), **fact.get('dimensions', {})}.get(dim)
                                is not None for dim in retained) for fact in relevant)):
            matched.add(index)
    return matched


def grouping_issues(turn, artifact=None):
    """选定口径须有真实原生结果；明细已处于分组粒度时保留原生恒等路径。"""
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
        matched.update(identity_grouping_matches(capture, receipt, requirements, artifact))
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
    issues = [{'code': 'GROUPING_RESULT_REQUIRED',
             'message': '本轮要求的分组尚未完成。须采用同来源、任务表样和范围的原生分组结果；直接维度已在完整查询明细中且无需再聚合时可继续分析，属性分组仍须原生聚合。未识别、元数据不完整与明确不支持须分别说明。',
             'groupingStatus': item['status'], 'groupingKeyword': item['keyword'],
             'discoveryCaptureRef': item['captureRef'], 'bindingRef': item['bindingRef'],
             'organizationType': item['organizationType'], 'groupByCandidates': item['candidates']}
            for index, item in enumerate(requirements) if index not in matched
            and (item['status'] == 'SUPPORTED' or item['keyword'] not in completed_keywords)]
    for entry in issues:
        # A successful dimension discovery does not imply all requested data
        # arrived. Report the fetch failure rather than sending the model back
        # into a loop of changing otherwise valid grouping parameters.
        if entry['groupingStatus'] != 'SUPPORTED':
            continue
        try:
            capture = turn.service.store.get_capture(turn.service.owner, entry['discoveryCaptureRef'])
        except AnalysisError:
            # Keep the original unmet requirement if an older diagnostic's
            # receipt is unavailable; the optional explanation cannot crash it.
            continue
        if capture.get('originalToolName') != 'execute_budget_query':
            continue
        receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        data = receipt.get('data') or {}
        failures = data.get('unresolvedGroups') or []
        if entry['groupingStatus'] == 'SUPPORTED' and failures:
            entry.update(message='分组维度已确认，但本次来源取数尚未完成。保留已取得数据，按原请求恢复失败取数；不要修改已确认的分组参数来绕过数据缺失。',
                         recoveryAction='RETRY_INCOMPLETE_SOURCE', sourceIssues=failures,
                         failureSummary=data.get('failureSummary'))
    return issues
