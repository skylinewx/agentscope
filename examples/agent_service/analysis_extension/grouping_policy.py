"""Keep grouping evidence tied to the selected source, task and form."""
from .models import AnalysisError, digest


def remember_grouping(turn, capture):
    if capture.get('originalToolName') != 'discover_query_targets':
        return
    adapter = turn.service.sources.for_capture(capture)
    if adapter.adapter_id != 'budget-v2':
        return
    try:
        receipt = adapter.raw_receipt(capture['response'])
    except AnalysisError as error:
        # 调用失败也要保留回执；无法解析分组元数据不能反过来中断证据登记。
        turn.state.setdefault('groupingDiagnostics', {})[capture['captureRef']] = error.as_dict()
        return
    data = receipt.get('data') or {}
    if receipt.get('status') != 'SUCCEEDED' or data.get('resolutionKind') != 'GROUPING':
        return
    if data.get('bindingIri') != capture.get('request', {}).get('bindingIri'):
        return
    from .actions import source_identity
    from .adapters.budget_v2 import data_context
    boundary = {'source': list(source_identity(capture)), 'context': data_context(receipt.get('context')),
                'bindingIri': data['bindingIri']}
    key = digest([boundary, capture['request'].get('groupingKeyword')])
    turn.state.setdefault('groupingRequirements', {})[key] = {
        **boundary, 'captureRef': capture['captureRef'], 'keyword': capture['request'].get('groupingKeyword'),
        'status': data.get('status'),
        'metadataComplete': data.get('metadataComplete'), 'message': data.get('message'),
        'organizationType': data.get('organizationType'), 'diagnostics': data.get('diagnostics', []),
        'candidates': [candidate['groupBy'] for candidate in data.get('candidates', []) if candidate.get('supported')]}


def validate_grouping_selection(turn, capture_refs):
    """明确选用的口径必须来自本轮真实发现；仅发现候选不会增加业务要求。"""
    if capture_refs is None:
        return
    known = {item['captureRef'] for item in turn.state.get('groupingRequirements', {}).values()}
    if (not isinstance(capture_refs, list) or len(capture_refs) > 20
            or any(not isinstance(ref, str) or ref not in known for ref in capture_refs)
            or len(capture_refs) != len(set(capture_refs))):
        raise AnalysisError('INVALID_GROUPING_SELECTION', 'groupingCaptureRefs 只能选择本轮真实分组发现的 captureRef，不能填机构或属性标识。')


def grouping_requirements(turn):
    """区分探索证据与实际选择；默认从明确意图及提交的原生聚合请求识别选择。"""
    from .actions import source_identity
    from .adapters.budget_v2 import data_context, _bindings, _step_request
    candidates = list(turn.state.get('groupingRequirements', {}).values())
    if not candidates:
        return []
    explicit = turn.state.get('selectedGroupingCaptureRefs')
    if explicit is not None:
        selected = set(explicit)
    else:
        # 这里只做已声明概念的精确对应，不从用户原话猜业务字段或机构类型。
        intent = turn.state.get('queryIntent', {}).get('grouping')
        selected = {item['captureRef'] for item in candidates if intent and item.get('keyword') == intent}
        for ref in turn.state.get('queryCaptureRefs', []):
            capture = turn.service.store.get_capture(turn.service.owner, ref)
            if capture.get('originalToolName') != 'execute_semantic_plan':
                continue
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            for step in capture.get('request', {}).get('plan', {}).get('steps', []):
                groups = step.get('batchQuery', {}).get('aggregation', {}).get('groupBy', [])
                if not groups:
                    continue
                target, steps = _step_request(capture, step['id'])
                bindings = _bindings(target, steps)
                bindings.update(metric['bindingIri'] for metric in step.get('batchQuery', {}).get('metrics', [])
                                if metric.get('bindingIri'))
                for item in candidates:
                    if (list(item['source']) == list(source_identity(capture))
                            and item['context'] == data_context(receipt.get('context'))
                            and item['bindingIri'] in bindings
                            and any(group in groups for group in item['candidates'])):
                        selected.add(item['captureRef'])
    return [{**item, 'required': item['captureRef'] in selected} for item in candidates]


def grouping_issues(turn, artifact=None):
    """选定的预算分组须有原生聚合；未采用的探索候选不阻断交付。"""
    from .actions import source_identity
    from .adapters.budget_v2 import data_context
    from .adapters import budget_batch
    requirements = [item for item in grouping_requirements(turn) if item['required']]
    if not requirements:
        return []
    matched = set()
    refs = artifact['captureRefs'] if artifact is not None else turn.state.get('queryCaptureRefs', [])
    for ref in refs:
        capture = turn.service.store.get_capture(turn.service.owner, ref)
        if capture.get('originalToolName') != 'execute_semantic_plan':
            continue
        receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        if receipt.get('status') != 'SUCCEEDED':
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
                        and requirement['bindingIri'] in bindings
                        and any(group in groups for group in requirement['candidates'])):
                    matched.add(index)
    return [{'code': 'GROUPING_RESULT_REQUIRED',
             'message': '当前任务表样的分组口径尚无成功的预算聚合结果；复用原定位和范围执行批量聚合，不能用手工加总、父级属性继承或 Python 替代。',
             'discoveryCaptureRef': item['captureRef'], 'bindingIri': item['bindingIri'],
             'organizationType': item['organizationType'], 'groupByCandidates': item['candidates']}
            for index, item in enumerate(requirements) if index not in matched and item['status'] == 'SUPPORTED']
