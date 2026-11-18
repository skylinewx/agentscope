"""Business dimension ranges: retain explicit intent, never promote opening defaults to user scope."""
from copy import deepcopy
import re

from .models import AnalysisError, digest


def concept(value):
    value = re.sub(r'\s+', '', str(value)).casefold()
    if value in ('单位', '机构', '组织机构', '公司', '企业', '组织', '集团', 'md_org'):
        return '组织机构'
    if value in ('情景', '场景', 'md_scene'):
        return '情景'
    if value in ('时期', '期间', 'datatime'):
        return '时期'
    return value


STANDARD_FIELDS = {'组织机构': 'population', '情景': 'scenario', '时期': 'period'}


def _scope_aliases(record):
    return {concept(record['draft']['dimensionHint'])} | {
        concept(alias) for alias in record.get('dimensionAliases', [])} | {
        concept(binding.get('dimCode', binding.get('dimensionCode')))
        for binding in record.get('bindings', [])
        if binding.get('dimCode', binding.get('dimensionCode'))}


def _declared_changes(state):
    """Both public change declarations must release the same inherited conditions."""
    context = state.get('followupContext', {})
    fields = set(context.get('changedFields', [])) | set(state.get('basePendingChanges', []))
    dimensions = {concept(value) for value in context.get('changedDimensions', []) + state.get('basePendingDimensions', [])}
    for record in state.get('inheritedDimensionScopes', []):
        aliases = _scope_aliases(record)
        prior = state.get('previousQueryIntent', {})
        draft = record.get('draft', {})
        phrases = {draft.get('sourceText')} | set((draft.get('selector') or {}).get('members', []))
        if any(prior.get(field) is not None and prior.get(field) in phrases for field in fields):
            dimensions |= aliases
        if dimensions & aliases:
            dimensions |= aliases
    fields |= {field for dimension, field in STANDARD_FIELDS.items() if dimension in dimensions}
    dimensions |= {dimension for dimension, field in STANDARD_FIELDS.items() if field in fields}
    return fields, dimensions


def changed_fields(state):
    return _declared_changes(state)[0]


def changed_dimensions(state):
    return _declared_changes(state)[1]


def retained_scopes(state):
    """Keep confirmed base conditions of an unfinished task, excluding explicit changes."""
    changes = changed_dimensions(state)
    return [deepcopy(item) for item in state.get('inheritedDimensionScopes', []) if not changes & _scope_aliases(item)]


def scope_identity(record):
    """Metadata identity, not a display label, separates dimension conditions."""
    roles = {(str(b.get('dimCode', b.get('dimensionCode', ''))),
              str(b.get('valueDomain', b.get('baseDataCode', '')))) for b in record.get('bindings', [])}
    return tuple(sorted(roles)) if roles else (('legacy', concept(record['draft']['dimensionHint'])),)


def condition_history(state, actual):
    """Relaxation is local to a query; explicit replacement/clear changes history."""
    records = retained_scopes(state)
    for item in actual:
        identity = scope_identity(item)
        # A current explicit confirmation replaces prior choices in that dimension.
        # An inherited execution only refreshes its proof, never its confirmation age.
        same = [old for old in records if scope_identity(old) == identity]
        if state.get('conditionTurn') is not None and item.get('confirmedTurn') == state['conditionTurn']:
            records = [old for old in records if scope_identity(old) != identity
                       or old.get('confirmedTurn') == item.get('confirmedTurn')]
        elif any((old.get('confirmedTurn') or 0) > (item.get('confirmedTurn') or 0) for old in same):
            continue
        records = [old for old in records if not (scope_identity(old) == identity
                   and old['draft']['selector'] == item['draft']['selector'])]
        records.append(deepcopy(item))
    return records


