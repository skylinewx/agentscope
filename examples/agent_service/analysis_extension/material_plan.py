"""One model-owned material plan; transport operations are derived by the host."""
from copy import deepcopy

from .models import AnalysisError


ROLES = ('PRIMARY', 'REFERENCE', 'STRUCTURE')
KINDS = ('VALUES', 'FORMULAS')
DEFAULT = [{'role': 'PRIMARY', 'kinds': ['VALUES']}]


def normalize(value, previous=None):
    if value is None:
        return deepcopy(previous if previous is not None else DEFAULT)
    if not isinstance(value, list) or not 1 <= len(value) <= len(ROLES):
        raise AnalysisError('INVALID_MATERIAL_PLAN', 'materials 按用途声明 VALUES/FORMULAS，每种 role 只出现一次。')
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {'role', 'kinds'}:
            raise AnalysisError('INVALID_MATERIAL_PLAN', '每项材料需求只包含 role 和 kinds；对象通过 subjectSources/formulaSubjects 选择。')
        role, kinds = item['role'], item['kinds']
        if (role not in ROLES or role in seen or not isinstance(kinds, list) or not kinds
                or any(kind not in KINDS for kind in kinds) or len(kinds) != len(set(kinds))):
            raise AnalysisError('INVALID_MATERIAL_PLAN', '材料用途和种类必须有效且不重复。')
        seen.add(role)
        # Formula material includes its source values; there is no competing values switch.
        result.append({'role': role, 'kinds': [kind for kind in KINDS if kind in kinds or kind == 'VALUES']})
    return sorted(result, key=lambda item: ROLES.index(item['role']))


def needs_formulas(state, role=None):
    return any('FORMULAS' in item['kinds'] and (role is None or role == item['role'])
               for item in state.get('materials', DEFAULT))


def query_purpose(state, request):
    """Filtering frozen facts precedes preparation of the selected facts only."""
    role = request.get('materialRole', 'PRIMARY')
    if role not in ROLES:
        raise AnalysisError('INVALID_MATERIAL_ROLE', '本次查询的 materialRole 必须是 PRIMARY、REFERENCE 或 STRUCTURE。')
    if (request.get('basis') or {}).get('kind') == 'FACTS' and (request.get('intentDraft') or {}).get('valueFilter'):
        return 'QUERY'
    # Unadopted discovery queries do not imply a new reference/primary selection.
    return 'PREPARE_MATERIALS' if needs_formulas(state, role) else 'QUERY'


def request_key(request):
    from .models import digest
    return digest({key: value for key, value in request.items() if key != 'requestId'})
