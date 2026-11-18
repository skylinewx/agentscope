import { EventType, ReplyFinishedReason, type AgentEvent } from '@agentscope-ai/agentscope/event';
import { type TextBlock, AssistantMsg, UserMsg, type Msg } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useLayoutEffect } from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { setAuth, type AuthSession } from '@/api/auth-state';
import { ChatContent } from '@/components/chat/ChatContent';
import { TooltipProvider } from '@/components/ui/tooltip';
import { useMessages } from '@/hooks/useMessages';
import i18n from '@/i18n';
import '@/index.css';

const harness = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const at = '2026-10-26T06:13:58.122Z';
const user = UserMsg({ id: 'question', name: 'user', content: [
	{ type: 'text', id: 'question-text', text: '测试问题：查询预算', created_at: at, finished_at: at },
], created_at: at, finished_at: at });
const completed = AssistantMsg({ id: 'reply', name: '测试智能体', content: [
	{ type: 'text', id: 'answer', text: '查询已完成', created_at: at, finished_at: at },
], created_at: at, finished_at: at, finished_reason: 'completed' as Msg['finished_reason'] });
type Snapshot = { messages: Msg[]; is_running: boolean; event_cursor: string | null; has_more: boolean };
const snapshot = (messages = [user], running = false, cursor: string | null = 'saved-1'): Snapshot => ({
	messages, is_running: running, event_cursor: cursor, has_more: false,
});
let server = snapshot();
let historyReads = 0;
let historyFailures = 0;
let streamFailure = 0;
let posts = 0;
let releaseSend: (() => void) | null = null;
let delaySend = false;
let delayedHistory: (() => void) | null = null;
let delayHistory = false;
let shortHeartbeat = false;
let root: ReturnType<typeof createRoot> | null = null;
let state: ReturnType<typeof useMessages>;
const streams: { url: URL; signal: AbortSignal; send: (...events: object[]) => void; close: () => void; fail: () => void; heartbeat: () => void }[] = [];
const originalTimeout = window.setTimeout.bind(window);
window.setTimeout = ((fn: TimerHandler, ms?: number, ...args: unknown[]) =>
	originalTimeout(fn, shortHeartbeat && ms === 90_000 ? 150 : ms, ...args)) as typeof window.setTimeout;
const pause = (ms: number) => new Promise((resolve) => originalTimeout(resolve, ms));
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
const until = async (check: () => unknown, description: string) => {
	for (let i = 0; i < 400; i++) {
		if (check()) return;
		await pause(20);
	}
	throw new Error(`等待超时：${description}`);
};
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
	status, headers: { 'Content-Type': 'application/json' },
});

// Fail closed: no request from the fixture reaches a real user session.
window.fetch = async (input, init) => {
	const url = new URL(String(input), location.origin);
	const signal = init?.signal as AbortSignal;
	if (url.pathname === '/api/chat/') {
		posts++;
        if (delaySend) await new Promise<void>((resolve) => { releaseSend = resolve; });
		return json({ status: 'accepted', session_id: 'fixture' });
	}
	if (url.pathname.endsWith('/messages')) {
		historyReads++;
		if (historyFailures-- > 0) return json({ detail: 'Restarting' }, 503);
		const value = structuredClone(server);
		if (delayHistory) await new Promise<void>((resolve) => { delayedHistory = resolve; });
		return json(value);
	}
	if (url.pathname.endsWith('/stream')) {
		if (streamFailure) return json({ detail: 'Unavailable' }, streamFailure);
		let ended = false;
		const encoder = new TextEncoder();
		return new Response(new ReadableStream<Uint8Array>({
			start(controller) {
				const finish = (error?: Error) => {
					if (ended) return;
					ended = true;
					if (error) controller.error(error); else controller.close();
				};
				signal.addEventListener('abort', () => finish(signal.reason), { once: true });
				streams.push({
					url, signal,
					send: (...events) => controller.enqueue(encoder.encode(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''))),
					heartbeat: () => controller.enqueue(encoder.encode(':\n\n')),
					close: () => finish(),
					fail: () => finish(new TypeError('Network connection lost')),
				});
			},
		}), { headers: { 'Content-Type': 'text/event-stream' } });
	}
	throw new Error(`Unexpected fixture request: ${url.pathname}`);
};

