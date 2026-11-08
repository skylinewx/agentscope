"""消费本体定位的目标；不推断业务范围，不复制公式计算引擎。"""
from copy import deepcopy

from .models import AnalysisError


def prepared_forms(capture):
    from .adapters.budget_v2 import receipt_from_capture, form_rows, validate_form_request
    receipt = receipt_from_capture(capture)
    for step, form in form_rows(receipt):
        preparation = form.get('preparation')
        if not isinstance(preparation, dict):
            continue
        context, observation = validate_form_request(capture, receipt, step, form)
        count, keys = preparation.get('targetCount'), preparation.get('targetKeys')
        if (type(count) is not int or count < 0 or not isinstance(keys, list)
                or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)
                or len(keys) > count or preparation.get('targetKeysComplete') is True and len(keys) != count):
            raise AnalysisError('PREPARED_TARGETS_INVALID', '本体返回的目标目录计数不一致。')
        yield step, form, context, observation


def summaries(capture):
    """给模型短选择器和候选信息；目标坐标保留在完整回执中。"""
    result = []
    for step, form, context, observation in prepared_forms(capture):
        preparation = form['preparation']
        selector = {'captureRef': capture['captureRef'], 'stepId': step,
                    'observationId': observation['observationId'], 'preparedTargets': True, 'role': 'PRIMARY'}
        grain = _confirmed_grain(capture, form, context)
        if grain:
            selector['grain'] = grain
        summary = {key: deepcopy(value) for key, value in preparation.items() if key != 'targetKeys'}
        summary.update(stepId=step, observationId=observation['observationId'],
                       formulaRequested=bool(form.get('formulaGraph')),
                       formulaComplete=(form.get('formulaGraph') or {}).get('complete'),
                       formulaIssues=(form.get('formulaGraph') or {}).get('issues', []))
        if preparation.get('targetKeysComplete') is True and preparation['targetCount']:
            summary['selection'] = selector
            # 仅呈现少量目标候选的差异，不把公式依赖或大网格重新搬进模型上下文。
            dimensions = preparation.get('varyingDimensions', [])
            if dimensions and preparation['targetCount'] <= 12:
                from .grid_files import captured_cells
                keys = set(preparation['targetKeys'])
                summary['candidates'] = [{'targetDimensions': {key: cell.get('dimensions', {})[key]
                                              for key in dimensions if key in cell.get('dimensions', {})},
                                          'headers': cell.get('headers', [])}
                                         for cell in captured_cells(capture, form) if cell.get('key') in keys]
        result.append(summary)
    return result


def _confirmed_grain(capture, form, context, requested=None):
    """Use the executed dimension scope, or resolve names against its own catalog."""
    if capture.get('originalToolName') != 'execute_budget_query':
        return requested
    from .adapters.budget_v2 import receipt_from_capture
    from .grid_files import captured_cells
    data = receipt_from_capture(capture).get('data', {})
    keys = set(form['preparation']['targetKeys'])
    roots = [cell for cell in captured_cells(capture, form) if cell.get('key') in keys]
    if not roots:
        return requested
    coordinates = [{**context.get('effectiveDimensions', {}), **cell.get('dimensions', {})} for cell in roots]
    available = set.intersection(*(set(row) for row in coordinates))
    models = {cell['modelCode'] for cell in roots if isinstance(cell.get('modelCode'), str)}
    aliases = {code: {code} for code in available}
    for entry in data.get('dimensionCatalog', []):
        code, model = entry.get('dimCode'), entry.get('modelCode')
        if code not in available or not model or not any(value == model or value.rsplit('_', 1)[0] == model for value in models):
            continue
        for name in [entry.get('label'), *entry.get('names', [])]:
            if isinstance(name, str) and name:
                aliases.setdefault(name, set()).add(code)
    if requested is not None:
        if not isinstance(requested, list) or not requested or any(not isinstance(name, str) or not name for name in requested):
            raise AnalysisError('INVALID_GRAIN', '分析粒度使用非空维度名称数组。')
        result = []
        for name in requested:
            codes = {name} if name in available else aliases.get(name, set())
            if len(codes) != 1:
                raise AnalysisError('ANALYSIS_GRAIN_UNRESOLVED', '分析维度名称未在当前目标中唯一确认。',
                                    {'dimension': name, 'candidates': sorted(codes), 'availableDimensions': sorted(available)})
            result.append(next(iter(codes)))
        if len(set(result)) != len(result):
            raise AnalysisError('INVALID_GRAIN', '分析粒度不能重复指定同一维度。')
        return result
    binding = context.get('target', {}).get('formBindingKey')
    scopes = [item for item in data.get('scopeResolution', [])
              if item.get('status') == 'RESOLVED' and item.get('binding') == binding]
    # A source-confirmed range identifies subjects even when only one value matched
    # the predicate. Fixed scenario/currency values do not become subject dimensions.
    ranged = {item['dimCode'] for item in scopes if item.get('dimCode') in available
              and (any((item.get('condition') or {}).get(key) for key in ('all', 'hierarchy', 'where'))
                   or len((item.get('condition') or {}).get('values', [])) > 1)}
    return sorted(ranged) or None


