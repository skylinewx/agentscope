"""把本轮已确认的预算指标和主体转换成可复用的表展开证据。"""
from collections import defaultdict
from copy import deepcopy
from time import perf_counter
import asyncio
import logging

from .actions import source_identity
from .adapters import budget_batch
from .adapters.budget_v2 import receipt_from_capture, stored_form_request
from .models import AnalysisError, digest
from .prepared_inputs import prepared_forms


def data_analysis(turn):
    return turn is not None and turn.state.get('mode') in ('ANALYSIS', 'REPORT')


def boundary(capture, receipt):
    """同一用户会话内仍按实际连接、查询上下文与快照隔离。"""
    return digest([source_identity(capture), receipt['context'], receipt['querySessionId']])


def remember_capability(turn, capture):
    if capture.get('originalToolName') not in ('discover_query_targets', 'list_capabilities'):
        return
    if turn.service.sources.for_capture(capture).adapter_id != 'budget-v2':
        return
    try:
        receipt = receipt_from_capture(capture)
    except AnalysisError:
        return
    if receipt.get('status') != 'SUCCEEDED':
        return
    data = receipt.get('data') or {}
    capability = data.get('formCapabilityId')
    if not capability:
        choices = {item.get('capabilityId') for item in data.get('capabilities', [])
                   if item.get('operation') == 'FORM_VIEW_QUERY' and item.get('modelCode') == 'BUDGET_TASK_FORM_BINDING'}
        capability = choices.pop() if len(choices) == 1 else None
    if isinstance(capability, str) and capability:
        cache = turn.state.setdefault('budgetFormCapabilities', {})
        cache[boundary(capture, receipt)] = capability
        while len(cache) > 32:
            del cache[next(iter(cache))]


def metric_target(capture, receipt, metric, subject=None):
    """定位观察提供模板结构；变更主体时按来源表外维值重新展开。"""
    context = metric['context']
    outer = dict(context['effectiveDimensions'])
    selected = dict(metric['selectedDimensions'])
    period = context['period']
    explicit = dict(context.get('requestedDimensions') or {})
    observation_id = metric.get('selectionObservationId')
    expression = metric.get('cellExp')
    if not observation_id or not expression or not metric.get('measureCode'):
        raise AnalysisError('PREPARATION_TARGET_UNPROVEN', '该批量指标缺少已定位的观察、表达式或指标。')
    for dimension, value in (subject or {}).items():
        if not isinstance(value, str) or not value:
            raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '所选行缺少可用于定位单元格的维值。', {'dimension': dimension})
        if dimension == 'DATATIME':
            if value == selected.get(dimension, period):
                continue
            binding = metric.get('periodBinding')
            if binding == 'EXECUTION' and value[4:5] == period[4:5]:
                period = value
                outer[dimension] = value
            elif binding != 'CELL':
                raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '无法从数据期确定该单元格的运行期，请明确表展开条件。', {'dimension': dimension})
            selected[dimension] = value
        elif dimension == 'MD_STAGE':
            if value != selected.get(dimension, outer.get(dimension)):
                raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '所选行与表样只读阶段不同。', {'dimension': dimension})
        elif dimension in selected:
            # 单元格表达式的维值优先；即使同名维度也出现在表外，运行条件仍沿用原观察。
            selected[dimension] = value
        elif dimension in outer:
            if outer[dimension] != value:
                outer[dimension] = explicit[dimension] = value
        else:
            raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '所选行的维度尚未在表外条件或单元格中定位。', {'dimension': dimension})
    # 批量定位的表达式可能仍含 -0N，而 INPUT_VO 返回具体时期。物理表来自方括号前的来源标识；
    # 时期只使用本体已经解析的 selectedDimensions，不在宿主再实现偏移运算。
    physical_table, bracket, _ = expression.partition('[')
    if not bracket or not physical_table.strip():
        raise AnalysisError('PREPARATION_TARGET_UNPROVEN', '无法从来源指标定位物理表。')
    query = {'modelCode': physical_table.strip(), 'measureCode': metric['measureCode'],
             'targetDimensions': {key: value for key, value in selected.items() if key != 'MD_STAGE'}, 'includeFormulaDependencies': True}
    if outer == context['effectiveDimensions']:
        query['observationId'] = observation_id
    else:
        # effectiveDimensions 含 initMainDim 注入的只读阶段、表样默认值等；它们用来核对结果。
        # 只提交原来显式选择过的表外条件及本次主体覆盖，让预算服务重新解析只读维度。
        query.update(periodCode=period, dimensions={key: value for key, value in explicit.items()
                                                               if key not in ('DATATIME', 'MD_STAGE')})
    binding = context['target']['formBindingKey']
    identity = [boundary(capture, receipt), context['target'], period, outer, selected, expression, metric['measureCode']]
    return {'key': digest(identity), 'boundary': boundary(capture, receipt), 'binding': binding, 'outer': outer, 'period': period,
            'context': context, 'query': query, 'source': capture['captureRef'], 'subject': subject or {}}


