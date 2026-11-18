"""分析阶段的结构化计时；只记标识、数量和状态，不记录业务正文。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

from agentscope._logging import logger


_TOOL = ContextVar('analysis_timing_tool', default=None)


@contextmanager
def tool_timing_context(call_id, tool_name):
    token = _TOOL.set({'callId': call_id, 'toolName': tool_name})
    try:
        yield
    finally:
        _TOOL.reset(token)


def timing_event(turn, stage, elapsed_ms, *, kind='SUBSTAGE', **details):
    """父工具耗时包含内部阶段；有并行时按起止区间计算，不直接累加。"""
    ended = time.time()
    owner = turn.service.owner
    context = _TOOL.get() or {}
    if context.get('callId') and details.get('callId', context['callId']) != context['callId']:
        details.setdefault('parentCallId', context['callId'])
    event = {'schemaVersion': 1, 'eventId': uuid4().hex, 'stage': stage, 'kind': kind,
             'agentId': owner.agent_id, 'sessionId': owner.session_id,
             'turnKey': turn.state.get('turnKey'), **context,
             'startedAt': datetime.fromtimestamp(ended - elapsed_ms / 1000, timezone.utc).isoformat(),
             'endedAt': datetime.fromtimestamp(ended, timezone.utc).isoformat(),
             'elapsedMs': round(elapsed_ms, 2), **details}
    logger.info('分析耗时 %s', json.dumps(event, ensure_ascii=False, separators=(',', ':')))
    return event


def append_timing(state, event):
    """保留有界明细；日志保留全部事件，裁剪量明确可见。"""
    events = state.setdefault('timingEvents', [])
    events.append(event)
    if len(events) > 512:
        state['timingEventsOmitted'] = state.get('timingEventsOmitted', 0) + len(events) - 512
        del events[:-512]


def record_timing(turn, stage, elapsed_ms, **details):
    if turn is None:
        return
    from .review_control import progress
    event = timing_event(turn, stage, elapsed_ms, **details)
    try:
        progress(turn, lambda state: append_timing(state, event))
    except Exception as error:
        # 可观测性写入失败不得覆盖真实业务异常或将已完成交付改成失败。
        logger.warning('分析耗时明细保存失败 会话=%s 提问=%s 阶段=%s 错误类型=%s',
                       turn.service.owner.session_id, turn.state.get('turnKey'), stage, type(error).__name__)
