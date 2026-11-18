"""按输入用途和实际粒度汇编分析；参考值不扩大主体，结构证据不伪装成金额。"""
from copy import deepcopy

from .models import AnalysisError, digest
from .sources import combined_context


def structure_evidence(captures, selection):
    from .adapters.budget_v2 import receipt_from_capture, validate_form_request, form_rows, resolve_form_requests
    selected_capture = captures[selection['captureRef']]
    related = {ref: capture for ref, capture in captures.items()
               if all(capture.get(key) == selected_capture.get(key) for key in
                      ('resourceId', 'originalToolName', 'serverName', 'connectionFingerprint'))}
    capture = resolve_form_requests(related)[selection['captureRef']]
    receipt = receipt_from_capture(capture)
    wanted = set(selection.get('cellKeys', []))
    evidence, found = [], set()
    for step, form in form_rows(receipt):
        if step != selection['stepId']:
            continue
        context, observation = validate_form_request(capture, receipt, step, form)
        if selection.get('observationId') and observation['observationId'] != selection['observationId']:
            continue
        from .grid_files import captured_cells
        cells = captured_cells(capture, form)
        selected = {cell['key']: cell for cell in cells if cell['key'] in wanted}
        found.update(selected)
        evidence.append({'captureRef': selection['captureRef'], 'stepId': step,
                         'context': context, 'observation': observation,
                         'coverage': form.get('coverage'), 'status': form.get('status'),
                         'cells': list(selected.values()), 'inputRole': 'STRUCTURE'})
    if found != wanted or not wanted:
        raise AnalysisError('INVALID_STRUCTURE_SELECTION', '结构证据须引用已捕获的实际单元格。')
    return evidence


