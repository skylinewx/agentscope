"""保存本会话的业务上下文；模型选择承接对象，宿主维护真实结果和完成状态。"""
from copy import deepcopy
import json
from types import SimpleNamespace

from .models import AnalysisError, digest


DELIVERED = {'DIRECT', 'READY', 'PARTIAL'}


def _snapshot(state, status):
    """意图、来源范围和交付结果一起冻结，不能从不同轮次拼接。"""
    completed = status in DELIVERED
    scope = deepcopy(state.get('resolvedSubjectScope', {}) if completed else state.get('inheritedScope', {}))
    answer = state.get('deliveredAnswer', '') if completed else ''
    captures = state.get('queryCaptureRefs', []) + scope.get('captureRefs', [])
    captures += [selection['captureRef'] for selection in scope.get('selections', [])]
    return {
        'contextRef': 'ctx_' + digest(state['turnKey'])[:24],
        'turnKey': state['turnKey'], 'request': state.get('request', ''),
        'goal': state.get('goal', ''), 'intent': deepcopy(state.get('queryIntent', {})),
        'status': status, 'kind': 'DELIVERED' if completed else 'PENDING',
        'subjectScope': scope,
        'answerPreview': answer[:2000], 'answerPreviewComplete': len(answer) <= 2000,
        'baseContextRefs': deepcopy(state.get('selectedContextRefs', [])),
        'pendingChanges': [] if completed else list(dict.fromkeys(
            state.get('basePendingChanges', []) + state.get('followupContext', {}).get('changedFields', []))),
        'analysisRef': state.get('analysisRef') or scope.get('analysisRef'),
        'resultRefs': deepcopy(state.get('queryResultRefs', [])),
        'captureRefs': list(dict.fromkeys(captures)),
        'remainingIssues': deepcopy(state.get('lastCheck', {}).get('issues', [])),
    }


def save_context(state, status):
    """成功上下文成为默认锚点；未完成任务另存，供用户继续时显式选用。"""
    if not state.get('queryIntent') and not state.get('queryCaptureRefs') and not state.get('analysisRef'):
        return
    record = _snapshot(state, status)
    records = state.setdefault('businessContexts', [])
    records[:] = [item for item in records if item['contextRef'] != record['contextRef']]
    records.append(record)
    if status in DELIVERED:
        state['activeBusinessContextRef'] = record['contextRef']


def next_context_state(saved):
    """新用户轮保留上下文索引，未交付的新意图不覆盖成功上下文。"""
    if not saved:
        return {}
    previous = deepcopy(saved)
    status = previous.get('lastCheck', {}).get('status')
    # resolvedSubjectScope 只在成功交付时写入；支持已有会话没有目录的情形。
    if status in DELIVERED or not status and 'resolvedSubjectScope' in previous:
        save_context(previous, status or 'DIRECT')
    elif previous.get('mode'):
        save_context(previous, 'INCOMPLETE')
    records = previous.get('businessContexts', [])
    active_ref = previous.get('activeBusinessContextRef')
    active = next((item for item in records if item['contextRef'] == active_ref), None)
    if active is None:
        active = next((item for item in reversed(records) if item['kind'] == 'DELIVERED'), None)
    result = {'businessContexts': records, 'activeBusinessContextRef': active['contextRef'] if active else None,
              'selectedContextRefs': [active['contextRef']] if active else [],
              'previousQueryIntent': deepcopy(active['intent']) if active else {},
              'inheritedScope': deepcopy(active['subjectScope']) if active else {}}
    if not records:
        # 仅旧状态有可核对的上一轮意图时恢复，不能把失败轮当前意图当成交付意图。
        result['previousQueryIntent'] = deepcopy(saved.get('previousQueryIntent', {}))
        result['inheritedScope'] = deepcopy(saved.get('inheritedScope', {}))
    return result