def form_selector(capture, receipt, target):
    """只复用同一上下文中确实含目标及公式结构的观察。"""
    if boundary(capture, receipt) != target['boundary']:
        return None
    from .grid_files import captured_cells
    for step, form, context, observation in prepared_forms(capture):
        if (observation.get('entryType') != 'INPUT_VO' or context['target'] != target['context']['target']
                or context.get('period') != target.get('period', target['context']['period']) or context.get('effectiveDimensions') != target['outer']):
            continue
        preparation = form['preparation']
        graph = form.get('formulaGraph') or {}
        if not graph or not preparation.get('targetKeysComplete'):
            continue
        keys = set(preparation['targetKeys'])
        query = target['query']
        matches = [cell for cell in captured_cells(capture, form)
                   if cell.get('key') in keys and cell.get('modelCode') == query['modelCode']
                   and cell.get('measureCode') == query['measureCode']
                   and all(cell.get('dimensions', {}).get(key) == value for key, value in query['targetDimensions'].items())]
        if len(matches) == 1:
            selector = {'captureRef': capture['captureRef'], 'stepId': step, 'observationId': observation['observationId']}
            if len(keys) == 1:
                selector['preparedTargets'] = True
            else:
                # 组合观察中其它科目可能共用指标和维值，按已核实的原始目标缩小到这一格。
                selector.update(cellKeys=[matches[0]['key']], includeFormulaDependencies=True)
            return selector
    return None


def batch_targets(service, capture, selections=None):
    """只读取被选批量指标的定位元数据，主体汇总金额仍由原生批量表提供。"""
    if service.sources.for_capture(capture).adapter_id != 'budget-v2':
        return []
    if capture.get('originalToolName') != 'execute_semantic_plan':
        return []
    receipt = receipt_from_capture(capture)
    targets = []
    for step, table, _ in budget_batch.tables(receipt):
        if table.get('disposition') != 'SUCCEEDED':
            continue
        chosen = [item for item in selections or [] if item.get('captureRef') == capture['captureRef']
                  and item.get('batchId') == table['batchId'] and item.get('stepId') == step
                  and item.get('role', 'PRIMARY') == 'PRIMARY']
        if selections is not None and not chosen:
            continue
        budget_batch.validate(capture, receipt, step, table)
        if selections is None:
            names = {name for name, role in table['columnRoles'].items() if role.get('kind') == 'METRIC'}
        else:
            names = {name for selection in chosen for name in budget_batch.selection_scope(selection, table)[0]}
        targets.extend(metric_target(capture, receipt, metric) for metric in table['metrics'] if metric['name'] in names)
    return targets


