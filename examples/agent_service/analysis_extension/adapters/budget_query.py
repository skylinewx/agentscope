"""Validate the business intent before accepting a server-owned runtime plan."""
from decimal import Decimal, InvalidOperation
from collections import Counter, defaultdict
import json

from ..models import AnalysisError
from ..budget_period_selection import opening_matches, selected_period


def is_business_query(capture):
    return capture.get('originalToolName') == 'execute_budget_query'


def runtime_value_types(receipt):
    """Types are declared by the source business result, never inferred from display text."""
    data = receipt.get('data', {})
    origins = {origin.get('originId'): origin for origin in data.get('origins', [])}
    result = {}
    for result_set in data.get('resultSets', []):
        for row in result_set.get('rows', []):
            if not row.get('valueType'):
                continue
            for ref in row.get('originRefs', []):
                origin = origins.get(ref, {})
                if origin.get('sourceType') == 'FORM_RUNTIME':
                    runtime = origin.get('runtime', {})
                    key = (runtime.get('observationId'), runtime.get('cell'))
                    previous = result.setdefault(key, row['valueType'])
                    _require(previous == row['valueType'], '同一运行格返回了相互冲突的类型。')
    return result


def runtime_dimension_names(receipt):
    """Reuse declared labels through the same observation/cell origin binding."""
    data = receipt.get('data', {})
    origins = {origin.get('originId'): origin for origin in data.get('origins', [])}
    result = {}
    for result_set in data.get('resultSets', []):
        for row in result_set.get('rows', []):
            for ref in row.get('originRefs', []):
                origin = origins.get(ref, {})
                if origin.get('sourceType') != 'FORM_RUNTIME':
                    continue
                runtime = origin.get('runtime', {})
                names = result.setdefault((runtime.get('observationId'), runtime.get('cell')), {})
                for dimension, label in row.get('dimensionNames', {}).items():
                    if label:
                        _require(dimension not in names or names[dimension] == label, '同一运行格返回了相互冲突的维值名称。')
                        names[dimension] = label
    return result


def present_value_groups(data):
    """Group equal facts for presentation, with an exact source-task list; never add their values."""
    groups = {}
    for index, result in enumerate(data.get('resultSets', [])):
        context = result.get('context', {})
        source = context.get('target', {})
        for row in result.get('rows', []):
            if row.get('valueState') != 'PRESENT':
                continue
            item = {key: row.get(key) for key in ('title', 'measureCode', 'dimensions', 'dimensionNames', 'dataPeriod', 'value', 'unit', 'unitBasis')}
            item['valueType'] = row.get('valueType', 'DECIMAL_STRING')
            item.update(formCode=source.get('formCode'), form=context.get('formName'))
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            group = groups.setdefault(key, {**item, 'tasks': [], 'resultSetIndices': [], 'factRefs': []})
            group['factRefs'].append({'resultSetId': result.get('resultSetId'), 'factId': row.get('factId')})
            task = {'code': source.get('taskCode'), 'name': context.get('taskName')}
            if task not in group['tasks']:
                group['tasks'].append(task)
            if index not in group['resultSetIndices']:
                group['resultSetIndices'].append(index)
    return list(groups.values())


def result_set_summaries(data):
    """Count every returned state by task and data period, without aggregating amounts."""
    result = []
    for index, item in enumerate(data.get('resultSets', [])):
        context = item.get('context', {})
        rows = item.get('rows', [])
        periods = defaultdict(Counter)
        for row in rows:
            periods[row.get('dataPeriod') or 'UNRESOLVED'][row.get('valueState', 'UNKNOWN')] += 1
        result.append({'index': index, 'resultSetId': item.get('resultSetId'), 'task': context.get('taskName'), 'form': context.get('formName'),
                       'taskCode': context.get('target', {}).get('taskCode'),
                       'formCode': context.get('target', {}).get('formCode'),
                       'period': context.get('period'), 'effectiveDimensions': context.get('effectiveDimensions', {}),
                       'rowCount': len(rows), 'rowsComplete': item.get('rowsComplete', True),
                       'dataPeriodStates': {period: dict(states) for period, states in periods.items()}})
    return result


def _require(condition, message):
    if not condition:
        raise AnalysisError('BUDGET_QUERY_MISMATCH', message)


