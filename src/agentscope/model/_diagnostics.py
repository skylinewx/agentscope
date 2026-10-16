"""Safe diagnostics for actual model attempts, without request or error bodies."""
import json
import re
from time import perf_counter
from uuid import uuid4

from .._logging import logger


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