def context_directory(state, search=None, offset=0, limit=6):
    """仅返回轻量目录；历史数据继续通过原始引用读取，不复制金额大表。"""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise AnalysisError('INVALID_CONTEXT_READ', 'offset 为非负整数，limit 为 1—20。')
    if search is not None and (not isinstance(search, str) or len(search) > 200):
        raise AnalysisError('INVALID_CONTEXT_READ', 'search 使用不超过 200 字的业务内容。')
    records = list(reversed(state.get('businessContexts', [])))
    if search:
        records = [item for item in records if search.casefold() in json.dumps(
            {key: item.get(key) for key in ('request', 'goal', 'intent')}, ensure_ascii=False).casefold()]
    rows = [{key: deepcopy(item[key]) for key in ('contextRef', 'request', 'goal', 'intent', 'status', 'kind')}
            for item in records[offset:offset + limit]]
    return {'contexts': rows, 'activeContextRef': state.get('activeBusinessContextRef'),
            'total': len(records), 'nextOffset': offset + len(rows) if offset + len(rows) < len(records) else None}


def select_context(turn, context):
    """承接显式指定的历史结果或未完成任务；不按业务关键词自动换题或综合。"""
    if not isinstance(context, dict) or 'contextRefs' not in context and context.get('mode') != 'NEW':
        return
    refs = context.get('contextRefs', [])
    mode = context.get('mode')
    if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != len(set(refs)) or len(refs) > 20
            or mode == 'CONTINUE' and len(refs) != 1
            or mode == 'NEW' and refs or mode == 'SYNTHESIZE' and len(refs) < 2):
        raise AnalysisError('INVALID_FOLLOWUP_CONTEXT', 'CONTINUE 指定一个 contextRef，SYNTHESIZE 指定明确相关的多个上下文，NEW 不带历史引用。')
    known = {item['contextRef']: item for item in turn.state.get('businessContexts', [])}
    if not set(refs) <= set(known):
        raise AnalysisError('UNKNOWN_BUSINESS_CONTEXT', '上下文引用不属于本会话；使用 AnalysisTask(contexts) 返回的 contextRef。',
                            {'availableContexts': context_directory(turn.state)})
    if refs == turn.state.get('selectedContextRefs', []) and not (
            mode == 'NEW' and (turn.state.get('previousQueryIntent') or turn.state.get('inheritedScope'))):
        return
    quote = context.get('userQuote')
    changed_base = bool(refs or turn.state.get('previousQueryIntent') or turn.state.get('inheritedScope'))
    if changed_base and (not isinstance(quote, str) or not quote.strip() or quote not in turn.state.get('request', '')):
        raise AnalysisError('FOLLOWUP_CHANGE_BASIS_REQUIRED', '选择其他历史结果或恢复未完成任务时，userQuote 引用本轮原话。')
    selected = [known[ref] for ref in refs]
    # 综合时仅继承一致的条件；冲突条件交给模型依据用户意图确定，不做隐式并集。
    intent = deepcopy(selected[0]['intent']) if selected else {}
    for item in selected[1:]:
        intent = {key: value for key, value in intent.items() if item['intent'].get(key) == value}
    scopes = [item['subjectScope'] for item in selected]
    scope = deepcopy(scopes[0]) if len(scopes) == 1 else {}
    turn.state.update(selectedContextRefs=list(refs), previousQueryIntent=intent, inheritedScope=scope,
                      basePendingChanges=deepcopy(selected[0].get('pendingChanges', [])) if len(selected) == 1 else [])
    # 本轮改选依据时清除旧的临时声明，随后由 declare 合并本次显式意图。
    turn.state.pop('queryIntent', None)
    turn.state.pop('inheritedScopeUnavailable', None)


def selected_contexts(state):
    """执行与核对使用完全相同的已选上下文；不会展开所有历史资料。"""
    wanted = set(state.get('selectedContextRefs', []))
    return [{key: deepcopy(item.get(key)) for key in
             ('contextRef', 'request', 'intent', 'status', 'kind', 'analysisRef', 'resultRefs', 'captureRefs', 'remainingIssues',
              'answerPreview', 'answerPreviewComplete')}
            for item in state.get('businessContexts', []) if item['contextRef'] in wanted]


