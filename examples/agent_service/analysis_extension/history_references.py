"""Bind a current business reference to archived sources owned by this conversation."""
from .models import AnalysisError
from .query_lineage import original_request


def execution_view(turn, context_ref):
    record = next((r for r in turn.state.get('businessContexts', []) if r['contextRef'] == context_ref), None)
    if record is None:
        raise AnalysisError('UNKNOWN_BUSINESS_CONTEXT', '请从本会话的 contexts 目录选择执行记录。')
    steps = []
    for ref in record.get('executionCaptureRefs', record.get('captureRefs', [])):
        try:
            capture = turn.service.store.get_capture(turn.service.owner, ref)
        except AnalysisError as error:
            if error.code == 'NOT_FOUND':
                steps.append({'captureRef': ref, 'status': 'EXPIRED'})
                continue
            raise
        if capture.get('turnKey') != record.get('turnKey'):
            continue
        try:
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
        except AnalysisError as error:
            steps.append({'captureRef': ref, 'tool': capture.get('originalToolName'),
                          'status': capture.get('captureState'), 'error': {'code': error.code, 'message': str(error)}})
            continue
        if not isinstance(receipt, dict):
            steps.append({'captureRef': ref, 'tool': capture.get('originalToolName'), 'status': capture.get('captureState')})
            continue
        data = receipt.get('data') or {}
        is_budget = capture.get('originalToolName') == 'execute_budget_query'
        effective = original_request(capture, receipt) if is_budget else {}
        step = {'tool': capture.get('originalToolName'), 'status': receipt.get('status'), 'captureRef': ref}
        if isinstance(receipt.get('error'), dict):
            step['error'] = {k: v for k, v in receipt['error'].items() if k in ('code', 'message')}
        if is_budget:
            step['conditions'] = {k: effective[k] for k in ('scope', 'groupBy') if effective.get(k)}
            draft = effective.get('intentDraft') or {}
            step['conditions'].update({k: draft[k] for k in ('targets', 'dimensionScopes', 'valueFilter', 'sharedConditions') if draft.get(k)})
            step['results'] = []
            for result in data.get('resultSets', []):
                context = result.get('context') or {}
                target = context.get('target') or {}
                step['results'].append({'task': context.get('taskName') or target.get('taskName'),
                    'form': context.get('formName') or target.get('formName'),
                    'period': context.get('period') or list(dict.fromkeys(row['dataPeriod'] for row in result.get('rows', [])
                                                                         if isinstance(row.get('dataPeriod'), str))),
                    'returnedRows': len(result.get('rows', [])), 'status': result.get('status')})
        steps.append(step)
    return {'view': 'EXECUTION', 'contextRef': context_ref, 'question': record.get('request'),
            'deliveryStatus': record.get('status'), 'steps': steps,
            'recordedFailures': record.get('toolFailures', []),
            'coverage': '仅列原轮已保存的步骤。' if steps else '原轮未保存可读取的执行步骤。'}


def trace_markdown(view):
    from .business_tables import encode
    def display(value):
        return (encode(value) if isinstance(value, (list, dict)) else str(value or '')).replace('|', '\\|').replace('\n', ' ')
    lines = [f"原问：{view['question']}", f"原轮交付状态：{view['deliveryStatus']}。{view['coverage']}"]
    for step in view['steps']:
        lines.append(' · '.join(filter(None, (step.get('tool'), step.get('status'), step.get('captureRef')))))
        if step.get('error'):
            lines.append(display(step['error']))
        if step.get('conditions'):
            lines.append('执行条件 ' + display(step['conditions']))
        if step.get('results'):
            rows = ['| 任务 | 表样 | 数据时期 | 回执状态 | 本页行数 |', '|---|---|---|---|---:|']
            for row in step['results']:
                rows.append(f"| {display(row['task'])} | {display(row['form'])} | {display(row['period'])} | {display(row['status'])} | {row['returnedRows']} |")
            lines.append('\n'.join(rows))
    if view['recordedFailures']:
        lines.append('原轮失败回执 ' + display(view['recordedFailures']))
    return '\n\n'.join(lines)