def _confirmed_member_groups(matches):
    """Separate distinct dimensions; one term with multiple roles stays ambiguous."""
    roles_by_term, groups = {}, {}
    for member in matches:
        role = (member.get('dimensionCode'), member.get('baseDataCode'))
        roles_by_term.setdefault(member['term'], set()).add(role)
        groups.setdefault(role, []).append(member)
    for role, members in groups.items():
        if all(len(roles_by_term[member['term']]) == 1 for member in members):
            # A single member gives a precise source reference even when its
            # enclosing phrase also contains another dimension such as scene.
            # A multi-member phrase can retain its existing whole-phrase proof
            # only when that proof has a single dimension role.
            if len(groups) == 1 or len({member['term'] for member in members}) == 1:
                yield role, members


def remember(turn, capture):
    if capture.get('originalToolName') != 'execute_budget_query':
        return
    receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
    if receipt.get('status') not in ('SUCCEEDED', 'PARTIAL'):
        return
    from .condition_context import effective_request, native_records
    request = effective_request(capture.get('request', {}), receipt.get('data') or {})
    draft = request.get('intentDraft') or {}
    scopes = draft.get('dimensionScopes', [])
    data = receipt.get('data') or {}
    targets = draft.get('targets', [])
    if 'conditionState' in data:
        history = native_records(receipt, capture['captureRef'])
        turn.state.setdefault('nativeConditionHistory', {})[capture['captureRef']] = history
        actual = set(data['conditionState']['active'])
        records = [record for record in history if record['conditionId'] in actual]
    else:
        # Existing captures predate native condition snapshots. Preserve their scope
        # proofs for factual follow-ups without assigning a new confirmation turn.
        records = _legacy_scopes(receipt, capture, draft, scopes, data, targets)
    turn.state.setdefault('dimensionScopeCaptures', {})[capture['captureRef']] = records
    if (capture.get('request', {}).get('conditionContext') or {}).get('omitted'):
        turn.state.setdefault('conditionEffectiveScopes', {})[capture['captureRef']] = [
            {key: deepcopy(value) for key, value in result.get('context', {}).items()
             if key in ('taskName', 'formName', 'period', 'effectiveDimensions', 'dataPeriodSelection')}
            for result in data.get('resultSets', [])]
    period = {key: deepcopy(value) for key, value in request.get('scope', {}).items()
              if key in ('periodCode', 'periodRequest')}
    native_period = next((row.get('systemRequest') for row in records if row.get('scopeField') == 'period'), None)
    if native_period and native_period.get('scope'):
        turn.state.setdefault('systemRequestCaptures', {})[capture['captureRef']] = deepcopy(native_period)
    elif period:
        turn.state.setdefault('systemRequestCaptures', {})[capture['captureRef']] = {
            'scope': period, 'periodTexts': list(dict.fromkeys(t['periodText'] for t in targets if t.get('periodText')))}


