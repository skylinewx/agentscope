"""宿主管理只读查询的执行尝试；未知结果沿用身份，明确失败才换身份。"""
from .models import digest


def logical_request(owner, turn_key, origin, request):
    content = {key: value for key, value in request.items() if key != 'requestId'}
    return digest([owner.key, turn_key, origin, content])


def request_identity(logical_key, attempt=0):
    return 'host_' + (logical_key if attempt == 0 else digest([logical_key, attempt]))[:48]


def prepare(store, owner, turn_key, origin, request):
    """事务内领取下一次尝试；并发调用与重启恢复共享同一个当前身份。"""
    logical_key = logical_request(owner, turn_key, origin, request)
    selected = {}

    def update(progress):
        entries = progress.setdefault('queryAttempts', {})
        current = entries.setdefault(logical_key, {'attempt': 0, 'status': 'UNKNOWN'})
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
        current.update(status=receipt['status'], executionId=receipt['executionId'],
                       errorCode=error.get('code'),
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