export function Probe({ sessionId }: { sessionId: string }) {
	const current = useMessages('fixture-agent', sessionId);
	useLayoutEffect(() => { state = current; });
	return <QueryClientProvider client={queryClient}><TooltipProvider>
		<output data-testid="hook-state">{current.phase} / {current.connectionNotice ?? 'connected'}</output>
		<ChatContent msgs={current.msgs} loading={current.loading} phase={current.phase}
			historyError={current.historyError} connectionNotice={current.connectionNotice}
			onReloadHistory={current.reloadHistory} disabled={false} onSend={current.send}
			onUserConfirm={current.onUserConfirm} onInterrupt={current.interrupt}
			allowedInputTypes={[]} fileProcessor={async () => null}
			agentId={null} sessionId={sessionId} cwd={null} onCwdChange={() => {}} />
	</TooltipProvider></QueryClientProvider>;
}
function render(sessionId = 'fixture') {
	flushSync(() => root!.render(<Probe sessionId={sessionId} />));
}
function reset(value = snapshot()) {
	if (root) flushSync(() => root!.unmount());
	root = createRoot(harness);
	server = structuredClone(value);
	historyReads = 0; historyFailures = 0; streamFailure = 0; posts = 0;
	delayedHistory = null; delayHistory = false; shortHeartbeat = false; streams.length = 0; delaySend = false; releaseSend = null;
}
const start = (): AgentEvent => ({ type: EventType.REPLY_START, id: crypto.randomUUID(), created_at: at, session_id: 'fixture', reply_id: 'reply', name: '测试智能体', role: 'assistant' });
const textStart = (): AgentEvent => ({ type: EventType.TEXT_BLOCK_START, id: crypto.randomUUID(), created_at: at, reply_id: 'reply', block_id: 'answer' });
const delta = (text: string): AgentEvent => ({ type: EventType.TEXT_BLOCK_DELTA, id: crypto.randomUUID(), created_at: at, reply_id: 'reply', block_id: 'answer', delta: text });
const end = (): AgentEvent => ({ type: EventType.REPLY_END, id: crypto.randomUUID(), created_at: at, session_id: 'fixture', reply_id: 'reply', finished_reason: ReplyFinishedReason.COMPLETED, metadata: {} });
const mounted = async (value = snapshot()) => {
	reset(value); render(); await until(() => streams.length === 1 && !state.loading, '初次连接');
};
const live = async () => {
	streams[0].send(start(), textStart(), delta('片段'));
	await until(() => state.msgs.length === 2 && state.phase === 'streaming', '收到回复片段');
};

