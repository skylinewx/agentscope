"""Compile declared comparisons to normal budget reads and bind their actual receipts."""
from copy import deepcopy
from dataclasses import dataclass

from .models import AnalysisError, digest


@dataclass(frozen=True)
class ComparisonSource:
    capture_ref: str
    full_receipt: dict
    selected_receipt: dict
    result_indices: frozenset[int]


def make_plan(turn, declaration):
    from .history_references import bind
    from .budget_period_selection import annual_caption_year
    items = []
    shared = list(dict.fromkeys(declaration.get('shared', [])))
    linked = []
    periods = {s['periodText'] for s in declaration['items'] if s.get('terms') and s.get('periodText')}
    for index, spec in enumerate(declaration['items'], 1):
        item = deepcopy(spec)
        period = item.get('periodRequest')
        caption = item.get('periodText')
        if len(periods) > 1 and item.get('terms') and caption:
            year = annual_caption_year(caption)
            question = turn.state['request'] if turn else ''
            quoted_year = year and (str(year) + '年' in question or str(year)[-2:] + '年' in question)
            if question and caption not in question and not quoted_year:
                raise AnalysisError('INVALID_COMPARISON_PERIOD',
                    f'第{index}项的 periodText“{caption}”需要对应用户原话，如“去年同期”。'
                    '解析出的年份单独填 periodRequest，例如 {"kind":"PERIOD","start":{"type":"YEAR","year":2024}}，年份按本问填写。')
            if not period and not year:
                raise AnalysisError('INVALID_COMPARISON_PERIOD',
                    f'第{index}项时期“{caption}”需要 periodRequest。填 kind 和 start；单期年度例 '
                    '{"kind":"PERIOD","start":{"type":"YEAR","year":2024}}，年份按本问填写。保留各项及原文 periodText。')
        if period and 'type' in period and not set(period) - {'type', 'year', 'period', 'offset', 'yearOffset', 'date'}:
            # A single native period point has exactly one envelope. Bounds still go through native validation.
            item['periodRequest'] = {'kind': 'PERIOD', 'start': period}
        if 'sameAs' in item:
            n = item['sameAs']
            quote = item.pop('mention', None)
            if quote and quote not in turn.state['request']:
                raise AnalysisError('INVALID_COMPARISON', 'sameAs 已指定沿用的来源。省略 mention，或只填本轮原话。')
            if (type(n) is not int or not 1 <= n < index or not items[n-1].get('sourceRef')
                    or set(item) - {'sameAs', 'periodText', 'periodRequest', 'scenario'}
                    or not any(item.get(k) for k in ('periodRequest', 'scenario'))):
                raise AnalysisError('INVALID_COMPARISON', 'sameAs 填之前已选原值项的序号，仅声明要变更的时期或情景。不同指标单独填 terms。')
            item.update(_requery_conditions(turn, items[n-1]['sourceRef'], item, shared))
            linked.append([n, index])
        elif item.get('terms') and not ('sourceRef' in item or 'contextRef' in item):
            # A literal quote of this target is also valid evidence for a fresh lookup.
            # It contributes no additional condition or identity to the budget request.
            quote = item.pop('mention', None)
            if quote and (quote not in turn.state['request'] or not any(t in quote for t in item['terms'])):
                raise AnalysisError('INVALID_COMPARISON', '新查询项只需 terms。附带的 mention 应为本轮原话且包含这项的业务词；要沿用旧数值，请单列历史来源。')
        elif 'sourceRef' in item or 'mention' in item or 'contextRef' in item:
            if set(item) - {'mention', 'sourceRef', 'contextRef'} or not (item.get('mention') or item.get('sourceRef')):
                raise AnalysisError('INVALID_COMPARISON', '沿用原值的项填本轮指代原词 mention，可从目录选 contextRef、sourceRef。新增业务词或时期另列查询项。')
            if item.get('sourceRef') and not item.get('mention'):
                item['mention'] = turn.state['request']
            elif item.get('sourceRef') and item['mention'] not in turn.state['request']:
                raise AnalysisError('INVALID_HISTORY_REFERENCE', '已选择具体来源，只填 sourceRef 即可。附带 mention 时使用本轮原话。')
            reference, = bind(turn, [{'use': 'FACTS', **item}])
            item['sourceRef'] = reference['sourceRef']
            if reference.get('contextRef'):
                item['contextRef'] = reference['contextRef']
        else:
            raise AnalysisError('INVALID_COMPARISON', '查询项填写该项业务原词 terms。')
        # Physical tools may reuse t1. A comparison item is identified by its own declaration.
        item['id'] = 'cmp' + str(index) + '_' + digest([shared, item])[:10]
        items.append(item)
    all_items = set(range(1, len(items) + 1))
    paired = {i for pair in linked for i in pair} == all_items
    groups = declaration.get('groups', linked if paired else [list(range(1, len(items) + 1))])
    if (len(items) < 2 or not groups or any(len(g) < 2 or len(set(g)) != len(g) for g in groups)
            or {i for group in groups for i in group} != set(range(1, len(items) + 1))):
        raise AnalysisError('INVALID_COMPARISON', 'groups 用从 1 起的项序号，每组至少两项，并覆盖全部项。全部一起比较时省略 groups。')
    return {'items': items, 'shared': shared, 'groups': [[items[i-1]['id'] for i in g] for g in groups]}


