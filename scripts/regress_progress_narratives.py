"""Offline public-event reconstruction from a recorded real business run.

Reads tool calls, receipts and the final answer only. No thinking blocks enter
the summary generator. It does not call a model or external business service.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from agentscope.app._service._progress_context import public_observations
from agentscope.app._service._reply_progress import apply_progress, with_reply_progress
from agentscope.event import (
    CustomEvent, ReplyStartEvent, ReplyEndEvent, ModelCallStartEvent, ModelCallEndEvent,
    ToolCallStartEvent, ToolCallDeltaEvent, ToolCallEndEvent, ToolResultStartEvent,
    ToolResultTextDeltaEvent, ToolResultEndEvent, TextBlockStartEvent, TextBlockDeltaEvent,
)
from agentscope.message import AssistantMsg


async def run(args):
    messages = json.loads(Path(args.messages).read_text(encoding='utf-8'))
    captures = json.loads(Path(args.captures).read_text(encoding='utf-8'))
    sources = {c['data']['toolCallId']: c for c in captures}
    original = next(m for m in reversed(messages) if m['role'] == 'assistant')
    rid = original['id']
    blocks = [b for b in original['content'] if b['type'] in ('tool_call', 'tool_result', 'text')]

    async def source():
        yield ReplyStartEvent(session_id='recorded-regression', reply_id=rid, name=original['name'])
        selecting = False
        for block in blocks:
            if block['type'] == 'tool_call':
                if not selecting:
                    yield ModelCallStartEvent(reply_id=rid, model_name='recorded-model')
                    selecting = True
                yield ToolCallStartEvent(reply_id=rid, tool_call_id=block['id'], tool_call_name=block['name'])
                yield ToolCallDeltaEvent(reply_id=rid, tool_call_id=block['id'], delta=block['input'])
                yield ToolCallEndEvent(reply_id=rid, tool_call_id=block['id'])
            else:
                if selecting:
                    yield ModelCallEndEvent(reply_id=rid, input_tokens=0, output_tokens=0)
                    selecting = False
                if block['type'] == 'tool_result':
                    metadata = deepcopy(block.get('metadata', {}))
                    metadata.pop('public_progress', None)
                    capture = sources.get(block['id'])
                    if capture:
                        item = capture['data']
                        receipt = json.loads(item['response']['content'][0]['text'])
                        metadata['public_progress'] = public_observations(receipt, capture['ref'], item['toolName'], item['request'])
                    yield ToolResultStartEvent(reply_id=rid, tool_call_id=block['id'], tool_call_name=block['name'])
                    for content in block.get('output', []):
                        if content['type'] == 'text':
                            yield ToolResultTextDeltaEvent(reply_id=rid, tool_call_id=block['id'], delta=content['text'])
                    yield ToolResultEndEvent(reply_id=rid, tool_call_id=block['id'], state=block['state'], metadata=metadata)
                else:
                    yield TextBlockStartEvent(reply_id=rid, block_id=block['id'])
                    yield TextBlockDeltaEvent(reply_id=rid, block_id=block['id'], delta=block['text'])
        metadata = deepcopy(original.get('metadata', {}))
        metadata.pop('agentProgress', None)
        yield ReplyEndEvent(session_id='recorded-regression', reply_id=rid,
                            finished_reason=original['finished_reason'], metadata=metadata)

    message, events = AssistantMsg(original['name'], [], id=rid), []
    async for event in with_reply_progress(source()):
        events.append(event.model_dump(mode='json'))
        if isinstance(event, CustomEvent):
            apply_progress(message, event)
        elif event.type != 'REPLY_START':
            message.append_event(event)
    steps = message.metadata['agentProgress']['steps']
    summaries = [s['summary'] for s in steps if s['summary']]
    assert len(set(summaries)) == len(summaries), 'Duplicate narrative paragraphs'
    assert not any('执行所选操作' in s or '取得了本阶段的返回结果' in s for s in summaries)
    assert any('候选' in s and '利润表' in s for s in summaries)
    assert any('本次请求已登记' in s for s in summaries)
    assert all(s['nextAction'] for s in steps), 'Missing actual selected follow-up in recorded phases'
    assert any('289929166.64' in s and '未标注' in s for s in summaries)
    assert any('50' in s and '单次读取条数' in s for s in summaries)
    assert message.get_text_content() == '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
    report = {'caseType': 'offline replay of recorded real tool calls and receipts',
              'networkCalls': 0, 'captureRef': 'multiple recorded captures',
              'events': events, 'message': message.model_dump(mode='json'),
              'checks': {'uniqueSummaries': True, 'specificCandidates': True, 'specificFailure': True,
                         'missingGroupAmount': True, 'answerUnchanged': True}}
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'narratives.txt').write_text('\n\n'.join(f'{i+1}. {s["title"]}\n{s["summary"]}\n{s["nextAction"]}'
                                                    for i, s in enumerate(steps)), encoding='utf-8')
    print(json.dumps({'stages': len(steps), 'checks': report['checks']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--messages', required=True)
    parser.add_argument('--captures', required=True)
    parser.add_argument('--output', required=True)
    asyncio.run(run(parser.parse_args()))
