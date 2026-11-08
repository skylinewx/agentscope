"""宿主管理只读查询的执行尝试；未知结果沿用身份，明确失败才换身份。"""
from .models import digest, AnalysisError


def failure_key(origin, request):
    """识别只更换草案 id、标签、顺序的同一请求；真实条件修正产生不同键。"""
    if origin.get('originalToolName') != 'execute_budget_query':
        return None
    def normalized(value):
        if isinstance(value, dict):
            return {key: normalized(child) for key, child in value.items()
                    if key not in ('id', 'label', 'requestId', 'presentation')}
        if isinstance(value, list):
            return sorted((normalized(child) for child in value), key=digest)
        return value
    from copy import deepcopy
    canonical = deepcopy(request)
    draft = canonical.get('intentDraft') or {}
    targets = {item.get('id'): item for item in draft.get('targets', [])}
    signatures = {}
    def target_key(key, visiting=()):
        if key in signatures:
            return signatures[key]
        if key not in targets or key in visiting:
            raise ValueError('unresolved intent reference')
        item = targets[key]
        content = {field: value for field, value in item.items() if field not in ('id', 'inheritFrom')}
        content['inheritFrom'] = sorted(target_key(parent.split(':', 1)[0], (*visiting, key))
                                       + (':' + parent.split(':', 1)[1] if ':' in parent else '')
                                       for parent in item.get('inheritFrom', []))
        signatures[key] = digest(normalized(content))
        return signatures[key]
    try:
        for key in targets:
            target_key(key)
        scope_ids = {item['id']: digest(normalized({**item,
            'targetIds': [target_key(key) for key in item.get('targetIds', [])]}))
            for item in draft.get('dimensionScopes', [])}
        for item in draft.get('targets', []):
            item['inheritFrom'] = [target_key(key.split(':', 1)[0]) + (':' + key.split(':', 1)[1] if ':' in key else '')
                                   for key in item.get('inheritFrom', [])]
        for item in draft.get('relations', []) + draft.get('dimensionScopes', []):
            item['targetIds'] = [target_key(key) for key in item.get('targetIds', [])]
        canonical['groupBy'] = [{'scopeId': scope_ids.get(item['scopeId'], item['scopeId'])}
                                if isinstance(item, dict) and 'scopeId' in item else item
                                for item in canonical.get('groupBy', [])]
    except ValueError:
        # Invalid graphs retain their literal references; don't equate different broken references.
        return digest([origin, {key: value for key, value in request.items() if key != 'requestId'}])
    return digest([origin, normalized(canonical)])


def logical_request(owner, turn_key, origin, request):
    content = {key: value for key, value in request.items() if key != 'requestId'}
    return digest([owner.key, turn_key, origin, content])


def request_identity(logical_key, attempt=0):
    return 'host_' + (logical_key if attempt == 0 else digest([logical_key, attempt]))[:48]


def prepare(store, owner, turn_key, origin, request):
    """事务内领取下一次尝试；并发调用与重启恢复共享同一个当前身份。"""
    logical_key = logical_request(owner, turn_key, origin, request)
    equivalent = failure_key(origin, request)
    selected = {}

    def update(progress):
        entries = progress.setdefault('queryAttempts', {})
        prior = next((entry for key, entry in entries.items() if key != logical_key and equivalent is not None
                      and entry.get('failureKey') == equivalent and entry.get('status') == 'FAILED'
                      and not entry.get('retryAllowed') and entry.get('recoveryAction') != 'READ_STATUS'), None)
        if prior:
            raise AnalysisError('QUERY_EQUIVALENT_FAILURE', '相同业务条件已明确失败。修改草案标识或顺序不会修复问题，请按原诊断修正实际条件或补充来源证据。',
                                {'executionId': prior.get('executionId'), 'errorCode': prior.get('errorCode'),
                                 'recoveryAction': prior.get('recoveryAction')})
        current = entries.setdefault(logical_key, {'attempt': 0, 'status': 'UNKNOWN'})
        current['failureKey'] = equivalent
        if current.get('retryAllowed') and current['attempt'] < store.limits.max_query_retries:
            current = {'attempt': current['attempt'] + 1, 'status': 'UNKNOWN',
                       'previousExecutionId': current['executionId'], 'retryReason': current['errorCode']}
            entries[logical_key] = current
        selected.update(current)

    store.turn_progress(owner, turn_key, update)
    request_id = request_identity(logical_key, selected['attempt'])
    return {**request, 'requestId': request_id}, {**selected, 'logicalKey': logical_key, 'requestId': request_id}


def record(store, owner, turn_key, attempt, receipt):
    """仅接受来源明确的执行回执；超时和传输异常不推定为失败。"""
    if (not attempt or not receipt or receipt.get('contractVersion') != '2.0'
            or not receipt.get('executionId')
            or receipt.get('status') not in ('IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'PARTIAL')):
        return
    error = receipt.get('error') or {}
    if not isinstance(error, dict):
        return

    def update(progress):
        current = progress.get('queryAttempts', {}).get(attempt['logicalKey'])
        # 旧调用的迟到响应不能覆盖新尝试，也不能把终态倒退成执行中。
        if (not current or current['attempt'] != attempt['attempt']
                or current.get('status') in ('SUCCEEDED', 'FAILED', 'PARTIAL')):
            return
        from .query_waiting import waiting
        current.update(status='IN_PROGRESS' if waiting(receipt) else receipt['status'], executionId=receipt['executionId'],
                       errorCode=error.get('code'),
                       recoveryAction=error.get('recoveryAction'),
                       retryAllowed=(receipt['status'] == 'FAILED' and error.get('retryable') is True
                                     and error.get('recoveryAction') == 'RETRY'))

    store.turn_progress(owner, turn_key, update)


def _retryable_failure(receipt):
    if not isinstance(receipt, dict):
        return False
    error = receipt.get('error') or {}
    if not isinstance(error, dict):
        return False
    return bool(receipt.get('contractVersion') == '2.0' and receipt.get('executionId')
                and receipt.get('status') == 'FAILED' and error.get('retryable') is True
                and error.get('recoveryAction') == 'RETRY')


def can_retry(store, attempt, receipt):
    return bool(attempt and _retryable_failure(receipt)
                and attempt['attempt'] < store.limits.max_query_retries)


def exhausted(store, attempt, receipt):
    """来源允许重试与本轮仍有重试额度分开返回，避免模型反复领取旧失败回执。"""
    return bool(attempt and _retryable_failure(receipt)
                and attempt['attempt'] >= store.limits.max_query_retries)