def _legacy_scopes(receipt, capture, draft, scopes, data, targets):
    bindings = data.get('scopeResolution', [])
    records = []
    target_ids = {target['id'] for target in draft.get('targets', [])}
    for scope in scopes:
        confirmed = [item for item in bindings if item.get('id') == scope.get('id') and item.get('status') == 'RESOLVED']
        if not confirmed:
            continue
        records.append({'draft': deepcopy(scope), 'bindings': deepcopy(confirmed),
                        'captureRef': capture['captureRef'],
                        'confirmedSource': {'executionId': receipt.get('executionId'), 'scopeId': scope['id'],
                                            'contextId': receipt.get('context', {}).get('contextId'), 'querySessionId': receipt.get('querySessionId')},
                        'appliesToAllTargets': not scope.get('targetIds') or set(scope['targetIds']) == target_ids})
    # Metadata-confirmed common target phrases and shared phrases have the same
    # status. Where the model placed a product/expense word cannot decide whether
    # it belongs to the history. Opening defaults never become explicit conditions.
    catalog = data.get('dimensionCatalog', [])
    represented = {binding.get('dimCode') for record in records for binding in record['bindings']}
    common = set.intersection(*({p.get('text') for p in target.get('businessPhrases', [])} for target in targets)) if targets else set()
    phrases = {p['text']: p for p in draft.get('sharedConditions', [])}
    for target in targets:
        phrases.update({p['text']: p for p in target.get('businessPhrases', []) if p.get('text') in common})
    for phrase in phrases.values():
        texts = {phrase.get('text')} | {part for parts in phrase.get('alternatives', []) for part in parts}
        matches = [member for result in data.get('resultSets', []) for member in result.get('resolvedTerms', [])
                   if member.get('term') in texts]
        for (code, domain), members in _confirmed_member_groups(matches):
            if code in represented:
                continue
            labels = {entry['label'] for entry in catalog if entry.get('dimCode') == code and entry.get('valueDomain') == domain}
            if code == 'MD_ORG':
                labels = {'组织机构'}
            if len(labels) != 1:
                continue
            # Inherit the metadata-confirmed member, not the surrounding metric
            # phrase. Keep the original quote so the source can verify its origin.
            matched_terms = {member['term'] for member in members}
            reference_term = next(iter(matched_terms)) if len(matched_terms) == 1 else phrase['text']
            item = {'id': 'shared-' + digest([code, domain, phrase])[:16], 'dimensionHint': next(iter(labels)),
                    'sourceText': phrase['sourceText'], 'targetIds': [],
                    'selector': {'type': 'VALUES', 'members': [reference_term]}}
            records.append({'draft': item, 'bindings': deepcopy(members), 'captureRef': capture['captureRef'],
                            'confirmedSource': {'executionId': receipt.get('executionId'), 'sharedTerm': reference_term,
                                                'contextId': receipt.get('context', {}).get('contextId'), 'querySessionId': receipt.get('querySessionId')},
                            'appliesToAllTargets': True, 'basis': 'EXPLICIT_SHARED_CONDITION'})
    for record in records:
        roles = {(binding.get('dimCode', binding.get('dimensionCode')),
                  binding.get('valueDomain', binding.get('baseDataCode'))) for binding in record['bindings']}
        record['dimensionAliases'] = sorted({record['draft']['dimensionHint']} | {
            alias for entry in catalog if (entry.get('dimCode'), entry.get('valueDomain')) in roles
            for alias in [entry.get('label'), entry.get('dimCode'), *entry.get('names', [])] if alias})
    return records


def snapshot(state, captures):
    """Only scopes shared by the selected source captures become an unambiguous follow-up anchor."""
    by_concept = {}
    for ref in dict.fromkeys(captures):
        for record in state.get('dimensionScopeCaptures', {}).get(ref, []):
            key = scope_identity(record)
            choices = by_concept.setdefault(key, {})
            selector = record.get('conditionId') or digest(record['draft']['selector'])
            if selector not in choices or (record.get('confirmedTurn') or 0) > (choices[selector].get('confirmedTurn') or 0):
                choices[selector] = record
    result = []
    for records in by_concept.values():
        for record in records.values():
            item = deepcopy(record)
            if len(records) > 1:
                item['ambiguousScope'] = True
            result.append(item)
    return result