def _requery_conditions(turn, source_ref, changes, shared=()):
    from .adapters.budget_v2 import receipt_from_capture
    from .query_lineage import selected_result_sets
    store, owner = turn.service.store, turn.service.owner
    source = store.get_source_view(owner, source_ref)
    selection = source['selection']
    cap = store.get_capture(owner, selection['captureRef'])
    sets = selected_result_sets(receipt_from_capture(cap), selection)
    if len(sets) != 1 or not sets[0].get('context', {}).get('formName') or not source.get('rowCount'):
        raise AnalysisError('COMPARISON_SOURCE_RANGE', '所选原值没有唯一的可取数表样和成员范围。分列新查询项，或从来源目录选择具体结果。')
    rs = sets[0]
    changed_dims = ({'MD_SCENE'} if changes.get('scenario') else set()) | ({'DATATIME'} if changes.get('periodRequest') else set())
    terms = [m['term'] for m in rs.get('resolvedTerms', []) if m.get('term')
             and (m.get('dimensionCode') or m.get('baseDataCode')) not in changed_dims]
    terms.extend(rs.get('resolvedTitleTerms') or [])
    if not terms:
        raise AnalysisError('COMPARISON_SOURCE_TARGET', '原结果未记录可复用的指标条件。用 terms 指明本项要查的业务指标。')
    result = {'membersRef': source_ref, 'form': rs['context']['formName'], 'terms': list(dict.fromkeys(terms))}
    # A MEMBERS read already binds these exact coordinates. Re-emitting the organization
    # as a free-text filter would ask inheritance to restore the population it just replaced.
    if shared:
        rows = rs.get('rows') or []
        fixed = {k: v for k, v in (rows[0].get('dimensionNames') or {}).items() if v in shared} if rows else {}
        for row in rows[1:]:
            if not fixed:
                break
            names = row.get('dimensionNames') or {}
            fixed = {k: v for k, v in fixed.items() if names.get(k) == v}
        result['boundShared'] = [t for t in shared if t in result['terms'] or t in fixed.values()]
    period = changes.get('periodRequest')
    if period and not (period.get('referenceDate') or period.get('referencePeriod')) and any(
            k in (period.get(point) or {}) for point in ('start', 'end') for k in ('offset', 'yearOffset')):
        periods = {row['dataPeriod'] for row in rs.get('rows', []) if row.get('dataPeriod')}
        if len(periods) != 1:
            raise AnalysisError('COMPARISON_PERIOD_REFERENCE', '原结果含多个数据时期。请明确本项参照时期，或分项查询各期。')
        result['periodRequest'] = {**period, 'referencePeriod': periods.pop()}
    return result


