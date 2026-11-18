import { AssistantMsg, UserMsg, type Msg } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useLayoutEffect } from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { Toaster } from 'sonner';

import type { MessageQueueState, QueuedMessage } from '@/api/message-queue';
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
const at = new Date().toISOString();
const pause = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
const assert = (value: unknown, text: string) => { if (!value) throw new Error(text); };
const until = async (check: () => unknown) => { for (let n = 0; n < 200; n++) { if (check()) return; await pause(); } throw new Error('等待超时'); };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const makeMessage = (text: string) => UserMsg({ id: crypto.randomUUID(), name: 'user', content: [{ type: 'text', id: crypto.randomUUID(), text, created_at: at }], created_at: at });
const makeItem = (text: string, sequence: number): QueuedMessage => ({ message: makeMessage(text), sequence, version: 0, enqueued_at: at });
let queue: MessageQueueState;
let history: Msg[] = [];
let deleted: QueuedMessage[] = [];
let posts: string[] = [];
let failure: 'before' | 'after' | null = null;
let stopCount = 0;
let delay = 0;
let legacy = false;
let legacyPosts = 0;
let directPosts: string[] = [];
let serverBusy = false;
let state: ReturnType<typeof useMessages>;
let root: ReturnType<typeof createRoot> | null = null;
let scopeId = 'queue-fixture';
const sockets = new Set<ReadableStreamDefaultController<Uint8Array>>();
const emit = (name: string, value: object) => {
	const event = { type: 'CUSTOM', id: crypto.randomUUID(), created_at: at, name, value };
	for (const socket of sockets) socket.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
};

// No fixture request is allowed to reach a real session/model.
window.fetch = async (input, init) => {
	const url = new URL(String(input), location.origin);
	if (url.pathname === '/api/chat/') {
		if (legacy) { legacyPosts++; return json({ status: 'started', session_id: scopeId }); }
		const message = JSON.parse(String(init!.body)).input as Msg;
		directPosts.push(message.id);
		if (delay) await pause(delay);
		if (failure === 'before') { failure = null; return json({ detail: '测试提交失败' }, 503); }
		if (history.some((item) => item.id === message.id)) return json({ status: 'started', session_id: scopeId });
		if (serverBusy || queue.activity !== 'idle' || queue.items.length) {
			queue.items.push({ message, sequence: queue.items.length, version: 0, enqueued_at: at });
			queue.activity = 'running'; queue.revision++;
			return json({ status: 'queued', session_id: scopeId });
		}
		history.push(message); queue.active_id = message.id; queue.activity = 'running'; queue.revision++;
		emit('queued_message_started', { message }); emit('message_queue_updated', {});
		if (failure === 'after') { failure = null; throw new TypeError('模拟回执丢失'); }
		return json({ status: 'started', session_id: scopeId });
	}
	if (url.pathname.endsWith('/messages')) return json({ messages: history, is_running: queue.activity !== 'idle', has_more: false, event_cursor: null });
	if (url.pathname.endsWith('/stream')) return new Response(new ReadableStream<Uint8Array>({ start(controller) {
		sockets.add(controller);
		init?.signal?.addEventListener('abort', () => { sockets.delete(controller); try { controller.close(); } catch {} }, { once: true });
	} }), { headers: { 'Content-Type': 'text/event-stream' } });
	if (url.pathname.endsWith('/interrupt')) { stopCount++; queue.paused = true; queue.pause_reason = 'stopped'; queue.revision++; emit('message_queue_updated', {}); return json({ session_id: scopeId }); }
	if (url.pathname.endsWith('/queue')) {
		if (legacy) return json({ detail: 'Not Found' }, 404);
		if (init?.method === 'POST') {
			const message = JSON.parse(String(init.body)).message as Msg;
			posts.push(message.id);
			if (delay) await pause(delay);
			if (failure === 'before') { failure = null; return json({ detail: '测试提交失败' }, 503); }
			if (queue.activity === 'idle' && !queue.items.length && !queue.paused && !history.some((x) => x.id === message.id)) {
				history.push(message); queue.activity = 'running'; queue.active_id = message.id; queue.revision++;
				emit('queued_message_started', { message });
				return json(queue);
			}
			if (!queue.items.some((x) => x.message.id === message.id) && !history.some((x) => x.id === message.id)) queue.items.push({ message, sequence: queue.items.length, version: 0, enqueued_at: at });
			queue.revision++;
			if (failure === 'after') { failure = null; throw new TypeError('模拟回执丢失'); }
		}
		return json(queue);
	}
	if (url.pathname.endsWith('/queue/actions')) {
		const body = JSON.parse(String(init!.body));
		const item = queue.items.find((x) => x.message.id === body.item_id);
		if (body.operation === 'pause' || body.operation === 'resume') { queue.paused = body.operation === 'pause'; queue.pause_reason = queue.paused ? 'manual' : null; }
		else if (body.operation === 'restore') { const restored = deleted.find((x) => x.message.id === body.item_id)!; queue.items.push(restored); queue.items.sort((a, b) => a.sequence - b.sequence); }
		else {
			if (!item || item.version !== body.version) return json({ detail: '消息已变化' }, 409);
			if (body.operation === 'begin_edit') { item.version++; queue.editing_id = item.message.id; }
			if (body.operation === 'save') { item.message = body.message; item.version++; queue.editing_id = null; }
			if (body.operation === 'cancel_edit') queue.editing_id = null;
			if (body.operation === 'delete') { deleted.push(item); queue.items = queue.items.filter((x) => x !== item); if (queue.editing_id === item.message.id) queue.editing_id = null; }
		}
		queue.revision++;
		return json(queue);
	}
	throw new Error(`Unexpected fixture request: ${url.pathname}`);
};

