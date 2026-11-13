"""Select captured task/form candidates; models never manufacture execution proofs."""
from copy import deepcopy
import re
import unicodedata

from .models import AnalysisError

MODELS = {'BUDGET_TASK': '任务', 'BUDGET_FORM_TEMPLATE': '表样'}


def candidates(receipt, capture):
    if capture.get('originalToolName') != 'search_entities' or receipt.get('status') != 'SUCCEEDED':
        return []
    result = []
    for index, item in enumerate(receipt.get('data', {}).get('candidates', [])):
        if (item.get('modelCode') not in MODELS or item.get('kind') != 'RESOURCE'
                or 'RESOURCE_RDF_VERIFIED' not in item.get('matchedBy', [])):
            continue
        result.append({'candidateRef': f"scand:{capture['captureRef']}:{index}",
                       'kind': MODELS[item['modelCode']], 'name': item.get('label'),
                       'key': item.get('key'), 'matchedBy': item.get('matchedBy', []),
                       'searchText': capture.get('request', {}).get('entityMention')})
    return result


def bind(turn, origin, request):
    bindings = request.get('scope', {}).get('bindings')
    if not bindings:
        return request
    result = deepcopy(request)
    result['scope']['bindings'] = [item['binding'] for item in resolve(turn, origin, bindings)]
    return result


def resolve(turn, origin, bindings):
    """Resolve public references once under the capture owner and connection boundary."""
    resolved = []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {'text', 'candidateRef'}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '范围绑定只填写 text 原话和候选返回的 candidateRef。')
        match = re.fullmatch(r'scand:(cap_[0-9a-f]+):([0-9]+)', str(binding.get('candidateRef', '')))
        if match is None:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '候选引用格式无效，先使用 search_entities 搜索范围原话。')
        capture = turn.service.store.get_capture(turn.service.owner, match[1])
        if capture.get('originalToolName') != 'search_entities' or any(
                capture.get(key) != origin.get(key) for key in ('resourceId', 'serverName', 'connectionFingerprint')):
            raise AnalysisError('SCOPE_CANDIDATE_SOURCE_MISMATCH', '候选不属于当前预算工具连接。')
        normalize = lambda value: unicodedata.normalize('NFKC', str(value or '')).strip()
        if normalize(binding.get('text')) != normalize(capture.get('request', {}).get('entityMention')):
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '绑定原话须与候选搜索原话一致；使用用户范围原话搜索后选择。')
        adapter = turn.service.sources.for_capture(capture)
        receipt = adapter.raw_receipt(capture['response'])
        available = candidates(receipt, capture)
        if binding['candidateRef'] not in {item['candidateRef'] for item in available}:
            raise AnalysisError('INVALID_SCOPE_CANDIDATE', '所选引用不是已核验的任务或表样候选。')
        candidate = receipt['data']['candidates'][int(match[2])]
        resolved.append({'binding': {'text': binding['text'], 'modelCode': candidate['modelCode'], 'executionId': receipt['executionId'],
                                    'candidateId': candidate['candidateId'], 'contextId': receipt['context']['contextId'],
                                    'querySessionId': receipt['querySessionId']},
                         'code': candidate['key'], 'name': candidate['label']})
    return resolved


def inherit(scope, previous, changed):
    """A selected task/form and its original quotation survive or clear together."""
    for model, role, fields in (('BUDGET_TASK', 'task', ('taskCodes', 'taskNames')),
                               ('BUDGET_FORM_TEMPLATE', 'metric', ('formCodes', 'formNames'))):
        prior = [item for item in previous.get('bindings', []) if item.get('modelCode') == model]
        if not prior or role in changed:
            continue
        current = [item for item in scope.get('bindings', []) if item.get('modelCode') == model]
        if current:
            if role == 'task' and current != prior:
                raise AnalysisError('FOLLOWUP_SCOPE_CONFLICT', '关联追问改变了未声明变化的任务绑定。')
            continue
        if any(scope.get(field) and scope.get(field) != previous.get(field) for field in fields):
            continue
        scope.setdefault('bindings', []).extend(deepcopy(prior))