def formula_subjects(turn, subjects):
    """AI 选择业务主体；所有维值必须唯一命中所选历史明细，且沿用真实指标。"""
    from .followup_scope import _inherited_tables, _selectors, _changes
    if (not isinstance(subjects, list) or not 1 <= len(subjects) <= 100
            or any(not isinstance(item, dict) or not item or
                   any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in item.items())
                   for item in subjects) or len({digest(item) for item in subjects}) != len(subjects)):
        raise AnalysisError('INVALID_FORMULA_SUBJECTS', 'formulaSubjects 使用已确认且不重复的主体维值映射。')
    if _changes(turn) & {'task', 'period', 'scenario', 'metric'}:
        raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '本轮已改变指标或取数条件，请先定位新条件下的目标。')
    tables = _inherited_tables(turn, _selectors(turn))
    if len(tables) != 1:
        raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '展开主体需要唯一的已选批量明细来源。')
    selection, capture, table, rows = tables[0]
    receipt = receipt_from_capture(capture)
    data = budget_batch.validate(capture, receipt, selection['stepId'], table)
    names, _ = budget_batch.selection_scope(selection, table)
    metrics = [metric for metric in table['metrics'] if metric['name'] in names]
    if len(metrics) != 1 or data.get('aggregationSummary'):
        raise AnalysisError('PREPARATION_TARGET_AMBIGUOUS', '请选择一个具体明细指标，再确定要分析其构成的主体。')
    columns = {column['name']: position for position, column in enumerate(data['columns'])
               if table['columnRoles'][column['name']]['kind'] == 'DIMENSION'}
    selected_rows = data['rows'] if rows is None else [data['rows'][index] for index in rows]
    targets = []
    seen = set()
    for subject in subjects:
        matches = (row for row in selected_rows if subject.keys() <= columns.keys()
                   and all(row[columns[key]] == value for key, value in subject.items()))
        row = next(matches, None)
        if row is None or next(matches, None) is not None:
            raise AnalysisError('PREPARATION_SUBJECT_NOT_UNIQUE', '主体须唯一命中承接范围中的真实数据行。', {'subject': subject})
        coordinates = {key: row[index] for key, index in columns.items()}
        target = metric_target(capture, receipt, metrics[0], coordinates)
        if target['key'] in seen:
            raise AnalysisError('INVALID_FORMULA_SUBJECTS', '不同的主体选择重复命中了同一单元格。')
        seen.add(target['key'])
        target['grain'] = selection.get('grain', list(columns))
        target['batchSource'] = {'stepId': selection['stepId'], 'batchId': table['batchId'], 'columnName': metrics[0]['name']}
        targets.append(target)
    return targets


def formula_fact_links(turn, facts, root_ids):
    """仅在同一已确认主体、指标和已验证的表观察中关联批量事实与公式目标。"""
    if not data_analysis(turn) or not root_ids:
        return {}
    declared = {target['key']: target for target in turn.state.get('budgetFormulaTargets', [])}
    primary = defaultdict(list)
    for fact in facts:
        if fact.get('inputRole') == 'PRIMARY':
            for ref in fact['captureRefs']:
                primary[ref].append(fact)
    candidates = defaultdict(set)
    for record in turn.state.get('budgetFormulaPreparation', []):
        target = declared.get(record['targetKey'])
        selection = record.get('selection')
        if not selection or not target or not target.get('batchSource'):
            continue
        source_rows = [fact for fact in primary[target['source']]
                       if all(fact.get(key) == value for key, value in target['batchSource'].items())
                       and all(fact['dimensions'].get(key) == value for key, value in target['subject'].items())]
        selected_roots = [fact for fact in primary[selection['captureRef']]
                          if fact['factId'] in root_ids and fact.get('stepId') == selection['stepId']
                          and fact.get('observationId') == selection['observationId']
                          and ('cellKeys' not in selection or fact.get('cellKey') in selection['cellKeys'])]
        # 各自保留来源值；只有一对一且金额一致时，工作项才可沿关联复用公式检查。
        if len(source_rows) == len(selected_roots) == 1 and source_rows[0].get('value') == selected_roots[0].get('value'):
            candidates[source_rows[0]['factId']].add(selected_roots[0]['factId'])
    return {source: next(iter(roots)) for source, roots in candidates.items() if len(roots) == 1}


