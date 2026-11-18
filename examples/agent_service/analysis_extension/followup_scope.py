"""从已交付的来源范围承接追问；不靠业务关键词猜测用户是否换题。"""
from copy import deepcopy

from .models import AnalysisError, digest


INTENT_FIELDS = ('metric', 'task', 'period', 'scenario', 'population', 'grouping',
                 'order', 'analysisFocus', 'referencePopulation')
# 意图与变化字段共用同一契约；分组、排序变化不解除主体或时期边界。
CHANGE_FIELDS = INTENT_FIELDS


def declare_context(state, context):
    """模型解释语义，宿主校验声明依据；原话的语义仍交由已有审查核对。"""
    inherited = bool(state.get('inheritedScope') or state.get('previousQueryIntent'))
    result = deepcopy(context if context is not None else state.get('followupContext') or
                      {'mode': 'CONTINUE' if inherited else 'NEW'})
    changes = result.get('changedFields', []) if isinstance(result, dict) else None
    if (not isinstance(result, dict) or set(result) - {'mode', 'changedFields', 'userQuote', 'contextRefs'}
            or result.get('mode') not in ('CONTINUE', 'NEW', 'SYNTHESIZE')
            or not isinstance(changes, list) or any(field not in CHANGE_FIELDS for field in changes)
            or len(changes) != len(set(changes))):
        raise AnalysisError('INVALID_FOLLOWUP_CONTEXT', 'context 使用 CONTINUE、NEW 或 SYNTHESIZE；changedFields 仅列实际改变的范围项。')
    if inherited and (result['mode'] != 'CONTINUE' or changes):
        quote = result.get('userQuote')
        if not isinstance(quote, str) or not quote.strip() or quote not in state.get('request', ''):
            raise AnalysisError('FOLLOWUP_CHANGE_BASIS_REQUIRED', '换题、综合多轮或改变既定范围时，userQuote 必须引用本轮用户实际表达该意图的原话。')
    return result


def _changes(turn):
    context = turn.state.get('followupContext', {})
    return set(context.get('changedFields', [])) | set(turn.state.get('basePendingChanges', []))


def _selectors(turn):
    context = turn.state.get('followupContext', {})
    if context.get('mode', 'CONTINUE') != 'CONTINUE':
        return []
    return turn.state.get('inheritedScope', {}).get('selections', [])


def _tables(turn, selectors):
    """范围只引用当前所有者可读的原回执，状态中不复制大表。"""
    from .batch_scope import selected_tables
    captures = {item['captureRef']: turn.service.store.get_capture(turn.service.owner, item['captureRef'])
                for item in selectors}
    return [(selection, captures[selection['captureRef']], table, rows)
            for selection, table, _, _, rows in selected_tables(captures, selectors)]


def _inherited_tables(turn, selectors):
    """历史回执过期不阻断新题；明确报告证据缺失，由已有意图和审查恢复范围。"""
    try:
        return _tables(turn, selectors)
    except AnalysisError as error:
        if error.code != 'NOT_FOUND':
            raise
        turn.state['inheritedScopeUnavailable'] = {'code': error.code,
            'message': '上一轮范围回执已不可读取。按 previousIntent 和用户上下文恢复条件后查询，不能假称已验证旧范围。'}
        return []


def _query(capture, step_id):
    return next(step.get('batchQuery', {}) for step in capture['request']['plan']['steps'] if step['id'] == step_id)


def _bindings(capture, step_id):
    from .adapters.budget_v2 import _step_request, _bindings as bindings
    step, steps = _step_request(capture, step_id)
    result = bindings(step, steps)
    result.update(metric['bindingIri'] for metric in step.get('batchQuery', {}).get('metrics', []) if metric.get('bindingIri'))
    return result


def _same_source(left, right):
    from .actions import source_identity
    return source_identity(left) == source_identity(right)


