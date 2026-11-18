"""Replay a real saved query through a real model, ChatService and SQL history.

Run explicitly against an existing capture. Credentials are resolved from the
source service in memory, never copied to the test database or output files.
No source business data or existing conversation is modified.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import httpx
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/agent_service'))
from service_config import redis_settings
from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    RedisStorage, AsyncSQLAlchemyStorage, AgentRecord, AgentData, SessionRecord,
    SessionConfig, SessionNaming,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.app._service import _chat
from agentscope.app._service._progress_facts import result_facts
from agentscope.app._service._reply_progress import apply_progress, with_reply_progress
from agentscope.event import AgentEvent, CustomEvent
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg
from agentscope.permission import PermissionMode, PermissionDecision, PermissionBehavior
from agentscope.tool import ToolBase, ToolChunk


async def replay_local(args):
    """Reprocess a recorded real stream without credentials or network access."""
    original = json.loads(Path(args.replay_events).read_text(encoding='utf-8'))
    adapter = TypeAdapter(AgentEvent)
    calls, chunks = {}, {}

    async def source():
        for value in deepcopy(original['events']):
            if value['type'] == 'CUSTOM':
                continue
            if value['type'] == 'TOOL_CALL_START':
                calls[value['tool_call_id']] = value['tool_call_name']
            if value['type'] == 'TOOL_RESULT_TEXT_DELTA':
                key = value['tool_call_id']
                chunks[key] = chunks.get(key, '') + value['delta']
            if value['type'] == 'TOOL_RESULT_END' and calls.get(value['tool_call_id']) == 'ReadBudgetSnapshot':
                facts = result_facts(json.loads(chunks[value['tool_call_id']]), original['captureRef'])
                value.setdefault('metadata', {})['public_progress'] = {'version': 1, 'facts': facts}
            if value['type'] == 'REPLY_END':
                value.get('metadata', {}).pop('agentProgress', None)
            yield adapter.validate_python(value)

    observed, message = [], None
    async for event in with_reply_progress(source()):
        observed.append(event.model_dump(mode='json'))
        if event.type == 'REPLY_START':
            message = AssistantMsg(event.name, [], id=event.reply_id, created_at=event.created_at)
        elif isinstance(event, CustomEvent):
            apply_progress(message, event)
        elif message:
            message.append_event(event)
    assert message and message.finished_reason == 'completed'
    restored = Msg.model_validate_json(message.model_dump_json())
    assert message.metadata == restored.metadata
    assert message.get_text_content() == Msg.model_validate(original['message']).get_text_content()
    progress = message.metadata['agentProgress']
    assert len(progress['steps']) == 2, 'Final composition must not duplicate a tool stage'
    first = progress['steps'][0]
    assert '42' in first['summary'] and '7030100.00' in first['summary'] and '元' in first['summary']
    assert '未标注' in first['summary'] and '2026年' in first['summary'] and '净利润' in first['summary']
    assert '1 条缺少业务板块名称' in progress['steps'][-1]['summary']
    report = {key: value for key, value in original.items() if key not in ('events', 'message', 'checks')}
    report.update(caseType='local replay of recorded live-model real-data run',
                  originalRun=str(Path(args.replay_events).resolve()), networkCalls=0,
                  progressEvents=sum(event['type'] == 'CUSTOM' for event in observed),
                  checks={'valuesUnitsScope': True, 'missingGroupExplicit': True,
                          'noDuplicateFinalStage': True, 'serializationMatchesStream': True,
                          'finalAnswerUnchanged': True}, message=message.model_dump(mode='json'), events=observed)
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    ui = ROOT / 'examples/web_ui/frontend/.cache'
    ui.mkdir(exist_ok=True)
    (ui / 'progress-real-case.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('events', 'message')}, ensure_ascii=False))


async def run(args):
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source_root = ROOT / 'examples/agent_service/analysis-data'
    with sqlite3.connect(f'file:{source_root / "analysis.sqlite3"}?mode=ro', uri=True) as db:
        row = db.execute('SELECT digest FROM captures WHERE ref=?', (args.capture_ref,)).fetchone()
    if not row:
        raise ValueError('Capture not found')
    raw = (source_root / 'blobs' / (row[0] + '.json')).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row[0]
    capture = json.loads(raw)
    receipt = json.loads(capture['response']['content'][0]['text'])
    batches = [batch for step in receipt['data']['stepResults'] for batch in step.get('batchResults', [])]
    batch = next(batch for batch in batches if batch.get('data', {}).get('rows'))
    data = batch['data']
    user = capture['ownerUserId']
    calls = []

    class ReadBudgetSnapshot(ToolBase):
        name = 'ReadBudgetSnapshot'
        description = '读取真实保存的预算查询快照，返回来源、时期、汇总范围、列定义和原始结果。'
        input_schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        is_read_only = True
        is_concurrency_safe = True

        async def check_permissions(self, tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW)

        async def call(self):
            calls.append(self.name)
            value = {'captureRef': capture['captureRef'], 'capturedAt': capture['capturedAt'],
                     'source': capture['originalToolName'], 'data': data,
                     'completeness': receipt.get('completeness')}
            return ToolChunk(content=[TextBlock(text=json.dumps(value, ensure_ascii=False))],
                metadata={'public_progress': {'version': 1, 'facts': result_facts(value, capture['captureRef'])}})

    class CheckBudgetScope(ToolBase):
        name = 'CheckBudgetScope'
        description = '读取快照后，核验返回行数与列定义，并检查业务板块名称是否缺失。只核查形状，不确认业务原因。'
        input_schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        is_read_only = True
        is_concurrency_safe = True

        async def check_permissions(self, tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW)

        async def call(self):
            assert 'ReadBudgetSnapshot' in calls, 'Read before checking'
            calls.append(self.name)
            col = next(index for index, column in enumerate(data['columns']) if column['name'].endswith('MD_YWBK_NAME'))
            missing = sum(row[col] is None or row[col] == '' for row in data['rows'])
            value = {'returnedRows': len(data['rows']), 'declaredRows': data['rowCount'],
                     'rowsMatch': len(data['rows']) == data['rowCount'], 'missingGroupNames': missing,
                     'scope': '仅当前查询快照', 'businessMeaningVerified': False}
            fact = {'text': f'当前返回的 {len(data["rows"])} 条汇总结果中，有 {missing} 条缺少业务板块名称',
                    'sourceId': capture['captureRef'], 'pointer': '/data/rows', 'value': missing,
                    'unit': '条', 'scope': '仅当前查询快照', 'state': 'calculated'}
            return ToolChunk(content=[TextBlock(text=json.dumps(value, ensure_ascii=False))],
                metadata={'public_progress': {'version': 1, 'facts': [fact]}})

    async with RedisStorage(**redis_settings(str(ROOT / 'examples/agent_service/service-config.local.json'))) as source:
        original_session = await source.get_session(user, capture['agentId'], capture['sessionId'])
        original_agent = await source.get_agent(user, capture['agentId'])
        config = (original_session.config.chat_model_config or original_agent.data.defaults.chat_model_config).model_copy(deep=True)
        # Read-only data access uses the existing configured provider, no mock LLM.
        print(json.dumps({'model': config.model, 'captureRef': capture['captureRef'], 'rows': len(data['rows'])}), flush=True)

        class CaseStorage(AsyncSQLAlchemyStorage):
            async def get_credential(self, user_id, credential_id):
                return await source.get_credential(user_id, credential_id)

        async def extra_tools(*args, **kwargs):
            return [ReadBudgetSnapshot(), CheckBudgetScope()]

        storage = CaseStorage('sqlite+aiosqlite:///' + (directory / 'case.sqlite3').as_posix())
        app = create_app(storage=storage, message_bus=InMemoryMessageBus(),
            workspace_manager=LocalWorkspaceManager(str(directory / 'workspaces')),
            extra_agent_tools=extra_tools, enable_index_worker=False, enable_scheduler=False)
        observed = []
        original_publish = _chat.publish_session_event

        async def observe(bus, session_id, event, **kwargs):
            cursor = await original_publish(bus, session_id, event, **kwargs)
            observed.append(deepcopy(event))
            return cursor

        async with app.router.lifespan_context(app):
            agent = AgentRecord(user_id=user, data=AgentData(name='阶段摘要真实案例回归', system_prompt=
                '请使用中文完成只读回归任务。先调用 ReadBudgetSnapshot，再调用 CheckBudgetScope，'
                '随后根据两次真实结果给出最终答复。仅使用这两个工具，不使用任何其他工具。'
                '说明返回范围和待确认事项，不推测业务原因。'))
            agent.data.chat_config.react_config.max_iters = 6
            await storage.upsert_agent(user, agent)
            session = SessionRecord(user_id=user, agent_id=agent.id, config=SessionConfig(
                workspace_id='progress-real-case', name='真实预算快照回归', naming=SessionNaming(auto=False),
                chat_model_config=config))
            session.state.permission_context.mode = PermissionMode.BYPASS
            session = await storage.upsert_session(user, agent.id, session.config,
                                                   state=session.state, session_id=session.id)
            async with asyncio.timeout(180):
                with patch.object(_chat, 'publish_session_event', observe):
                    await app.state.chat_service.run(user_id=user, agent_id=agent.id, session_id=session.id,
                        input_msg=UserMsg('user', '请读取保存的2026年净利润按业务板块预算查询，核对返回的汇总行数量以及是否缺少分组名称，并说明还有哪些口径需要确认。先读取再核查。'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://regression') as client:
                response = await client.get(f'/sessions/{session.id}/messages',
                    params={'agent_id': agent.id}, headers={'X-User-ID': user})
                response.raise_for_status()
                history = response.json()
            messages = history['messages']
            final = next(message for message in reversed(messages) if message['role'] == 'assistant')
            assert final['finished_reason'] == 'completed', final.get('error')
            progress = final['metadata']['agentProgress']
            custom = [event for event in observed if event.get('name') == 'agent_progress']
            assert final['finished_reason'] == 'completed', final.get('error')
            assert calls == ['ReadBudgetSnapshot', 'CheckBudgetScope'], calls
            assert custom[-1]['value'] == progress
            texts = '\n'.join(step['summary'] for step in progress['steps'])
            assert '42' in texts and '5' in texts and '7030100.00' in texts
            assert '1 条缺少业务板块名称' in texts
            for step in progress['steps']:
                for fact in step['facts']:
                    assert fact['sourceId'] == capture['captureRef']
            report = {'caseType': 'real historical query snapshot with live configured model',
                'captureRef': capture['captureRef'], 'captureSha256': row[0], 'capturedAt': capture['capturedAt'],
                'model': config.model, 'toolCalls': calls, 'progressEvents': len(custom),
                'checks': {'valuesAndUnits': True, 'sourceReferences': True, 'historyMatchesStream': True,
                           'finishedNormally': True, 'missingGroupIdentified': True},
                'message': final, 'events': observed}
            (directory / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            ui = ROOT / 'examples/web_ui/frontend/.cache'
            ui.mkdir(exist_ok=True)
            (ui / 'progress-real-case.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({key: value for key, value in report.items() if key not in ('events', 'message')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture-ref')
    parser.add_argument('--replay-events', help='Local-only replay of a previously recorded result.json; no model call')
    parser.add_argument('--output', default=str(ROOT / '.cache/progress-real-regression'))
    args = parser.parse_args()
    if not args.replay_events and not args.capture_ref:
        parser.error('Provide --capture-ref or --replay-events')
    asyncio.run(replay_local(args) if args.replay_events else run(args))
