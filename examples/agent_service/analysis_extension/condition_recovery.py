"""Execute every native-authorized branch at one priority before relaxing further."""
from copy import deepcopy
from itertools import combinations
from time import perf_counter

from agentscope.tool import ToolResponse
from .models import AnalysisError
from .query_results import encode


def scope_recovery(receipt):
    if (receipt and receipt.get('status') == 'FAILED'
            and (receipt.get('error') or {}).get('code') == 'DIMENSION_SCOPE_UNRESOLVED'):
        return (receipt.get('data') or {}).get('conditionRecovery')
    return None


def plans(request, descriptor):
    """Stream combinations; neither an arbitrary TopK nor an exponential list."""
    context = request.get('conditionContext') or {}
    known = {item['id'] for item in context.get('conditions', [])}
    fixed, choices = descriptor.get('dropAll'), descriptor.get('chooseFrom')
    count = descriptor.get('chooseCount')
    if (not isinstance(fixed, list) or not isinstance(choices, list) or type(count) is not int
            or not 1 <= count <= len(choices) or len(set(fixed + choices)) != len(fixed + choices)
            or not set(fixed + choices) <= known or descriptor.get('tier') != len(fixed) + count):
        raise AnalysisError('CONDITION_RECOVERY_INVALID', '来源返回的历史条件恢复计划不完整。')
    from .condition_context import apply_omissions
    for combination in combinations(choices, count):
        candidate = deepcopy(request)
        candidate.pop('requestId', None)
        candidate['requestId'] = 'host-pending'
        candidate['conditionContext']['omitted'] = sorted([*fixed, *combination])
        apply_omissions(candidate)  # Check protected conditions before external I/O.
        yield candidate


async def run(capture, agent, input_kwargs, next_handler):
    first = None
    async for event in capture._run_pages(agent, input_kwargs, next_handler):
        if isinstance(event, ToolResponse):
            first = event
        else:
            yield event
    if first is None:
        return
    def saved(event):
        ref = event.metadata.get('analysisCaptureRef')
        if not ref:
            return None, None
        record = capture.store.get_capture(capture.owner, ref)
        adapter = capture.sources.for_capture(record)
        return record, adapter.raw_receipt(record['response'])
    record, receipt = saved(first)
    descriptor = scope_recovery(receipt)
    if not descriptor or record.get('originalToolName') != 'execute_budget_query':
        yield first
        return
    initial = deepcopy(record['request'])
    started = perf_counter()
    probes = [record['captureRef']]
    previous_tier = 0
    plan_receipt = receipt
    while descriptor:
        if descriptor['tier'] <= previous_tier:
            raise AnalysisError('CONDITION_RECOVERY_CYCLE', '历史条件恢复没有前进，已停止重复查询。')
        previous_tier = descriptor['tier']
        events, later, all_recoverable = [], None, True
        next_receipt = None
        for request in plans(initial, descriptor):
            if 'groups' in descriptor:
                request['conditionContext']['recoverySource'] = {'executionId': plan_receipt['executionId'],
                    'contextId': plan_receipt['context']['contextId'], 'querySessionId': plan_receipt['querySessionId']}
            call = input_kwargs['tool_call'].model_copy(update={'input': encode(request)})
            response = None
            async for event in capture._run_pages(agent, {**input_kwargs, 'tool_call': call}, next_handler,
                                                  frozen_request=request):
                if isinstance(event, ToolResponse):
                    response = event
                else:
                    yield event
            if response is None:
                raise AnalysisError('CONDITION_BRANCH_UNFINISHED', '一个查询范围尚未确认完成，其余结果已保留。')
            events.append(response)
            branch, result = saved(response)
            if branch:
                probes.append(branch['captureRef'])
            next_plan = scope_recovery(result)
            if next_plan:
                if later is not None and later != next_plan:
                    raise AnalysisError('CONDITION_RECOVERY_CONFLICT', '同层查询返回了不一致的后续条件计划。')
                later = next_plan
                next_receipt = result
            else:
                all_recoverable = False
        if all_recoverable and later:
            descriptor, plan_receipt = later, next_receipt
            continue
        if len(events) == 1:
            response = events[0]
        else:
            from .query_continuation import combined_response
            response = combined_response(capture, capture.sources.for_capture(record), events, started,
                                         'CONDITION_FRONTIER_COMPLETED', kind='CONDITIONS')
        # Diagnostic attempts are retained server-side, not appended to audit AI material.
        response.metadata['conditionExecution'] = {'tier': previous_tier, 'branchCount': len(events),
                                                  'attemptCaptureRefs': probes, 'frontierComplete': True}
        yield response
        return
