"""Bind the authorized budget connection to an actual, queryable snapshot."""
import asyncio
from datetime import datetime, timezone

from .actions import source_identity
from .models import AnalysisError, digest


class QueryContexts:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def bind(self, turn, origin, request, runner):
        if 'contextId' in request or 'querySessionId' in request:
            if not (request.get('contextId') and request.get('querySessionId')):
                raise AnalysisError('INCOMPLETE_QUERY_CONTEXT', '省略两个上下文标识可自动绑定；自行选择来源时同时提交该来源返回的两个标识。')
            return request
        from .execution_policy import require
        from .objectives import query_objective
        objective = query_objective(turn.state, dict(request))
        role = (objective or {}).get('role', request.get('materialRole', 'PRIMARY'))
        basis = request.get('basis')
        if not basis and not (objective or {}).get('membersFromObjective'):
            adopted = [item for item in turn.state.get('historyReferences', [])
                       if item.get('role') == role and item.get('use') in ('FACTS', 'MEMBERS')]
            if len(adopted) == 1:
                basis = {'kind': adopted[0]['use'], 'sourceRef': adopted[0]['sourceRef']}
        if basis and basis.get('kind') == 'FACTS' and basis.get('sourceRef'):
            source = turn.service.store.get_source_view(turn.service.owner, basis['sourceRef'])
            if any(origin.get(key) != value for key, value in source['sourceIdentity'].items()):
                raise AnalysisError('SOURCE_CONNECTION_MISMATCH', '所选事实不属于本次预算连接。')
            capture = turn.service.store.get_capture(turn.service.owner, source['selection']['captureRef'])
            receipt = turn.service.sources.for_capture(capture).raw_receipt(capture['response'])
            context_id = (receipt.get('context') or {}).get('contextId')
            session_id = receipt.get('querySessionId')
            if context_id and session_id:
                return {**request, 'contextId': context_id, 'querySessionId': session_id}
            raise AnalysisError('SOURCE_CONTEXT_REQUIRED', '该来源缺少可核实的上下文，无法读取冻结事实。')
        if (basis or {}).get('kind') != 'FACTS':
            require(turn, 'sourceQueries', 'execute_budget_query')
        key = digest(source_identity(origin))
        async with self.lock:
            async def read(action, **args):
                refs, error = await runner(turn, origin, 'get_context', {'contractVersion': '2.0', 'action': action, **args})
                if error or not refs:
                    raise AnalysisError((error or {}).get('code', 'QUERY_CONTEXT_UNAVAILABLE'),
                        (error or {}).get('message') or '读取来源上下文失败，尚未发出取数请求。', error)
                capture = turn.service.store.get_capture(turn.service.owner, refs[-1])
                return turn.service.sources.for_capture(capture).raw_receipt(capture['response'])

            contexts = turn.state.setdefault('budgetQueryContexts', {})
            bound = contexts.get(key)
            if bound and _valid(bound):
                return {**request, **bound['ids']}
            choices, cursors, revision = {}, set(), None
            page_args = {}
            while True:
                page = (await read('discover', **page_args)).get('directory') or {}
                if not isinstance(page.get('entries'), list) or not page.get('revision') or (
                        revision is not None and page['revision'] != revision):
                    raise AnalysisError('QUERY_DIRECTORY_CHANGED', '来源目录不完整或分页期间发生变化，请重新发现来源。')
                revision = page['revision']
                for entry in page['entries']:
                    if entry.get('queryable') is True and entry.get('sourceId') and entry.get('snapshotId'):
                        choices[(entry['sourceId'], entry['snapshotId'])] = entry
                cursor = page.get('nextCursor')
                if cursor is None:
                    break
                if not cursor or cursor in cursors:
                    raise AnalysisError('QUERY_DIRECTORY_INCOMPLETE', '来源目录分页未完成，尚未选择快照。')
                cursors.add(cursor)
                page_args = {'contextDiscoveryCursor': cursor}
            if len(choices) != 1:
                raise AnalysisError('QUERY_SOURCE_SELECTION' if choices else 'NO_QUERYABLE_SOURCE',
                    '请按问题选择适用的来源快照，通过 get_context(open) 绑定后取数。' if choices else '该连接的完整目录中没有可查询快照。',
                    {'candidates': list(choices.values())})
            source, snapshot = next(iter(choices))
            session = (await read('open', sourceId=source, snapshotId=snapshot)).get('session') or {}
            context = session.get('context') or {}
            ids = {'contextId': context.get('contextId'), 'querySessionId': session.get('querySessionId')}
            if (context.get('sourceId'), context.get('snapshotId')) != (source, snapshot) or not all(ids.values()):
                raise AnalysisError('QUERY_CONTEXT_MISMATCH', '打开的上下文与选定快照不一致，尚未取数。')
            bound = {'ids': ids, 'expiresAt': session.get('expiresAt')}
            if not _valid(bound):
                raise AnalysisError('QUERY_CONTEXT_EXPIRED', '打开的来源上下文已过期或缺少有效期。')
            contexts[key] = bound
            return {**request, **ids}


def _valid(bound):
    try:
        return datetime.fromisoformat(bound['expiresAt']) > datetime.now(timezone.utc)
    except (TypeError, ValueError, KeyError):
        return False