def inherit(turn, request):
    context = turn.state.get('followupContext', {})
    if context.get('mode', 'CONTINUE') not in ('CONTINUE', 'NEW'):
        return request
    previous = deepcopy(turn.state.get('inheritedDimensionScopes', []))
    if context.get('mode') == 'NEW' and (request.get('basis') or {}).get('kind', 'INTENT') == 'INTENT':
        from .condition_context import history_sources
        previous = [*[item for item in previous if not item.get('conditionId')],
                    *history_sources(turn.state, previous).values()]
    # Index migration uses the original capture; a historical quote is never
    # rewritten into the current question when the active native session changes.
    turn.state['inheritedDimensionScopes'] = previous
    cohort = set((request.get('basis') or {}).get('memberDimensions', []))
    if not previous:
        return request
    changes = changed_dimensions(turn.state)
    draft = request.setdefault('intentDraft', {})
    scopes = draft.setdefault('dimensionScopes', [])
    current = {concept(item.get('dimensionHint')): item for item in scopes}
    inherited_text = []
    for record in previous:
        if record.get('scopeField') == 'period':
            continue
        old = record['draft']
        key = concept(old['dimensionHint'])
        aliases = _scope_aliases(record)
        if cohort & {binding.get('dimCode', binding.get('dimensionCode')) for binding in record.get('bindings', [])}:
            continue
        if changes & aliases:
            continue
        proof = record.get('confirmedSource') or {}
        if proof and not (proof.get('contextId') and proof.get('querySessionId')):
            capture = turn.service.store.get_capture(turn.service.owner, record['captureRef'])
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            if not receipt.get('context', {}).get('contextId') or not receipt.get('querySessionId'):
                raise AnalysisError('SOURCE_CONTEXT_UNAVAILABLE', '历史范围缺少原始查询凭据，需要重新确认该范围。')
            proof.update(contextId=receipt['context']['contextId'], querySessionId=receipt['querySessionId'])
        if record.get('ambiguousScope'):
            raise AnalysisError('FOLLOWUP_DIMENSION_SCOPE_AMBIGUOUS',
                '所选历史结果在同一维度上具有不同范围，请选择来源或明确本轮范围，不能丢弃条件。',
                {'dimensionHint': old['dimensionHint']})
        matches = [item for name, item in current.items() if name in aliases and
                   (not (item.get('confirmedSource') or {}).get('scopeId') or not record.get('conditionId')
                    or item['confirmedSource']['scopeId'] == record['conditionId'])]
        if len(matches) > 1:
            raise AnalysisError('FOLLOWUP_DIMENSION_SCOPE_CONFLICT', '同一已确认维度不能重复声明多个范围。')
        if matches:
            if matches[0].get('selector') != old.get('selector'):
                raise AnalysisError('FOLLOWUP_DIMENSION_SCOPE_CONFLICT',
                    '使用 scopeEdits 明确修改维度范围，或提交由本轮原话支持的实际选择器。', {'dimensionHint': old['dimensionHint']})
            if record.get('confirmedSource', {}).get('executionId'):
                matches[0]['confirmedSource'] = deepcopy(record['confirmedSource'])
            # Identical selectors inherit their already confirmed wording as well.
            # A model paraphrase is not new evidence for a prior range.
            matches[0]['sourceText'] = old['sourceText']
            inherited_text.extend(selector_evidence(old['selector']))
            continue
        if not record.get('appliesToAllTargets'):
            raise AnalysisError('FOLLOWUP_DIMENSION_TARGET_REQUIRED',
                '上一轮范围只作用于部分指标，请明确它在本轮对应的目标，不能扩大到所有指标。', {'scope': old})
        item = deepcopy(old)
        if record.get('confirmedSource', {}).get('executionId'):
            item['confirmedSource'] = deepcopy(record['confirmedSource'])
        item['id'] = record.get('conditionId') or 'inherited-' + digest([key, item['selector']])[:16]
        item['targetIds'] = []
        scopes.append(item)
        inherited_text.extend(selector_evidence(item['selector']))
    if inherited_text:
        request['contextText'] = ' '.join(dict.fromkeys([request.get('contextText', ''), *inherited_text])).strip()
    return request


def selector_evidence(selector):
    """Names already confirmed for this scope; never default member codes."""
    return ([selector['rootText']] if selector.get('rootText') else []) + list(selector.get('members', []))


def check_scope_evidence(request):
    """The host validates supplied evidence; dimension/range semantics belong to the metadata service."""
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    if not scopes:
        return
    evidence = ' '.join(str(request.get(key) or '') for key in ('question', 'contextText'))
    for scope in scopes:
        source = scope.get('sourceText')
        if not isinstance(source, str) or not source.strip() or not scope.get('confirmedSource') and source not in evidence:
            raise AnalysisError('DIMENSION_SCOPE_EVIDENCE_INVALID',
                '维度范围必须引用本轮原文或宿主已确认上下文，不能编造范围要求。')