def _and(previous, proposed):
    """保留原筛选的布尔结构；新条件作为整体与旧条件求交。"""
    if not proposed or previous == proposed:
        return deepcopy(previous)
    if isinstance(proposed, dict) and proposed.get('op') == 'AND' and previous in proposed.get('children', []):
        return deepcopy(proposed)
    return {'op': 'AND', 'children': [deepcopy(previous), deepcopy(proposed)]}


def _includes(proposed, required):
    """只证明明确包含的旧条件，不实现不可靠的通用布尔表达式求解。"""
    return (proposed == required or isinstance(proposed, dict) and proposed.get('op') == 'AND'
            and any(_includes(child, required) for child in proposed.get('children', [])))


def _condition(previous, proposed):
    if proposed is None:
        return deepcopy(previous)
    result = deepcopy(proposed)
    selectors = ('all', 'values', 'periodRequest', 'hierarchy', 'where')
    if not any(key in proposed for key in selectors):
        return {**deepcopy(previous), **result}
    # 明确的不同范围保留原请求，供比较用途使用；构建 PRIMARY 时再核对范围。
    # values/all/periodRequest 与谓词互斥，不能为了继承生成预算接口不接受的报文。
    if any(key in proposed for key in ('all', 'values', 'periodRequest')) or any(key in previous for key in ('values', 'periodRequest')):
        return result
    old_hierarchy, new_hierarchy = previous.get('hierarchy', {}), proposed.get('hierarchy', {})
    if any(new_hierarchy[key] != old_hierarchy[key] for key in old_hierarchy.keys() & new_hierarchy.keys()):
        return result
    for key, value in previous.items():
        if key == 'where':
            result[key] = _and(value, result.get(key))
        elif key == 'hierarchy' and isinstance(result.get(key), dict):
            result[key] = {**deepcopy(value), **result[key]}
        elif key not in result and key != 'all':
            result[key] = deepcopy(value)
    return result


def _metric_period(previous_metrics, metric):
    """按绑定及定位语义继承时期；别名和数组位置都不是指标身份。"""
    selection = metric.get('selection', {})
    identity = {key: value for key, value in selection.items() if key != 'periodCode'}
    same_binding = [old for old in previous_metrics
                    if old.get('bindingIri') == metric.get('bindingIri')]
    matches = [old for old in same_binding if identity == {
        key: value for key, value in old.get('selection', {}).items() if key != 'periodCode'}]
    # 未知新指标只可继承同一绑定内明确共同的时期，不能跨绑定借值。
    candidates = matches or same_binding
    periods = {old.get('selection', {}).get('periodCode') for old in candidates}
    if len(periods) == 1 and None not in periods and '' not in periods:
        return next(iter(periods))
    return None


def _inherit_query(previous, proposed, changes, period_dimensions=()):
    result = deepcopy(proposed)
    conditions = {item['dimCode']: item for item in result.get('batchDimensions', [])}
    for old in previous.get('batchDimensions', []):
        changed = 'period' if old['dimCode'] in period_dimensions else 'population'
        if changed not in changes:
            conditions[old['dimCode']] = _condition(old, conditions.get(old['dimCode']))
    if conditions:
        result['batchDimensions'] = list(conditions.values())
    if 'population' not in changes:
        # 指标改变后旧值筛选不能套到新指标上；成员边界仍由原始结果行检查。
        if 'metric' not in changes and previous.get('metrics') == proposed.get('metrics'):
            if previous.get('resultWhere'):
                result['resultWhere'] = _and(previous['resultWhere'], result.get('resultWhere'))
            if previous.get('having') and previous.get('aggregation') == proposed.get('aggregation'):
                result['having'] = _and(previous['having'], result.get('having'))
    if not {'period', 'task'} & changes:
        if 'periodRequest' not in result and 'periodRequest' in previous:
            result['periodRequest'] = deepcopy(previous['periodRequest'])
        for metric in result.get('metrics', []):
            if 'periodCode' in metric.get('selection', {}):
                continue
            period = _metric_period(previous.get('metrics', []), metric)
            if period is not None:
                metric.setdefault('selection', {})['periodCode'] = period
    return result


