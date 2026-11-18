"""按已解析的运行上下文组织审查资料；原始回执和业务数据均保持完整。"""
from copy import deepcopy


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def focus_query_metadata(evidence):
    """已确定任务/表样后，只带相关定义；未取数的候选澄清仍保留完整候选。"""
    anchors, identities = [], set()
    for run in evidence.get('pythonRuns', []):
        for source in run.get('fileInputs', []):
            context = source.get('context', {})
            if context.get('target'):
                anchors.append(context['target'])
                identities.update(_strings(context.get('effectiveDimensions', {})))
        # 文件条件不只在表外维度中：保留实际读集涉及的产品、期间、渠道等身份定义。
        from .query_result_evidence import source_cells
        for data in run.get('sourceData', []):
            if not isinstance(data, dict) or 'rows' not in data or 'columns' not in data:
                continue
            for cell in source_cells(data):
                identities.update(_strings(cell.get('dimensions', {})))
                identities.update(_strings(cell.get('measureCode')))
    for source in evidence.get('sources', []):
        for fact in source.get('facts', []):
            identities.update(_strings({key: fact.get(key) for key in
                              ('objectId', 'sourceObjectId', 'bindingRef', 'measureCode', 'dimensions')}))
    if not anchors:
        return
    for anchor in anchors:
        identities.update(_strings(anchor))
    # 单字符类型标识可能出现在所有任务中，不是可靠实体身份。
    identities = {value for value in identities if len(value) > 1}
    retained, omitted = 0, 0
    for source in evidence.get('sources', []):
        entities = source.get('entities', [])
        if not entities:
            continue
        selected = [entity for entity in entities if identities.intersection(_strings(entity))]
        removed = len(entities) - len(selected)
        retained += len(selected)
        omitted += removed
        if removed:
            source['entities'] = selected
            source['definitionProjection'] = {
                'originalCount': len(entities), 'retainedCount': len(selected),
                'omittedCount': removed, 'reason': '与已选运行对象无关的发现定义不进入本次核对',
                'read': {'captureRef': source['captureRef'], 'pointer': ''},
                'boundary': '仅省略发现定义；没有删减查询事实、计算读集、失败诊断或用户要求的业务成员。'}
    evidence['materialSelection'] = {'basis': 'RESOLVED_RUNTIME_CONTEXT', 'targets': deepcopy(anchors),
                                     'retainedDefinitionCount': retained, 'omittedDefinitionCount': omitted}
    if omitted:
        evidence['scope'] = 'all captured facts and actual calculation reads; discovery definitions projected to resolved runtime identities'


def failure_summary(failure):
    """保留错误及根因，重试代码、完整请求和恢复说明留在执行日志中按需读取。"""
    result = {key: deepcopy(failure[key]) for key in ('tool', 'state', 'callId', 'captureRef') if key in failure}
    error = failure.get('error')
    if isinstance(error, dict):
        result['error'] = {key: deepcopy(error[key]) for key in ('code', 'message', 'cause', 'rootCause') if key in error}
        details = error.get('details')
        if isinstance(details, dict):
            result['error']['details'] = {key: deepcopy(details[key]) for key in
                ('exceptionType', 'rootCause', 'cause', 'sourceCode', 'sourceMessage', 'path',
                 'missingFields', 'missingFactRefs', 'missingObjectIds', 'requiresSourceQuery') if key in details}
    elif error is not None:
        result['error'] = deepcopy(error)
    return result
