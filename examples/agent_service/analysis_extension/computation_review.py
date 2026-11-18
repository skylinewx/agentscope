"""Reuse completed computation review without reusing the report's text verdict."""
from copy import deepcopy

from .models import digest


def computation_basis(material):
    fields = ('evidenceCoverage', 'methodRevisions', 'queryIntent', 'scope', 'sourceContexts', 'sourceScopes',
              'facts', 'derived', 'relations', 'structureEvidence', 'pythonRuns', 'groupingRequirements', 'reviewTargets')
    result = {key: deepcopy(material.get(key)) for key in fields}
    # The original execution intent is immutable. A later "continue reviewing"
    # request is checked by final review, not treated as a new computation.
    result['computationIntent'] = deepcopy(material.get('computationIntent') or {
        'userRequest': material.get('userRequest'), 'materials': material.get('materials'),
        'analysisRequirement': material.get('analysisRequirement')})
    # Current-turn grouping/sorting may be cleared for a review-only follow-up.
    # Audit the original calculation against its frozen execution intent; the
    # final business review still checks all conditions of the current request.
    if 'queryIntent' in result['computationIntent']:
        result['queryIntent'] = deepcopy(result['computationIntent']['queryIntent'])
    if 'computationGroupingRequirements' in material:
        result['groupingRequirements'] = deepcopy(material['computationGroupingRequirements'])
    return result


def computation_key(material, prompt, budget=None):
    if not material.get('pythonRuns'): return None
    from .turn_completion import ANALYSIS_REVIEW_SCHEMA
    from .review_budget import current_budget
    from .review_business_evidence import WIRE_POLICY
    return digest(['COMPUTATION_REVIEW_V2', WIRE_POLICY, prompt, ANALYSIS_REVIEW_SCHEMA,
                   (budget or current_budget()).semantic_identity(), computation_basis(material)])


def use_reviewed_computation(material, proof):
    result = deepcopy(material)
    result['pythonRuns'] = []
    result['calculationReview'] = proof
    result['evidencePartition'] = {'kind': 'REPORT_WITH_REVIEWED_COMPUTATIONS',
        'meaning': '相同目标、范围、数据、用途、代码、输出和规则的完整计算已审查通过；本次仍核对当前正文、来源范围、数值引用及图表说明，不重复已完成的代码审查。计算证据仍保存在宿主。'}
    return result


def calculation_material(material, key):
    result = computation_basis(material)
    context = result['computationIntent']
    result['userRequest'] = context.get('userRequest') or material.get('userRequest')
    for run in result.get('pythonRuns') or []:
        for field in ('stdout', 'stderr', 'runtime', 'timings', 'containerId', 'environment', 'requestKey'):
            run.pop(field, None)
    result.update(reviewStage='CALCULATION', computationKey=key,
        stageInstruction='专项核对用户意图、完整相关输入、实际代码、实际输出及范围；不核对报告措辞、排版或交付。'
                         '合法子集不等于缺少输入；根据实际时期和维度核对全年范围、合计与明细、主体对照及分母，不能凭记录条数判正确。'
                         '预算原值可信，来源空值与复算差额如实保留，不追加差额成因调查。')
    return result


def business_material(material, proof=None, key=None):
    """Final review sees business results and opinions, never executor internals."""
    result = use_reviewed_computation(material, proof or {'status': 'PENDING', 'materialKey': key})
    result.update(reviewStage='FINAL_BUSINESS', computationKey=key)
    result['evidencePartition'] = {'kind': 'FINAL_BUSINESS',
        'meaning': '业务结果与本次候选内容；计算审核状态以 calculationReview 为准，PENDING 不表示通过。'}
    result.pop('pythonRuns', None)
    result.pop('computationGroupingRequirements', None)
    for field in ('pythonInputSets', 'executionInputs', 'inputDeliveryManifest', 'inputDeliverySummary'):
        result.pop(field, None)
    for value in result.get('derived', []):
        if value.get('operator') == 'PYTHON':
            value['operator'] = 'COMPUTED'
        for field in ('pythonRunRef', 'runtime', 'code', 'reviewInputRefs', 'inputDeliveryManifest', 'inputDeliverySummary'):
            value.pop(field, None)
    result['stageInstruction'] = ('独立核对当前候选内容是否回答用户要求，列表与排序、范围、金额、表图说明和局限是否与业务结果一致。'
        '计算专项意见是本阶段依据；不重复核对执行代码，不因缺代码要求补查。不重新表达或改写候选。'
        '专项审核未完成应如实说明，不能推定计算正确；已知错误影响的结论不能当作确定事实。合理业务解释允许不同角度。')
    # Original wording and actual selections, not every abandoned planning draft.
    result.pop('agentPlan', None)
    result.pop('methodRevisions', None)
    requirement = result.get('analysisRequirement')
    if isinstance(requirement, dict):
        result['analysisRequirement'] = {field: deepcopy(requirement[field]) for field in ('materials', 'investigations')
                                         if field in requirement}
    if (result.get('computationIntent') or {}).get('selectedContextRequests'):
        result['selectedContextRequests'] = deepcopy(result['computationIntent']['selectedContextRequests'])
        result.pop('previousMessages', None)
    return result


def refresh_business_proof(turn, payload):
    """Materialize the completed stage receipt before freezing the final call."""
    from .review_request import review_material, make_review_request
    material = review_material(payload)
    if material.get('reviewStage') != 'FINAL_BUSINESS' or not material.get('computationKey'):
        return payload
    proof = turn.service.store.get_semantic_review(turn.service.owner, material['computationKey'])
    if proof is not None:
        material['calculationReview'] = proof
    return make_review_request(material)
