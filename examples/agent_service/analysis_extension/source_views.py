"""Immutable result views and explicit adoption, independent of tool completion order."""
from copy import deepcopy
from .models import AnalysisError, digest

SELECTION_FIELDS = ('captureRef', 'stepId', 'batchId', 'columnNames', 'rowIndices',
                    'bindingRef', 'executionPeriod', 'observationId', 'cellKeys',
                    'preparedTargets', 'targetDimensions', 'includeFormulaDependencies', 'grain')


def register(turn, selection):
    service = turn.service
    selected = {key: deepcopy(selection[key]) for key in SELECTION_FIELDS if key in selection}
    capture = service.store.get_capture(service.owner, selected['captureRef'])
    row_count, label, grain, contexts = None, '', selected.get('grain'), []
    if 'batchId' in selected:
        from .batch_scope import selected_tables
        from .adapters.budget_batch import result_ranges
        from .adapters.budget_v2 import receipt_from_capture
        _, table, _, columns, rows = next(selected_tables({selected['captureRef']: capture}, [selected]))
        selected['columnNames'] = sorted(columns)
        if len(columns) != 1:
            raise AnalysisError('SOURCE_METRIC_SELECTION_REQUIRED', '一个来源视图选择一个实际指标列；多指标分别登记并可一起采用。')
        column = next(iter(columns))
        receipt = receipt_from_capture(capture)
        ranges = result_ranges(receipt, selected['stepId'], table)
        indices = set(range(table['data']['rowCount'])) if rows is None else set(rows)
        if ranges is not None:
            indices &= ranges.get(column, set())
        row_count = len(indices)
        if row_count != table['data']['rowCount']:
            selected['rowIndices'] = sorted(indices)
        else:
            selected.pop('rowIndices', None)
        grain = grain or [name for name, role in table['columnRoles'].items() if role['kind'] in ('DIMENSION', 'PROPERTY')]
        from .query_lineage import selected_result_sets
        for result in selected_result_sets(receipt, selected):
            label = next((row.get('title') for row in result.get('rows', []) if row.get('title')), label)
            contexts.append(deepcopy(result.get('context', {})))
        label = label or next((item.get('label') or item.get('title') or column
                               for item in table['data']['columns'] if item['name'] == column), column)
    else:
        # Existing precise cell/form selections remain valid; no forced conversion
        # to a batch population and no lost formula/input provenance.
        label = '已选单元格'
        row_count = len(selected.get('cellKeys', [])) or None
    record = {'selection': selected, 'label': label, 'rowCount': row_count,
              'grain': grain or [], 'contexts': contexts,
              'question': capture.get('request', {}).get('question', ''),
              'turnKey': capture.get('turnKey'), 'intentRevision': capture.get('intentRevision'),
              'sourceIdentity': {key: capture.get(key) for key in
                                 ('resourceId', 'serverName', 'connectionFingerprint')}}
    return service.store.save_source_view(service.owner, record)


def register_selections(turn, selections):
    result = []
    for selection in selections:
        columns = selection.get('columnNames')
        if 'batchId' in selection and not columns:
            from .batch_scope import selected_tables
            capture = turn.service.store.get_capture(turn.service.owner, selection['captureRef'])
            _, _, _, columns, _ = next(selected_tables({selection['captureRef']: capture}, [selection]))
        for column in columns if 'batchId' in selection else (None,):
            result.append(register(turn, {**selection, **({'columnNames': [column]} if column else {})}))
    return result