def requests(plan):
    # A native scope has one periodRequest. Independent periods and archived fact views stay separate.
    batches = {}
    for item in plan['items']:
        key = (('FACTS', item['id']) if item.get('sourceRef') else
               ('INTENT', digest([item.get(k) for k in ('periodRequest', 'periodText', 'scenario', 'form', 'task', 'membersRef')])))
        batches.setdefault(key, []).append(item)
    for items in batches.values():
        ids = [item['id'] for item in items]
        if items[0].get('sourceRef'):
            yield ids, {'contractVersion': '2.0', 'basis': {'kind': 'FACTS', 'sourceRef': items[0]['sourceRef']}}
            continue
        targets = [{'id': item['id'], 'businessPhrases': [{'text': t} for t in item['terms']],
                    **({'periodText': item['periodText']} if item.get('periodText') else {})} for item in items]
        relations = []
        for group in plan['groups']:
            included = [i for i in group if i in ids]
            if len(included) > 1:
                relations.append({'type': 'COMPARE', 'targetIds': included})
        scope = {'periodRequest': items[0]['periodRequest']} if items[0].get('periodRequest') else {}
        for name in ('task', 'form'):
            if items[0].get(name):
                scope[name + 'Names'] = [items[0][name]]
        dimensions = [{'id': item['id'] + 'd' + str(index), 'targetIds': [item['id']], 'dimensionHint': dimension,
                       'selector': {'type': 'VALUES', 'members': members}}
                      for item in items for index, (dimension, members) in enumerate(item.get('dimensions', {}).items())]
        basis = {'kind': 'MEMBERS', 'sourceRef': items[0]['membersRef']} if items[0].get('membersRef') else {'kind': 'INTENT'}
        yield ids, {'contractVersion': '2.0', 'basis': basis, 'selection': 'CELLS', 'scope': scope,
                    'intentDraft': {'sharedConditions': [{'text': t} for t in dict.fromkeys([
                                        *(t for t in plan['shared'] if t not in items[0].get('boundShared', ())),
                                        *([items[0]['scenario']] if items[0].get('scenario') else [])])],
                                    'targets': targets, 'relations': relations, **({'dimensionScopes': dimensions} if dimensions else {})}}


def apply_item_intent(state, request):
    """Scope only the temporary request state. The other comparison and conversation scopes stay independent."""
    from .scope_changes import intent_context, merge_intent
    ids = {t['id'] for t in (request.get('intentDraft') or {}).get('targets', [])}
    items = [i for i in (state.get('comparisonPlan') or {}).get('items', []) if i['id'] in ids]
    if not items:
        return
    patch = {}
    for field, key in (('period', 'periodText'), ('scenario', 'scenario'), ('task', 'task'), ('form', 'form')):
        values = {i.get(key) for i in items}
        if len(values) == 1 and None not in values:
            patch[field] = values.pop()
    if patch:
        context = intent_context(state, state.get('followupContext') or {'mode':'CONTINUE'}, patch)
        state['queryIntent'] = merge_intent(state, context, patch)
        state['followupContext'] = context


def batch_coverage(state, request):
    """Attribute literal conditions to the other lookup items without adding them to this lookup."""
    plan = state.get('comparisonPlan') or {}
    active = [t['id'] for t in (request.get('intentDraft') or {}).get('targets', [])]
    lookups = [i for i in plan.get('items', []) if not i.get('sourceRef')]
    if not active or not set(active) < {i['id'] for i in lookups}:
        return None
    question = state['request']

    def literals(item):
        members = (v for values in item.get('dimensions', {}).values() for v in values)
        return {s for s in [*item.get('terms', []), item.get('scenario'), item.get('task'), item.get('form'), *members]
                if s and s in question}

    used = set(plan.get('shared', []))
    own_periods = set()
    for item in lookups:
        if item['id'] in active:
            used.update(literals(item))
            own_periods.add(item.get('periodText'))
    peers = []
    for item in lookups:
        if item['id'] in active:
            continue
        terms = literals(item) - used
        period = item.get('periodText')
        scenario = item.get('scenario')
        peer = {'targetId': item['id'], 'terms': sorted(terms - {scenario})}
        if scenario in terms:
            peer['scenario'] = scenario
        if period and period not in own_periods and period in question:
            peer['periodText'] = period
        if terms or 'periodText' in peer:
            peers.append(peer)
    return {'targetIds': active, 'peers': peers} if peers else None


