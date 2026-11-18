"""Stream observed internal phases through the normal persisted reply event flow."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from agentscope.event import CustomEvent


async def stream_progress(service, agent, source):
    queue = asyncio.Queue()
    closed = False
    previous = getattr(service, 'progress_notifier', None)
    async def notify(value):
        if not closed:
            await queue.put((CustomEvent(name='analysis_execution_progress', value={
                **value, 'replyId': agent.state.reply_id}), None))
    service.progress_notifier = notify
    sentinel = object()
    async def forward():
        try:
            async for event in source:
                # Core events require consumer acknowledgement. In particular,
                # ReplyEnd is mutable and the outer agent decides whether it
                # ends the reply; reading ahead can create duplicate ends.
                accepted = asyncio.get_running_loop().create_future()
                await queue.put((event, accepted))
                await accepted
        finally:
            await queue.put(sentinel)
    worker = asyncio.create_task(forward())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                await worker
                break
            event, accepted = item
            try:
                yield event
            finally:
                if accepted is not None and not accepted.done(): accepted.set_result(None)
    finally:
        closed = True
        service.progress_notifier = previous
        if not worker.done(): worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def phase(turn, stage, status, *, operation_id=None, attempt=1, **details):
    if turn is None: return operation_id
    operation_id = operation_id or uuid4().hex
    snapshot = turn.state.setdefault('executionProgress', {'version': 1, 'revision': 0, 'operations': []})
    snapshot['revision'] += 1
    operation = next((item for item in snapshot['operations'] if item['operationId'] == operation_id), None)
    timestamp = datetime.now(timezone.utc).isoformat()
    if operation is None:
        operation = {'operationId': operation_id, 'stage': stage, 'startedAt': timestamp}
        snapshot['operations'].append(operation)
    operation.update(status=status, attempt=attempt, **details)
    if status != 'RUNNING': operation['endedAt'] = timestamp
    # Nested review completion must not hide an outer publication or another
    # still-running operation. The most recent active phase is the headline.
    active = next((item for item in reversed(snapshot['operations']) if item['status'] == 'RUNNING'), operation)
    snapshot.update(turnKey=turn.state.get('turnKey'), stage=active['stage'], status=active['status'],
                    startedAt=active['startedAt'], attempt=active['attempt'])
    snapshot['operations'] = snapshot['operations'][-32:]
    notifier = getattr(turn.service, 'progress_notifier', None)
    if notifier:
        await notifier(deepcopy(snapshot))
    return operation_id