const tests: [string, () => Promise<void>][] = [
    ['刷新恢复运行中的摘要，使用快照游标续接并忽略结束后迟到的摘要', async () => {
        const progress = {version: 1, replyId: 'reply', revision: 3, steps: [{
            id: 'stage-1', sequence: 1, status: 'running',
            summary: '已读取本次返回的5个分组，金额单位为元。',
            nextAction: '接下来检查业务板块名称缺失的记录。', facts: [],
        }]};
        const partial = AssistantMsg({id: 'reply', name: '测试智能体', created_at: at,
            content: [{type: 'text', id: 'answer', text: '已经保存', created_at: at}],
            metadata: {agentProgress: progress}});
        await mounted(snapshot([user, partial], true, 'in-flight-1024'));
        assert(harness.textContent?.includes('已读取本次返回的5个分组'), '快照摘要没有显示');
        assert(streams[0].url.searchParams.get('after') === 'in-flight-1024', '没有使用快照游标');
        streams[0].send(delta('，后续结果'), end());
        await until(() => state.phase === 'idle', '恢复后的回复结束');
        const text = state.msgs[1].content.filter(b => b.type === 'text').map(b => b.text).join('');
        assert(text === '已经保存，后续结果', '没有 REPLY_START 时内容丢失或重复');
        streams[0].send({type: EventType.CUSTOM, id: crypto.randomUUID(), created_at: at,
            name: 'agent_progress', value: {...progress, revision: 99}});
        await pause(100);
        assert(state.phase === 'idle', '迟到摘要恢复了运行状态');
    }],
    ['发送后立即显示进度，准备通知不等待模型且迟到的确认不覆盖新阶段', async () => {
        await mounted(snapshot([], false));
        delaySend = true;
        const sending = state.send([{ type: 'text', id: 'new-question', text: '查询金额', created_at: new Date().toISOString() } as TextBlock]);
        await until(() => harness.textContent?.includes('正在发送'), '即时发送反馈');
        assert(state.msgs.length === 1 && state.msgs[0].role === 'user', '为了显示进度错误创建了助手答案');
        streams[0].send({type: EventType.CUSTOM, id: crypto.randomUUID(), name: 'execution_progress',
            value: {messageId: state.msgs[0].id, stage: 'TOOLS', runId: 'fixture-run'}, created_at: at});
        await until(() => harness.textContent?.includes('正在加载工具'), '工具准备反馈');
        releaseSend!(); await sending;
        assert(harness.textContent?.includes('正在加载工具'), '迟到的发送确认覆盖了准备阶段');
        streams[0].send(start(), textStart(), delta('结果'), end());
        await until(() => state.phase === 'idle', '执行结束');
        assert(!harness.querySelector('[data-testid=execution-progress]'), '结束后残留进度');
    }],
    ['刷新后正在准备的会话仍有进度，空闲会话不误显示', async () => {
        await mounted(snapshot([user], true));
        assert(harness.querySelector('[data-testid=execution-progress]'), '刷新后没有准备状态');
        await mounted(snapshot([user], false));
        assert(!harness.querySelector('[data-testid=execution-progress]'), '空闲会话误显示准备状态');
    }],
	['首次订阅失败也会恢复，且不会重发提问', async () => {
		reset(snapshot([user], true)); streamFailure = 503; render();
		await until(() => state.connectionNotice === 'reconnecting', '首次订阅失败');
		streamFailure = 0;
		await until(() => streams.length === 1 && state.connectionNotice === null, '首次订阅恢复');
		assert(state.phase === 'streaming' && posts === 0, '任务状态或重发行为错误');
	}],
	['网络异常后恢复空闲状态，清除失效的思考气泡并提示重新发送', async () => {
		await mounted(snapshot([user], true)); await live();
		server = snapshot(); streams[0].fail();
		await until(() => state.connectionNotice === 'reconnecting', '断线提示');
		assert(harness.textContent?.includes('连接已断开'), '断线必须可见');
		assert(!harness.querySelector('[aria-label="正在运行"]'), '不能继续显示运行图标');
		await until(() => streams.length === 2 && state.connectionNotice === 'interrupted', '中断恢复');
		assert(state.phase === 'idle' && state.msgs.length === 1, '旧回复或运行状态残留');
		assert(posts === 0, '不能自动重发问题');
	}],
	['正常 EOF 也会重连，读取断线期间完成的回复及新游标', async () => {
		await mounted(snapshot([user], true)); await live();
		server = snapshot([user, completed], false, 'saved-2'); streams[0].close();
		await until(() => streams.length === 2 && state.connectionNotice === null, 'EOF 恢复');
		assert(state.phase === 'idle' && state.msgs.length === 2, '未恢复完整回答');
		assert(streams[1].url.searchParams.get('after') === 'saved-2', '使用了旧游标');
		assert(state.msgs[1].content[0].type === 'text' && state.msgs[1].content[0].text === '查询已完成', '最终答案不正确');
	}],
	['仍在运行时从快照重放，不重复拼接已收到的文本', async () => {
		await mounted(snapshot([user], true)); await live(); streams[0].fail();
		await until(() => streams.length === 2, '运行任务重连');
		streams[1].send(start(), textStart(), delta('片段'), delta('后续'), end());
		await until(() => state.phase === 'idle' && state.msgs.length === 2, '重放完成');
		assert(state.msgs[1].content[0].type === 'text' && state.msgs[1].content[0].text === '片段后续', '出现重复增量');
		assert(posts === 0, '重连执行了新请求');
	}],
	['恢复后保留工具确认，并让没有新 REPLY_START 的续传写入原回复', async () => {
		const parked = AssistantMsg({ id: 'reply', name: '测试智能体', content: [
			{ type: 'tool_call', id: 'call', name: 'Read', input: '{}', state: 'asking', created_at: at },
		], created_at: at });
		await mounted(snapshot([user, parked])); streams[0].fail();
		await until(() => streams.length === 2 && state.connectionNotice === null, '恢复工具确认');
		assert(state.phase === 'streaming' && state.msgs.length === 2, '等待确认不能被判定为中断');
		streams[1].send(textStart(), delta('确认后继续'), end());
		await until(() => state.phase === 'idle', '确认后续传');
		assert(state.msgs[1].content.some((b) => b.type === 'text' && b.text === '确认后继续'), '续传目标丢失');
	}],
	['服务重启期间历史读取失败会继续重试，并保留已显示的记录', async () => {
		await mounted(snapshot([user], true)); await live();
		historyFailures = 1; server = snapshot([user, completed]); streams[0].fail();
		await until(() => historyReads === 2, '第一次恢复失败');
		assert(state.msgs.length === 2 && state.connectionNotice === 'reconnecting', '失败读取清空了会话');
		await until(() => streams.length === 2 && state.connectionNotice === null, '服务恢复');
		assert(state.phase === 'idle', '恢复后仍然忙碌');
	}],
	['无事件但有心跳的长任务不超时，心跳停止后自动恢复', async () => {
		reset(snapshot([user], true)); shortHeartbeat = true; render();
		await until(() => streams.length === 1, '心跳测试连接');
		for (let i = 0; i < 6; i++) { streams[0].heartbeat(); await pause(50); }
		assert(streams.length === 1 && state.connectionNotice === null, '正常心跳被当作超时');
		server = snapshot(); shortHeartbeat = false;
		await until(() => state.connectionNotice === 'reconnecting', '静默连接超时');
		await until(() => streams.length === 2 && state.phase === 'idle', '超时后恢复');
	}],
	['403/404 会停止自动重试并显示明确错误', async () => {
		await mounted(); streamFailure = 404; streams[0].fail();
		await until(() => state.connectionNotice === 'failed', '终止错误');
		const reads = historyReads; await pause(1200);
		assert(historyReads === reads, '永久错误仍在重试');
		assert(harness.textContent?.includes('无法连接此会话'), '未显示终止错误');
	}],
	['切换会话会取消退避重试', async () => {
		await mounted(snapshot([user], true)); streams[0].fail();
		await until(() => state.connectionNotice === 'reconnecting', '进入退避');
		server = snapshot([user, completed]); render('another');
		await until(() => streams.length === 2 && state.phase === 'idle', '新会话');
		const reads = historyReads; await pause(1200);
		assert(historyReads === reads && streams.length === 2, '旧会话重试未取消');
		assert(streams[0].signal.aborted, '旧订阅未取消');
	}],
	['切换会话后，迟到的历史响应不能覆盖新会话', async () => {
		reset(snapshot([user], true)); delayHistory = true; render();
		await until(() => delayedHistory !== null, '挂起历史读取');
		const release = delayedHistory!;
		delayHistory = false; server = snapshot([user, completed]); render('another');
		await until(() => streams.length === 1 && state.msgs.length === 2, '新会话读取');
		release(); await pause(100);
		assert(state.msgs.length === 2 && state.phase === 'idle' && streams.length === 1, '旧响应污染新会话');
	}],
	['注销会取消正在等待的重连，不跨账号恢复', async () => {
		const auth = { user: { id: 'fixture-user' }, csrf_token: 'fixture-only' } as AuthSession;
		setAuth(auth); await mounted(); streams[0].fail();
		await until(() => state.connectionNotice === 'reconnecting', '注销前退避');
		setAuth(null); const reads = historyReads; await pause(1200);
		assert(historyReads === reads && streams.length === 1, '注销后仍请求旧会话');
	}],
];

run.addEventListener('click', async () => {
	run.disabled = true; results.replaceChildren(); summary.textContent = '测试中';
	await i18n.changeLanguage('zh');
	let passed = 0;
	for (const [name, test] of tests) {
		const row = document.createElement('li');
		try { await test(); row.textContent = `通过：${name}`; passed++; }
		catch (error) { row.textContent = `失败：${name} — ${String(error)}`; row.className = 'text-destructive'; }
		finally { if (root) flushSync(() => root!.unmount()); root = null; }
		results.append(row);
	}
	summary.textContent = `${passed}/${tests.length} 通过`; run.disabled = false;
	// Leave the recovered interruption visible as a reviewable example.
	reset(snapshot()); render();
});
