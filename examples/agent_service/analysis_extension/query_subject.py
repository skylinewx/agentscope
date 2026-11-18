"""冻结最终答复的主体来源，探查顺序不能替代用户实际得到的结果。"""
from copy import deepcopy

from .models import AnalysisError, digest


def candidates(turn):
    """候选只来自当前所有者的真实成功回执，不复制数据行或按业务名称猜测。"""
    from .adapters.budget_batch import tables, validate
    result = []
    for ref in dict.fromkeys(turn.state.get('queryCaptureRefs', [])):
        try:
            capture = turn.service.store.get_capture(turn.service.owner, ref)
            adapter = turn.service.sources.for_capture(capture)
            if adapter.adapter_id != 'budget-v2':
                continue
            receipt = adapter.raw_receipt(capture['response'])
        except AnalysisError as error:
            # 失败或已过期的探查回执不是主体候选，仍由原证据链保留其诊断。
            turn.state.setdefault('querySubjectDiagnostics', {})[ref] = error.code
            continue
        if receipt.get('status') != 'SUCCEEDED':
            continue
        for step, table, _ in tables(receipt):
            if table.get('disposition') != 'SUCCEEDED':
                continue
            try:
                validate(capture, receipt, step, table)
            except AnalysisError as error:
                turn.state.setdefault('querySubjectDiagnostics', {})[ref] = error.code
                continue
            selection = {'captureRef': ref, 'stepId': step, 'batchId': table['batchId'],
                         'columnNames': [name for name, role in table['columnRoles'].items() if role['kind'] == 'METRIC']}
            query = table.get('request', {})
            result.append({'candidateRef': 'subject_' + digest(selection)[:24], **selection,
                'rowCount': table['data']['rowCount'],
                'conditions': {key: deepcopy(query[key]) for key in
                               ('batchDimensions', 'periodRequest', 'resultWhere', 'aggregation', 'having') if key in query},
                'fixedDimValMap': deepcopy(table['data'].get('fixedDimValMap', {}))})
    return result


def freeze_subject(turn, available, choices):
    """沿用同次核对的主体选择。错误或冲突只标记待解析，不能偷换成最后一个查询。"""
    known = {item['candidateRef']: item for item in available}
    selections = {}
    invalid = False
    for choice in choices:
        candidate = known.get(choice.get('candidateRef'))
        rows = choice.get('rowIndices')
        if (candidate is None or rows is not None and
                (not isinstance(rows, list) or not rows or
                 any(type(index) is not int or index < 0 or index >= candidate['rowCount'] for index in rows))):
            invalid = True
            break
        selected = {key: deepcopy(candidate[key]) for key in ('captureRef', 'stepId', 'batchId', 'columnNames')}
        if rows is not None:
            selected['rowIndices'] = sorted(set(rows))
        prior = selections.get(choice['candidateRef'])
        if prior is not None and prior != selected:
            invalid = True
            break
        selections[choice['candidateRef']] = selected
    if invalid:
        selections.clear()
    turn.state['queryAnswerSubject'] = {'selections': list(selections.values()),
        'candidateCount': len(available), 'basis': 'REVIEWED_ANSWER' if selections else 'UNRESOLVED',
        'invalidSelection': invalid}


def remembered_subject(turn):
    """单一来源可保留上界；多个来源必须明确选中，不按最后一次工具调用猜主体。"""
    reviewed = turn.state.get('queryAnswerSubject')
    if reviewed and reviewed.get('selections'):
        return deepcopy(reviewed)
    available = candidates(turn)
    if len(available) == 1 and not (reviewed or {}).get('invalidSelection'):
        selected = {key: deepcopy(available[0][key]) for key in ('captureRef', 'stepId', 'batchId', 'columnNames')}
        return {'selections': [selected], 'basis': 'SOURCE_RANGE_UPPER_BOUND',
                'boundary': '来源范围上界；具体筛选以本轮已交付结果及意图为准。'}
    return {'selections': [], 'basis': 'UNRESOLVED_QUERY_SUBJECT', 'candidateCount': len(available),
            'boundary': '存在多个查询来源，尚未确定答复主体；按已交付内容和意图选择，不能默认最后一次查询。'}