def bind(turn, references):
    from . import source_views
    if not isinstance(references, list) or not 1 <= len(references) <= 20:
        raise AnalysisError('INVALID_HISTORY_REFERENCE', 'references 包含一至二十项本轮明确指向的用途。')
    contexts = turn.state.get('businessContexts', [])
    bound = []
    for ref in references:
        if not isinstance(ref, dict):
            raise AnalysisError('INVALID_HISTORY_REFERENCE', '每项填写 use 和本轮指代原词 mention。')
        use, mention = ref.get('use'), ref.get('mention')
        if (use not in ('FACTS', 'MEMBERS', 'FORM', 'TASK', 'QUERY_TRACE', 'REPORT', 'REQUERY') or not isinstance(mention, str)
                or not mention.strip() or mention not in turn.state['request']):
            raise AnalysisError('INVALID_HISTORY_REFERENCE', 'mention 使用本轮原话中的指代短语，use 说明要使用什么。')
        candidates = [c for c in contexts if (not ref.get('contextRef') or c['contextRef'] == ref['contextRef'])
                      and (use != 'REPORT' or c.get('analysisRef'))
                      and (use not in ('FACTS', 'MEMBERS', 'FORM', 'TASK') or c.get('kind') == 'DELIVERED')]
        if ref.get('sourceRef') and use in ('FACTS', 'MEMBERS', 'FORM', 'TASK') and not ref.get('contextRef'):
            source = turn.service.store.get_source_view(turn.service.owner, ref['sourceRef'])
            capture_ref = source['selection']['captureRef']
            candidates = [c for c in candidates if capture_ref in c.get('captureRefs', [])
                          or any(item.get('captureRef') == capture_ref for item in c.get('subjectScope', {}).get('selections', []))]
            if len(candidates) > 1:
                identical = [c for c in candidates if any(item.get('sourceRef') == ref['sourceRef']
                             for item in c.get('adoptedInputs', []))]
                if identical:
                    candidates = identical
                    original = [c for c in identical if c.get('turnKey') == source.get('turnKey')]
                    if len(original) == 1:
                        candidates = original
        if len(candidates) > 1 and not ref.get('contextRef'):
            # A pronoun may have occurred in an unrelated earlier request as well.
            # Only an explicit quote of the whole source question settles this tie.
            literal = [c for c in candidates if c.get('request') and c['request'].strip() == mention.strip()]
            if len(literal) == 1:
                candidates = literal
        if len(candidates) != 1:
            message = ('根据原问与粒度选择 contextRef 后重交本轮声明。更多候选见 AnalysisTask(action=contexts)。'
                       if candidates else '本会话没有与此引用及用途匹配的已存记录。可查看 contexts 核对来源；需要新的业务数据时按本轮条件查询。')
            raise AnalysisError('HISTORY_CONTEXT_SELECTION', message,
                {'use': use, 'total': len(candidates), 'candidates': [
                    {k: c.get(k) for k in ('contextRef', 'request', 'status', 'intent')}
                    for c in reversed(candidates[-6:])]})
        context = candidates[0]
        item = {'use': use, 'mention': mention, 'contextRef': context['contextRef'], 'role': ref.get('role', 'PRIMARY')}
        if use in ('FACTS', 'MEMBERS'):
            directory = source_views.directory(turn, limit=6, context_refs=[context['contextRef']], source_ref=ref.get('sourceRef'))
            if directory['total'] == 1:
                item['sourceRef'] = directory['sources'][0]['sourceRef']
            else:
                raise AnalysisError('HISTORY_SOURCE_SELECTION', '此上下文包含多个来源或没有可采用的来源。按所需粒度选择 sourceRef，或重新取数。',
                    {'contextRef': context['contextRef'], 'candidates': directory['markdown']})
        elif use in ('FORM', 'TASK'):
            paths = source_views.directory(turn, context_refs=[context['contextRef']],
                                          source_ref=ref.get('sourceRef'), scope_kind=use)
            matching = [path for path in paths if not ref.get('sourceCode') or path['code'] == ref['sourceCode']]
            if len(matching) != 1:
                raise AnalysisError('HISTORY_SCOPE_SELECTION', '按本轮所指任务或表样选择实际 sourceRef 与 sourceCode。更多来源可在 sources 目录查看。',
                                    {'contextRef': context['contextRef'], 'total': len(paths), 'candidates': paths[:12]})
            item.update(matching[0])
        elif use == 'REPORT':
            item['analysisRef'] = context['analysisRef']
        bound.append(item)
    return bound


def apply_source_paths(turn, origin, request, role):
    """Fix only the requested part of the source identity on the same connection."""
    paths = [item for item in turn.state.get('historyReferences', [])
             if item['use'] in ('FORM', 'TASK') and item.get('role', 'PRIMARY') == role]
    terms = []
    for use, field in (('FORM', 'form'), ('TASK', 'task')):
        selected = [item for item in paths if item['use'] == use]
        if not selected:
            continue
        for item in selected:
            source = turn.service.store.get_source_view(turn.service.owner, item['sourceRef'])
            if any(origin.get(key) != value for key, value in source['sourceIdentity'].items()):
                raise AnalysisError('SOURCE_CONNECTION_MISMATCH', '已选择的历史表样或任务属于另一预算连接。')
        scope = request.setdefault('scope', {})
        codes = list(dict.fromkeys(item['code'] for item in selected))
        names = {item['name'] for item in selected}
        if (scope.get(field+'Codes') and set(scope[field+'Codes']) != set(codes)
                or scope.get(field+'Names') and set(scope[field+'Names']) != names):
            raise AnalysisError('HISTORY_SCOPE_CONFLICT', '本次填写的范围与已选历史来源不一致。使用已选来源可省略重复的名称和编码。',
                                {'field': field, 'selectedNames': sorted(names)})
        scope[field+'Codes'] = codes
        terms.extend([*codes, *sorted(names)])
    return terms