def prepare_request(turn, origin, request):
    """在同来源、同绑定下补齐遗漏条件；不增加工具或模型往返。"""
    selectors = _selectors(turn)
    if not selectors or origin.get('originalToolName') not in ('execute_semantic_plan', 'discover_query_targets'):
        return request
    anchors = _inherited_tables(turn, selectors)
    _check_anchor_identifiers(turn, origin, request, anchors)
    if origin.get('originalToolName') != 'execute_semantic_plan':
        return request
    result = deepcopy(request)
    for step in result.get('plan', {}).get('steps', []):
        if step.get('operation') != 'BATCH_DATA_QUERY':
            continue
        own = {'request': result}
        bindings = _bindings(own, step['id'])
        matches = [(selection, capture, table) for selection, capture, table, _ in anchors
                   if _same_source(capture, origin) and bindings
                   and bindings == _bindings(capture, selection['stepId'])]
        queries = {_query_key(_query(capture, selection['stepId'])):
                   _query(capture, selection['stepId']) for selection, capture, _ in matches}
        # 多个不同主体不擅自选一个。已有审查会依据用户要求和实际输入检查。
        if len(queries) != 1:
            continue
        period_dimensions = {period['dimCode'] for _, _, table in matches for period in table['data'].get('resolvedPeriods', [])}
        step['batchQuery'] = _inherit_query(next(iter(queries.values())), step.get('batchQuery', {}), _changes(turn), period_dimensions)
    if result != request:
        applications = turn.state.setdefault('scopeApplications', [])
        record = {'submittedDigest': digest(request), 'effectiveDigest': digest(result),
                  'scopeRef': turn.state['inheritedScope']['scopeRef']}
        if record not in applications:
            applications.append(record)
    return result


def query_anchors(turn):
    """返回已成功查询的完整身份；分组追问不靠模型重写长标识。"""
    if 'task' in _changes(turn):
        return []
    result = []
    for selection, capture, _, _ in _inherited_tables(turn, _selectors(turn)):
        request = capture.get('request', {})
        for binding in sorted(_bindings(capture, selection['stepId'])):
            anchor = {'captureRef': selection['captureRef'], 'resourceId': capture['resourceId'], 'bindingIri': binding,
                      'contextId': request.get('contextId'), 'querySessionId': request.get('querySessionId')}
            grouping = turn.state.get('queryIntent', {}).get('grouping')
            if grouping and 'task' not in _changes(turn):
                anchor['discoverGroupingArguments'] = {
                    'contractVersion': '2.0', 'contextId': request.get('contextId'),
                    'querySessionId': request.get('querySessionId'), 'bindingIri': binding,
                    'groupingKeyword': grouping, 'requestId': 'grouping-' + digest([binding, grouping])[:24]}
            if anchor not in result:
                result.append(anchor)
    return result


def _check_anchor_identifiers(turn, origin, request, anchors):
    """只提示与已知绑定资源路径完全一致的身份笔误，不猜其他资源或静默改查询。"""
    if 'task' in _changes(turn):
        return
    known = set()
    for selection, capture, _, _ in anchors:
        if (_same_source(capture, origin)
                and capture.get('request', {}).get('contextId') == request.get('contextId')
                and capture.get('request', {}).get('querySessionId') == request.get('querySessionId')):
            known.update(_bindings(capture, selection['stepId']))
    if not known:
        return
    corrected, replacements = deepcopy(request), []

    def visit(value, path=''):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = path + '/' + key
                if key in ('bindingIri', 'resourceIri') and isinstance(child, str):
                    value[key] = match(child, child_path)
                elif key == 'resourceIris' and isinstance(child, list):
                    value[key] = [match(item, child_path + '/' + str(index)) if isinstance(item, str) else item
                                  for index, item in enumerate(child)]
                else:
                    visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + '/' + str(index))

    def match(provided, path):
        if provided in known or '/resource/' not in provided:
            return provided
        resource_path = provided.split('/resource/', 1)[1]
        candidates = [ref for ref in known if '/resource/' in ref and ref.split('/resource/', 1)[1] == resource_path]
        if len(candidates) == 1:
            replacements.append({'path': path, 'provided': provided, 'expected': candidates[0]})
            return candidates[0]
        return provided

    visit(corrected)
    if replacements:
        raise AnalysisError('QUERY_REFERENCE_MISMATCH', '资源标识与当前已成功查询的绑定不一致；先修复标识，不修改分组、指标或范围试错。',
                            {'replacements': replacements, 'retryArguments': corrected, 'requiresSourceQuery': False,
                             'hint': 'retryArguments 只替换已知绑定身份，其他条件保留原样；按原工具重新提交该完整参数。不能据此认定来源不支持分组。'})