def adapt_inputs(registry, captures, selections, scope, max_facts):
    """每个输入独立校验来源、成员及粒度，再合并有来源的事实。"""
    from .batch_scope import selected_tables, resolve_members
    from .member_scopes import member_ref
    facts, contexts, subjects, references, audits, structures = {}, {}, set(), set(), [], []
    period_scopes = {}
    complete, pages_complete, population_complete = True, True, True
    for selection in selections:
        role = selection.get('role', 'PRIMARY')
        if role not in ('PRIMARY', 'REFERENCE', 'STRUCTURE'):
            raise AnalysisError('INVALID_INPUT_ROLE', '输入用途使用 PRIMARY、REFERENCE 或 STRUCTURE。')
        if selection.get('captureRef') not in captures:
            raise AnalysisError('UNKNOWN_CAPTURE', '输入回执未纳入本次分析。')
        if role == 'STRUCTURE':
            structures.extend(structure_evidence(captures, selection))
            continue
        local_scope = {key: deepcopy(value) for key, value in scope.items()
                       if key in ('label', 'selectionReason', 'contextMode', 'expectedDimensions')}
        grain = selection.get('grain')
        table = None
        if 'batchId' in selection:
            _, table, _, _, rows = next(selected_tables(captures, [selection]))
            if grain is None:
                grain = [name for name, value in table['columnRoles'].items()
                         if value['kind'] in ('DIMENSION', 'PROPERTY')]
            if rows is not None and len(rows) != table['data']['rowCount'] and role == 'PRIMARY' and not selection.get('selectionReason'):
                raise AnalysisError('SELECTION_REASON_REQUIRED', '主体筛选须说明与用户要求对应的选择理由；审查保留被排除行。')
        elif grain is None:
            grain = [scope.get('memberDimension', 'MD_ORG')]
        if not isinstance(grain, list) or not grain or any(not isinstance(dim, str) or not dim for dim in grain) or len(set(grain)) != len(grain):
            raise AnalysisError('INVALID_GRAIN', 'grain 使用实际维度或属性列名组成的非空、不重复数组。')
        local_scope['memberDimension'] = grain[0]
        if table is not None:
            local_scope = resolve_members(registry, captures, [selection], local_scope, max_facts)
        else:
            candidates = registry.catalog(captures[selection['captureRef']], grain[0], grid_selection=selection)
            wanted = set(selection.get('cellKeys', []))
            members = {fact.get('memberRef') for fact in candidates
                       if fact.get('stepId') == selection['stepId'] and fact.get('cellKey') in wanted
                       and (not selection.get('observationId') or fact.get('observationId') == selection['observationId'])}
            local_scope['members'] = sorted(member for member in members if member)
        adapter = registry.for_capture(captures[selection['captureRef']])
        namespace = adapter.member_namespace(captures[selection['captureRef']], grain[0])
        local_captures = {ref: capture for ref, capture in captures.items()
                          if ref == selection['captureRef']
                          or registry.for_capture(capture).member_namespace(capture, grain[0]) == namespace}
        data = registry.adapt(local_captures, [selection], local_scope, max_facts)
        periods = data['scope'].get('periodCoverage', [])
        if periods:
            # 同一时期计划可能由多个选择器各选一页；汇编完成后统一检查所需时期，
            # 不能要求每个选择器单独包含其他选择器中的年度。
            for period in periods:
                period_scopes[(period['sourceContextId'], period['planId'], period['workIndex'])] = period
            complete &= not data['scope'].get('pendingMembers') and not data['scope'].get('pendingReferenceMembers')
        else:
            complete &= data['scope']['selectedDataComplete']
        pages_complete &= data['scope'].get('sourcePagesComplete', False)
        if role == 'PRIMARY':
            population_complete &= data['scope'].get('populationComplete', False)
        contexts.update({item['sourceContextId']: item for item in data['sourceContexts']})
        namespace = adapter.member_namespace(captures[selection['captureRef']], '|'.join(sorted(grain)))
        for fact in data['facts']:
            if not fact.get('captureRefs'):
                raise AnalysisError('INPUT_DATA_INCOMPLETE', '所选输入仍有未查询的成员。')
            dimensions = {**fact.get('effectiveDimensions', {}), **fact.get('dimensions', {})}
            if any(dim not in dimensions for dim in grain):
                raise AnalysisError('GRAIN_NOT_CAPTURED', '粒度字段必须来自当前事实，不能凭名称补齐。', {'grain': grain, 'factId': fact['factId']})
            identity = {dim: dimensions[dim] for dim in grain}
            member = member_ref(namespace, identity if len(grain) > 1 else identity[grain[0]])
            names = fact.get('dimensionNames', {})
            label = (names.get(grain[0]) or fact.get('objectLabel') or str(dimensions[grain[0]] or '（空）')) if len(grain) == 1 else ' / '.join(str(names.get(dim) or dimensions[dim] or '（空）') for dim in grain)
            value = {**fact, 'objectId': member, 'memberRef': member, 'objectLabel': label,
                     'grain': grain, 'rowIdentity': identity, 'inputRole': role}
            old = facts.get(fact['factId'])
            if old and (old['grain'] != grain or old['inputRole'] != role):
                raise AnalysisError('INPUT_ROLE_CONFLICT', '同一事实不能同时声明不同用途或粒度。')
            facts[fact['factId']] = value
            (subjects if role == 'PRIMARY' else references).add(member)
        for audit in data['scope'].get('sourceScopes', []):
            audits.append({**audit, 'inputRole': role, 'grain': grain,
                           'selectionReason': selection.get('selectionReason', scope['selectionReason'])})
        if len(facts) > max_facts:
            raise AnalysisError('FACT_LIMIT', '分析输入超过本次事实容量。')
    if not subjects:
        raise AnalysisError('EMPTY_SELECTION', '至少需要一个 PRIMARY 主体数据输入。')
    if subjects & references:
        raise AnalysisError('INPUT_ROLE_CONFLICT', '同一粒度对象同时出现在主体和对照范围，请明确输入用途。')
    # 调用方若还显式声明旧式范围，必须与每个选择器实际选中的对象一致，不能静默丢弃约束。
    for field, role, actual in (('members', 'PRIMARY', subjects), ('referenceMembers', 'REFERENCE', references)):
        if field not in scope:
            continue
        resolved = set()
        for expected in scope[field]:
            matches = {fact['memberRef'] for fact in facts.values() if fact['inputRole'] == role
                       and (expected == fact['memberRef'] or len(fact['grain']) == 1 and expected == fact.get('sourceObjectId'))}
            if len(matches) != 1:
                raise AnalysisError('SCOPE_MISMATCH', '显式范围与输入选择器不一致；组合粒度按选择器选行，不按单维度省略其他对象。')
            resolved.update(matches)
        if resolved != actual:
            raise AnalysisError('SCOPE_MISMATCH', '输入选择器超出了显式主体或参考范围。')
    if scope.get('population'):
        population = scope['population']
        capture = captures.get(population.get('captureRef'))
        raw_members = list({fact.get('sourceObjectId') for fact in facts.values() if fact['inputRole'] == 'PRIMARY'})
        if capture is None or any(len(fact['grain']) != 1 for fact in facts.values() if fact['inputRole'] == 'PRIMARY'):
            raise AnalysisError('POPULATION_MISMATCH', '单维成员枚举不能证明复合粒度全集。')
        population_complete = registry.for_capture(capture).population(capture, population, raw_members)
    grains = {tuple(sorted(fact['grain'])) for fact in facts.values()}
    period_coverage = []
    for key, period in period_scopes.items():
        selected = [fact for fact in facts.values() if
                    (fact.get('sourceContextId'), fact.get('periodPlanId'), fact.get('periodWorkIndex')) == key]
        present = {(fact['executionPeriod'], fact.get('sourceObjectId', fact['objectId'])) for fact in selected}
        members = {member for _, member in present}
        codes = period['resolution']['periodCodes']
        missing_count = len(codes) * len(members) - len(present)
        missing = []
        for code in codes:
            for member in sorted(members, key=str):
                if (code, member) not in present:
                    missing.append({'executionPeriod': code, 'sourceObjectId': member})
                    if len(missing) == 1000:
                        break
            if len(missing) == 1000:
                break
        complete &= not missing_count
        period_coverage.append({**period, 'selectedDataComplete': not missing_count,
            'missingMemberPeriods': missing, 'missingMemberPeriodCount': missing_count,
            'missingMemberPeriodsTruncated': missing_count > len(missing)})
    common_dimension = next(iter(grains))[0] if len(grains) == 1 and len(next(iter(grains))) == 1 else 'PER_INPUT_GRAIN'
    result_scope = {**scope, 'members': sorted(subjects), 'referenceMembers': sorted(references),
                    'memberDimension': common_dimension, 'scopeId': 'scope_' + digest([scope, selections])[:24],
                    'sourceScopes': audits, 'populationComplete': population_complete,
                    'selectedDataComplete': complete, 'sourcePagesComplete': pages_complete,
                    'coverage': 'COMPLETE' if complete and population_complete else 'UNKNOWN' if complete else 'PARTIAL',
                    'observedMembers': sorted(subjects), 'observedReferenceMembers': sorted(references),
                    'pendingMembers': [], 'pendingReferenceMembers': [], 'periodCoverage': period_coverage,
                    'excludedMembers': deepcopy(scope.get('excludedMembers', [])),
                    'semanticCompleteness': 'UNVERIFIED'}
    if common_dimension != 'PER_INPUT_GRAIN':
        result_scope['memberBindings'] = [{'memberRef': fact['memberRef'], 'sourceObjectId': fact.get('sourceObjectId')}
                                          for fact in facts.values()]
    context = combined_context(list(contexts.values()))
    if context.get('kind') == 'COMPOSITE' and scope.get('contextMode') != 'COMPOSITE':
        raise AnalysisError('CONTEXT_CONFLICT', '跨来源或快照分析须显式声明 scope.contextMode=COMPOSITE。')
    return {'facts': list(facts.values()), 'scope': result_scope, 'structureEvidence': structures,
            'sourceContexts': list(contexts.values()), 'context': context}
