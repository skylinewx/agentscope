"""Storage availability is not an authentication or business validation error."""


class StorageTemporarilyUnavailable(RuntimeError):
    pass


def is_storage_unavailable(error):
    if isinstance(error, StorageTemporarilyUnavailable):
        return True
    try:
        from redis.exceptions import ConnectionError, TimeoutError
        return isinstance(error, (ConnectionError, TimeoutError))
    except ImportError:
        return False


def unavailable_detail():
    return {'code': 'STORAGE_TEMPORARILY_UNAVAILABLE',
            'message': '会话存储暂时不可用，请稍后重试；不会清除已有会话或登录信息。'}