def _target(value):
    return {
        'id': value.get('id'),
        'label': value.get('label') or value.get('id'),
        'allOf': [{'term': condition.get('term'), 'anyOf': sorted({
            (member.get('baseDataCode'), member.get('objectCode'))
            for member in condition.get('anyOf', [])
        })} for condition in value.get('allOf', [])],
        'titleTerms': list(dict.fromkeys(value.get('titleTerms', []))),
    }


def validate(capture, receipt):
    request, data = capture['request'], receipt.get('data', {})
    if data.get('sourceBasis', {}).get('operation') == 'IMMUTABLE_FACT_VIEW':
        from .budget_source_view import validate_view
        return validate_view(capture, receipt, validate)
    basis = request.get('basis') or {}
    if basis.get('kind') in ('FACTS', 'MEMBERS'):
        from .budget_source_view import validate_basis
        validate_basis(request, data)
        if basis['kind'] == 'FACTS':
            request = {**request, 'sourceSelection': basis['source']}
    _require(data.get('resultType') == 'BUDGET_QUERY_RESULT', '业务查询没有返回对应的数据契约。')
    _require(data.get('purpose') == request.get('purpose'), '查询与分析用途发生变化。')
    selection = request.get('selection', 'CELLS')
    _require(data.get('selection', 'CELLS') == selection, '整表与单元格读取范围发生变化。')
    _require(isinstance(request.get('question'), str) and request['question'] == data.get('question'),
             '服务端解析结果没有对应到原始用户问题。')
    interpretations = {item['id']: item for item in data.get('resolution', {}).get('interpretations', [])}
    _require(data.get('resolution', {}).get('exactRecallComplete') is True,
             '服务端没有完成成员候选枚举。')
    _require(data.get('resolution', {}).get('contextText', '') == request.get('contextText', ''),
             '关联追问语境与宿主提交的业务文字不同。')
    if request.get('intentDraft') is not None:
        def preserves(expected, actual):
            if isinstance(expected, dict):
                return isinstance(actual, dict) and all(k in actual and preserves(v, actual[k]) for k, v in expected.items())
            if isinstance(expected, list):
                return isinstance(actual, list) and len(expected) == len(actual) and all(preserves(a, b) for a, b in zip(expected, actual))
            return expected == actual
        _require(preserves(request['intentDraft'], data.get('resolution', {}).get('intentDraft')),
                 '服务端语义草案与提交的原文提炼不一致。')
    if request.get('sourceSelection') is not None:
        resolution = data.get('resolution', {})
        _require(resolution.get('sourceSelection') == request['sourceSelection'], '所选结果引用与提交的引用不一致。')
        selected = resolution.get('selectedFacts', [])
        _require({item.get('factId') for item in selected} == set(request['sourceSelection']['factIds'])
                 and len(selected) == len(request['sourceSelection']['factIds']), '历史事实引用未全部恢复。')
        expected = {item['targetId']: item for item in selected}
        _require(set(interpretations) == set(expected), '恢复的主体超出了所选历史事实。')
        for result in data.get('resultSets', []):
            source = expected.get(result.get('targetId'))
            _require(source is not None, '返回了未选择的历史主体。')
            _require(result.get('context', {}).get('target') == source['context']['target']
                     and result.get('context', {}).get('period') == source['context']['period'], '历史事实的任务、表样或时期发生变化。')
            for row in result.get('rows', []):
                _require(all(row.get('dimensions', {}).get(key) == value for key, value in source['dimensions'].items()),
                         '恢复的运行格与所选事实的实际维值不一致。')
    order = (request.get('intentDraft') or {}).get('presentation', {}).get('valueOrder')
    if order is not None:
        presentation = data.get('presentation', {})
        _require(order in ('ASC', 'DESC') and presentation.get('valueOrder') == order and presentation.get('nulls') == 'LAST',
                 '数值排序与展示要求不一致。')
        proofs = {item['resultSetId']: item for item in presentation.get('resultSets', [])}
        for result in data.get('resultSets', []):
            rows = result.get('rows', [])
            expected = sorted((row for row in rows if row.get('valueState') == 'PRESENT'),
                              key=lambda row: Decimal(row['value']), reverse=order == 'DESC')
            expected.extend(row for row in rows if row.get('valueState') != 'PRESENT')
            _require(rows == expected, '返回数值未按要求的精确数值顺序排列。')
            proof = proofs.get(result.get('resultSetId'), {})
            _require(proof.get('rowCount') == len(rows) and proof.get('orderedFactIds') == [row['factId'] for row in rows],
                     '排序结果的成员覆盖与来源不一致。')
    for target in data.get('targets', []):
        interpretation = interpretations.get(target.get('id'))
        _require(interpretation is not None and _target(target) == _target({**interpretation, 'label': target.get('label')}),
                 '业务目标与服务端记录的短语解释不一致。')
        _require(target.get('organizations', []) == interpretation.get('organizations', []), '组织范围与短语解释不一致。')
        _require(target.get('scope') == interpretation.get('scope'), '目标时期与语义解释不一致。')
        expected_hierarchies = [relation.get('hierarchy') for relation in (request.get('intentDraft') or {}).get('relations', [])
                                if relation.get('type') == 'HIERARCHY'
                                and target.get('logicalTargetId', target['id'].split(':', 1)[0]) in relation.get('targetIds', [])]
        actual_hierarchy = (target.get('scope') or {}).get('organizationHierarchy')
        normalized = lambda value: None if not value else (value.get('rootText'), value.get('level'))
        _require((not expected_hierarchies and actual_hierarchy is None) or
                 (expected_hierarchies and all(normalized(value) == normalized(actual_hierarchy) for value in expected_hierarchies)),
                 '实际机构层级范围与声明的语义关系不同。')
    _require(type(data.get('complete')) is bool, '业务查询缺少执行完整性。')
    _require(not data['complete'] or not data.get('unresolvedGroups'), '未确认的执行组不能声明全部完成。')
    from ..dimension_scopes import validate_execution
    validate_execution(request, data)
    scope = _resolved_scope(request, data)
    if selection == 'FULL_FORM':
        _require(any(scope.get(k) for k in ('formCodes', 'formNames')),
                 '整表读取必须来自明确表样范围。')

    steps = data.get('runtimeRequests', [])
    results = data.get('stepResults', [])
    _require(isinstance(steps, list) and all(isinstance(step, dict) and step.get('id') for step in steps),
             '服务端运行步骤缺少身份。')
    _require(len({step['id'] for step in steps}) == len(steps)
             and {step['id'] for step in steps} == {step.get('stepId') for step in results},
             '服务端步骤与结果没有一一对应。')

    def check_context(context, names, named_result=True):
        target = context.get('target', {})
        for key, field, values in (
                ('taskCodes', 'taskCode', target), ('formCodes', 'formCode', target),
                ('taskNames', 'taskName', names), ('formNames', 'formName', names)):
            if key.endswith('Names') and not named_result:
                continue  # Native context has codes; names are validated on canonical result sets below.
            _require(not scope.get(key) or values.get(field) in scope[key], '结果超出了指定的任务或表样。')
        _require(not scope.get('periodCode') or opening_matches(scope['periodCode'], context.get('period')),
                 '执行期与明确指定的时期不一致。')
        if scope.get('periodRequest'):
            resolution = context.get('periodResolution') or {}
            _require(context.get('period') in resolution.get('periodCodes', [])
                     and resolution.get('resolutionId') and resolution.get('ruleVersion'),
                     '运行时期缺少原时期请求的解析依据。')
        if scope.get('organizations'):
            _require(any(org['organizationDefinitionCode'] == target.get('organizationCategory')
                         and org['objectCode'] == context.get('effectiveDimensions', {}).get('MD_ORG')
                         for org in scope['organizations']), '结果超出了指定的组织机构。')
        base = context.get('baseContext') or {}
        _require(all(base.get(key) == receipt.get('context', {}).get(key)
                     for key in ('contextId', 'sourceId', 'snapshotId', 'asOf', 'schemaHash')),
                 '运行结果与固定的本体查询上下文不一致。')

    by_step = {step['id']: step for step in steps}
    for result in results:
        planned = by_step[result['stepId']]
        _require(planned.get('operation') == result.get('operation'), '运行步骤类型与结果不一致。')
        for form in result.get('formResults', []):
            check_context(form.get('runtime', {}).get('context', {}), form, False)
        for batch in result.get('batchResults', []):
            for metric in batch.get('metrics', []):
                check_context(metric.get('context', {}), metric, False)
            if 'cellTargets' in batch.get('request', {}) and batch.get('disposition') == 'SUCCEEDED':
                from .budget_exact import validate as validate_exact
                validate_exact(batch, planned.get('batchQuery', {}))

    targets = {target['id']: target for target in data.get('targets', [])}
    origins = data.get('origins', [])
    _require(len({o.get('originId') for o in origins}) == len(origins), '事实来源标识重复。')
    by_origin = {origin['originId']: origin for origin in origins}
    by_result = {result['stepId']: result for result in results}
    exact_values = {(result['stepId'], table.get('batchId')): {
                        value['targetRef']: value for value in table.get('data', {}).get('cellResults', [])}
                    for result in results for table in result.get('batchResults', [])
                    if 'cellTargets' in table.get('request', {}) and table.get('disposition') == 'SUCCEEDED'}
    for result_set in data.get('resultSets', []):
        context = result_set.get('context', {})
        check_context(context, context)
        target = targets.get(result_set.get('targetId'))
        _require(selection == 'FULL_FORM' or target is not None, '返回了未请求的业务目标。')
        organizations = ((target or {}).get('organizations', []) if selection != 'FULL_FORM' else
                         [org for item in interpretations.values() for org in item.get('organizations', [])])
        if organizations:
            _require(any(org['organizationDefinitionCode'] == context.get('target', {}).get('organizationCategory')
                         and org['objectCode'] == context.get('effectiveDimensions', {}).get('MD_ORG')
                         for org in organizations), '结果不满足原问题所召回的组织范围。')
        resolved = result_set.get('resolvedTerms', [])
        target_scope = (target or {}).get('scope') or {}
        period_scope = target_scope if target_scope.get('periodCode') or target_scope.get('periodRequest') else scope
        _require(not target_scope.get('periodCode') or opening_matches(target_scope['periodCode'], context.get('period')),
                 '结果的时期不属于对应语义目标。')
        for condition in (target or {}).get('allOf', []):
            allowed = {(m['baseDataCode'], m['objectCode']) for m in condition['anyOf']}
            _require(any(item.get('term') == condition['term']
                         and (item.get('baseDataCode'), item.get('objectCode')) in allowed for item in resolved),
                     '成员解释没有命中原目标的候选集合。')
        for row in result_set.get('rows', []):
            dimensions = row.get('dimensions', {})
            _require(all(_hierarchy_member(item, target_scope, row, by_origin, by_result) or
                dimensions.get(item.get('dimensionCode')) == item.get('objectCode') or (
                item.get('titleFallback') is True and item.get('dimensionCode') not in dimensions and any(
                    proof.get('term') == item.get('term') and proof.get('baseDataCode') == item.get('baseDataCode')
                    and proof.get('objectCode') == item.get('objectCode') and proof.get('verifiedBy') == 'RUNTIME_TITLE'
                    and proof.get('runtimeHeaders') for proof in row.get('conditionEvidence', []))) for item in resolved),
                     '运行单元格不满足已确认的成员条件。')
            _require(selection == 'FULL_FORM' or selected_period(context, row, period_scope, bool(request.get('sourceSelection'))),
                     '单元格时期不在所问范围内，或缺少时期回退的选择依据。')
            _require(row.get('originRefs') and all(ref in by_origin for ref in row['originRefs']), '事实缺少可追溯来源。')
            if row.get('valueState') == 'PRESENT':
                value_type = row.get('valueType', 'DECIMAL_STRING')
                _require(value_type in ('DECIMAL_STRING', 'STRING', 'BOOLEAN', 'DATE', 'DATETIME', 'UNKNOWN'), '未知事实类型。')
                if value_type == 'DECIMAL_STRING':
                    try:
                        _require(isinstance(row.get('value'), str) and Decimal(row['value']).is_finite(), '事实值必须是精确十进制字符串。')
                    except InvalidOperation as exc:
                        raise AnalysisError('BUDGET_QUERY_MISMATCH', '事实值不是有效十进制数。') from exc
                else:
                    _require(type(row.get('value')) in ((str, bool) if value_type == 'UNKNOWN' else
                             (bool,) if value_type == 'BOOLEAN' else (str,)), '事实值与声明类型不一致。')
            else:
                _require(row.get('value') is None, '空值、未加载或错误单元格不应包含数值。')
            for ref in row['originRefs']:
                origin = by_origin[ref]
                _require(origin.get('taskCode') == context.get('target', {}).get('taskCode')
                         and origin.get('formCode') == context.get('target', {}).get('formCode'),
                         '事实来源跨越任务或表样。')
                if origin.get('sourceType') != 'BATCH_QUERY' or row.get('valueState') in ('NOT_LOADED', 'ERROR'):
                    continue
                tables = by_result.get(origin.get('stepId'), {}).get('batchResults', [])
                table = next((t for t in tables if t.get('batchId') == origin.get('batchId')), None)
                _require(table is not None and table.get('disposition') == 'SUCCEEDED', '事实缺少成功的原生取数结果。')
                if 'cellTargets' in table.get('request', {}):
                    native = exact_values[(origin.get('stepId'), origin.get('batchId'))].get(origin.get('targetRef'))
                    _require(native is not None, '事实缺少唯一的精确目标引用。')
                    _require(origin.get('sourceColumn') == native.get('metric')
                             and row.get('valueType') == native.get('valueType')
                             and row.get('valueState') == ('NULL' if native.get('valueState') == 'NULL_VALUE' else native.get('valueState'))
                             and type(row.get('value')) is type(native.get('value')) and row.get('value') == native.get('value')
                             and all(row.get('dimensions', {}).get(key) == value for key, value in native.get('dimensions', {}).items()),
                             '业务事实与原生精确取数结果不一致。')
                    continue
                values, columns = table.get('data', {}).get('rows', []), table.get('data', {}).get('columns', [])
                if origin.get('rowIndex') is None:
                    _require(not values and row.get('value') is None, '空查询结果与事实值不一致。')
                else:
                    names = [column.get('name') for column in columns]
                    _require(origin.get('sourceColumn') in names and 0 <= origin['rowIndex'] < len(values),
                             '事实的原始行列定位不合法。')
                    column_index = names.index(origin['sourceColumn'])
                    native = values[origin['rowIndex']][column_index]
                    _require(row.get('valueType', 'DECIMAL_STRING') == columns[column_index].get('type'),
                             '事实类型与原生列类型不一致。')
                    _require(type(row.get('value')) is type(native) and row.get('value') == native,
                             'AI 事实值与原生数据值不一致。')
    _validate_value_filter(request, data, by_origin, by_result)
    return steps