def evidence_turn(turn):
    """纯复用历史结果也沿真实引用核对；不重新取数，也不加载其他话题的资料。"""
    if (turn.state.get('queryCaptureRefs') or turn.state.get('pythonRunRefs')
            or turn.state.get('followupContext', {}).get('mode', 'CONTINUE') != 'CONTINUE'):
        return turn
    contexts = selected_contexts(turn.state)
    if not contexts:
        return turn
    state = dict(turn.state)
    captures = [ref for context in contexts for ref in context.get('captureRefs', []) or []]
    for context in contexts:
        if not context.get('analysisRef'):
            continue
        try:
            artifact = turn.service.store.get_analysis(turn.service.owner, context['analysisRef'])
            captures.extend(artifact.get('captureRefs', []))
        except AnalysisError as error:
            if error.code != 'NOT_FOUND':
                raise
            # 产物过期时仍核对尚存的原回执；不伪造已读到产物或重新取数。
    state['queryCaptureRefs'] = list(dict.fromkeys(captures))
    if not state.get('queryResultRefs'):
        state['queryResultRefs'] = [selection for context in contexts for selection in context.get('resultRefs', []) or []]
    return SimpleNamespace(service=turn.service, state=state)


def result_subject(turn):
    """保存实际选中的 Python 结果；无法证明具体成员时不冒用原查询全集。"""
    selections = turn.state.get('queryResultRefs')
    if not selections:
        return None
    outputs, captures, batch_selections = [], [], []
    for selection in selections:
        run = turn.service.store.get_python_run(turn.service.owner, selection['pythonRunRef'])
        wanted = set(selection['outputRefs'])
        for output in run.get('outputs', []):
            if output['derivedId'] in wanted:
                outputs.append({key: deepcopy(output[key]) for key in
                                ('derivedId', 'label', 'outputKind', 'columns', 'rowCount', 'value', 'unit') if key in output})
        captures.extend(item['captureRef'] for item in run.get('fileInputs', []) if item.get('captureRef'))
        if run.get('sourceAnalysisRef'):
            artifact = turn.service.store.get_analysis(turn.service.owner, run['sourceAnalysisRef'])
            captures.extend(artifact.get('captureRefs', []))
            batch_selections.extend(_result_selections(artifact, run, wanted))
    return {'scopeRef': 'scope_' + digest(selections)[:24], 'selections': batch_selections,
            'basis': 'SELECTED_RESULTS', 'resultRefs': deepcopy(selections), 'outputs': outputs,
            'captureRefs': list(dict.fromkeys(captures)),
            'boundary': '主体以已交付产物和当前意图为准；原查询范围只是来源，不能用它替代筛选后的结果。'}


def _result_selections(artifact, run, wanted):
    """只用真实事实引用或明确维度列恢复结果成员，不从名称、金额或列别名猜测。"""
    known = {item['factId']: item for item in artifact.get('facts', [])}
    inputs = {item['ref'] for item in run.get('inputRecords', [])}
    known = {ref: item for ref, item in known.items() if ref in inputs}
    facts, complete = [], True
    tables = [item for item in run.get('outputs', []) if item['derivedId'] in wanted
              and item.get('outputKind') == 'TABLE']
    if not tables:
        return []
    dimension_codes = {code for fact in known.values() for code in fact.get('dimensions', {})}
    for table in tables:
        dimension_columns = [(index, column.get('dimCode', column['name']))
                             for index, column in enumerate(table['columns'])
                             if column.get('dimCode', column['name']) in dimension_codes]
        for row in table['rows']:
            matched = [known[cell] for cell in row if isinstance(cell, str) and cell in known]
            if not matched and dimension_columns:
                matched = [fact for fact in known.values() if all(
                    code in fact.get('dimensions', {}) and fact['dimensions'][code] == row[index]
                    for index, code in dimension_columns)]
            if not matched:
                complete = False
            facts.extend(matched)
    if not complete or not facts:
        return []
    grouped = {}
    for fact in facts:
        if not all(key in fact for key in ('batchId', 'stepId', 'rowIndex', 'columnName')):
            return []
        for capture_ref in fact.get('captureRefs', []):
            key = (capture_ref, fact['stepId'], fact['batchId'])
            item = grouped.setdefault(key, {'captureRef': capture_ref, 'stepId': fact['stepId'],
                                           'batchId': fact['batchId'], 'rowIndices': [], 'columnNames': []})
            if fact['rowIndex'] not in item['rowIndices']:
                item['rowIndices'].append(fact['rowIndex'])
            if fact['columnName'] not in item['columnNames']:
                item['columnNames'].append(fact['columnName'])
    return list(grouped.values())
