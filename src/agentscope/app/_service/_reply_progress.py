"""Render observed public progress directly; no auxiliary model or side task.

The caller owns source execution, cancellation, persistence and SSE order.
"""
from __future__ import annotations

from copy import deepcopy
import json
from uuid import uuid4

from ...event import CustomEvent, ReplyEndEvent
from ..._logging import logger
from ._progress_context import object_value, operation, observations_from_output

PROGRESS_KEY = 'agentProgress'
MAX_STEPS = 48


def apply_progress(message, event):
    """Application-specific reducer shared by live accumulation and history."""
    if not isinstance(event, CustomEvent) or event.name != 'agent_progress':
        return False
    value = object_value(event.value)
    if value.get('replyId') != message.id or value.get('version') != 1 or message.finished_at:
        return True
    old = message.metadata.get(PROGRESS_KEY, {})
    if isinstance(value.get('revision'), int) and value['revision'] > old.get('revision', -1):
        message.metadata[PROGRESS_KEY] = deepcopy(value)
    return True


class ReplyProgress:
    def __init__(self, previous=None):
        self.value = deepcopy((previous.metadata.get(PROGRESS_KEY) if previous else None) or
                              {'version': 1, 'replyId': '', 'revision': 0, 'steps': []})
        self.reply_id = previous.id if previous else ''
        self.calls, self.round_calls, self.pending_calls = {}, [], set()
        self.step = self.value['steps'][-1] if self.value['steps'] else None
        self.labels = dict(self.value.get('context', {}).get('labels', {}))
        self.seen = {self.fact_key(f) for step in self.value['steps'] for f in step.get('facts', [])}
        self.closed = False
        self.published = None
        if previous:
            for block in previous.content:
                if block.type == 'tool_call':
                    self.calls[block.id] = self.call(block.name, block.input)
                elif block.type == 'tool_result' and block.id in self.calls:
                    self.calls[block.id]['state'] = block.state
            self.pending_calls = {k for k, c in self.calls.items() if c['state'] in ('pending', 'running')}

    @staticmethod
    def call(name, arguments=''):
        return {'name': name, 'arguments': arguments or '', 'text': '', 'state': 'pending',
                'facts': [], 'topic': '', 'title': '', 'nextAction': ''}

    @staticmethod
    def fact_key(fact):
        # Capturing the same observation again is not new user-visible work.
        return json.dumps([fact.get('text'), fact.get('scope'), fact.get('state')], ensure_ascii=False)

    def event(self):
        self.value['replyId'] = self.reply_id
        self.value['revision'] += 1
        self.value['steps'] = self.value['steps'][-MAX_STEPS:]
        self.value['context'] = {'labels': dict(list(self.labels.items())[-128:])}
        return CustomEvent(name='agent_progress', value=deepcopy(self.value))

    def emit(self):
        snapshot = json.dumps(self.value['steps'], ensure_ascii=False, sort_keys=True)
        if snapshot == self.published:
            return []
        self.published = snapshot
        return [self.event()]

    def begin(self, topic='', title='准备处理'):
        if self.step and not self.step.get('facts') and not self.step.get('summary'):
            self.step.update(topic=topic, title=title)
            return
        if self.step and self.step['status'] == 'running':
            self.step['status'] = 'completed'
        self.step = {'id': uuid4().hex, 'topic': topic, 'title': title, 'status': 'running',
                     'summary': '', 'nextAction': '', 'facts': []}
        self.value['steps'].append(self.step)

    def plan(self):
        plans = []
        for key in self.round_calls:
            call = self.calls[key]
            profile = operation(call['name'], call['arguments'], self.labels)
            call.update(profile)
            if profile['nextAction'] and profile['nextAction'] not in plans:
                plans.append(profile['nextAction'])
        if not plans:
            return []
        if not self.step['summary']:
            first = next(c for c in (self.calls[k] for k in self.round_calls) if c['nextAction'])
            self.step.update(title=first['title'], topic=first['topic'])
        self.step['nextAction'] = '\n'.join(plans[:2])
        return self.emit()

    def finish_batch(self):
        calls = [self.calls[key] for key in self.round_calls if key in self.calls]
        pending = [c for c in calls if c['state'] in ('pending', 'running', 'background')]
        failures = [c for c in calls if c['state'] in ('error', 'denied', 'interrupted')
                    or any(f.get('kind') == 'failure' for f in c['facts'])]
        facts, fresh = [], []
        for call in calls:
            for fact in call['facts']:
                if self.fact_key(fact) not in {self.fact_key(f) for f in facts}:
                    facts.append(fact)
                    if self.fact_key(fact) not in self.seen:
                        fresh.append(fact)
        if fresh:
            profile = next((c for c in reversed(calls) if c['facts']), calls[-1])
            topic = profile.get('topic') or 'result'
            # Combine small metadata reads within one phase. Distinct monetary
            # results keep their own stage; no previous summary is copied.
            same_phase = (self.step.get('topic') == topic and topic in ('source', 'scope', 'schema', 'targets', 'entities', 'grouping')
                          and self.step['status'] not in ('error', 'interrupted'))
            if self.step['summary'] and not same_phase:
                self.begin(topic, profile.get('title') or '取得新结果')
            else:
                self.step.update(topic=topic, title=profile.get('title') or '取得新结果')
            if same_phase and topic == 'scope':
                self.step['title'] = '核对查询口径'
            selected = (self.step.get('facts', []) + fresh)[-4:] if same_phase and topic == 'scope' else fresh[:4]
            self.step.update(facts=selected, summary=''.join(f['text'].rstrip('。；') + '。' for f in selected),
                             canRewrite=not failures and not pending, status='running')
            self.seen.update(self.fact_key(f) for f in fresh)
        elif failures:
            failed = failures[0]
            self.begin('error', failed.get('title') or '操作未完成')
            self.step['summary'] = f'“{failed.get("title") or "当前操作"}”未成功完成，这次请求没有取得可用结果。'
        elif not pending:
            # Finishing a bookkeeping call or rereading an identical receipt
            # does not deserve another narrative item or boilerplate sentence.
            self.step['nextAction'] = ''
            return self.emit()
        self.step['nextAction'] = ''
        if pending:
            self.step.update(status='waiting', canRewrite=False,
                             nextAction='已提交的操作仍在执行，正在等待实际返回结果。')
        elif failures:
            self.step.update(status='error', canRewrite=False)
        return self.emit()

    def observe(self, event):
        kind = event.type
        if kind == 'REPLY_START':
            self.reply_id = event.reply_id
            if not self.step:
                self.begin()
                self.step['nextAction'] = '正在准备本次请求；具体操作确定后会在这里说明。'
            return self.emit()
        if not self.reply_id:
            self.reply_id = getattr(event, 'reply_id', '')
        if self.reply_id and not self.step:
            self.begin()
            self.round_calls = list(self.pending_calls)
        if not self.step or self.closed or getattr(event, 'reply_id', self.reply_id) != self.reply_id:
            return []
        if kind == 'MODEL_CALL_START':
            self.round_calls = list(self.pending_calls)
        elif kind == 'TOOL_CALL_START':
            key = event.tool_call_id
            self.calls.setdefault(key, self.call(event.tool_call_name))
            if key not in self.round_calls:
                self.round_calls.append(key)
            self.pending_calls.add(key)
        elif kind == 'TOOL_CALL_DELTA':
            if event.tool_call_id in self.calls:
                call = self.calls[event.tool_call_id]
                call['arguments'] = (call['arguments'] + event.delta)[:65536]
        elif kind == 'MODEL_CALL_END' and self.round_calls:
            return self.plan()
        elif kind == 'TOOL_RESULT_START':
            key = event.tool_call_id
            self.calls.setdefault(key, self.call(event.tool_call_name))
            if key not in self.round_calls:
                self.round_calls.append(key)
        elif kind == 'TOOL_RESULT_TEXT_DELTA' and event.tool_call_id in self.calls:
            call = self.calls[event.tool_call_id]
            call['text'] = (call['text'] + event.delta)[:65536]
        elif kind == 'TOOL_RESULT_END':
            key = event.tool_call_id
            call = self.calls.get(key)
            if call is None:
                return []
            call['state'] = 'background' if event.metadata.get('backgroundToolCallId') else event.state
            if call['state'] != 'background':
                observation = observations_from_output(call['text'], event.metadata, key, call['name'], call['arguments'])
                if call['state'] != 'success':
                    observation['facts'] = [f for f in observation['facts'] if f.get('kind') == 'failure']
                call.update({k: observation[k] for k in ('facts', 'topic', 'title')})
                self.labels.update(observation['labels'])
            call['text'] = ''
            self.pending_calls.discard(key)
            if any(self.calls[k]['state'] == 'pending' for k in self.round_calls):
                return []
            return self.finish_batch()
        elif kind == 'HINT_BLOCK':
            source = object_value(event.source)
            if (source.get('label') != 'tool_output' or not isinstance(source.get('backgroundToolCallId'), str)
                    or source.get('backgroundState') not in ('success', 'error', 'denied', 'cancelled', 'interrupted')):
                return []
            key = source['backgroundToolCallId']
            call = self.calls.setdefault(key, self.call(source.get('backgroundToolName') or '后台任务'))
            call['state'] = 'interrupted' if source['backgroundState'] == 'cancelled' else source['backgroundState']
            chunks = [event.hint] if isinstance(event.hint, str) else [getattr(b, 'text', '') for b in event.hint]
            for chunk in chunks[:4]:
                observation = observations_from_output(chunk[:65536], object_value(source.get('toolResponseMetadata')),
                                                       key, call['name'], call['arguments'])
                if call['state'] != 'success':
                    observation['facts'] = [f for f in observation['facts'] if f.get('kind') == 'failure']
                if observation['facts']:
                    call.update({k: observation[k] for k in ('facts', 'topic', 'title')})
                    self.labels.update(observation['labels'])
                    break
            self.round_calls = [key]
            self.pending_calls.discard(key)
            return self.finish_batch()
        elif kind in ('REQUIRE_USER_CONFIRM', 'REQUIRE_EXTERNAL_EXECUTION'):
            self.step.update(status='waiting', canRewrite=False)
            self.step['nextAction'] = ('已选定的操作需要你的确认，确认前不会执行。' if kind == 'REQUIRE_USER_CONFIRM'
                                      else '已选定的操作正在等待外部执行结果。')
            return self.emit()
        elif kind == 'REPLY_END':
            completion = event.metadata.get('analysisCompletion', {})
            status = ('waiting' if completion.get('status') == 'WAITING' else
                      'completed' if event.finished_reason == 'completed' else
                      'interrupted' if event.finished_reason == 'interrupted' else 'error')
            failed_step = status == 'completed' and self.step['status'] == 'error'
            self.step.update(status='error' if failed_step else status, canRewrite=False)
            self.step['nextAction'] = {
                'completed': '本次处理已结束，最终结果已在下方答复中整理。',
                'waiting': '后台操作尚未完成，正在等待实际结果，当前内容还不能作为最终交付。',
                'interrupted': '本次处理已中断，尚未完成的操作和后续计划未继续执行。',
                'error': '本次处理未能完成，已有进展保留，未完成的事项不能视为已完成。',
            }[status]
            if failed_step:
                self.step['nextAction'] = '本次回复已结束；这一步仍未成功，不能把它视为已取得有效结果。'
            updates = self.emit()
            self.closed = True
            event.metadata[PROGRESS_KEY] = deepcopy(self.value)
            return updates
        return []


async def with_reply_progress(events, *, previous=None):
    tracker = ReplyProgress(previous)
    try:
        async for event in events:
            try:
                updates = tracker.observe(event)
            except Exception:
                # Observability must never turn a usable tool response into a
                # failed user request. Keep the original event stream intact.
                logger.warning('阶段进展生成失败，保留主执行流', exc_info=True)
                updates = []
            # Terminal metadata and progress settle before the end reaches clients.
            if isinstance(event, ReplyEndEvent):
                for update in updates:
                    yield update
                yield event
            else:
                yield event
                for update in updates:
                    yield update
    except Exception:
        if tracker.step and not tracker.closed:
            tracker.step.update(status='error', nextAction='执行发生错误，本阶段未完成的工作尚未取得结果。')
            yield tracker.event()
        raise
    finally:
        tracker.closed = True