def normalize_prepared_grains(captures, selections, scope):
    result = deepcopy(selections)
    for item in result:
        capture = captures.get(item.get('captureRef'), {})
        if capture.get('originalToolName') != 'execute_budget_query' or not item.get('preparedTargets'):
            continue
        matches = [(form, context) for step, form, context, observation in prepared_forms(capture)
                   if step == item['stepId'] and (not item.get('observationId')
                       or item['observationId'] == observation['observationId'])]
        if len(matches) != 1:
            continue  # The normal selection validator reports missing/ambiguous observations.
        requested = item.get('grain')
        if requested is None and scope.get('memberDimension'):
            requested = [scope['memberDimension']]
        grain = _confirmed_grain(capture, *matches[0], requested)
        if grain:
            item['grain'] = grain
    return result


def resolve(capture, selection):
    """按已确认维度选择本体目标；维度只约束目标，不误滤公式依赖。"""
    if selection.get('cellKeys') or selection.get('includeFormulaDependencies') is not None:
        raise AnalysisError('INVALID_PREPARED_SELECTION', 'preparedTargets 与 cellKeys/includeFormulaDependencies 二选一；依赖由本体准备状态确定。')
    matches = [(form, observation) for step, form, context, observation in prepared_forms(capture)
               if step == selection['stepId']
               and all(selection.get(key) is None or selection[key] == value for key, value in (
                   ('observationId', observation['observationId']),
                   ('bindingRef', context['target']['formBindingKey']), ('executionPeriod', context.get('period'))))]
    if len(matches) != 1:
        raise AnalysisError('PREPARED_TARGETS_AMBIGUOUS', '选择器必须指向唯一的本体目标目录。')
    form, observation = matches[0]
    preparation = form['preparation']
    if preparation.get('targetKeysComplete') is not True or not preparation['targetCount']:
        raise AnalysisError('PREPARED_TARGETS_INCOMPLETE', '目标目录为空或超出容量，请在原观察中进一步限定已确认条件。')
    from .grid_files import captured_cells
    keys = set(preparation['targetKeys'])
    targets = {cell.get('key'): cell for cell in captured_cells(capture, form) if cell.get('key') in keys}
    if set(targets) != keys:
        raise AnalysisError('PREPARED_TARGETS_INCOMPLETE', '目标目录对应的原始单元格未完整捕获。')
    dimensions = selection.get('targetDimensions', {})
    if not isinstance(dimensions, dict) or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                                               for key, value in dimensions.items()):
        raise AnalysisError('INVALID_PREPARED_SELECTION', 'targetDimensions 使用已确认的维度标识和值，不接受名称或空值。')
    context = form['runtime']['context']
    selected = [key for key in preparation['targetKeys']
                if all({**context.get('effectiveDimensions', {}), **targets[key].get('dimensions', {})}.get(dim) == value
                       for dim, value in dimensions.items())]
    if not selected:
        raise AnalysisError('PREPARED_TARGET_NOT_FOUND', '已确认条件未命中本体目标；保留原候选，不扩大范围或选择首格。',
                            {'targetDimensions': dimensions, 'targetCount': preparation['targetCount']})
    current = deepcopy(selection)
    current.pop('preparedTargets')
    current.pop('targetDimensions', None)
    current.setdefault('role', 'PRIMARY')
    current.update(cellKeys=selected, observationId=observation['observationId'],
                   includeFormulaDependencies=bool(form.get('formulaGraph')))
    return current