def _form_roots(capture, selection):
    """比较已验证的观察与原始目标格，展开的依赖不参与目标身份。"""
    if selection.get('preparedTargets'):
        from .prepared_inputs import resolve
        resolved = resolve(capture, selection)
        return resolved['observationId'], frozenset(resolved['cellKeys'])
    from .adapters.budget_v2 import selected_form
    _, _, _, observation = selected_form(capture, receipt_from_capture(capture), selection)
    keys = selection.get('cellKeys')
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
        raise AnalysisError('INVALID_SELECTOR', '选择器需要无重复的实际 cellKeys。')
    return observation['observationId'], frozenset(keys)


def _merge_prepared(selections, addition, capture):
    """同一来源、步骤、观察和目标只纳入一次，其余目标及对照分别保留。"""
    result = []
    merged = False
    roots = None
    structure_sample = addition['role'] == 'STRUCTURE' and str(capture.get('toolCallId', '')).startswith('host-read-')
    for selection in selections:
        if ('batchId' in selection or selection.get('captureRef') != addition['captureRef']
                or selection.get('stepId') != addition['stepId']
                or selection.get('observationId') not in (None, addition['observationId'])):
            result.append(selection)
            continue
        observation, selected_keys = _form_roots(capture, selection)
        if observation != addition['observationId']:
            result.append(selection)
            continue
        if roots is None:
            roots = _form_roots(capture, addition)[1]
        if selected_keys != roots:
            # 样本中主动选择的其他格仍可作为结构材料，样本金额不进入主体或对照。
            result.append({**selection, 'role': 'STRUCTURE'} if structure_sample else selection)
            continue
        if addition['role'] == 'PRIMARY' and selection.get('role', 'PRIMARY') != 'PRIMARY':
            raise AnalysisError('INPUT_ROLE_CONFLICT', '同一已确认的主体公式目标不能同时作为其他用途。')
        if not merged:
            # 使用宿主从实际主体确定的粒度，例如科目已固定时，不再充当主体维度。
            matched = dict(addition)
            if selection.get('selectionReason'):
                matched['selectionReason'] = selection['selectionReason']
            result.append(matched)
            merged = True
    if not merged:
        result.append(addition)
    return result