def directory(turn, search=None, offset=0, limit=6, context_refs=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise AnalysisError('INVALID_PAGE', '来源目录使用非负 offset 和 1 至 20 的 limit。')
    selections = []
    def scope_selections(scope, capture_refs):
        selected = scope.get('selections', [])
        if selected or scope.get('basis') != 'UNRESOLVED_QUERY_SUBJECT':
            return selected
        # An ambiguous earlier answer has candidates, not an empty source
        # catalog. Expose only that context's successful result ranges; reading
        # the directory must never pick or adopt one on the model's behalf.
        from types import SimpleNamespace
        from .query_subject import candidates
        return candidates(SimpleNamespace(service=turn.service, state={'queryCaptureRefs': capture_refs}))
    if context_refs is not None:
        known = {item['contextRef']: item for item in turn.state.get('businessContexts', [])}
        if not isinstance(context_refs, list) or any(ref not in known for ref in context_refs):
            raise AnalysisError('UNKNOWN_BUSINESS_CONTEXT', '先从 contexts 目录选择真实上下文。')
        for ref in context_refs:
            record = known[ref]
            selections.extend(scope_selections(record.get('subjectScope', {}), record.get('captureRefs', [])))
    else:
        from .query_subject import candidates
        selections.extend(candidates(turn))
        if turn.state.get('followupContext', {}).get('mode') != 'NEW':
            scope = turn.state.get('inheritedScope', {})
            selections.extend(scope_selections(scope, scope.get('captureRefs', [])))
        selections.extend(item['selection'] for item in turn.state.get('adoptedInputs', []))
    records = {}
    diagnostics = []
    for selection in selections:
        try:
            for record in register_selections(turn, [selection]):
                records[record['sourceRef']] = record
        except AnalysisError as error:
            diagnostics.append({'captureRef': selection.get('captureRef'), 'code': error.code})
    items = list(records.values())
    if search:
        items = [item for item in items if search.casefold() in (item['label'] + item['question']).casefold()]
    page = items[offset:offset+limit]
    def safe(value):
        return str(value or '').replace('|', '\\|').replace('\n', ' ')[:300]
    def context_label(item):
        return '；'.join(dict.fromkeys(' / '.join(str(value) for value in
            (ctx.get('taskName'), ctx.get('formName'), ctx.get('period'), ctx.get('effectiveDimensions', {}).get('MD_SCENE')) if value)
            for ctx in item['contexts']))
    markdown = ['| 来源引用 | 指标/内容 | 已选行数 | 时期/口径 | 原查询 |', '|---|---|---:|---|---|']
    markdown.extend(f"| {item['sourceRef']} | {safe(item['label'])} | {item['rowCount'] if item['rowCount'] is not None else '按单元格选择'} | {safe(context_label(item))} | {safe(item['question'])} |" for item in page)
    markdown.append(f'本页 {len(page)} 个，共 {len(items)} 个来源。' + (f'下一页 offset={offset+limit}。' if offset+limit < len(items) else ''))
    return {'viewType': 'result_sources', 'sources': [{key: value for key, value in item.items() if key not in ('selection', 'sourceIdentity')}
                        for item in page], 'markdown': '\n'.join(markdown), 'total': len(items),
            'nextOffset': offset+limit if offset+limit < len(items) else None,
            'diagnostics': diagnostics, 'boundary': '目录不改变采用范围；不能按工具完成顺序选择主体。'}


def select(turn, choices):
    if not isinstance(choices, list) or not choices or len(choices) > turn.service.store.limits.max_selections:
        raise AnalysisError('INVALID_SUBJECT_SOURCES', 'subjectSources 使用非空的来源列表；不是全部历史来源的通配符。')
    inputs, identities = [], set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) - {'sourceRef', 'role', 'membersFrom'}:
            raise AnalysisError('INVALID_SUBJECT_SOURCES', '来源使用 sourceRef、role 和可选 membersFrom。')
        role = choice.get('role', 'PRIMARY')
        if role not in ('PRIMARY', 'REFERENCE', 'STRUCTURE'):
            raise AnalysisError('INVALID_INPUT_ROLE', '来源用途使用 PRIMARY、REFERENCE 或 STRUCTURE。')
        source = turn.service.store.get_source_view(turn.service.owner, choice.get('sourceRef'))
        selection = deepcopy(source['selection'])
        if choice.get('membersFrom'):
            selection = match_members(turn, source, turn.service.store.get_source_view(turn.service.owner, choice['membersFrom']))
            source = register(turn, selection)
        identity = (source['sourceRef'], role)
        if identity in identities:
            raise AnalysisError('DUPLICATE_INPUT', '同一来源和用途不重复采用。')
        identities.add(identity)
        input_ref = 'input_' + digest([source['sourceRef'], role, choice.get('membersFrom')])[:24]
        selection.update(role=role, inputRef=input_ref, sourceRef=source['sourceRef'],
                         selectionReason=turn.state.get('goal') or turn.state.get('request') or '明确采用来源')
        inputs.append({'inputRef': input_ref, 'sourceRef': source['sourceRef'], 'role': role,
                       'membersFrom': choice.get('membersFrom'), 'selection': selection,
                       'label': source['label'], 'rowCount': source['rowCount'], 'contexts': source['contexts']})
    if not any(item['role'] == 'PRIMARY' for item in inputs):
        raise AnalysisError('PRIMARY_INPUT_REQUIRED', '分析至少明确采用一个主体来源。')
    return inputs


