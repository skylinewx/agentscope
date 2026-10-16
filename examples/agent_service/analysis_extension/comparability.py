"""Explicit, versioned exceptions to conservative metric comparability."""
from .models import AnalysisError, digest


def validate_policy(policy):
    allowed = {'periods', 'metrics', 'varyingDimensions', 'contexts', 'sourceOrigins'}
    if not isinstance(policy, dict) or not policy or set(policy) - allowed:
        raise AnalysisError('INVALID_RULE', '可比规则需要明确的时期、指标映射、维度或来源范围。')
    for key in ('periods', 'varyingDimensions', 'sourceOrigins'):
        if key in policy and (not isinstance(policy[key], list) or not policy[key] or len(policy[key]) > 1000
                              or any(not isinstance(value, str) or not value for value in policy[key])
                              or len(set(policy[key])) != len(policy[key])):
            raise AnalysisError('INVALID_RULE', '可比规则范围必须是有界、无重复的明确标识列表。')
    if 'metrics' in policy and (not isinstance(policy['metrics'], list) or not policy['metrics'] or len(policy['metrics']) > 1000
            or any(not isinstance(item, dict) or set(item) != {'bindingRef', 'measureCode', 'cellKey'}
                   or any(not isinstance(value, str) or not value for value in item.values()) for item in policy['metrics'])):
        raise AnalysisError('INVALID_RULE', '指标映射需要明确 bindingRef、measureCode、cellKey。')
    if 'contexts' in policy and (not isinstance(policy['contexts'], list) or not policy['contexts'] or len(policy['contexts']) > 1000
                               or any(not isinstance(value, dict) or not value for value in policy['contexts'])):
        raise AnalysisError('INVALID_RULE', '跨快照规则须列出适用的实际来源上下文。')
    return policy


def check(facts, scope, same_metric=True, varying_dimensions=(), policy=None):
    policy = policy or {}
    # A registered list bounds applicability even when this particular input
    # happens to contain only one period, origin or metric.
    bounds = {
        'periods': ({fact.get('period') for fact in facts}, set(policy.get('periods', []))),
        'contexts': ({digest(fact.get('context')) for fact in facts}, {digest(value) for value in policy.get('contexts', [])}),
        'sourceOrigins': ({value for fact in facts for value in fact.get('sourceOrigins', [])}, set(policy.get('sourceOrigins', []))),
        'metrics': ({(fact.get('bindingRef'), fact.get('measureCode'), fact.get('cellKey')) for fact in facts},
                    {(item['bindingRef'], item['measureCode'], item['cellKey']) for item in policy.get('metrics', [])}),
    }
    if any(key in policy and not actual <= allowed for key, (actual, allowed) in bounds.items()):
        raise AnalysisError('RULE_NOT_APPLICABLE', '所选输入超出已登记可比规则的适用范围。')
    if any(not fact.get('unit') or not fact.get('scale') for fact in facts):
        raise AnalysisError('UNIT_UNCONFIRMED', '来源尚未明确单位或缩放，不能作该业务计算。')
    if len({(fact['unit'], fact['scale']) for fact in facts}) != 1:
        raise AnalysisError('UNIT_CONFLICT', '所选值的单位或缩放不一致。')
    periods = {fact.get('period') for fact in facts}
    if len(periods) > 1 and (None in periods or not periods <= set(policy.get('periods', []))):
        raise AnalysisError('COMPARABILITY_UNCONFIRMED', '跨时期计算需要适用于所选时期的已登记可比规则。')
    boundaries = {digest(fact.get('dataContext', fact.get('context'))) for fact in facts}
    contexts = {digest(fact.get('context')) for fact in facts}
    if len(boundaries) > 1 and not contexts <= {digest(value) for value in policy.get('contexts', [])}:
        raise AnalysisError('COMPARABILITY_UNCONFIRMED', '跨来源快照的数据可分组展示，计算需要明确的上下文可比规则。')
    origins = {tuple(sorted(fact.get('sourceOrigins', []))) for fact in facts}
    if len(origins) > 1 and (any(not origin for origin in origins)
                            or not {value for origin in origins for value in origin} <= set(policy.get('sourceOrigins', []))):
        raise AnalysisError('COMPARABILITY_UNCONFIRMED', '不同来源连接的计算需要明确的来源映射。')
    signatures = {(fact.get('bindingRef'), fact.get('measureCode'), fact.get('cellKey')) for fact in facts}
    metric_keys = {(fact.get('bindingRef'), fact.get('measureCode'), fact.get('metricKey', fact.get('cellKey'))) for fact in facts}
    mapped = {(item['bindingRef'], item['measureCode'], item['cellKey']) for item in policy.get('metrics', [])}
    binding_conflict = len({fact.get('bindingRef') for fact in facts}) > 1
    metric_conflict = same_metric and len(metric_keys) > 1
    if (binding_conflict or metric_conflict) and not signatures <= mapped:
        raise AnalysisError('COMPARABILITY_UNCONFIRMED' if binding_conflict else 'METRIC_CONFLICT',
                            '指标或表绑定不同，需要覆盖全部输入的明确指标映射。')
    ignored = {scope.get('memberDimension'), *varying_dimensions, *policy.get('varyingDimensions', [])}
    if len({digest({key: value for key, value in fact.get('dimensions', {}).items() if key not in ignored}) for fact in facts}) > 1:
        raise AnalysisError('METRIC_CONFLICT', '所选数值存在规则未允许变化的维度。')