def _resolved_scope(request, data):
    """Enforce the actual identities verified by native candidate resolution."""
    scope = dict(request.get('scope') or {})
    bindings = scope.get('bindings', [])
    proofs = (data.get('resolution') or {}).get('scopeBindings', [])
    _require(isinstance(proofs, list) and len(proofs) == len(bindings), '候选范围缺少完整的服务端确认依据。')
    for binding, proof in zip(bindings, proofs):
        _require(isinstance(proof, dict) and proof.get('binding') == binding,
                 '候选范围确认依据与请求不一致。')
        model, code, name = binding.get('modelCode'), proof.get('code'), proof.get('name')
        _require(model in ('BUDGET_TASK', 'BUDGET_FORM_TEMPLATE')
                 and isinstance(code, str) and bool(code.strip()) and isinstance(name, str) and bool(name.strip()),
                 '候选范围缺少确定的任务或表样身份。')
        prefix = 'task' if model == 'BUDGET_TASK' else 'form'
        scope[prefix + 'Codes'] = list(dict.fromkeys([*scope.get(prefix + 'Codes', []), code]))
        scope[prefix + 'Names'] = [value for value in scope.get(prefix + 'Names', []) if value != binding['text']]
    return scope


def _validate_value_filter(request, data, origins, results):
    expected = (request.get('intentDraft') or {}).get('valueFilter')
    if expected is None:
        return
    proof = data.get('valueFilter') or {}
    if proof.get('basis') in ('BUDGET_NATIVE_RESULT_WHERE', 'BUDGET_NATIVE_FILTER'):
        from ..value_predicates import validate, compile_native
        _require(validate(proof.get('condition')) == validate(expected), '原生数值筛选与业务条件不一致。')
        native = {batch['batchId']: batch for step in results.values() for batch in step.get('batchResults', [])}
        verified = set()
        for item in proof.get('batches', []):
            batch = native.get(item.get('batchId'))
            _require(batch is not None, '筛选摘要缺少原生回执。')
            aliases = item.get('logicalTargetColumns') or {}
            _require(bool(aliases) and len(set(aliases.values())) == len(aliases), '筛选目标与指标列没有一一绑定。')
            filter_key = 'having' if item.get('filterStage') == 'HAVING' else 'resultWhere'
            if filter_key == 'having':
                _require(bool(batch.get('request', {}).get('aggregation')), '汇总值筛选缺少分组汇总请求。')
            _require(compile_native(expected, aliases) == batch.get('request', {}).get(filter_key) == item.get(filter_key),
                     '批量请求未原样执行完整的组合条件。')
            _require(item.get('rangeSummary') == batch.get('data', {}).get('rangeSummary', {}), '筛选统计与预算回执不一致。')
            _require(item.get('aggregationSummary', {}) == batch.get('data', {}).get('aggregationSummary', {}),
                     '汇总统计与预算回执不一致。')
            _require(item.get('status') == batch.get('disposition'), '筛选状态与原生回执不一致。')
            verified.add(batch['batchId'])
        selected = []
        for selection in proof.get('analysisSelection', []):
            batch = native.get(selection.get('batchId'), {})
            metric = next((m for m in batch.get('metrics', []) if m.get('name') == selection.get('sourceColumn')), {})
            rows = batch.get('data', {}).get('rows', [])
            index = selection.get('rowIndex')
            _require(selection.get('batchId') in verified and metric and type(index) is int and 0 <= index < len(rows),
                     '分析目标缺少筛选命中行的来源。')
            dimensions = dict(metric.get('selectedDimensions') or {})
            for position, column in enumerate(batch.get('data', {}).get('columns', [])):
                role = batch.get('columnRoles', {}).get(column.get('name'), {})
                if role.get('kind') in ('DIMENSION', 'PROPERTY') and rows[index][position] is not None:
                    key = role['dimCode'] + ('__' + role['field'] if role.get('field') else '')
                    dimensions[key] = rows[index][position]
            _require(selection.get('dimensions') == dimensions
                     and selection.get('measureCode') == metric.get('measureCode')
                     and selection.get('modelCode') == str(metric.get('cellExp', '')).split('[', 1)[0]
                     and selection.get('binding') == metric.get('context', {}).get('target', {}).get('formBindingKey'),
                     '公式目标的坐标与筛选来源不一致。')
            selected.append(selection)
        for result in data.get('resultSets', []):
            for row in result.get('rows', []):
                if 'analysisSelection' in proof:
                    _require(any(item.get('targetId') == result.get('targetId')
                        and item.get('binding') == result.get('context', {}).get('target', {}).get('formBindingKey')
                        and item.get('dimensions') == row.get('dimensions')
                        and item.get('measureCode') == row.get('measureCode')
                        and item.get('modelCode') == str(row.get('cellExp', '')).split('[', 1)[0] for item in selected),
                        '分析格不是完整数值条件命中的对象。')
                    continue
                for ref in row.get('originRefs', []):
                    origin = origins.get(ref, {})
                    if origin.get('sourceType') == 'BATCH_QUERY' and row.get('valueState') not in ('ERROR', 'NOT_LOADED'):
                        _require(origin.get('batchId') in verified, '结果没有对应已执行的筛选回执。')
        if data.get('complete'):
            _require(bool(verified), '筛选尚未执行，不能声明完整。')
        return
    from ..value_predicates import scalar_comparison
    compare, threshold = scalar_comparison(expected)
    proof = data.get('valueFilter') or {}
    _require(all(proof.get(key) == value for key, value in expected.items()), '数值筛选与原始要求不一致。')
    candidates = proof.get('evaluatedFacts', [])
    _require(proof.get('populationCount') == len(candidates), '数值筛选没有完整的总体计数。')
    matched = 0
    def admitted_value(row):
        if row.get('valueState') != 'PRESENT':
            return False
        try:
            value = Decimal(row['value'])
            _require(value.is_finite(), '数值筛选的来源值不是有限数值。')
            return compare(value, threshold)
        except (InvalidOperation, TypeError, KeyError):
            raise AnalysisError('BUDGET_QUERY_MISMATCH', '数值筛选的来源值不是有效十进制数。') from None
    for row in candidates:
        admitted = admitted_value(row)
        _require(row.get('matched') is admitted, '数值筛选的判断与来源数值不一致。')
        matched += admitted
        _require(row.get('originRefs'), '数值筛选缺少原生取数依据。')
        for reference in row['originRefs']:
            origin = origins.get(reference, {})
            tables = results.get(origin.get('stepId'), {}).get('batchResults', [])
            if origin.get('sourceType') != 'BATCH_QUERY':
                _require(origin.get('sourceType') == 'FORM_RUNTIME', '数值筛选的来源未确认。')
                continue
            table = next((table for table in tables if table.get('batchId') == origin.get('batchId')), None)
            _require(table is not None and table.get('disposition') == 'SUCCEEDED', '筛选依据没有成功的原生查询。')
            native = table.get('data', {})
            if 'cellTargets' in table.get('request', {}):
                cell = next((cell for cell in native.get('cellResults', []) if cell.get('targetRef') == origin.get('targetRef')), None)
                _require(cell is not None and cell.get('value') == row.get('value'), '筛选值与精确单元格不一致。')
            else:
                columns = [column.get('name') for column in native.get('columns', [])]
                index = origin.get('rowIndex')
                if index is None:
                    _require(not native.get('rows') and row.get('value') is None, '空总体被伪造成数值。')
                else:
                    _require(type(index) is int and 0 <= index < len(native.get('rows', []))
                             and origin.get('sourceColumn') in columns, '筛选值的原生行列引用无效。')
                    _require(native['rows'][index][columns.index(origin['sourceColumn'])] == row.get('value'), '筛选值与原生数据不一致。')
    _require(proof.get('matchedCount') == matched, '数值筛选的命中计数不一致。')
    if data.get('complete'):
        _require(proof.get('populationComplete') is True, '总体未确认完毕，不能声明筛选全部完成。')
    for result in data.get('resultSets', []):
        for row in result.get('rows', []):
            _require(admitted_value(row), '交付值不满足数值筛选条件。')


