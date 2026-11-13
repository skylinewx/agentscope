"""Verify archived facts independently of the current query session's lifetime."""
from copy import deepcopy
from decimal import Decimal
from ..models import AnalysisError


def require(condition, message):
    if not condition:
        raise AnalysisError('SOURCE_VIEW_MISMATCH', message)


def validate_basis(request, data):
    basis, proof = request.get('basis') or {}, data.get('sourceBasis') or {}
    actual = {key: value for key, value in proof.get('basis', {}).items()
              if value is not None and not (key == 'memberDimensions' and not value)}
    require(actual == basis, '来源范围凭据与原请求不一致。')
    require(set(proof.get('selectedFactIds', [])) == set(basis['source']['factIds'])
            and len(proof.get('selectedFactIds', [])) == len(basis['source']['factIds']), '未完整恢复已选事实。')
    if basis['kind'] == 'MEMBERS':
        dimensions = basis['memberDimensions']
        members = proof.get('memberTuples', [])
        require(bool(members) and all(set(item) == set(dimensions) and all(isinstance(v, str) for v in item.values()) for item in members),
                '成员范围缺少真实维度身份。')
        for result in data.get('resultSets', []):
            for row in result.get('rows', []):
                if row.get('valueState') in ('PRESENT', 'NULL') and all(dim in row.get('dimensions', {}) for dim in dimensions):
                    require({dim: row['dimensions'][dim] for dim in dimensions} in members, '新查询返回了成员范围以外的对象。')


def matches(condition, row):
    """Same three-valued predicate semantics as the native budget filter."""
    op = condition['operator']
    if op in ('AND', 'OR', 'NOT'):
        values = [matches(child, row) for child in condition['children']]
        if op == 'NOT':
            return None if values[0] is None else not values[0]
        if op == 'AND':
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if op == 'IS_NULL':
        return row['valueState'] == 'NULL'
    if op == 'IS_NOT_NULL':
        return row['valueState'] == 'PRESENT'
    if row['valueState'] != 'PRESENT':
        return None
    require(row.get('valueType', 'DECIMAL_STRING') == 'DECIMAL_STRING', '数值筛选需要数值事实。')
    left, right = Decimal(row['value']), Decimal(condition['value'])
    return {'LT': left < right, 'LE': left <= right, 'GT': left > right,
            'GE': left >= right, 'EQ': left == right, 'NE': left != right}[op]


def validate_view(capture, receipt, validate):
    request, data = capture['request'], receipt['data']
    validate_basis(request, data)
    proof = data['sourceBasis']
    require(request['basis']['kind'] == 'FACTS' and request['purpose'] == data['purpose'] == 'QUERY', '原事实视图不能冒充重新执行。')
    require(request['question'] == data['question'] and len(data['resultSets']) == 1, '视图与本次原问题或所选结果集不一致。')
    base = proof.get('sourceContext', {})
    require(all(base.get(key) == receipt['context'].get(key) for key in ('sourceId', 'snapshotId', 'schemaHash')),
            '历史事实与当前授权来源/本体版本不同。')
    selected = proof.get('selectedFacts', [])
    require({row['factId'] for row in selected} == set(request['basis']['source']['factIds'])
            and len(selected) == len(request['basis']['source']['factIds']), '事实筛选缺少完整原集合。')
    require(data['resultSets'][0]['resultSetId'] == request['basis']['source']['resultSetId'], '视图改变了结果集身份。')
    condition = (request.get('intentDraft') or {}).get('valueFilter')
    if condition:
        from ..value_predicates import validate as predicate
        require(predicate(proof.get('valueFilter')) == predicate(condition), '视图数值条件被改变。')
    retained = [row for row in selected if condition is None or matches(condition, row) is True]
    actual = data['resultSets'][0]['rows']
    require({row['factId']: row for row in actual} == {row['factId']: row for row in retained}
            and len(actual) == len(retained), '筛选结果遗漏、增加或改写了原事实。')
    order = (request.get('intentDraft') or {}).get('presentation', {}).get('valueOrder')
    if order:
        ordered = sorted((row for row in retained if row['valueState'] == 'PRESENT'),
                         key=lambda row: Decimal(row['value']), reverse=order == 'DESC')
        ordered += [row for row in retained if row['valueState'] != 'PRESENT']
        require(actual == ordered, '视图排序未保留精确数值顺序。')
    original = deepcopy(proof['originalRequest'])
    original.pop('basis', None)
    draft = original.get('intentDraft') or {}
    draft.pop('valueFilter', None)
    draft.pop('presentation', None)
    # Validate every selected source coordinate against the retained original native
    # matrix, including rows excluded by the new filter. Never trust display values.
    native_data = {**data, 'question': original['question'], 'purpose': original['purpose'],
                   'resultSets': [{**data['resultSets'][0], 'rows': selected}]}
    native_data.pop('sourceBasis', None)
    return validate({**capture, 'request': original}, {**receipt, 'context': base, 'data': native_data})
