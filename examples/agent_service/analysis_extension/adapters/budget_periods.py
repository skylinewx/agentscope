"""Validate source period receipts and coverage; calendar arithmetic stays in the budget service."""

import re

from ..models import AnalysisError


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    return value


def period_execution(capture, receipt, step_id, context=None):
    steps = [s for s in receipt.get('data', {}).get('stepResults', []) if s.get('stepId') == step_id]
    requests = [s for s in capture['request'].get('plan', {}).get('steps', []) if s.get('id') == step_id]
    query = requests[0].get('formQuery', {}) if len(requests) == 1 else {}
    progress = steps[0].get('periodExecution') if len(steps) == 1 else None
    if progress is None:
        if query.get('periodRequest') or (context or {}).get('periodResolution'):
            raise AnalysisError('PERIOD_RECEIPT_MISSING', '时期请求缺少服务端冻结的时期回执。')
        return None
    resolution = progress.get('resolution') or {}
    codes = resolution.get('periodCodes')
    index, work = progress.get('periodIndex'), progress.get('workIndex')
    valid = (len(requests) == 1 and isinstance(codes, list) and 1 <= len(codes) <= 1000
             and all(isinstance(c, str) and re.fullmatch(r'\d{4}[NHJYXZRW]\d{4}', c) for c in codes)
             and codes == sorted(set(codes)) and all(c[4] == resolution.get('taskPeriodType') for c in codes)
             and type(index) is int and 0 <= index < len(codes)
             and progress.get('executionPeriod') == codes[index] and progress.get('periodCount') == len(codes)
             and type(work) is int and type(progress.get('workCount')) is int and 0 <= work < progress['workCount'] <= 12
             and all(isinstance(resolution.get(k), str) and resolution[k] for k in
                     ('resolutionId', 'ruleVersion', 'referenceDate', 'referenceInstant', 'timeZone'))
             and isinstance(progress.get('planId'), str) and bool(progress['planId'])
             and type(progress.get('periodFinished')) is bool and type(progress.get('planFinished')) is bool
             and isinstance(progress.get('bindingIris'), list) and 1 <= len(progress['bindingIris']) <= 32
             and all(isinstance(b, str) and b for b in progress['bindingIris'])
             and _clean(progress.get('periodRequest')) == _clean(query.get('periodRequest')))
    if not valid:
        raise AnalysisError('PERIOD_RECEIPT_CONFLICT', '时期进度、原请求与服务端解析清单不一致。')
    request = query.get('periodRequest') or {}
    expected_source = ('EXPLICIT_PERIOD' if request.get('referencePeriod') else
                       'EXPLICIT_DATE' if request.get('referenceDate') else 'SERVER_TIME')
    if (resolution.get('referenceSource') != expected_source
            or any(request.get(k) is not None and request[k] != resolution.get(k) for k in ('referenceDate', 'referencePeriod'))
            or query.get('periodCode') and codes != [query['periodCode']]):
        raise AnalysisError('PERIOD_REFERENCE_CONFLICT', '时期参照必须来自明确指定的参照或预算服务器系统时间。')
    if context is not None and (context.get('period') != codes[index] or context.get('periodResolution') != resolution
                               or context.get('target', {}).get('formBindingKey') not in progress['bindingIris']):
        raise AnalysisError('PERIOD_RECEIPT_CONFLICT', '观察的执行时期、绑定或冻结参照与进度不一致。')
    return progress


def period_coverage(facts, captures, receipts, source_contexts):
    selected = {}
    for fact in facts:
        if not fact.get('periodPlanId'):
            continue
        key = (fact['sourceContextId'], fact['periodPlanId'], fact['periodWorkIndex'])
        selected.setdefault(key, []).append(fact)
    selected_plans = {key[:2] for key in selected}
    groups = {}
    for ref, receipt in receipts.items():
        for step in receipt.get('data', {}).get('stepResults', []):
            raw = step.get('periodExecution')
            if not raw:
                continue
            key = (source_contexts[ref]['sourceContextId'], raw.get('planId'), raw.get('workIndex'))
            if key[:2] not in selected_plans:
                continue
            progress = period_execution(captures[ref], receipt, step.get('stepId'))
            group = groups.setdefault(key, {'resolution': progress['resolution'], 'finished': set(), 'statuses': {},
                                            'workCount': progress['workCount'], 'planFinished': False})
            if group['resolution'] != progress['resolution'] or group['workCount'] != progress['workCount']:
                raise AnalysisError('PERIOD_REFERENCE_DRIFT', '同一时期计划在续接过程中改变了参照时间或时期集合。')
            code = progress['executionPeriod']
            statuses = [f.get('status') for f in step.get('formResults', [])]
            group['statuses'][code] = statuses
            if progress['periodFinished'] and all(f.get('disposition') == 'SUCCEEDED' for f in step.get('formResults', [])) and statuses:
                group['finished'].add(code)
            group['planFinished'] |= progress['planFinished']
    result = []
    complete = True
    for key, members in selected.items():
        group = groups[key]
        expected = group['resolution']['periodCodes']
        present = {(f['executionPeriod'], f['objectId']) for f in members}
        objects = sorted({f['objectId'] for f in members})
        missing_count = len(expected) * len(objects) - len(present)
        missing = []
        for p in expected:
            for o in objects:
                if (p, o) not in present:
                    missing.append({'executionPeriod': p, 'objectId': o})
                    if len(missing) == 1000:
                        break
            if len(missing) == 1000:
                break
        pending = [p for p in expected if p not in group['finished']]
        # Source completion uses every captured work item in the selected plan;
        # selected facts separately prove the requested member/period coverage.
        # Replacing a fact with another observation must not erase a completed
        # source receipt, and final success alone cannot hide missing receipts.
        plan_groups = [g for k, g in groups.items() if k[:2] == key[:2]]
        if len({(g['resolution']['referenceInstant'], g['resolution']['timeZone']) for g in plan_groups}) != 1:
            raise AnalysisError('PERIOD_REFERENCE_DRIFT', '同一批次不同工作项没有使用统一冻结的服务器时间。')
        finished_work = {k[2] for k, g in groups.items() if k[:2] == key[:2]
                         and set(g['resolution']['periodCodes']) <= g['finished']}
        pending_work = [i for i in range(group['workCount']) if i not in finished_work]
        finished = (not pending and not missing_count and len(plan_groups) == group['workCount']
                    and not pending_work and any(g['planFinished'] for g in plan_groups))
        complete &= finished
        result.append({'sourceContextId': key[0], 'planId': key[1], 'workIndex': key[2],
                       'resolution': group['resolution'], 'complete': finished,
                       'selectedDataComplete': not missing_count, 'pendingPeriods': pending,
                       'pendingWorkIndexes': pending_work,
                       'missingMemberPeriods': missing, 'missingMemberPeriodCount': missing_count,
                       'missingMemberPeriodsTruncated': missing_count > len(missing), 'periodStatuses': group['statuses']})
    return result, complete
