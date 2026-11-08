"""按已解析的运行上下文组织审查资料；原始回执和业务数据均保持完整。"""
from copy import deepcopy


def business_query_projection(capture, receipt, source):
    """Use every verified business row once; native requests remain in the immutable capture.

    Call only after adapter validation. Do not apply to ANALYZE, whose formula evidence
    and calculation dependency graph require the original fact projection.
    """
    from .models import digest
    data = receipt['data']
    contexts, facts = {}, []
    fallback_origins = {origin.get('originId') for origin in data.get('origins', [])
                        if (origin.get('scenarioFallback') or {}).get('kind') == 'BUDGET_CELL_SCENARIO_OVERRIDE'
                        and origin['scenarioFallback'].get('formulaStatus') == 'NO_TARGET_CELL'}
    def target(value):
        return {key: deepcopy(value[key]) for key in
                ('taskCode', 'formCode', 'organizationCategory') if key in value}
    for result in data.get('resultSets', []):
        actual = result.get('context', {})
        context = {key: deepcopy(actual[key]) for key in
                   ('taskName', 'formName', 'period', 'periodResolution', 'effectiveDimensions', 'dataPeriodSelection') if key in actual}
        context['target'] = target(actual.get('target', {}))
        context.update({key: deepcopy(result[key]) for key in
                        ('targetId', 'interpretationId', 'resolvedTerms', 'resolvedTitleTerms', 'grain') if key in result})
        context_ref = digest(context)[:24]
        contexts[context_ref] = context
        for row in result.get('rows', []):
            fact = {key: deepcopy(row[key]) for key in
                    ('factId', 'title', 'measureCode', 'dimensions', 'dimensionNames', 'dataPeriod',
                     'value', 'valueType', 'valueState', 'unit', 'unitBasis', 'originRefs') if key in row}
            if row.get('originRefs') and set(row['originRefs']) <= fallback_origins:
                fact['formulaStatus'] = 'NO_TARGET_CELL'
            facts.append({**fact, 'contextRef': context_ref})
    source = deepcopy(source)
    business = source.get('businessQuery', {})
    # The contexts and facts above cover these two duplicated projections, including NULL/NOT_LOADED.
    business.pop('presentValueGroups', None)
    business.pop('resultSetCatalog', None)
    business['requestedTargets'] = deepcopy(data.get('targets', []))
    if data.get('presentation'):
        business['verifiedPresentation'] = deepcopy(data['presentation'])
    runtime_contexts = {}
    for query in source.get('batchQueries', []):
        scope = query.get('requestedScope', {})
        query['metricCount'] = len(scope.pop('metrics', []))
        for result in query.get('results', []):
            metrics = result.pop('metricContexts', [])
            # Keep each metric's selected conditions, task and period. Omit transport/session
            # identifiers and reusable tool selectors, which do not change the answer's scope.
            result['metricContexts'] = []
            for metric in metrics:
                actual = metric.get('context', {})
                runtime_context = {key: deepcopy(actual.get(key)) for key in ('period', 'effectiveDimensions')}
                runtime_context['target'] = target(actual.get('target', {}))
                runtime_context.update({key: metric.get(key) for key in ('taskName', 'formName')})
                context_ref = digest(runtime_context)[:24]
                runtime_contexts[context_ref] = runtime_context
                result['metricContexts'].append({**{key: deepcopy(metric[key]) for key in
                    ('name', 'measureCode', 'selectedDimensions', 'selectionCellKey') if key in metric},
                    'runtimeContextRef': context_ref})
    source['runtimeContexts'] = runtime_contexts
    source['projection'] = {'kind': 'VERIFIED_BUSINESS_ROWS', 'rowCount': len(facts),
                            'readTool': 'QueryResultRead',
                            'nativeRead': {'captureRef': capture['captureRef'], 'pointer': '/data'},
                            'boundary': '全部业务行、类型、空值状态和执行失败均保留；原生数据及定位参数已校验并完整保存。'}
    from .business_tables import build_tables, state_markdown, catalog
    tables = build_tables(receipt)
    locations = {}
    for table in tables:
        for fact, places in table.locations.items():
            locations.setdefault(fact, []).extend(places)
    source['businessTableView'] = {
        'markdown': state_markdown(receipt) + '\n\n' + '\n\n'.join(t.markdown() for t in tables),
        'tables': catalog(tables), 'factLocations': locations,
        'allFactsIncluded': sum(len(t.facts) for t in tables) == len(facts)}
    return contexts, facts, source


def markdown_review_material(value):
    """Only the model wire changes; validation, hashes and follow-up use full facts."""
    value = deepcopy(value)
    evidence = value.get('evidence', {})
    for item in evidence.get('sources', []):
        source = item.get('source', {})
        view = source.get('businessTableView')
        if not view or not view.get('allFactsIncluded'):
            continue
        item['businessTables'] = view['markdown']
        item['factReferences'] = [[fact, ';'.join(f"{p['tableRef']}.{p['rowRef']}.{p['columnRef']}" for p in places)]
                                  for fact, places in view['factLocations'].items()]
        item['tableDefinitions'] = view['tables']
        item['factCount'] = len(view['factLocations'])
        item['executionScopes'] = [{key: deepcopy(context[key]) for key in
            ('taskName', 'formName', 'period', 'targetId', 'interpretationId') if key in context}
            for context in item.get('contexts', {}).values()]
        for field in ('facts', 'factRows', 'factFields', 'contexts'):
            item.pop(field, None)
        item['source'] = {key: source[key] for key in ('status', 'error', 'completeness', 'resumeRef', 'projection') if key in source}
        # The original requested semantics and period selection are review inputs,
        # not expendable transport detail. Keep them once, without native rows.
        business = source.get('businessQuery', {})
        item['source']['businessQuery'] = {key: deepcopy(business[key]) for key in
            ('question', 'purpose', 'selection', 'complete', 'requestedTargets', 'verifiedPresentation', 'failureSummary', 'unresolvedGroups') if key in business}
        item['source']['verification'] = '原生请求、结果、来源及范围已通过现有程序校验；本表保留全部业务事实。审查仍核对原问题、答复、口径、异常及计算。'
    return value


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
