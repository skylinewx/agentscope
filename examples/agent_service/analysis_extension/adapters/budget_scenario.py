"""Validate the sole allowed difference between a budget anchor and a query coordinate."""
from ..models import AnalysisError


def validate_origin(origin):
    proof = origin.get('scenarioFallback')
    if proof is None:
        return None
    anchor = {**origin.get('context', {}).get('effectiveDimensions', {}), **origin.get('cellDimensions', {})}
    if (not isinstance(proof, dict) or proof.get('kind') != 'BUDGET_CELL_SCENARIO_OVERRIDE'
            or proof.get('dimensionCode') != 'MD_SCENE' or proof.get('fromValue') != 'BUDGET'
            or not isinstance(proof.get('toValue'), str) or not proof['toValue'] or proof['toValue'] == 'BUDGET'
            or proof.get('formulaStatus') != 'NO_TARGET_CELL' or anchor.get('MD_SCENE') != 'BUDGET'
            or proof.get('anchorDimensions') != anchor
            or origin.get('selectedDimensions') != {**anchor, 'MD_SCENE': proof['toValue']}
            or proof.get('anchorExpression') != origin.get('runtimeCellExp')
            or not proof.get('queryExpression')):
        raise AnalysisError('INVALID_SCENARIO_FALLBACK', '情景替换缺少预算格依据，或改变了情景以外的坐标。')
    return proof


def headers(origin, names):
    values = origin.get('headers', [])
    proof = origin.get('scenarioFallback')
    if not proof:
        return values
    return [v for v in values if v.strip() not in ('预算', '预算数', 'BUDGET', '[MD_SCENE]')] + [names.get('MD_SCENE') or proof['toValue']]