def _hierarchy_member(member, scope, row, origins, results):
    """A root is a selector, not equality with each child. Verify the actual native hierarchy and row key."""
    hierarchy = scope.get('organizationHierarchy')
    if not hierarchy or member.get('dimensionCode') != 'MD_ORG' or member.get('term') != hierarchy.get('rootText'):
        return False
    expected = {'rootCode': member.get('objectCode')}
    if hierarchy.get('level') is not None:
        expected['level'] = hierarchy['level']
    for ref in row.get('originRefs', []):
        origin = origins.get(ref, {})
        if origin.get('sourceType') != 'BATCH_QUERY':
            continue
        for table in results.get(origin.get('stepId'), {}).get('batchResults', []):
            if table.get('batchId') != origin.get('batchId') or table.get('disposition') != 'SUCCEEDED':
                continue
            conditions = [condition for condition in table.get('request', {}).get('batchDimensions', [])
                          if condition.get('dimCode') == 'MD_ORG']
            if conditions != [{'dimCode': 'MD_ORG', 'hierarchy': expected}]:
                continue
            columns, rows = table.get('data', {}).get('columns', []), table.get('data', {}).get('rows', [])
            index = origin.get('rowIndex')
            if index is None:
                return not rows and row.get('value') is None
            if type(index) is not int or not 0 <= index < len(rows):
                continue
            for position, column in enumerate(columns):
                role = table.get('columnRoles', {}).get(column.get('name'), {})
                if (role.get('kind') == 'DIMENSION' and role.get('dimCode') == 'MD_ORG'
                        and rows[index][position] == row.get('dimensions', {}).get('MD_ORG')):
                    return True
    return False


def server_steps(capture, receipt):
    return validate(capture, receipt)