def _query_key(query):
    return digest({key: query.get(key) for key in ('metrics', 'batchDimensions', 'periodRequest', 'resultWhere', 'aggregation', 'having')})


def _row_keys(table, row_indices, changes):
    data = table['data']
    period_dims = {item['dimCode'] for item in data.get('resolvedPeriods', [])}
    # 时期维度采用来源声明；没有展开成列的定位时期由下方独立检查。
    names = [column['name'] for column in data['columns']
             if table['columnRoles'][column['name']]['kind'] in ('DIMENSION', 'PROPERTY')
             and not ('period' in changes and column['name'] in period_dims)
             and not ('population' in changes and column['name'] not in period_dims)]
    indices = [index for index, column in enumerate(data['columns']) if column['name'] in names]
    keys = {digest([row[index] for index in indices]) for row_index, row in enumerate(data['rows'])
            if row_indices is None or row_index in row_indices}
    return names, keys


def _preserves_query(previous, current, changes, period_dimensions=()):
    if 'population' not in changes:
        current_dims = {condition['dimCode']: condition for condition in current.get('batchDimensions', [])}
        for old in previous.get('batchDimensions', []):
            if 'period' in changes and old['dimCode'] in period_dimensions:
                continue
            own = current_dims.get(old['dimCode'], {})
            if old.get('all'):
                continue
            for key, value in old.items():
                if key == 'where':
                    if not _includes(own.get(key), value):
                        return False
                elif key == 'hierarchy':
                    if any(own.get(key, {}).get(field) != val for field, val in value.items()):
                        return False
                elif own.get(key) != value:
                    return False
        for key in ('resultWhere', 'having'):
            if previous.get(key) and not _includes(current.get(key), previous[key]):
                return False
    if 'period' not in changes and previous.get('periodRequest') != current.get('periodRequest'):
        return False
    return True