def validate_execution(request, data):
    """Compare public range intent to model bindings and the native selectors actually submitted."""
    scopes = (request.get('intentDraft') or {}).get('dimensionScopes', [])
    if not scopes:
        return
    check_scope_evidence(request)
    resolved = [item for item in data.get('scopeResolution', []) if item.get('status') == 'RESOLVED']
    if not data.get('scopeVerification', {}).get('bindingComplete'):
        raise AnalysisError('BUDGET_QUERY_SCOPE_UNVERIFIED', '维度范围没有完成模型绑定校验。')
    targets = {target['id']: target for target in data.get('targets', [])}
    origins = {origin['originId']: origin for origin in data.get('origins', [])}
    batches = {batch['batchId']: batch for step in data.get('stepResults', []) for batch in step.get('batchResults', [])}
    for result in data.get('resultSets', []):
        target = targets.get(result.get('targetId'), {})
        logical_id = target.get('logicalTargetId', str(target.get('id', '')).split(':', 1)[0])
        binding_key = result.get('context', {}).get('target', {}).get('formBindingKey')
        for draft in scopes:
            if draft.get('targetIds') and logical_id not in draft['targetIds']:
                continue
            bindings = [item for item in resolved if item.get('id') == draft['id']
                        and item.get('targetId') == result.get('targetId') and item.get('binding') == binding_key
                        and item.get('interpretationId', result.get('interpretationId')) == result.get('interpretationId')]
            if not bindings:
                raise AnalysisError('BUDGET_QUERY_SCOPE_UNVERIFIED', '结果集缺少所要求维度范围的绑定依据。')
            for row in result.get('rows', []):
                if row.get('valueState') not in ('PRESENT', 'NULL', 'NO_DATA'):
                    continue
                source_batches = [batches.get(origins.get(ref, {}).get('batchId')) for ref in row.get('originRefs', [])]
                source_batches = [batch for batch in source_batches if batch is not None and batch.get('disposition') == 'SUCCEEDED']
                # Origin field layout is validated by the source adapter; keep the range check independent of amount checks.
                for binding in bindings:
                    condition = binding['condition']
                    selector_key = {'ALL': 'all', 'VALUES': 'values', 'HIERARCHY': 'hierarchy', 'FILTER': 'where'}[draft['selector']['type']]
                    if selector_key not in condition or selector_key == 'all' and condition['all'] is not True:
                        raise AnalysisError('BUDGET_QUERY_SCOPE_UNVERIFIED', '模型绑定改变了用户要求的范围类型。')
                    if request.get('purpose') == 'PREPARE_MATERIALS':
                        proof = data.get('scopeVerification', {})
                        coordinate = {'binding': binding_key, 'targetId': result.get('targetId'),
                                      'dimensions': row.get('dimensions'), 'measureCode': row.get('measureCode'),
                                      'modelCode': str(row.get('cellExp')).split('[', 1)[0]}
                        checks = [check for check in proof.get('selectorChecks', [])
                                  if check.get('binding') == binding_key and check.get('targetId') == result.get('targetId')
                                  and check.get('id') == draft['id']
                                  and check.get('interpretationId', result.get('interpretationId')) == result.get('interpretationId')]
                        if coordinate in proof.get('analysisCoordinates', []) and any(
                                _scope_admitted(condition, actual, {'metrics': check.get('executedMetrics', []), 'diagnostics': check}, row)
                                for check in checks for actual in check.get('executedConditions', [])) and (
                                not source_batches or any(all(_row_admitted(actual, row)
                                    for actual in batch.get('request', {}).get('batchDimensions', [])) for batch in source_batches)):
                            continue
                    exact = any(_exact_scope_admitted(condition, batch, row, origins.get(ref, {}))
                                for ref in row.get('originRefs', []) for batch in source_batches
                                if batch.get('batchId') == origins.get(ref, {}).get('batchId'))
                    if not exact and not any(any(_scope_admitted(condition, actual, batch, row) for actual in batch.get('request', {}).get('batchDimensions', [])) for batch in source_batches):
                        raise AnalysisError('BUDGET_QUERY_SCOPE_UNVERIFIED',
                            '实际批量请求没有执行已声明的维度范围，不能用默认维值代替全部或下级。',
                            {'scopeId': draft['id'], 'dimCode': binding['dimCode']})


