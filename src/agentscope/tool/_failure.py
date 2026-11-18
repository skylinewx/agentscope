"""Bounded tool failure diagnostics shared by foreground and background calls."""
import json
import re
from mcp.shared.exceptions import McpError

from .._logging import logger


def _safe_message(error):
    """Keep the cause readable without copying credentials or HTTP bodies."""
    names = {kind.__name__ for kind in type(error).__mro__}
    if names & {'HTTPStatusError', 'HTTPError', 'TransportError'}:
        return 'HTTP 或传输异常；请结合异常类型和状态码排查。'
    message = re.sub(r'https?://[^\s\'"<>]+', '[URL]', str(error))
    message = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', message)
    message = re.sub(
        r'''(?i)((?:password|passwd|token|secret|api[_-]?key|authorization)["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s,;]+)''',
        r'\1[REDACTED]', message,
    )
    return message[:384] or type(error).__name__


def tool_failure(error, tool_call, *, read_only=False):
    """Unwrap exception groups/causes; correlate without inventing a root cause.

    Recovery is advice only. A failed transport does not prove the remote
    operation was cancelled, so retry is offered only for read-only tools.
    """
    pending, seen, causes, kinds = [error], set(), [], set()
    while pending and len(seen) < 32:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        else:
            names = {kind.__name__ for kind in type(current).__mro__}
            response = getattr(current, 'response', None)
            status = getattr(response, 'status_code', None)
            if not isinstance(status, int):
                status = getattr(current, 'status_code', None)
            cause = {'type': type(current).__name__, 'message': _safe_message(current)}
            mcp_code = current.error.code if isinstance(current, McpError) else None
            if mcp_code is not None:
                cause['mcpCode'] = mcp_code
            if isinstance(status, int) and 100 <= status <= 599:
                cause['httpStatus'] = status
                kind = 'transient' if status in (408, 429) or status >= 500 else 'request'
                if status in (401, 403):
                    kind = 'permission'
            elif mcp_code == 408 or names & {'TimeoutError', 'TimeoutException', 'ConnectionError', 'NetworkError', 'RemoteProtocolError'}:
                kind = 'transient'
            elif mcp_code in (-32600, -32601, -32602):
                kind = 'request'
            else:
                kind = 'unknown'
            # A wrapper with a known cause is descriptive, not an additional
            # independent failure that would mask a recoverable timeout.
            linked = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
            if linked is None or kind in {'permission', 'request'}:
                kinds.add(kind)
            causes.append(cause)
        linked = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if linked is not None:
            pending.append(linked)

    retryable = kinds == {'transient'} and read_only and not pending
    recovery = ('RETRY' if retryable else 'CHECK_EXECUTION' if kinds == {'transient'}
                else 'CHECK_PERMISSION' if 'permission' in kinds
                else 'CORRECT_REQUEST' if kinds == {'request'} else 'CHECK_DIAGNOSTICS')
    details = {
        'code': 'TOOL_EXECUTION_FAILED', 'toolCallId': tool_call.id,
        'toolName': tool_call.name, 'exceptionType': type(error).__name__,
        'causes': causes, 'causesTruncated': bool(pending),
        'retryable': retryable, 'recoveryAction': recovery,
    }
    try:
        request = json.loads(tool_call.input)
        request_id = request.get('requestId') if isinstance(request, dict) else None
        if isinstance(request_id, str) and re.fullmatch(r'[\w.:-]{1,128}', request_id):
            details['requestId'] = request_id
    except (TypeError, ValueError):
        pass
    logger.warning('工具执行失败：%s', json.dumps(details, ensure_ascii=False))
    return details


def failure_text(details):
    """Make diagnostics visible to the model and persisted captures alike."""
    return '工具执行失败：' + json.dumps(details, ensure_ascii=False)