async def execute(turn, plan, reader):
    from .query_recovery import expand_targets
    from .execution_progress import phase
    if not reader:
        return '当前没有可自动执行的预算连接。使用当前会话的取数工具，按登记的项取值。'
    messages = []
    runs = plan.setdefault('runs', [])
    for ids, request in requests(plan):
        previous = next((r for r in reversed(runs) if r['items'] == ids), None)
        if previous and previous.get('complete'):
            continue
        if previous and previous.get('needsCorrection'):
            messages.append(previous['error'] + '。请修改指出的字段或采用已返回的候选。')
            continue
        run = {'items': ids, 'refs': []}
        runs.append(run)
        operation = await phase(turn, 'QUERYING', 'RUNNING', toolName='execute_budget_query')
        try:
            refs, error = await reader(turn, request)
            run['refs'] = refs
            if not error and refs:
                run['complete'] = set(ids).issubset(matching_sources(turn, plan))
            if not run.get('complete'):
                error = error or {}
                details = error.get('details') or []
                if isinstance(details, dict):
                    if details.get('releasedConditions'):
                        reasons = [f"检索词“{details.get('retrievalText', '')}”含已解除的条件“{'、'.join(details['releasedConditions'])}”"]
                    elif details.get('reason'):
                        reasons = [f"{details.get('path', '$')}：{details['reason'].splitlines()[0][:220]}"]
                    else:
                        reasons = []
                else:
                    reasons = [str(d['message']) for d in details if isinstance(d, dict) and d.get('message')]
                run['error'] = ' '.join(dict.fromkeys(reasons)) or error.get('message') or '查询未完成，请按回执处理尚未定位的项。'
                run['needsCorrection'] = error.get('retryable') is False or error.get('recoveryAction') == 'CORRECT_REQUEST'
                labels = ['、'.join(i.get('terms', [])) or i.get('mention', '') for i in plan['items'] if i['id'] in ids]
                messages.append('、'.join(labels) + '：' + run['error'])
                if refs and turn.service.budget_preparation.runner:
                    candidates = await expand_targets(turn, refs, turn.service.budget_preparation.runner)
                    if candidates:
                        messages.append(candidates)
        except AnalysisError as error:
            run['error'] = str(error)
            messages.append('、'.join(ids) + '：' + str(error))
        finally:
            await phase(turn, 'QUERYING', 'COMPLETE' if run.get('complete') else 'FAILED', operation_id=operation)
    return '\n\n'.join(messages) if messages else '已取得比较所需的回执，宿主正在核对并交付结果表。'


