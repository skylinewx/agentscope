"""Shared relationship presentation and exact reconciliation."""
from .models import AnalysisError, digest, decimal_value, decimal_text, precise_sum

def captured_relations(captures, sources=None, scope=None):
    from .adapters import default_sources
    return (sources or default_sources()).relations(captures, scope)


def hierarchy(artifact, view):
    relation = next((r for r in artifact['relations'] if r['relationRef'] == view.get('relationRef')), None)
    known = {f['factId']: f for f in artifact['facts']}
    if relation is None or any(ref not in known for ref in view['valueRefs']):
        raise AnalysisError('RELATION_UNPROVEN', '层级图需要 AnalysisInspect(section=relations) 返回的直接关系及来源事实。')
    facts = [known[ref] for ref in view['valueRefs']]
    parent, children = facts[0], facts[1:]
    if parent.get('objectModelCode') != relation.get('parentModelCode') or any(
            child.get('objectModelCode') != relation.get('childModelCodes', {}).get(child['objectId']) for child in children):
        raise AnalysisError('RELATION_IDENTITY_CONFLICT', '层级关系与数值的实体模型身份不一致。')
    if (parent['objectId'] != relation['parentId'] or len({f['objectId'] for f in facts}) != len(facts)
            or not {f['objectId'] for f in children} <= set(relation['childIds'])):
        raise AnalysisError('RELATION_UNPROVEN', '层级图首项必须为实际父级，其余项必须是已捕获的直接下级。')
    dimension = artifact['scope'].get('memberDimension')
    def signature(fact):
        return digest({**{k: fact.get(k) for k in ('bindingRef', 'period', 'cellKey', 'measureCode', 'unit', 'scale')},
                       'dimensions': {k: v for k, v in fact.get('dimensions', {}).items() if k != dimension}})
    if not dimension or len({signature(f) for f in facts}) != 1 or any(
        relation['sourceOrigin'] not in f.get('sourceOrigins', [])
        or f.get('dataContext', f.get('context')) != relation.get('dataContext', relation['context']) for f in facts
    ):
        raise AnalysisError('RELATION_CONTEXT_CONFLICT', '层级图需使用同一来源上下文、时期和指标；不能拼接不同口径的数据。')
    complete = relation['complete'] and {f['objectId'] for f in children} == set(relation['childIds'])
    return parent, children, complete


def reconcile(facts, scope, relations, operation, items):
    """Explicit equality check; the caller chooses the sum expectation.

    This is an arithmetic expectation, not a declaration that the source system
    has a consolidation rule. Missing units are allowed only with identical
    source labels/scales; they are neither invented nor converted.
    """
    if operation.get('aggregation') != 'SUM' or not operation.get('relationRef'):
        raise AnalysisError('AGGREGATION_REQUIRED', '汇总核对须显式选择 SUM 及已确认的直接关系。')
    if not items or any('factId' not in item for item in items):
        raise AnalysisError('FACTS_REQUIRED', '汇总核对使用父项及直属子项的来源事实。')
    view = {'relationRef': operation['relationRef'], 'valueRefs': [item['factId'] for item in items]}
    parent, children, complete = hierarchy({'facts': facts, 'scope': scope, 'relations': relations}, view)
    if not complete:
        raise AnalysisError('HIERARCHY_INCOMPLETE', '直属子项清单或同指标数值未齐，不能核对父子合计。')
    if not children:
        return {'status': 'NOT_APPLICABLE', 'parentValue': parent.get('value'), 'childSum': None, 'difference': None,
                'relationRef': operation['relationRef'], 'aggregation': 'SUM', 'childCount': 0,
                'expectation': 'PARENT_EQUALS_DIRECT_CHILDREN_SUM', 'origin': 'CALLER_SELECTED'}
    if any(f.get('valueState') != 'PRESENT' or f.get('precision') == 'display_only' for f in items):
        raise AnalysisError('INCOMPLETE_VALUES', '汇总核对包含缺值、冲突或仅有显示精度的数值。')
    total = precise_sum(decimal_value(f['value']) for f in children)
    difference = precise_sum([decimal_value(parent['value']), total.copy_negate()])
    from .comparisons import comparison
    return {'status': 'MATCH' if difference == 0 else 'MISMATCH', 'parentValue': parent['value'],
            'comparison': comparison(parent['value'], decimal_text(total), 'parent', 'direct_children_sum'),
            'childSum': decimal_text(total), 'difference': decimal_text(difference), 'childCount': len(children),
            'relationRef': operation['relationRef'], 'aggregation': 'SUM',
            'expectation': 'PARENT_EQUALS_DIRECT_CHILDREN_SUM', 'origin': 'CALLER_SELECTED'}
