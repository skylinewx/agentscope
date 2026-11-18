"""Safe diagnostics for actual model attempts, without request or error bodies."""
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

from .._logging import logger


_ATTEMPT_OBSERVER = ContextVar('model_attempt_observer', default=None)


@contextmanager
def observe_attempts(observer):
    token = _ATTEMPT_OBSERVER.set(observer)
    try:
        yield
    finally:
        _ATTEMPT_OBSERVER.reset(token)


async def observed_attempt(execute, *, call_id, strategy, attempt):
    """Adapter attempt timing, including cancellation; never log request bodies."""
    observer = _ATTEMPT_OBSERVER.get()
    if observer is None:
        return await execute()
    start = perf_counter()
    event = {'callId': call_id, 'strategy': strategy, 'attempt': attempt,
             'scope': 'MODEL_ADAPTER', 'status': 'STARTED'}
    def emit(value):
        try:
            observer(dict(value))
        except Exception:
            logger.warning('Model attempt observer failed; execution is unchanged')
    emit(event)
    try:
        result = await execute()
        event['status'] = 'COMPLETED'
        return result
    except BaseException as error:
        event.update(status='CANCELLED' if type(error).__name__ == 'CancelledError' else 'FAILED',
                     errorType=type(error).__name__)
        code = getattr(error, 'status_code', None)
        if type(code) is int:
            event['httpStatus'] = code
        raise
    finally:
        event['elapsedMs'] = round((perf_counter() - start) * 1000, 2)
        emit(event)


def causes(error):
    seen, pending = set(), [error]
    while pending and len(seen) < 32:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)


class ModelCallDiagnostic:
    def __init__(self):
        self.call_id = uuid4().hex
        self.started = perf_counter()

    def failed(self, error, phase, attempt, *, chunks=0, strategy=None):
        chain = list(causes(error))
        names = {cls.__name__ for exc in chain for cls in type(exc).__mro__}
        status, request_id = None, None
        for exc in chain:
            response = getattr(exc, 'response', None)
            code = getattr(exc, 'status_code', None) or getattr(exc, 'status', None) or getattr(response, 'status_code', None)
            if type(code) is int and 100 <= code <= 599:
                status = code
            candidate = getattr(exc, 'request_id', None)
            if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', candidate):
                request_id = candidate
        kind = ('http' if status is not None else
                'timeout' if names & {'TimeoutError', 'TimeoutException', 'APITimeoutError'} else
                'protocol' if names & {'ProtocolError', 'RemoteProtocolError', 'LocalProtocolError'} else
                'connection' if names & {'ConnectionError', 'TransportError', 'APIConnectionError', 'ClientConnectionError'} else
                'structured_output' if 'StructuredOutputError' in names else 'unknown')
        details = {'callId': self.call_id, 'phase': phase, 'failureKind': kind,
                   'elapsedMs': round((perf_counter() - self.started) * 1000), 'attempt': attempt,
                   'receivedChunks': chunks, 'exceptionTypes': [type(exc).__name__ for exc in chain],
                   'httpStatus': status, 'upstreamRequestId': request_id}
        if strategy is not None:
            details['strategy'] = strategy
        error._agentscope_model_diagnostics = details
        logger.warning('Model call failure: %s', json.dumps(details, ensure_ascii=False))


def error_diagnostics(error):
    for exc in causes(error):
        details = getattr(exc, '_agentscope_model_diagnostics', None)
        if isinstance(details, dict):
            return dict(details)
    return None