def matching_sources(turn, plan):
    """Accept only calls actually issued for these items, or their proven target-bound recoveries."""
    from .query_empty_result import business_outcome, incomplete_result_rows
    from .adapters.budget_v2 import receipt_from_capture
    store, owner = turn.service.store, turn.service.owner
    items = {i['id']: i for i in plan['items']}
    registered = {ref: tuple(run['items']) for run in plan.get('runs', []) for ref in run['refs']}
    failures = {}
    matched, complete_items, ambiguous = {}, set(), set()
    for ref in dict.fromkeys([*registered, *turn.state.get('queryCaptureRefs', [])]):
        cap = store.get_capture(owner, ref)
        if cap.get('originalToolName') != 'execute_budget_query' or cap.get('turnKey') != turn.state['turnKey']:
            continue
        request = cap['request']
        if request.get('question') != turn.state['request']:
            continue
        try:
            raw = turn.service.sources.for_capture(cap).raw_receipt(cap['response'])
        except AnalysisError:
            continue
        key = (cap.get('connectionFingerprint'), raw.get('querySessionId'), (raw.get('context') or {}).get('contextId'))
        ids = registered.get(ref)
        if not ids:
            if not all(key):
                continue
            owners = {failures.get((*key, b.get('recoveryExecutionId'))) for b in request.get('scope', {}).get('bindings', [])}
            owners.discard(None)
            if len(owners) != 1:
                continue
            ids = owners.pop()
        if raw.get('status') in ('FAILED', 'PARTIAL'):
            if all(key) and raw.get('executionId'):
                failures[(*key, raw['executionId'])] = ids
        data = raw.get('data') or {}
        if data.get('purpose') != 'QUERY' or data.get('selection', 'CELLS') != 'CELLS' or not data.get('resultSets'):
            continue
        complete = raw.get('status') == 'SUCCEEDED' and data.get('complete') is True and not data.get('unresolvedGroups')
        missing = set()
        if complete:
            if business_outcome(data) not in (None, 'ROWS_RETURNED', 'NO_DATA') or incomplete_result_rows(data):
                continue
        elif raw.get('status') == 'PARTIAL' and data.get('unresolvedGroups') and not data.get('valueFilter'):
            # Only scoped location failures establish that the other targets completed.
            issues = data['unresolvedGroups']
            missing = {(issue.get('diagnostics') or {}).get('logicalTargetId') for issue in issues}
            if not missing <= set(ids) or any(issue.get('code') not in
                    ('TARGET_DISCOVERY_UNRESOLVED', 'DIMENSION_SCOPE_UNRESOLVED') for issue in issues) or incomplete_result_rows(data):
                continue
        else:
            continue
        presentation = data.get('presentation') or {}
        if (presentation.get('valueOrder') or (turn.state.get('queryIntent') or {}).get('order')) and not presentation.get('verified'):
            continue
        mapped = {t['id']: t.get('logicalTargetId') or t['id'] for t in data.get('targets', [])}
        frozen = len(ids) == 1 and items[ids[0]].get('sourceRef')
        if frozen:
            if (request.get('basis') or {}).get('kind') != 'FACTS' or ref not in registered or not complete:
                continue
        else:
            observed = {mapped.get(rs.get('targetId')) for rs in data['resultSets']}
            if not (set(ids) - missing <= observed <= set(ids)) or not set(mapped.values()) <= set(ids):
                continue
        try:
            verified = receipt_from_capture(cap)
        except AnalysisError:
            continue
        for item_id in (i for i in ids if i not in missing):
            result_sets = verified['data']['resultSets']
            indices = [i for i, rs in enumerate(result_sets) if frozen or mapped.get(rs.get('targetId')) == item_id]
            selected = [result_sets[i] for i in indices]
            if not selected or incomplete_result_rows({'resultSets': selected}) or (not complete and item_id in matched):
                continue
            if item_id in complete_items and matched[item_id].capture_ref != ref:
                ambiguous.add(item_id)
                continue
            if complete:
                complete_items.add(item_id)
            row_count = sum(len(rs['rows']) for rs in selected)
            sliced = {**verified['data'], 'complete': True, 'unresolvedGroups': [], 'resultSets': selected,
                      'summary': {**(data.get('summary') or {}), 'rowCount': row_count, 'resultSetCount': len(selected)}}
            if not complete:
                sliced['failureSummary'] = {'outcome': 'ROWS_RETURNED' if row_count else 'NO_DATA'}
            matched[item_id] = ComparisonSource(ref, verified, {**verified, 'status': 'SUCCEEDED', 'data': sliced},
                                                frozenset(indices))
    return {item: source for item, source in matched.items() if item not in ambiguous}
