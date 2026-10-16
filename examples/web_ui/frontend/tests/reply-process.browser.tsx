import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { ASMessageBubble } from '@/components/chat/ASMessageBubble';
import { MessageScroller, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from '@/components/ui/message-scroller';
import { TooltipProvider } from '@/components/ui/tooltip';
import i18n from '@/i18n';
import '@/index.css';

const container = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
let root: ReturnType<typeof createRoot>;
const at = '2026-10-13T10:00:00Z';
const text = (id: string, value = id) => ({ type: 'text', id, text: value, created_at: at, finished_at: at } as ContentBlock);
const content: ContentBlock[] = [
	{ type: 'thinking', id: 'thought', thinking: '测试思考详情', created_at: at, finished_at: at },
	text('progress', '正在核对数据口径'),
	{ type: 'tool_call', id: 'call', name: 'Read', input: '{"path":"sample.txt"}', state: 'finished', created_at: at, finished_at: at },
	{ type: 'tool_result', id: 'call', name: 'Read', state: 'success', output: [text('output', '工具原始输出')], created_at: at, finished_at: at, metadata: {} },
	text('answer', '这是完整最终答案'), text('second', '第二段答案也需要保留'),
];
const msg = (extra: Partial<Msg> = {}): Msg => ({ id: 'reply', name: '测试智能体', role: 'assistant', metadata: {}, created_at: at,
	finished_at: '2026-10-13T10:00:20Z', finished_reason: 'completed' as Msg['finished_reason'], content, ...extra });
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
const visible = (selector: string) => {
	const element = container.querySelector<HTMLElement>(selector);
	return !!element?.getClientRects().length && getComputedStyle(element).display !== 'none';
};
const button = () => container.querySelector<HTMLButtonElement>('[data-testid="reply-process-summary"]')!;
const detailsButton = () => container.querySelector<HTMLButtonElement>('[data-testid="reply-process-details-summary"]')!;
const clickText = (value: string) => {
	const match = [...container.querySelectorAll<HTMLButtonElement>('button')].find((element) => element.textContent?.includes(value));
	assert(match, `Missing button: ${value}`); match!.click();
};
const until = async (predicate: () => unknown) => {
	for (let index = 0; index < 150; index++) {
		if (predicate()) return;
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
	throw new Error('等待界面更新超时');
};
function render(messages: Msg[], scope = 'one') {
	flushSync(() => root.render(<QueryClientProvider client={queryClient}><TooltipProvider>
		<MessageScrollerProvider autoScroll defaultScrollPosition="end">
			<div className="h-[360px]"><MessageScroller><MessageScrollerViewport><MessageScrollerContent>
				{messages.map((message) => <MessageScrollerItem key={`${scope}:${message.id}`} messageId={message.id}>
					<ASMessageBubble message={message} agentId={null} sessionId={null} onUserConfirm={() => {}} />
				</MessageScrollerItem>)}
			</MessageScrollerContent></MessageScrollerViewport></MessageScroller></div>
		</MessageScrollerProvider>
	</TooltipProvider></QueryClientProvider>));
}

const tests: [string, () => Promise<void>][] = [
	['历史回复默认收起，完整多段答案保留', async () => {
		render([msg()]);
		assert(button().getAttribute('aria-expanded') === 'false', 'History should be collapsed');
		assert(!visible('[data-testid="reply-process-content"]'), 'Process must be hidden');
		assert(container.textContent?.includes('第二段答案也需要保留'), 'Final paragraphs must remain');
		assert(button().textContent?.includes('1 次工具调用'), 'Calls/results must be counted once');
	}],
	['摘要展开后可查看步骤和原始工具详情', async () => {
		render([msg()]); button().click();
		await until(() => visible('[data-testid="reply-process-content"]'));
		assert(container.textContent?.includes('正在核对数据口径'), 'Progress should be visible');
		clickText('思考与工具详情');
		await until(() => container.textContent?.includes('读取 1 个文件'));
		clickText('收起过程');
		await until(() => !visible('[data-testid="reply-process-content"]'));
	}],
	['运行时过程和工具详情均默认展开，结束后两层自动收起', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Running process should be open');
		assert(detailsButton().getAttribute('aria-expanded') === 'true', 'Running details should be open');
		assert(visible('[data-testid="reply-process-details-content"]'), 'Running details must be visible');
		assert(container.textContent?.includes('读取 1 个文件'), 'Tool activity should be immediately visible');
		render([msg()]);
		await until(() => button().getAttribute('aria-expanded') === 'false');
		button().click();
		await until(() => detailsButton()?.getAttribute('aria-expanded') === 'false');
		assert(!visible('[data-testid="reply-process-details-content"]'), 'Completed details should be collapsed');
	}],
	['手动收起工具详情后，后续流式更新不会重新展开', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		detailsButton().click();
		await until(() => detailsButton().getAttribute('aria-expanded') === 'false');
		render([msg({ finished_at: null, finished_reason: null, content: [...content, text('new-progress')] })]);
		assert(detailsButton().getAttribute('aria-expanded') === 'false', 'Streaming must preserve explicit collapse');
	}],
	['用户主动查看工具详情时，完成后保留阅读状态', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		container.querySelector('[data-testid="reply-process-details-content"]')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		await new Promise((resolve) => setTimeout(resolve, 30));
		render([msg()]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Reading process must remain open');
		assert(detailsButton().getAttribute('aria-expanded') === 'true', 'Reading details must remain open');
	}],
	['用户手动展开后，流式更新和完成不打断阅读', async () => {
		render([msg({ finished_at: null, finished_reason: null })]); button().click();
		await until(() => button().getAttribute('aria-expanded') === 'false');
		button().click(); await until(() => button().getAttribute('aria-expanded') === 'true');
		render([msg()]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Explicit choice must survive completion');
	}],
	['过程中选中内容时保留展开状态', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		container.querySelector('[data-testid="reply-process-content"]')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		await new Promise((resolve) => setTimeout(resolve, 30));
		render([msg()]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Reading process must stay open');
	}],
	['失败提示、部分答案在收起状态仍可见', async () => {
		render([msg({ finished_reason: 'error' as Msg['finished_reason'], error: { type: 'connection', message: 'offline' } })]);
		assert(visible('[role="alert"]'), 'Failure alert must be outside process');
		assert(container.textContent?.includes('这是完整最终答案'), 'Partial output must remain');
	}],
	['停止提示在收起状态仍可见', async () => {
		render([msg({ finished_reason: 'interrupted' as Msg['finished_reason'] })]);
		assert(container.textContent?.includes('本轮已停止'), 'Interrupted state should be explicit');
	}],
	['确认操作不因收起过程而消失，收到结果后解除等待', async () => {
		const pending = [content[0], { ...content[2], state: 'asking' } as ContentBlock];
		render([msg({ content: pending, finished_at: null, finished_reason: null })]); button().click();
		await until(() => button().getAttribute('aria-expanded') === 'false');
		assert(container.textContent?.includes('请在输入框上方选择允许或拒绝'), 'Confirmation notice must remain');
		render([msg({ content: [...pending, content[3]], finished_at: null, finished_reason: null })]);
		assert(!container.textContent?.includes('请在输入框上方选择允许或拒绝'), 'Completed call must clear stale pending state');
	}],
	['普通直答无需摘要，工具结束但无答案时有明确提示', async () => {
		render([msg({ content: [text('plain', '直接回答')] })]);
		assert(!button(), 'Plain answer should have no summary');
		render([msg({ content: content.slice(0, 4), metadata: { presentation: { answer_block_ids: [] } } })]);
		assert(container.textContent?.includes('本轮未生成最终回答'), 'No-answer state must be visible');
	}],
	['报告链接不被折叠，切换会话后默认状态独立', async () => {
		const report = msg({ content: [text('card', '[报告入口](./reports/example.html)'), ...content], metadata: { presentation: { answer_block_ids: ['answer', 'second'] } } });
		render([report]);
		assert(container.textContent?.includes('报告入口'), 'Report link must remain outside process');
		button().click(); await until(() => button().getAttribute('aria-expanded') === 'true');
		render([report], 'different-session');
		assert(button().getAttribute('aria-expanded') === 'false', 'New scope should reset disclosure');
	}],
	['业务摘要仅使用成功回执，不重复统计报告重试', async () => {
		const report = { ...content[3], metadata: { process_summary: { kind: 'report', id: 'report', topicCount: 3 } } } as ContentBlock;
		render([msg({ content: [...content, report, { ...report, id: 'retry' }] })]);
		assert(button().textContent?.includes('覆盖 3 个主题，生成 1 份报告'), 'Business summary should use receipt metadata');
	}],
	['长过程收起后摘要保持在可见区域', async () => {
		const long = msg({ content: [content[0], ...Array.from({ length: 20 }, (_, i) => text(`p${i}`, `处理步骤 ${i + 1}`)), ...content.slice(2)] });
		render([long]); button().click();
		await until(() => visible('[data-testid="reply-process-content"]'));
		const viewport = container.querySelector<HTMLElement>('[data-slot="message-scroller-viewport"]')!;
		const close = [...container.querySelectorAll('button')].find((element) => element.textContent === '收起过程 ↑')!;
		viewport.scrollTop = close.offsetTop;
		close.click();
		await until(() => !visible('[data-testid="reply-process-content"]'));
		await new Promise((resolve) => setTimeout(resolve, 100));
		const top = button().getBoundingClientRect().top - viewport.getBoundingClientRect().top;
		assert(top >= -1 && top < viewport.clientHeight, `Summary lost after collapse: ${top}`);
	}],
];

run.addEventListener('click', async () => {
	run.disabled = true; results.replaceChildren(); summary.textContent = '测试中';
	if (root) flushSync(() => root.unmount());
	await i18n.changeLanguage('zh');
	let passed = 0;
	for (const [name, test] of tests) {
		root = createRoot(container);
		const row = document.createElement('li');
		try { await test(); row.textContent = `通过：${name}`; passed++; }
		catch (error) { row.textContent = `失败：${name} — ${String(error)}`; row.className = 'text-destructive'; }
		finally { flushSync(() => root.unmount()); }
		results.append(row);
	}
	summary.textContent = `${passed}/${tests.length} 通过`;
	run.disabled = false;
	root = createRoot(container); render([msg()]);
});

let previewStartedAt = at;
let previewScope = 0;
for (const running of [true, false]) {
	document.getElementById(running ? 'preview-running' : 'preview-completed')!.addEventListener('click', () => {
		if (run.disabled) return;
		if (!root) root = createRoot(container);
		if (running) { previewScope++; previewStartedAt = new Date(Date.now() - 31_000).toISOString(); }
		render([
			msg({ id: 'question', role: 'user', content: [text('question', '查询集团净利润')], created_at: previewStartedAt }),
			msg({ created_at: previewStartedAt, finished_at: running ? null : new Date().toISOString(),
				finished_reason: running ? null : 'completed' as Msg['finished_reason'], content: running ? content.slice(0, 4) : content }),
		], `preview-${previewScope}`);
	});
}