class BudgetPreparation:
    """宿主以已授权只读能力取得目标。缓存存引用，结果与原始查询分别持久化。"""
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self.runner = None

    async def ensure(self, turn, targets, role, captures=None):
        targets = list({target['key']: target for target in targets}.values())
        if not targets:
            return []
        started = perf_counter()
        async with self.lock:
            results = await self._prepare(turn, targets, captures or {})
        elapsed = round((perf_counter() - started) * 1000, 2)
        logging.getLogger(__name__).info('预算分析材料准备：用途=%s 预期目标=%d 就绪=%d 耗时毫秒=%.2f',
                                        role, len(targets), sum('selection' in result for result in results), elapsed)
        return [{**result, 'role': role} for result in results]

    async def _prepare(self, turn, targets, captures):
        cache = turn.state.setdefault('budgetPreparedObservations', {})
        failures = turn.state.setdefault('budgetPreparationFailures', {})
        candidates = dict(captures)
        resolved = {}
        groups = defaultdict(list)
        for target in targets:
            key = target['key']
            cached = cache.get(key)
            if cached:
                try:
                    if cached not in candidates:
                        candidates[cached] = self.service.store.get_capture(self.service.owner, cached)
                except AnalysisError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    cache.pop(key, None)
            for capture in candidates.values():
                if capture.get('originalToolName') != 'execute_semantic_plan' or self.service.sources.for_capture(capture).adapter_id != 'budget-v2':
                    continue
                selector = self._match(capture, target)
                if selector:
                    resolved[key] = {'selection': selector}
                    cache[key] = capture['captureRef']
                    failures.pop(key, None)
                    break
            if key not in resolved:
                if key in failures:
                    resolved[key] = {'error': failures[key]}
                else:
                    groups[(target['boundary'], target['binding'])].append(target)
        for group in groups.values():
            for offset in range(0, len(group), 11):
                # 计划最多十二步：一次解析绑定，其余步骤对应不同的实际运行条件。
                part = group[offset:offset + 11]
                try:
                    refs, diagnostic = await self._fetch(turn, part)
                    saved = [self.service.store.get_capture(self.service.owner, ref) for ref in refs]
                    for target in part:
                        for capture in saved:
                            selector = self._match(capture, target)
                            if selector:
                                cache[target['key']] = capture['captureRef']
                                resolved[target['key']] = {'selection': selector, 'chain': refs}
                                break
                        if target['key'] not in resolved:
                            failures[target['key']] = diagnostic or {'code': 'FORM_TARGET_NOT_PREPARED', 'message': '来源未返回该目标的完整定位目录。'}
                except AnalysisError as exc:
                    for target in part:
                        if target['key'] not in resolved:
                            failures[target['key']] = exc.as_dict()
        while len(cache) > 128:
            del cache[next(iter(cache))]
        return [{'targetKey': target['key'], 'sourceCaptureRef': target['source'], 'subject': target['subject'], 'grain': target.get('grain'),
                 **(resolved[target['key']] if target['key'] in resolved else {'error': failures[target['key']]})}
                for target in targets]

    def _match(self, capture, target):
        if str(capture.get('captureState', '')).upper() not in ('SUCCESS', 'SUCCEEDED'):
            return None
        receipt = receipt_from_capture(capture)
        if not any(step.get('formResults') for step in (receipt.get('data') or {}).get('stepResults', [])):
            return None
        effective = stored_form_request(self.service.store, self.service.owner, capture)
        return form_selector(effective, receipt, target)

    async def _fetch(self, turn, part):
        first = part[0]
        capture = self.service.store.get_capture(self.service.owner, first['source'])
        receipt = receipt_from_capture(capture)
        base = {'contractVersion': '2.0', 'contextId': receipt['context']['contextId'], 'querySessionId': receipt['querySessionId']}
        if not self.runner:
            raise AnalysisError('FORM_PREPARATION_UNAVAILABLE', '本轮没有已授权的表展开执行入口。')
        capability = turn.state.get('budgetFormCapabilities', {}).get(first['boundary'])
        if not capability:
            request = {**base, 'modelCodes': ['BUDGET_TASK_FORM_BINDING'], 'capabilityDiscoveryPageSize': 50}
            for _ in range(12):
                refs, error = await self.runner(turn, capture, 'list_capabilities', request)
                if error:
                    return [], error
                capability = turn.state.get('budgetFormCapabilities', {}).get(first['boundary'])
                if capability or not refs:
                    break
                page = receipt_from_capture(self.service.store.get_capture(self.service.owner, refs[-1]))['data'].get('page') or {}
                cursor = page.get('nextCursor')
                if not cursor or cursor == request.get('capabilityDiscoveryCursor'):
                    break
                request = {**request, 'capabilityDiscoveryCursor': cursor}
        if not capability:
            raise AnalysisError('FORM_CAPABILITY_UNAVAILABLE', '当前绑定上下文没有取得可验证的表展开能力标识。')
        steps = [{'id': 'binding', 'operation': 'RESOLVE_ENTITY', 'modelCode': 'BUDGET_TASK_FORM_BINDING',
                  'resourceIris': [first['binding']], 'inputs': []}]
        for index, target in enumerate(part, 1):
            steps.append({'id': 'form' + str(index), 'operation': 'FORM_VIEW_QUERY', 'inputs': ['binding'],
                          'capabilityId': capability, 'formQuery': target['query']})
        return await self.runner(turn, capture, 'execute_semantic_plan',
                                 {**base, 'plan': {'dslVersion': '1.0', 'steps': steps, 'outputStepIds': [step['id'] for step in steps[1:]]}})

    async def after_query(self, turn, capture):
        """聚合回执先保存，金额完整时在同一轮准备结构。子查询有独立回执和权限校验。"""
        if (not data_analysis(turn) or turn.state.get('requiredEvidence', 'VALUES') != 'VALUES'
                or not self.runner or capture.get('originalToolName') != 'execute_semantic_plan'
                or self.service.sources.for_capture(capture).adapter_id != 'budget-v2'):
            return None
        if not any(step.get('batchQuery', {}).get('aggregation') for step in capture.get('request', {}).get('plan', {}).get('steps', [])):
            return None
        targets = batch_targets(self.service, capture)
        if not targets:
            return None
        records = await self.ensure(turn, targets, 'STRUCTURE', {capture['captureRef']: capture})
        turn.state.setdefault('budgetStructureInputs', {})[capture['captureRef']] = records
        return {'expected': len(records), 'ready': sum('selection' in row for row in records),
                'issues': [row['error'] for row in records if 'error' in row],
                'build': {'captureRefs': [capture['captureRef']]},
                'note': '构建提供本批回执和 scope，金额取原生聚合，结构自动附入。仅问分组且各组齐全时可直接交付，成员示例的展示数量不影响完成状态。'}

    async def for_build(self, refs, selections, scope):
        """入口统一补齐真正的目标与结构用途，原子构建前保留所有预期分支。"""
        from .prepared_inputs import infer_selections
        from .adapters.budget_v2 import resolve_form_requests, stored_form_chain
        captures = {ref: self.service.store.get_capture(self.service.owner, ref) for ref in refs}
        for capture in list(captures.values()):
            if self.service.sources.for_capture(capture).adapter_id == 'budget-v2' and capture['request'].get('resumeRef'):
                captures.update(stored_form_chain(self.service.store, self.service.owner, capture))
        effective = dict(captures)
        for adapter, group in self.service.sources.groups(captures):
            if adapter.adapter_id == 'budget-v2' and any(item['request'].get('resumeRef') for item in group.values()):
                effective.update(resolve_form_requests(group))
        turn = self.service.turn
        declared = []
        if data_analysis(turn) and turn.state.get('budgetFormulaTargets'):
            # 准备失败后可能已通过普通表展开补得证据。以本次真实输入重新匹配，避免陈旧失败变成永久缺口。
            declared = await self.ensure(turn, turn.state['budgetFormulaTargets'], 'PRIMARY', effective)
            turn.state['budgetFormulaPreparation'] = declared
        known_structure = [row for ref in refs for row in (turn.state.get('budgetStructureInputs', {}).get(ref, []) if turn else [])]
        selections = (infer_selections(self.service, {ref: effective[ref] for ref in refs}, scope, [*declared, *known_structure])
                      if selections is None else deepcopy(selections))
        records = list(declared)
        if data_analysis(turn) and turn.state.get('requiredEvidence', 'VALUES') == 'VALUES':
            targets = [target for capture in effective.values() for target in batch_targets(self.service, capture, selections)]
            records.extend(await self.ensure(turn, targets, 'STRUCTURE', effective))
        for record in records:
            if 'selection' not in record:
                continue
            addition = {**record['selection'], 'role': record['role']}
            reference = addition['captureRef']
            if reference not in effective:
                capture = self.service.store.get_capture(self.service.owner, reference)
                chain = stored_form_chain(self.service.store, self.service.owner, capture)
                captures.update(chain)
                effective[reference] = resolve_form_requests(chain)[reference] if capture['request'].get('resumeRef') else capture
            if record.get('subject'):
                addition['grain'] = record.get('grain') or list(record['subject'])
            selections = _merge_prepared(selections, addition, effective[reference])
        errors = [{'subject': record.get('subject'), 'role': record['role'], 'error': record['error']}
                  for record in records if record.get('error')]
        summary = {'expected': len(records), 'ready': len(records) - len(errors), 'issues': errors} if records else None
        return list(captures), selections, summary