function Probe() {
	const current = useMessages('fixture-agent', scopeId);
	useLayoutEffect(() => { state = current; });
	return <QueryClientProvider client={queryClient}><TooltipProvider><section aria-label="聊天预览" className="h-[600px] rounded-2xl border bg-background p-4 text-foreground">
		<ChatContent msgs={current.msgs} loading={current.loading} phase={current.phase}
			historyError={current.historyError} connectionNotice={current.connectionNotice}
			onReloadHistory={current.reloadHistory} disabled={false} onSend={current.send}
			messageQueue={current.messageQueue} onUserConfirm={current.onUserConfirm} onInterrupt={current.interrupt}
			allowedInputTypes={['application/pdf']}
			fileProcessor={async (file) => ({ id: crypto.randomUUID(), type: 'data', name: file.name, source: { type: 'base64', media_type: file.type, data: 'Zml4dHVyZQ==' }, created_at: at })}
			agentId="fixture-agent" sessionId={scopeId} cwd={null} onCwdChange={() => {}} footerSlot={null} />
	</section><Toaster /></TooltipProvider></QueryClientProvider>;
}

function reset() {
	if (root) flushSync(() => root!.unmount());
	scopeId = crypto.randomUUID();
	queue = { revision: 1, items: [], active_id: 'current', activity: 'running', waiting_reason: null, paused: false, pause_reason: null, editing_id: null };
	history = []; deleted = []; posts = []; failure = null; stopCount = 0; delay = 0;
	legacy = false; legacyPosts = 0;
	directPosts = []; serverBusy = false;
	root = createRoot(harness);
}
async function mount(items: string[] = [], olderBackend = false, messages: Msg[] = [], idle = false) {
	reset(); queue.items = items.map(makeItem);
	history = messages;
	legacy = olderBackend; if (legacy) queue.activity = 'idle';
	if (idle) { queue.activity = 'idle'; queue.active_id = null; }
	flushSync(() => root!.render(<Probe />));
	await until(() => (olderBackend ? !!state.messageQueue.error : state.messageQueue.state?.revision === queue.revision) && !state.loading);
}
const input = () => harness.querySelector<HTMLTextAreaElement>('textarea[placeholder]')!;
function fill(node: HTMLTextAreaElement, text: string) {
	Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(node, text);
	flushSync(() => node.dispatchEvent(new Event('input', { bubbles: true })));
}
function enter(options: KeyboardEventInit = {}) {
	const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...options });
	flushSync(() => input().dispatchEvent(event));
	return event;
}
const button = (label: string) => [...harness.querySelectorAll<HTMLButtonElement>('button')].find((x) => x.getAttribute('aria-label') === label || x.textContent === label)!;

