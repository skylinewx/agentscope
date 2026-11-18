import { ErrorType } from '@agentscope-ai/agentscope/event';
import type { ContentBlock, Msg, TextBlock } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { ASMessageBubble } from '@/components/chat/ASMessageBubble';
import { MessageScroller, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from '@/components/ui/message-scroller';
import { TooltipProvider } from '@/components/ui/tooltip';
import i18n from '@/i18n';
import { applyReplyPresentation } from '@/lib/reply-presentation';
import '@/index.css';

const container = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// These fixtures verify card visibility, not report data fetching. Keep them
// isolated from the user's workspace API while rendering the real component.
queryClient.setQueryDefaults(['workspace-analysis'], { enabled: false });
let root: ReturnType<typeof createRoot>;
const at = '2026-10-13T10:00:00Z';
const text = (id: string, value = id): TextBlock => ({ type: 'text', id, text: value, created_at: at, finished_at: at });
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
function render(messages: Msg[], scope = 'one', withWorkspace = false) {
	flushSync(() => root.render(<QueryClientProvider client={queryClient}><TooltipProvider>
		<MessageScrollerProvider autoScroll defaultScrollPosition="end">
			<div className="h-[360px]"><MessageScroller><MessageScrollerViewport><MessageScrollerContent>
				{messages.map((message) => <MessageScrollerItem key={`${scope}:${message.id}`} messageId={message.id}>
					<ASMessageBubble message={message} agentId={withWorkspace ? 'fixture-agent' : null} sessionId={withWorkspace ? scope : null} onUserConfirm={() => {}} />
				</MessageScrollerItem>)}
			</MessageScrollerContent></MessageScrollerViewport></MessageScroller></div>
		</MessageScrollerProvider>
	</TooltipProvider></QueryClientProvider>));
}

const analysisId = 'ana_0123456789abcdef0123456789abcdef';
const analysisRef = (revision: number) => `${analysisId}:${revision}`;
const analysisPath = (revision: number) => `./analysis/${analysisId}/revisions/${revision}/analysis.json`;
function published(revision: number): ContentBlock[] {
	const ref = analysisRef(revision), path = analysisPath(revision), id = `publish-${revision}`;
	return [
		{ type: 'tool_call', id, name: 'AnalysisPublish', input: JSON.stringify({ analysisRef: ref }), state: 'finished', created_at: at, finished_at: at },
		{ type: 'tool_result', id, name: 'AnalysisPublish', state: 'success', created_at: at, finished_at: at,
			metadata: { process_summary: { kind: 'analysis', id: analysisId } }, output: [text(`receipt-${revision}`, JSON.stringify({
				analysisRef: ref, publicationState: 'PUBLISHED', delivery: { type: 'workspace_analysis', registered: true, analysisRef: ref, path },
			}))] },
		text(`link-${revision}`, `[查看结果](${path})`),
	];
}
const rejected: ContentBlock = { type: 'hint', id: 'rejected', created_at: at, finished_at: at,
	source: JSON.stringify({ label: 'System', sublabel: 'Completion check' }), hint: '请修正证据不一致之处' };
const cardCount = () => container.querySelectorAll('[data-slot="workspace-analysis"]').length;

function runningContent(): ContentBlock[] {
	const startedAt = new Date(Date.now() - 1_000).toISOString();
	return [
		{ type: 'thinking', id: 'live-thinking', thinking: '正在核对预算数据', created_at: startedAt, finished_at: null },
		{ type: 'hint', id: 'live-hint', source: '核对查询上下文', hint: '正在加载查询条件', created_at: startedAt, finished_at: null },
		{ type: 'tool_call', id: 'live-mcp', name: 'mcp__budget__query', input: '{}', state: 'finished', created_at: startedAt, finished_at: startedAt },
		{ type: 'tool_result', id: 'live-mcp', name: 'mcp__budget__query', output: [], state: 'running', metadata: {}, created_at: startedAt, finished_at: null },
		{ type: 'tool_call', id: 'live-read', name: 'Read', input: '{"file_path":"budget-report-with-a-long-file-name-for-narrow-screens.txt"}', state: 'finished', created_at: startedAt, finished_at: startedAt },
	];
}

function assertTextShimmer(row: HTMLElement) {
	const rowStyle = getComputedStyle(row);
	assert(rowStyle.backgroundImage === 'none', `整行出现了渐变背景：${row.textContent}`);
	assert(rowStyle.webkitTextFillColor !== 'rgba(0, 0, 0, 0)', '父容器不应把文字或图标设为透明');
	const labels = [...row.querySelectorAll<HTMLElement>(':scope > span')];
	assert(labels.length > 0, '标题文字缺失');
	for (const label of labels) {
		const style = getComputedStyle(label);
		assert(style.backgroundClip === 'text', `扫光必须裁剪到文字：${label.textContent}`);
		assert(style.backgroundImage.includes('linear-gradient'), '运行中的文字应保留扫光');
		assert(style.animationName.includes('tw-shimmer'), '运行中的文字应保留动画');
		assert(label.getBoundingClientRect().width > 0, '文字不能被布局隐藏');
	}
	assert(row.scrollWidth <= row.clientWidth + 1, '运行中的标题不能横向溢出');
}

const tests: [string, () => Promise<void>][] = [
	['分析被否决后，运行中、失败及重新加载均不展示正文卡片', async () => {
		const current = msg({ content: published(4), finished_reason: null, finished_at: null });
		render([current], 'rejected', true);
		assert(cardCount() === 0, '发布回执尚未经过最终审查，不应提前显示正文卡片');
		current.content = [...current.content, rejected, text('repair', `[修正尚未完成](${analysisPath(4)})`)];
		for (const reason of [null, 'error', 'interrupted', 'exceed_max_iters'] as Msg['finished_reason'][]) {
			const pending = { ...current, finished_reason: reason, finished_at: reason ? at : null };
			render([pending], 'rejected', true);
			assert(cardCount() === 0, `被否决的版本不能显示为正文卡片：${reason}`);
			render([JSON.parse(JSON.stringify(pending))], `reload-${reason}`, true);
			assert(cardCount() === 0, '重新加载后不得恢复旧卡片，Markdown 链接也不能绕过');
		}
	}],
	['仅更正答复文字后，同一报告复核通过可展示一张卡片', async () => {
		const current = msg({ content: [...published(4), rejected, text('answer', '依据已核对，结论已修正')], finished_reason: null, finished_at: null });
		render([current], 'text-repair', true);
		assert(cardCount() === 0, '等待复核时不得作为最终结果');
		applyReplyPresentation(current, { type: 'REPLY_END', reply_id: current.id,
			metadata: { analysisCompletion: { status: 'READY', analysisRef: analysisRef(4), semanticReview: 'PASS' } },
		} as Parameters<typeof applyReplyPresentation>[1]);
		current.finished_reason = 'completed' as Msg['finished_reason']; current.finished_at = at;
		render([{ ...current }], 'text-repair', true);
		assert(cardCount() === 1, '完成事件必须保留同一已通过版本');
		render([JSON.parse(JSON.stringify(current))], 'text-repair-reload', true);
		assert(cardCount() === 1, '保存后重新加载的结果必须一致');
	}],
	['修订后只显示最终复核通过的卡片，重复链接不生成第二张', async () => {
		const current = msg({ content: [...published(4), rejected, ...published(5), text('answer', `[最终分析](${analysisPath(5)})`)],
			metadata: { analysisCompletion: { status: 'PARTIAL', analysisRef: analysisRef(5), semanticReview: 'PASS' } } });
		render([current], 'revision', true);
		assert(cardCount() === 1, '修订版本和正文链接只能形成一张最终卡片');
		button().click(); await until(() => visible('[data-testid="reply-process-content"]'));
		assert(cardCount() === 1, '展开过程也不能把被否决链接变回独立卡片');
	}],
	...([false, true].map((dark): [string, () => Promise<void>] => [
		`${dark ? '深色窄屏' : '浅色常规宽度'}：运行中的文字扫光不铺满按钮，完成后停止`,
		async () => {
			const wasDark = document.documentElement.classList.contains('dark');
			const oldWidth = container.style.width;
			document.documentElement.classList.toggle('dark', dark);
			container.style.width = dark ? '360px' : '768px';
			try {
				const live = runningContent();
				render([msg({ content: live, finished_at: null, finished_reason: null })]);
				const details = container.querySelector<HTMLElement>('[data-testid="reply-process-details-content"]')!;
				const headings = [...details.querySelectorAll<HTMLButtonElement>('button')];
				assert(headings.length === 3, '应显示思考、提示和工具调用汇总三个标题');
				headings.forEach(assertTextShimmer);
				headings[2].click();
				await until(() => details.textContent?.includes('mcp__budget__query'));
				const mcpRow = [...details.querySelectorAll<HTMLButtonElement>('button')].find((element) => element.textContent?.includes('mcp__budget__query'))!;
				assertTextShimmer(mcpRow);
				const readRow = [...details.querySelectorAll('span')].find((element) => element.textContent === 'Read')!.parentElement!;
				assertTextShimmer(readRow);
				assert(readRow.tagName !== 'BUTTON', '没有详情的工具行不应成为空按钮');
				mcpRow.focus(); mcpRow.click();
				await until(() => mcpRow.getAttribute('aria-expanded') === 'true');
				assert(document.activeElement === mcpRow, '折叠操作必须保留焦点');
				assertTextShimmer(mcpRow);
				details.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
				await new Promise((resolve) => setTimeout(resolve, 30));
				const finished = live.map((block): ContentBlock => block.type === 'tool_result'
					? { ...block, finished_at: at, state: 'success' }
					: { ...block, finished_at: at });
				finished.push({ type: 'tool_result', id: 'live-read', name: 'Read', state: 'success', output: '读取完成', metadata: {}, created_at: at, finished_at: at });
				render([msg({ content: finished })]);
				await until(() => button().getAttribute('aria-expanded') === 'false');
				assert(document.activeElement === button(), '自动收起时，过程内的键盘焦点应回到摘要');
				button().click(); await until(() => visible('[data-testid="reply-process-content"]'));
				detailsButton().click(); await until(() => visible('[data-testid="reply-process-details-content"]'));
				const completedDetails = container.querySelector<HTMLElement>('[data-testid="reply-process-details-content"]')!;
				assert([...completedDetails.querySelectorAll('span')].every((element) => getComputedStyle(element).animationName === 'none'), '完成后重新查看时应停止扫光');
				assert([...completedDetails.querySelectorAll('button')].every((element) => getComputedStyle(element).backgroundImage === 'none'), '结束后不能残留渐变条');
			} finally {
				document.documentElement.classList.toggle('dark', wasDark);
				container.style.width = oldWidth;
			}
		},
	])),
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
	['用户主动查看工具详情时，完成后仍自动收起', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		container.querySelector('[data-testid="reply-process-details-content"]')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		await new Promise((resolve) => setTimeout(resolve, 30));
		render([msg()]);
		await until(() => button().getAttribute('aria-expanded') === 'false');
		assert(!visible('[data-testid="reply-process-details-content"]'), 'Reading details must close on completion');
	}],
	['手动展开在流式更新中保留，完成后收起，重新展开仍可阅读', async () => {
		render([msg({ finished_at: null, finished_reason: null })]); button().click();
		await until(() => button().getAttribute('aria-expanded') === 'false');
		button().click(); await until(() => button().getAttribute('aria-expanded') === 'true');
		render([msg({ finished_at: null, finished_reason: null, content: [...content, text('stream-update')] })]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Streaming updates must preserve explicit choice');
		render([msg()]);
		await until(() => button().getAttribute('aria-expanded') === 'false');
		button().click(); await until(() => button().getAttribute('aria-expanded') === 'true');
		render([msg({ usage: { input_tokens: 100, output_tokens: 20 } })]);
		assert(button().getAttribute('aria-expanded') === 'true', 'Reopening after completion must survive later updates');
	}],
	['过程中选中或滚动内容，完成后仍自动收起', async () => {
		render([msg({ finished_at: null, finished_reason: null })]);
		container.querySelector('[data-testid="reply-process-content"]')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		container.querySelector('[data-testid="reply-process-content"]')!.dispatchEvent(new WheelEvent('wheel', { bubbles: true }));
		await new Promise((resolve) => setTimeout(resolve, 30));
		render([msg()]);
		await until(() => button().getAttribute('aria-expanded') === 'false');
	}],
	['失败提示、部分答案在收起状态仍可见', async () => {
		render([msg({ finished_reason: 'error' as Msg['finished_reason'], error: { type: ErrorType.CONNECTION, message: 'offline' } })]);
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
				finished_reason: running ? null : 'completed' as Msg['finished_reason'], content: running ? runningContent() : content }),
		], `preview-${previewScope}`);
	});
}