def input_identity(inputs):
    """Only immutable source selection and its role define a data input set.

    Goal text, selection explanations, display labels and declaration order do
    not change the facts. Keep them out of readiness/concurrency fingerprints.
    """
    return sorted(digest({
        **{key: item.get(key) for key in ('sourceRef', 'inputRef', 'role', 'membersFrom')},
        'selection': {key: item['selection'][key] for key in SELECTION_FIELDS if key in item['selection']}
    }) for item in inputs)


def input_version(turn, inputs):
    identity = input_identity(inputs)
    if identity == input_identity(turn.state.get('adoptedInputs', [])) and turn.state.get('inputSetVersion'):
        # A saved conversation may already own an artifact with this version.
        # Unchanged facts keep that identity when the analysis goal is reworded.
        return turn.state['inputSetVersion']
    return digest(identity)


def install(turn, inputs):
    turn.state['sourceAdoptionTurnKey'] = turn.state.get('turnKey')
    changed = input_identity(inputs) != input_identity(turn.state.get('adoptedInputs', []))
    version = input_version(turn, inputs)
    turn.state['adoptedInputs'] = deepcopy(inputs)
    turn.state['inputSetVersion'] = version
    turn.state['selectedResultScope'] = {'selections': [deepcopy(item['selection']) for item in inputs],
                                         'inputSetVersion': turn.state['inputSetVersion']}
    if not changed:
        return
    # New input sets never inherit readiness or calculations of a replaced set.
    for key in ('budgetFormulaTargets', 'budgetFormulaPreparation', 'analysisRef', 'report', 'hostCloseout'):
        turn.state.pop(key, None)


def member_dimensions(table, columns, receipt, explicit=None):
    """Entity tuples use declared ranges/varying dimensions, not a metric's fixed coordinates."""
    if any(role['kind'] == 'PROPERTY' for role in table['columnRoles'].values()):
        raise AnalysisError('COHORT_DETAIL_REQUIRED', '属性分组不能代替成员身份；沿用企业等实体需选择实际成员明细。')
    available = {name for name, role in table['columnRoles'].items() if role['kind'] == 'DIMENSION'}
    declared_dimensions = set(available)
    available -= {'DATATIME', 'MD_SCENE', 'MD_STAGE'}
    metrics = [metric for metric in table.get('metrics', []) if metric.get('name') in columns]
    fixed_cells = set().union(*(set(metric.get('cellDimensions', {})) for metric in metrics))
    available -= fixed_cells
    if explicit:
        if set(explicit) - declared_dimensions:
            raise AnalysisError('COHORT_DETAIL_REQUIRED', '指定粒度含有非实体维度，不能静默删去后改变成员口径。')
        dimensions = set(explicit) & available
        if not dimensions:
            raise AnalysisError('COHORT_DETAIL_REQUIRED', '来源只有指标或时期情景坐标，没有可沿用的实体成员。')
        return sorted(dimensions)
    names = [column['name'] for column in table['data']['columns']]
    rows = table['data']['rows']
    varying = {name for name in available if len({row[names.index(name)] for row in rows}) > 1}
    ranged = {item.get('dimCode') for item in receipt.get('data', {}).get('scopeResolution', [])
              if item.get('status') == 'RESOLVED'} & available
    dimensions = varying | ranged
    if not dimensions and len(available) == 1:
        dimensions = available
    if not dimensions:
        raise AnalysisError('COHORT_DIMENSIONS_REQUIRED', '来源没有唯一可确认的实体维度；需使用有明确成员范围的明细结果，不能将指标坐标当作成员。')
    return sorted(dimensions)