const tests: [string, () => Promise<void>][] = [
	['空闲回车直接发送，消息立即进入聊天记录，全程不进入队列', async () => {
		await mount([], false, [], true); delay = 100; fill(input(), '空闲时直接发送'); enter(); enter();
		await until(() => state.msgs.length === 1);
		assert(queue.items.length === 0 && !harness.querySelector('[aria-label="消息队列"]'), '空闲消息进入了队列');
		await until(() => input().value === '');
		assert(directPosts.length === 1 && posts.length === 0 && state.msgs.length === 1, '没有直接发送或产生重复');
		fill(input(), '执行中的下一条'); enter(); await until(() => input().value === '');
		assert(posts.length === 1 && state.msgs.length === 1 && queue.items.length === 1, '执行中没有切回排队');
	}],
	['空闲直发失败保留草稿，重试仍使用同一消息 ID', async () => {
		await mount([], false, [], true); failure = 'before'; fill(input(), '直发失败重试'); enter();
		await until(() => directPosts.length === 1); await pause(100);
		assert(input().value === '直发失败重试' && state.msgs.length === 0 && state.phase === 'idle', '失败回滚错误');
		enter(); await until(() => input().value === '');
		assert(directPosts.length === 2 && directPosts[0] === directPosts[1] && history.length === 1, '直发重试重复执行');
	}],
	['直发成功但回执丢失时保留已确认的消息，重试不重复入队', async () => {
		await mount([], false, [], true); failure = 'after'; fill(input(), '直发回执丢失'); enter();
		await until(() => directPosts.length === 1 && state.msgs.length === 1); await pause(100);
		assert(input().value === '直发回执丢失' && state.phase === 'streaming', '丢失已确认的运行消息');
		enter(); await until(() => input().value === '');
		assert(history.length === 1 && queue.items.length === 0 && posts[0] === directPosts[0], '重试产生重复');
	}],
	['空闲状态已过时时，服务器转入队列且移除临时聊天气泡', async () => {
		await mount([], false, [], true); serverBusy = true; fill(input(), '另一窗口已抢先运行'); enter();
		await until(() => input().value === '' && state.messageQueue.state?.items.length === 1);
		assert(state.msgs.length === 0 && directPosts.length === 1, '待处理消息残留在聊天记录');
	}],
	['有待发积压时新消息保持排队，不抢先直接发送', async () => {
		await mount(['原来的待发消息'], false, [], true); fill(input(), '排在后面'); enter();
		await until(() => input().value === '');
		assert(directPosts.length === 0 && queue.items.length === 2 && state.msgs.length === 0, '新消息抢占了待发队列');
	}],
	['页面仍显示运行中但服务器已经空闲时，提交仍立即启动', async () => {
		await mount(); queue.activity = 'idle'; queue.active_id = null;
		fill(input(), '服务器已空闲'); enter();
		await until(() => input().value === '' && state.msgs.length === 1);
		assert(queue.items.length === 0 && !harness.querySelector('[aria-label="消息队列"]'), '旧状态导致不必要的排队');
	}],
	['完整聊天页反复更新时，同一条待执行消息只显示一个队列面板', async () => {
		await mount(['唯一的待执行消息'], false, [makeMessage('当前正在执行的问题')]);
		for (let n = 0; n < 5; n++) {
			queue.revision++; emit('message_queue_updated', {});
			await until(() => state.messageQueue.state?.revision === queue.revision);
			assert(harness.querySelectorAll('[aria-label="消息队列"]').length === 1, '出现重复队列面板');
			assert(harness.querySelectorAll('[data-queue-id]').length === 1, '同一消息重复显示');
		}
	}],
	['一条消息开始执行后，只在聊天记录中显示，队列自动消失', async () => {
		await mount(); fill(input(), '只发送这一条'); enter();
		await until(() => state.messageQueue.state?.items.length === 1 && input().value === '');
		const message = queue.items.shift()!.message;
		queue.active_id = message.id; queue.activity = 'running'; queue.revision++; history = [message];
		emit('queued_message_started', { message }); emit('message_queue_updated', {});
		await until(() => state.msgs.length === 1 && state.messageQueue.state?.items.length === 0);
		assert(posts.length === 1 && state.msgs[0].id === message.id, '单次发送的消息数量错误');
		assert(harness.querySelectorAll('[data-queue-id]').length === 0, '运行中的消息残留在队列');
		assert(!harness.querySelector('[aria-label="消息队列"]'), '空队列面板没有消失');
	}],
	['后端更新前仍可普通发送，队列接口缺失不会破坏现有聊天', async () => {
		await mount([], true); fill(input(), '兼容普通发送'); enter();
		await until(() => legacyPosts === 1 && input().value === '' && state.msgs.length === 1);
		assert(state.msgs.length === 1 && posts.length === 0, '未使用旧版发送接口');
	}],
	['执行中回车入队，保留停止按钮，不提前进入聊天记录', async () => {
		await mount(); fill(input(), '下一条问题'); enter();
		await until(() => state.messageQueue.state?.items.length === 1 && input().value === '');
		assert(state.msgs.length === 0 && !!button(i18n.t('textInput.stop')), '待处理消息或停止按钮错误');
	}],
	['快速连续回车只提交一次', async () => {
		await mount(); delay = 100; fill(input(), '单次提交'); enter(); enter();
		await until(() => input().value === ''); assert(posts.length === 1, '出现重复提交');
	}],
	['Shift+Enter 与中文输入法确认不触发入队', async () => {
		await mount(); fill(input(), '换行测试');
		assert(!enter({ shiftKey: true }).defaultPrevented, 'Shift+Enter 被阻止');
		assert(!enter({ isComposing: true }).defaultPrevented, '输入法确认被阻止');
		assert(posts.length === 0, '意外提交');
	}],
	['提交失败保留草稿，回执丢失重试复用同一消息 ID', async () => {
		await mount(); failure = 'after'; fill(input(), '网络重试'); enter();
		await until(() => posts.length === 1); await pause(100);
		assert(input().value === '网络重试', '失败清空了草稿'); enter();
		await until(() => posts.length === 2 && input().value === '');
		assert(posts[0] === posts[1] && queue.items.length === 1, '重试产生重复消息');
	}],
	['附件随消息入队，失败后保留附件', async () => {
		await mount(); const fileInput = harness.querySelector<HTMLInputElement>('input[type=file]')!;
		const transfer = new DataTransfer(); transfer.items.add(new File(['fixture'], 'fixture.pdf', { type: 'application/pdf' }));
		fileInput.files = transfer.files; flushSync(() => fileInput.dispatchEvent(new Event('change', { bubbles: true })));
		await until(() => harness.textContent?.includes('PDF'));
		failure = 'before'; fill(input(), '附件提问'); enter(); await pause(100);
		assert(harness.textContent?.includes('fixture.pdf'), '失败清除了附件'); enter();
		await until(() => input().value === ''); assert(queue.items[0].message.content.some((x) => x.type === 'data'), '未包含附件');
	}],
	['队列紧凑，默认三条，编辑删除均仅图标且有可访问名称', async () => {
		await mount(['消息一', '消息二', '消息三', '消息四']);
		assert(harness.querySelectorAll('[data-queue-id]').length === 3, '默认条数错误');
		const edit = button('编辑消息'); const remove = button('删除消息');
		assert(edit && remove && !edit.textContent && !remove.textContent, '不是图标按钮');
		assert(harness.querySelector('[data-queue-id]')!.getBoundingClientRect().height <= 48, '行高不够紧凑');
		flushSync(() => button('展开其余 1 条').click()); assert(harness.querySelectorAll('[data-queue-id]').length === 4, '未展开');
	}],
	['编辑在原位保存，保留原入队时间', async () => {
		await mount(['原始内容', '后一条']); const original = queue.items[0]; button('编辑消息').click();
		await until(() => !!harness.querySelector('textarea[aria-label="编辑消息"]'));
		fill(harness.querySelector('textarea[aria-label="编辑消息"]')!, '修改内容'); button('保存').click();
		await until(() => !queue.editing_id && queue.items[0].message.content[0].type === 'text' && queue.items[0].message.content[0].text === '修改内容');
		assert(queue.items[0].message.id === original.message.id && queue.items[0].enqueued_at === original.enqueued_at, '位置或时间变化');
	}],
	['删除可以撤销，恢复原来的顺序', async () => {
		await mount(['删除再恢复', '后一条']); const id = queue.items[0].message.id; button('删除消息').click();
		await until(() => queue.items.length === 1);
		await until(() => [...document.querySelectorAll('button')].some((x) => x.textContent === '撤销'));
		[...document.querySelectorAll<HTMLButtonElement>('button')].find((x) => x.textContent === '撤销')!.click();
		await until(() => queue.items.length === 2); assert(queue.items[0].message.id === id, '撤销顺序错误');
	}],
	['其他窗口更新后保留编辑草稿，取消不会解除其他窗口的编辑锁', async () => {
		await mount(['原始内容']); button('编辑消息').click();
		await until(() => !!harness.querySelector('textarea[aria-label="编辑消息"]'));
		fill(harness.querySelector('textarea[aria-label="编辑消息"]')!, '本地未保存草稿');
		queue.items[0].version++; queue.revision++; emit('message_queue_updated', {});
		await until(() => button('保存').disabled);
		assert(harness.querySelector<HTMLTextAreaElement>('textarea[aria-label="编辑消息"]')?.value === '本地未保存草稿', '草稿被覆盖');
		button('取消').click(); await until(() => !harness.querySelector('textarea[aria-label="编辑消息"]'));
		assert(!!queue.editing_id, '错误清除了其他窗口的编辑锁');
	}],
	['暂停/继续与停止当前任务各自生效', async () => {
		await mount(['等待中']); button('暂停').click(); await until(() => queue.paused); assert(stopCount === 0, '暂停中断了当前任务');
		await until(() => !!button('继续')); button('继续').click(); await until(() => !queue.paused);
		button(i18n.t('textInput.stop')).click(); await until(() => stopCount === 1); assert(queue.paused, '停止没有暂停队列');
	}],
	['重新挂载后恢复队列；开始执行事件只插入一次用户消息', async () => {
		await mount(['持久待发送']); flushSync(() => root!.unmount()); root = createRoot(harness); flushSync(() => root!.render(<Probe />));
		await until(() => state.messageQueue.state?.items.length === 1);
		const message = queue.items.shift()!.message; history = [message]; queue.revision++;
		emit('queued_message_started', { message }); emit('queued_message_started', { message }); emit('message_queue_updated', {});
		await until(() => state.msgs.length === 1 && state.messageQueue.state?.items.length === 0);
		assert(state.msgs[0].id === message.id, '消息未进入历史');
	}],
];

async function preview() { await mount(['继续分析亏损单位的主要原因'], false, [makeMessage('查询全公司所有单位的净利润预算'), AssistantMsg({ name: '测试智能体', content: [], created_at: at })]); }
run.addEventListener('click', async () => {
	run.disabled = true; results.innerHTML = ''; summary.textContent = '正在运行…';
	let passed = 0;
	for (const [name, test] of tests) {
		const row = document.createElement('li');
		try { await test(); row.textContent = `通过：${name}`; passed++; } catch (error) { row.textContent = `失败：${name} — ${error}`; }
		results.append(row);
	}
	summary.textContent = `${passed}/${tests.length} 通过`; run.disabled = false; await preview();
});
document.querySelector('#narrow')!.addEventListener('click', () => { harness.style.width = harness.style.width ? '' : '360px'; });
document.querySelector('#idle')!.addEventListener('click', async () => {
	await mount([], false, [], true); fill(input(), '空闲时直接发送这条消息'); enter();
});
document.querySelector('#theme')!.addEventListener('click', () => document.documentElement.classList.toggle('dark'));
await i18n.changeLanguage('zh');
await preview();