def formula_work(targets, facts, derived):
    """默认工作项由真实目标和已执行结果生成；不让模型重复维护同一套引用。"""
    available = {fact['factId'] for fact in facts}
    targets = sorted(set(targets) & available)
    if not targets:
        return []
    refs = [value['derivedId'] for value in derived if value.get('operator') == 'FORMULA_CHECK'
            and set(value.get('inputRefs', [])) & set(targets)]
    return [{'taskId': 'prepared_values', 'title': '已定位目标的来源值', 'kind': 'VALUES', 'refs': targets},
            {'taskId': 'prepared_formulas', 'title': '目标的表内公式构成', 'kind': 'COMPUTATION',
             'operator': 'FORMULA_CHECK', 'requiredEvidence': 'FORMULAS', 'targetRefs': targets, 'refs': refs}]


def bind_formula_work(tasks, root_ids, derived, links):
    """显式计划可用同一批量主体的事实引用。只补已经计算的、与该任务目标相符的结果。"""
    checks = {}
    for value in derived:
        if value.get('operator') == 'FORMULA_CHECK' and len(value.get('inputRefs', [])) == 1:
            checks.setdefault(value['inputRefs'][0], []).append(value['derivedId'])
    for task in tasks:
        original = task.get('targetRefs')
        if task.get('kind') != 'COMPUTATION' or task.get('operator') != 'FORMULA_CHECK' or not isinstance(original, list) or not original:
            continue
        if any(not isinstance(ref, str) for ref in original) or len(original) != len(set(original)):
            continue
        targets = [links.get(ref, ref) for ref in original]
        if not all(ref in root_ids and ref in checks for ref in targets) or len(targets) != len(set(targets)):
            continue
        task['targetRefs'] = targets
        if task.get('refs', []) == [] and all(len(checks[ref]) == 1 for ref in targets):
            task['refs'] = [checks[ref][0] for ref in targets]