def _exact_scope_admitted(expected, batch, row, origin):
    """Singleton intent can use exact cells; ALL/hierarchy/filter still require their native range evidence."""
    values = expected.get('values')
    if set(expected) != {'dimCode', 'values'} or not isinstance(values, list) or len(values) != 1:
        return False
    if batch.get('disposition') != 'SUCCEEDED':
        return False
    dimension, value, ref = expected['dimCode'], values[0], origin.get('targetRef')
    if not ref or row.get('dimensions', {}).get(dimension) != value:
        return False
    targets = [target for target in batch.get('request', {}).get('cellTargets', []) if target.get('targetRef') == ref]
    returned = [result for result in batch.get('data', {}).get('cellResults', []) if result.get('targetRef') == ref]
    if len(targets) != 1 or len(returned) != 1:
        return False
    target, actual = targets[0], returned[0]
    return (target.get('dimensions', {}).get(dimension) == value
            and actual.get('dimensions', {}).get(dimension) == value
            and target.get('metric') == actual.get('metric')
            and row.get('measureCode') == actual.get('measureCode')
            and all(target.get('dimensions', {}).get(key) == item and row.get('dimensions', {}).get(key) == item
                    for key, item in actual.get('dimensions', {}).items()))


def _row_admitted(condition, row):
    value = row.get('dimensions', {}).get(condition.get('dimCode'))
    return 'values' not in condition or value in condition['values']


def _preserves(expected, actual):
    if expected.get('dimCode') != actual.get('dimCode'):
        return False
    if 'values' in expected:
        return bool(actual.get('values')) and set(actual['values']) <= set(expected['values'])
    return all(actual.get(key) == value for key, value in expected.items())


def _scope_admitted(expected, actual, batch, row):
    if not _row_admitted(actual, row):
        return False
    if _preserves(expected, actual):
        return True
    # This evidence is generated by the application after runtime-grid matching, not by the model.
    for proof in batch.get('diagnostics', {}).get('scopeIntersections', []):
        if proof.get('condition') != expected or proof.get('executedCondition') != actual:
            continue
        value = proof.get('value')
        dimension = expected.get('dimCode')
        if value is None or actual.get('values') != [value] or actual.get('dimCode') != dimension:
            continue
        keys, metrics = proof.get('cellKeys', []), batch.get('metrics', [])
        if not keys or not metrics or not all(
                metric.get('selectionCellKey') in keys
                and metric.get('selectionObservationId') == proof.get('observationId')
                and metric.get('selectedDimensions', {}).get(dimension) == value for metric in metrics):
            continue
        if expected.get('all') is True:
            return value != '00000000'
        if 'values' in expected:
            return value in expected['values']
        resolution = batch.get('diagnostics', {}).get('scopeRangeResolution', {})
        if resolution.get('disposition') != 'SUCCEEDED' or not any(
                _preserves(expected, condition) for condition in resolution.get('request', {}).get('batchDimensions', [])):
            continue
        data = resolution.get('data', {})
        columns = [column.get('name') for column in data.get('columns', [])]
        if dimension in columns and any(len(row) > columns.index(dimension)
                and row[columns.index(dimension)] == value for row in data.get('rows', [])):
            return True
    return False