def match_members(turn, source, cohort):
    from .batch_scope import selected_tables
    service = turn.service
    selections = [source['selection'], cohort['selection']]
    if any('batchId' not in item for item in selections):
        raise AnalysisError('COHORT_DETAIL_REQUIRED', '沿用成员需要有真实维度身份的明细来源。')
    captures = {item['captureRef']: service.store.get_capture(service.owner, item['captureRef']) for item in selections}
    if source['sourceIdentity'] != cohort['sourceIdentity']:
        raise AnalysisError('COHORT_DOMAIN_MISMATCH', '成员来源的连接或粒度不同，不能按名称或相同编码猜测对应关系。')
    def members(record):
        selection = record['selection']
        _, table, _, selected_columns, indices = next(selected_tables(captures, [selection]))
        columns = [column['name'] for column in table['data']['columns']]
        adapter = service.sources.for_capture(captures[selection['captureRef']])
        receipt = adapter.receipt(captures[selection['captureRef']])
        grain = member_dimensions(table, selected_columns, receipt, selection.get('grain'))
        if not grain or any(dim not in columns or '__' in dim for dim in grain):
            raise AnalysisError('COHORT_DETAIL_REQUIRED', '属性分组不能代替成员身份。')
        domains = {}
        for dim in grain:
            values = {metric.get('dimensionModels', {}).get(dim) for metric in table.get('metrics', [])
                      if metric.get('name') in selected_columns}
            values.discard(None)
            if not values:
                values = {entry.get('valueDomain') for entry in receipt.get('data', {}).get('dimensionCatalog', [])
                          if entry.get('dimCode') == dim and entry.get('valueDomain')}
            if len(values) != 1:
                raise AnalysisError('COHORT_DOMAIN_MISMATCH', '成员维度缺少唯一的真实值域证明。', {'dimension': dim})
            domains[dim] = next(iter(values))
        namespace = digest([adapter.member_namespace(captures[selection['captureRef']], '|'.join(grain)), domains])
        result = {}
        for index in range(table['data']['rowCount']) if indices is None else indices:
            row = table['data']['rows'][index]
            key = (namespace, *(str(row[columns.index(dim)]) for dim in grain))
            result.setdefault(key, []).append(index)
        return namespace, result
    source_domain, available = members(source)
    cohort_domain, required = members(cohort)
    if source_domain != cohort_domain:
        raise AnalysisError('COHORT_DOMAIN_MISMATCH', '来源必须具有相同的实体维度和真实值域，不按相同编码猜测关联。')
    missing = set(required) - set(available)
    if missing:
        raise AnalysisError('COHORT_MEMBERS_MISSING', '目标来源缺少部分已选成员；保留缺口，不将交集冒充完整共同范围。',
                            {'missingMemberCount': len(missing), 'sourceRef': source['sourceRef']})
    return {**source['selection'], 'rowIndices': sorted(index for key in required for index in available[key])}


def query_basis(turn, origin, request):
    """Translate an opaque host ref into immutable native evidence, never model codes."""
    basis = request.get('basis')
    if not basis or 'sourceRef' not in basis:
        return request
    if basis.get('kind') not in ('FACTS', 'MEMBERS'):
        raise AnalysisError('INVALID_QUERY_BASIS', '只有 FACTS 或 MEMBERS 使用来源引用。')
    source = turn.service.store.get_source_view(turn.service.owner, basis['sourceRef'])
    if any(source['sourceIdentity'].get(key) != origin.get(key) for key in source['sourceIdentity']):
        raise AnalysisError('SOURCE_CONNECTION_MISMATCH', '来源引用不属于本次预算工具连接。')
    selection = source['selection']
    if 'batchId' not in selection:
        raise AnalysisError('SOURCE_DETAIL_REQUIRED', '预算范围查询需要带来源身份的明细结果。')
    from .batch_scope import selected_tables
    from .budget_preparation import _business_fact_reference
    from .adapters.budget_v2 import receipt_from_capture
    capture = turn.service.store.get_capture(turn.service.owner, selection['captureRef'])
    _, table, _, columns, rows = next(selected_tables({selection['captureRef']: capture}, [selection]))
    receipt = receipt_from_capture(capture)
    references = [_business_fact_reference(receipt, selection['stepId'], table, index, column)
                  for index in (range(table['data']['rowCount']) if rows is None else rows) for column in columns]
    if not references or len(references) > 1000:
        raise AnalysisError('SOURCE_FACT_LIMIT', '需要 1 至 1000 个确定事实，未截断来源范围。')
    if len({(ref['executionId'], ref['resultSetId']) for ref in references}) != 1:
        raise AnalysisError('SOURCE_RESULT_SET_CONFLICT', '分别选择实际结果集，不混合不相容的来源。')
    native = {**references[0], 'factIds': sorted({fact for ref in references for fact in ref['factIds']})}
    raw = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
    dimensions = (member_dimensions(table, columns, receipt, selection.get('grain'))
                  if basis['kind'] == 'MEMBERS' else [])
    if basis['kind'] == 'FACTS':
        from .query_lineage import fact_draft
        request = fact_draft(request)
    return {**request, 'basis': {'kind': basis['kind'], 'source': native,
                                 'contextId': raw['context']['contextId'], 'querySessionId': raw['querySessionId'],
                                 **({'memberDimensions': dimensions} if basis['kind'] == 'MEMBERS' else {})}}