def infer_selections(service, captures, scope, declared=()):
    """只在提交的回执能唯一确定选用范围时省略 selections。"""
    from .adapters import budget_batch
    from .adapters.budget_v2 import receipt_from_capture
    from .followup_scope import _selectors
    turn = service.turn
    inherited = _selectors(turn) if turn is not None else []
    if scope.get('referenceMembers'):
        raise AnalysisError('PREPARATION_NEEDS_SELECTION', '对照输入需要用 selections 指定实际来源和用途。')
    chosen, batches, form_steps, observed_steps = [], [], set(), set()
    declared_inputs = {(record['selection']['captureRef'], record['selection']['stepId'], record['selection']['observationId'])
                       for record in declared if 'selection' in record}
    form_contexts = {}
    for ref, capture in captures.items():
        if service.sources.for_capture(capture).adapter_id != 'budget-v2':
            raise AnalysisError('PREPARATION_NEEDS_SELECTION', '该来源请使用它返回的显式选择器。')
        receipt = receipt_from_capture(capture)
        for item in budget_batch.selectors(capture):
            prior = [selection for selection in inherited if selection.get('captureRef') == ref
                     and selection.get('stepId') == item['stepId'] and selection.get('batchId') == item['batchId']]
            if len(prior) == 1:
                batches.append({**deepcopy(prior[0]), 'role': prior[0].get('role', 'PRIMARY')})
            elif item['disposition'] == 'SUCCEEDED' and len(item['columnNames']) == 1 and not prior:
                batches.append({key: item[key] for key in ('captureRef', 'stepId', 'batchId', 'columnNames', 'rowIndices', 'selectionReason')
                                if key in item} | {'role': 'PRIMARY'})
            else:
                raise AnalysisError('PREPARATION_NEEDS_SELECTION', '请在候选指标或已有子集之间明确选择。', {'selectionRead': {'section': 'selectors', 'captureRef': ref}})
        # 一个计划有多个对象时逐分支核对到达情况；不同计划允许使用相同的 stepId。
        from .models import digest
        plan = capture['request'].get('plan') or {}
        identity = digest([receipt.get('context'), receipt.get('querySessionId'), plan])
        for step in plan.get('steps', []):
            if step.get('operation') == 'FORM_VIEW_QUERY':
                form_steps.add((identity, step['id']))
        for step, form, context, observation in prepared_forms(capture):
            observed_steps.add((identity, step))
            position = (ref, step, observation['observationId'])
            if position in declared_inputs:
                continue
            preparation = form['preparation']
            if form.get('file') and not form.get('formulaGraph'):
                raise AnalysisError('PREPARATION_FILE_INPUT', '这是供扫描的网格文件，请按已确认条件读取 gridInputs。', {'section': 'gridFiles', 'captureRef': ref})
            if (observation.get('entryType') != 'INPUT_VO' or not preparation.get('targetKeysComplete')
                    or preparation['targetCount'] != 1):
                raise AnalysisError('PREPARATION_NEEDS_SELECTION', '多个目标或目录未齐，请只补已确认的区别条件。', {
                    'varyingDimensions': preparation.get('varyingDimensions', []), 'targetCount': preparation['targetCount'],
                    'read': {'section': 'materials', 'captureRef': ref}})
            selected = {'captureRef': ref, 'stepId': step, 'observationId': observation['observationId'],
                        'preparedTargets': True, 'role': 'PRIMARY'}
            grain = _confirmed_grain(capture, form, context)
            if grain:
                selected['grain'] = grain
            chosen.append(selected)
            form_contexts[position] = context, form
    missing = sorted(step for plan_id, step in form_steps - observed_steps)
    if missing:
        raise AnalysisError('PREPARATION_INCOMPLETE', '以下预期表展开尚无可用准备目录。', {'missingSteps': missing})
    if len(batches) > 1 or (batches and chosen and not (turn and turn.state.get('requiredEvidence') == 'FORMULAS')):
        raise AnalysisError('PREPARATION_NEEDS_SELECTION', '多个数据来源的主体或对照用途需要明确。', {'section': 'selectors'})
    if batches and chosen:
        from .batch_scope import selected_tables
        from .budget_preparation import metric_target, form_selector
        selection, table, _, columns, rows = next(selected_tables(captures, batches))
        batch_capture = captures[selection['captureRef']]
        receipt = receipt_from_capture(batch_capture)
        metrics = [metric for metric in table['metrics'] if metric['name'] in columns]
        grain = {column['name']: index for index, column in enumerate(table['data']['columns'])
                 if table['columnRoles'][column['name']]['kind'] == 'DIMENSION'}
        if len(metrics) != 1 or not grain or table['data'].get('aggregationSummary'):
            raise AnalysisError('PREPARATION_NEEDS_SELECTION', '这些输入的业务粒度和用途需要显式选择。')
        source_rows = table['data']['rows']
        population = {tuple(source_rows[index][i] for i in grain.values()) for index in (rows if rows is not None else range(len(source_rows)))}
        for item in chosen:
            context, form = form_contexts[item['captureRef'], item['stepId'], item['observationId']]
            capture = captures[item['captureRef']]
            from .grid_files import captured_cells
            root_key, = form['preparation']['targetKeys']
            root = next((cell for cell in captured_cells(capture, form) if cell.get('key') == root_key), None)
            if root is None:
                raise AnalysisError('PREPARATION_NEEDS_SELECTION', '无法从本次观察读取已选目标单元格。')
            coordinates = {**context['effectiveDimensions'], **(root.get('dimensions') or {})}
            subject = {key: coordinates.get(key) for key in grain}
            if tuple(subject.values()) not in population or form_selector(capture, receipt_from_capture(capture),
                    metric_target(batch_capture, receipt, metrics[0], subject)) != {key: value for key, value in item.items() if key not in ('role', 'grain')}:
                raise AnalysisError('PREPARATION_NEEDS_SELECTION', '此表展开不能证明对应所选总体的实际成员与同一指标。')
            item['grain'] = selection.get('grain', list(grain))
    elif chosen and not scope.get('memberDimension') and not all(item.get('grain') for item in chosen):
        raise AnalysisError('PREPARATION_NEEDS_SELECTION', '请按已确认的分析对象提供 scope.memberDimension 或显式 grain。')
    if not chosen and not batches and not declared_inputs:
        raise AnalysisError('PREPARATION_INCOMPLETE', '所选回执没有准备完成的数据目标。')
    return batches + chosen
