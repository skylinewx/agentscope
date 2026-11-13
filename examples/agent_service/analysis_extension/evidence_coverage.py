"""Separate source rows, entity identities, and formula targets without changing any selection."""
from collections import Counter, defaultdict
from .models import digest


def summarize(artifact):
    formula_targets = {ref for task in artifact.get('plan', {}).get('workPlan', [])
                       if task.get('requiredEvidence') == 'FORMULAS' for ref in task.get('targetRefs', [])}
    facts = artifact.get('facts', [])
    contexts = defaultdict(list)
    for fact in facts:
        contexts[(fact.get('sourceContextId'), fact.get('inputRole', 'PRIMARY'),
                  tuple(fact.get('grain') or [artifact.get('scope', {}).get('memberDimension', 'MD_ORG')]))].append(fact)
    groups = []
    for (context, role, grain), rows in contexts.items():
        entities = {}
        for fact in rows:
            dims = {**fact.get('effectiveDimensions', {}), **fact.get('dimensions', {})}
            if all(key in dims for key in grain):
                identity = {key: dims[key] for key in grain}
                entities[digest(identity)] = identity
        groups.append({'sourceContextId': context, 'inputRole': role, 'grain': list(grain),
                       'entityCount': len(entities), 'factCount': len(rows),
                       'formulaTargetCount': sum(fact['factId'] in formula_targets for fact in rows),
                       'valueStateCounts': dict(Counter(fact.get('valueState', 'UNKNOWN') for fact in rows))})
    return {'groups': groups, 'selectedFactCount': len(facts), 'formulaTargetCount': len(formula_targets),
            'formulaTargetRefs': sorted(formula_targets),
            'definition': '实体按已声明粒度和真实维值去重；不同来源上下文分别计数。公式目标仅来自声明的构成工作项。展示范围不改变总体；不按名称排除差额、合计或空属性。'}