def validate_primary(turn, data):
    """只阻断主体实质扩围。参考事实、结构和文字表达不参与成员范围判断。"""
    selectors = _selectors(turn)
    if not selectors:
        return
    changes = _changes(turn)
    anchors = _inherited_tables(turn, selectors)
    if not anchors:
        return
    prepared = [(selection, capture, table, rows, *_row_keys(table, rows, changes))
                for selection, capture, table, rows in anchors]
    own_captures_by_ref = {}
    invalid = []
    for fact in data.get('facts', []):
        if fact.get('inputRole', 'PRIMARY') != 'PRIMARY' or not fact.get('captureRefs'):
            continue
        accepted = False
        for selection, capture, table, rows, dimensions, allowed in prepared:
            for ref in fact['captureRefs']:
                if ref not in own_captures_by_ref:
                    own_captures_by_ref[ref] = turn.service.store.get_capture(turn.service.owner, ref)
            own_captures = [own_captures_by_ref[ref] for ref in fact['captureRefs']]
            if not any(_same_source(capture, own) for own in own_captures):
                continue
            target = fact.get('formContext', {}).get('target', {})
            original_targets = [metric.get('context', {}).get('target', {}) for metric in table.get('metrics', [])]
            if target and not any(
                    ('task' in changes or target.get('mainTaskId') == old.get('mainTaskId')) and
                    ('population' in changes or target.get('organizationCategory') == old.get('organizationCategory'))
                    for old in original_targets):
                continue
            if 'period' not in changes:
                period_values = {value for period in table['data'].get('resolvedPeriods', []) for value in period.get('values', [])}
                if not period_values:
                    period_values = {metric.get('context', {}).get('period') for metric in table.get('metrics', [])} - {None}
                if fact.get('period') is not None and period_values and fact['period'] not in period_values:
                    continue
            actual = fact.get('dimensions', {})
            if all(dimension in actual for dimension in dimensions):
                accepted = digest([actual[dimension] for dimension in dimensions]) in allowed
            if not accepted and fact.get('batchId') and rows is None:
                # 聚合结果可能不再展示原维度；须由真实执行条件证明保留原范围。
                previous = _query(capture, selection['stepId'])
                period_dimensions = {period['dimCode'] for period in table['data'].get('resolvedPeriods', [])}
                accepted = any(_preserves_query(previous, _query(own, fact['stepId']), changes, period_dimensions) for own in own_captures)
            if accepted:
                break
        if not accepted:
            invalid.append({'factRef': fact['factId'], 'objectId': fact.get('sourceObjectId', fact.get('objectId')),
                            'dimensions': fact.get('dimensions', {})})
    if invalid:
        raise AnalysisError('FOLLOWUP_SCOPE_MISMATCH', '本轮 PRIMARY 超出了上一轮有效对象或时期范围。普通追问应在该范围内筛选；外部比较数据使用 REFERENCE。用户确实改变范围时，先在 AnalysisTask.context 声明具体变更与本轮原话。',
                            {'scopeRef': turn.state['inheritedScope']['scopeRef'], 'invalidFactCount': len(invalid),
                             'examples': invalid[:5], 'requiresSourceQuery': False, 'requiresBuild': True,
                             'path': '/selections'})


