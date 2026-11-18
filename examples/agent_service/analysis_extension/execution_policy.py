"""Enforce the user-turn operation limits declared by the planner."""
from copy import deepcopy
from .models import AnalysisError


def install(state, policy):
    if policy is None:
        return
    if (not isinstance(policy, dict) or set(policy) - {'sourceQueries', 'computations'}
            or any(type(value) is not bool for value in policy.values())):
        raise AnalysisError('INVALID_EXECUTION_POLICY', '执行限制使用 sourceQueries/computations 布尔值。')
    previous = state.get('executionPolicy', {})
    if any(previous.get(key) is False and value for key, value in policy.items()):
        raise AnalysisError('EXECUTION_POLICY_CONFLICT', '本轮已明确禁止的操作不能由后续计划自行放开；保留已有材料。')
    state['executionPolicy'] = {**deepcopy(previous), **policy}


def require(turn, capability, operation):
    if turn is not None and turn.state.get('executionPolicy', {}).get(capability) is False:
        raise AnalysisError('EXECUTION_POLICY_BLOCKED',
                            '本轮仅使用已有材料：' + ('不重新取数。' if capability == 'sourceQueries' else '不重新计算。'),
                            {'operation': operation, 'capability': capability, 'requiresSourceQuery': False})


def record(turn, kind, reference, **details):
    if turn is not None:
        turn.state.setdefault('executionEvents', {})[kind + ':' + reference] = {
            'kind': kind, 'reference': reference, **deepcopy(details)}


def check_query(turn, origin, request):
    name = origin.get('originalToolName')
    if name == 'execute_budget_query' and (request.get('basis') or {}).get('kind') == 'FACTS' and request.get('purpose') == 'QUERY':
        return  # An immutable fact view never dispatches a budget query.
    if name in ('execute_budget_query', 'discover_query_targets') or (
            name == 'execute_semantic_plan' and any(step.get('operation') in
                ('FORM_VIEW_QUERY', 'CELL_VALUE_QUERY', 'BATCH_DATA_QUERY')
                for step in request.get('plan', {}).get('steps', []))):
        require(turn, 'sourceQueries', name)
