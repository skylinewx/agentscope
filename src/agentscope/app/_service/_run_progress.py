"""会话准备阶段的真实进度及分段耗时，不包含提示词或业务数据。"""
from time import perf_counter
from uuid import uuid4

from ..._logging import logger
from ...event import CustomEvent
from .._bus_ops import publish_session_event


class RunProgress:
    def __init__(self, bus, session_id, message_id):
        self.bus, self.session_id, self.message_id = bus, session_id, message_id
        self.run_id = uuid4().hex
        self.started = self.previous = perf_counter()
        self.stage = None
        self.stages = {}

    async def advance(self, stage):
        now = perf_counter()
        if self.stage:
            self.stages[self.stage] = round((now - self.previous) * 1000, 2)
        self.previous, self.stage = now, stage
        payload = {'runId': self.run_id, 'messageId': self.message_id, 'stage': stage,
                   'elapsedMs': round((now - self.started) * 1000, 2), 'stages': dict(self.stages)}
        logger.info('会话执行进度 会话=%s 执行=%s 阶段=%s 总耗时毫秒=%.2f 分段=%s',
                    self.session_id, self.run_id, stage, payload['elapsedMs'], self.stages)
        try:
            await publish_session_event(self.bus, self.session_id,
                CustomEvent(name='execution_progress', value=payload).model_dump(mode='json'))
        except Exception:
            # 观测事件失败不能中止已接收的用户请求；主执行仍按原有错误处理交付。
            logger.warning('会话进度通知失败 会话=%s 执行=%s', self.session_id, self.run_id, exc_info=True)
        return payload