def remember_subject(turn, status):
    """仅在最终交付通过后替换主体，失败、中途查询和参考数据不覆盖上轮主体。"""
    if status.get('status') not in ('DIRECT', 'READY', 'PARTIAL'):
        return
    from .conversation_context import result_subject, save_context
    selected_results = result_subject(turn)
    if selected_results is not None and not status.get('analysisRef'):
        turn.state['resolvedSubjectScope'] = selected_results
        save_context(turn.state, status['status'])
        return
    selectors, artifact = [], None
    if status.get('analysisRef'):
        artifact = turn.service.store.get_analysis(turn.service.owner, status['analysisRef'])
        candidates = [item for item in artifact.get('plan', {}).get('selections', [])
                      if item.get('role', 'PRIMARY') == 'PRIMARY' and item.get('batchId')]
        members = set(artifact.get('scope', {}).get('members', []))
        primary = [fact for fact in artifact.get('facts', []) if fact.get('inputRole', 'PRIMARY') == 'PRIMARY'
                   and (not members or fact.get('objectId') in members)]
        # 未使用 role 的旧式选择器也可能携带 referenceMembers，按实际主体事实冻结。
        for selection, _, table, _ in _tables(turn, candidates):
            facts = [fact for fact in primary if selection['captureRef'] in fact.get('captureRefs', [])
                     and fact.get('stepId') == selection['stepId'] and fact.get('batchId') == selection['batchId']]
            if not facts:
                continue
            selected = deepcopy(selection)
            selected['columnNames'] = list(dict.fromkeys(fact['columnName'] for fact in facts))
            rows = sorted({fact['rowIndex'] for fact in facts})
            if len(rows) != table['data']['rowCount']:
                selected['rowIndices'] = rows
            else:
                selected.pop('rowIndices', None)
            selectors.append(selected)
    else:
        from .query_subject import remembered_subject
        answer_subject = remembered_subject(turn)
        selectors = answer_subject['selections']
    if selectors:
        turn.state['resolvedSubjectScope'] = {'scopeRef': 'scope_' + digest(selectors)[:24], 'selections': selectors}
        if artifact is None:
            turn.state['resolvedSubjectScope'].update({key: deepcopy(answer_subject[key]) for key in
                ('basis', 'boundary') if key in answer_subject})
    elif artifact is None and answer_subject.get('candidateCount', 0) > 0:
        turn.state['resolvedSubjectScope'] = deepcopy(answer_subject)
    elif turn.state.get('followupContext', {}).get('mode', 'CONTINUE') == 'CONTINUE':
        turn.state['resolvedSubjectScope'] = deepcopy(turn.state.get('inheritedScope', {}))
    else:
        turn.state['resolvedSubjectScope'] = {}
    # 非批量表展开同样保留成功的来源身份；没有批量选择器不表示没有业务上下文。
    scope = turn.state['resolvedSubjectScope']
    if not selectors and not turn.state.get('queryCaptureRefs') and turn.state.get('inheritedScope'):
        # 纯历史追问没有新回执，不能因此把来源身份清空；实际子集由本轮意图和交付预览表明。
        scope = deepcopy(turn.state['inheritedScope'])
        scope.update(basis='INHERITED_SOURCE',
                     boundary='这是复用结果的来源范围上界；本次实际筛选结果以已交付内容和当前意图为准，不能据此扩回全集。')
        from .conversation_context import evidence_turn, selected_contexts
        contexts = selected_contexts(turn.state)
        if len(contexts) == 1:
            # 纯文字追问也延续原报告的来源，避免再追问构成时只剩概览数据。
            scope['captureRefs'] = list(dict.fromkeys(scope.get('captureRefs', [])
                + evidence_turn(turn).state.get('queryCaptureRefs', [])))
            if contexts[0].get('analysisRef'):
                scope['analysisRef'] = contexts[0]['analysisRef']
        turn.state['resolvedSubjectScope'] = scope
    if not scope.get('selections') and turn.state.get('queryCaptureRefs'):
        scope.update(captureRefs=list(dict.fromkeys(turn.state['queryCaptureRefs'])),
                     analysisRef=status.get('analysisRef'), basis=scope.get('basis', 'SOURCE_CONTEXT'))
    if artifact is not None:
        # 批量选择器只描述主体范围，不能代替报告使用的全部来源。
        # 恢复旧报告可能没有新取数，同样保留构成表等回执供下一轮复用。
        scope['captureRefs'] = list(dict.fromkeys(scope.get('captureRefs', []) + artifact.get('captureRefs', [])))
        scope['analysisRef'] = status['analysisRef']
    save_context(turn.state, status['status'])


def review_context(turn):
    """审查沿用已有调用；仅补充小型条件摘要，不把大表成员重复装进提示词。"""
    summaries = []
    for selection, capture, table, rows in _inherited_tables(turn, turn.state.get('inheritedScope', {}).get('selections', [])):
        query = _query(capture, selection['stepId'])
        summaries.append({'captureRef': selection['captureRef'], 'batchId': table['batchId'],
                          'scope': {key: query[key] for key in ('batchDimensions', 'periodRequest', 'resultWhere', 'having') if key in query},
                          'selectedRowCount': len(rows) if rows is not None else table['data']['rowCount'],
                          'selectedRowsOnly': rows is not None})
    from .conversation_context import selected_contexts
    inherited = turn.state.get('inheritedScope', {})
    return {'context': turn.state.get('followupContext', {}), 'inheritedSubject': summaries,
            'selectedContexts': selected_contexts(turn.state),
            **({'resultSubject': {key: deepcopy(inherited[key]) for key in ('basis', 'resultRefs', 'outputs', 'boundary') if key in inherited}}
               if inherited.get('basis') == 'SELECTED_RESULTS' else {}),
            'appliedInheritanceCount': len(turn.state.get('scopeApplications', [])),
            **({'unavailable': turn.state['inheritedScopeUnavailable']} if turn.state.get('inheritedScopeUnavailable') else {})}
