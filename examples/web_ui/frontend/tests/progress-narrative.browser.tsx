import type { AgentEvent } from '@agentscope-ai/agentscope/event';
import { appendEvent, AssistantMsg, type Msg } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { ASMessageBubble } from '@/components/chat/ASMessageBubble';
import { Button } from '@/components/ui/button';
import { TooltipProvider } from '@/components/ui/tooltip';
import i18n from '@/i18n';
import { applyAgentProgress, progressSteps } from '@/lib/agent-progress';
import { applyReplyPresentation } from '@/lib/reply-presentation';
import '@/index.css';

const root = createRoot(document.querySelector('#harness')!);
const result = document.querySelector('#result')!;
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
const wait = () => new Promise((resolve) => setTimeout(resolve, 60));

async function run() {
	await i18n.changeLanguage('zh');
	const response = await fetch('/.cache/progress-real-case.json');
	if (!response.ok) throw new Error('请先运行 scripts/regress_reply_progress.py 生成真实案例');
	const data = await response.json() as { events: AgentEvent[]; message: Msg; captureRef: string };
	let message: Msg | null = null;
	let running: Msg | null = null;
	for (const event of data.events) {
		if (event.type === 'REPLY_START') message = AssistantMsg({ id: event.reply_id, name: event.name, content: [], created_at: event.created_at });
		else if (message && event.type === 'CUSTOM') {
			applyAgentProgress(message, event);
			if (progressSteps(message).at(-1)?.summary &&
				progressSteps(message).at(-1)?.nextAction.startsWith('接下来') &&
				progressSteps(message).at(-1)?.status === 'running' &&
				!message.content.some((block) => block.type === 'text')) running = structuredClone(message);
		} else if (message) { appendEvent(message, event); applyReplyPresentation(message, event); }
	}
	assert(message && running, '实际事件中缺少事实与具体下一步同时存在的阶段摘要');
	assert(progressSteps(message!).some(step => step.summary.includes('元')), '缺少真实金额摘要');
	const prose = progressSteps(message!).map(step => step.summary).filter(Boolean);
	assert(new Set(prose).size === prose.length, '阶段摘要重复');
	assert(!prose.some(text => text.includes('执行所选操作') || text.includes('取得了本阶段的返回结果')), '存在通用占位摘要');
	assert(JSON.stringify(progressSteps(message!)) === JSON.stringify(progressSteps(data.message)), '直播与持久化进度不一致');
	const restored = JSON.parse(JSON.stringify(data.message)) as Msg;
	const render = (value: Msg) => flushSync(() => root.render(
		<QueryClientProvider client={client}><TooltipProvider>
			<div className="flex gap-3 my-4 flex-wrap">
				<Button variant="outline" onClick={() => render(running!)}>查看运行中</Button>
				<Button variant="outline" onClick={() => render(restored)}>从保存记录恢复</Button>
				<Button variant="outline" onClick={() => document.documentElement.classList.toggle('dark')}>切换深浅主题</Button>
			</div>
			<ASMessageBubble key={value.finished_at ? 'saved' : 'running'} message={value} onUserConfirm={() => {}} />
		</TooltipProvider></QueryClientProvider>));
	// The captured last progress snapshot precedes REPLY_END; show it as live.
	running!.finished_at = null;
	render(running!);
	await wait();
	const nodes = document.querySelectorAll('[data-testid="progress-narrative"]');
	assert(nodes.length > 0, '进展自然段未渲染');
	assert(document.querySelectorAll('[data-testid="progress-narrative"] table').length === 0, '摘要不得包含数据表格');
	assert(document.querySelectorAll('[data-testid="progress-narrative"] p').length >= 2, '摘要必须展示两段说明');
	assert(document.querySelector('[data-testid="reply-process-content"]')?.textContent?.includes('接下来') ||
		document.querySelector('[data-testid="reply-process-content"]')?.textContent?.includes('本次处理'), '缺少后续说明');
	render(restored);
	await wait();
	assert(document.querySelector('[data-testid="reply-process-content"]'), '完成后摘要必须保持可见');
	assert(!document.querySelector('[data-testid="reply-process-summary"]')?.hasAttribute('aria-expanded'), '总状态不能再折叠摘要');
	assert(document.querySelectorAll('[data-testid="progress-narrative"]').length === progressSteps(restored).length, '恢复后阶段数量变化');
	assert(!document.querySelector('[data-testid="progress-narrative"] button'), '摘要不应出现标题或折叠按钮');
	for (const step of progressSteps(restored)) {
		assert(document.querySelector('[data-testid="reply-process-content"]')?.textContent?.includes(step.summary), '历史摘要正文必须直接可见');
	}
	assert(!document.querySelector('[data-testid="progress-narrative"] table'), '历史恢复不应改变纯文字布局');
	result.textContent = '通过：真实事件回放、数字来源、纯自然段、无标题和折叠、完成及恢复后全文可见。';
	result.setAttribute('data-status', 'passed');
	render(running!);
}
run().catch((error) => { result.textContent = String(error); result.setAttribute('data-status', 'failed'); });
