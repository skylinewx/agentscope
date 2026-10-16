import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from '@/components/ui/message-scroller';
import '@/index.css';

const harness = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
let root: ReturnType<typeof createRoot>;
let demoCount = 8;
const viewport = () => harness.querySelector<HTMLElement>('[data-slot="message-scroller-viewport"]')!;
const down = () => harness.querySelector<HTMLButtonElement>('[data-slot="message-scroller-button"]')!;
const gap = () => viewport().scrollHeight - viewport().clientHeight - viewport().scrollTop;
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
const settle = () => new Promise((resolve) => setTimeout(resolve, 250));
const atBottom = () => assert(gap() <= 2, `距底部仍有 ${Math.round(gap())}px`);

function render(count = 8, lastHeight = 100, height = 320, scope = 'one') {
	flushSync(() => root.render(<MessageScrollerProvider key={scope} autoScroll defaultScrollPosition="end">
		<div style={{ height }}><MessageScroller><MessageScrollerViewport><MessageScrollerContent>
			{Array.from({ length: count }, (_, index) => <MessageScrollerItem key={index} messageId={`message-${index}`}>
				<div className="rounded-lg bg-muted p-4" style={{ height: index === count - 1 ? lastHeight : 100 }}>
					<p>{index === count - 1 ? '最新回复' : `历史消息 ${index + 1}`}</p>
					<p className="text-muted-foreground">滚动查看记录，停留在底部时跟随新增内容。</p>
				</div>
			</MessageScrollerItem>)}
		</MessageScrollerContent></MessageScrollerViewport><MessageScrollerButton className="rounded-full" /></MessageScroller></div>
	</MessageScrollerProvider>));
}

// Dispatch the input event and its resulting native scroll position together,
// so the browser fixture exercises intent and scroll handling independently.
async function move(delta: number) {
	viewport().dispatchEvent(new WheelEvent('wheel', { deltaY: delta, bubbles: true }));
	viewport().scrollTop += delta;
	await settle();
}

const tests: [string, () => Promise<void>][] = [
	['处于底部时，追加消息和流式增高都持续跟随', async () => {
		render(); await settle(); atBottom();
		render(9); await settle(); atBottom();
		render(9, 450); await settle(); atBottom();
		assert(down().dataset.active === 'false', '已在底部不应显示箭头');
	}],
	['底部继续向下滚动后仍跟随新内容', async () => {
		render(); await settle(); await move(200); atBottom();
		render(9); await settle(); atBottom();
	}],
	['底部滚动手势与新内容同时到达时不丢失跟随', async () => {
		render(); await settle();
		viewport().dispatchEvent(new WheelEvent('wheel', { deltaY: 200, bubbles: true }));
		render(9, 300); await settle(); atBottom();
	}],
	['没有改变聊天位置的滚动手势不会停止跟随', async () => {
		render(); await settle();
		viewport().dispatchEvent(new WheelEvent('wheel', { deltaX: 120, deltaY: 0, bubbles: true }));
		await settle(); render(8, 300); await settle(); atBottom();
		viewport().dispatchEvent(new Event('touchmove', { bubbles: true }));
		await settle(); render(8, 500); await settle(); atBottom();
	}],
	['手动上滚后停在阅读位置，并显示向下箭头', async () => {
		render(); await settle(); await move(-240);
		const top = viewport().scrollTop;
		assert(down().dataset.active === 'true', '离开底部应显示箭头');
		render(9, 400); await settle();
		assert(Math.abs(viewport().scrollTop - top) <= 2, '新增内容抢走了阅读位置');
	}],
	['点击向下箭头后恢复持续跟随', async () => {
		render(); await settle(); await move(-300);
		down().click(); await new Promise((resolve) => setTimeout(resolve, 700)); atBottom();
		render(9, 280); await settle(); atBottom();
	}],
	['手动滚回底部后恢复跟随', async () => {
		render(); await settle(); await move(-280);
		await move(10_000); atBottom();
		render(9); await settle(); atBottom();
	}],
	['键盘离开底部时暂停，回到底部后恢复', async () => {
		render(); await settle();
		viewport().dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
		viewport().scrollTop = 0; await settle();
		render(9); await settle();
		assert(viewport().scrollTop <= 2, '键盘上滚后仍被自动拉到底部');
		viewport().dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
		viewport().scrollTop = viewport().scrollHeight; await settle();
		render(10); await settle(); atBottom();
	}],
	['底部内容收起、窗口高度变化后仍跟随', async () => {
		render(8, 600); await settle(); atBottom();
		render(8, 100); await settle(); atBottom();
		render(8, 100, 240); await settle(); atBottom();
		render(9, 100, 240); await settle(); atBottom();
	}],
	['上滚后调整窗口高度不会跳回底部', async () => {
		render(); await settle(); await move(-240);
		const top = viewport().scrollTop;
		render(8, 100, 240); await settle();
		assert(Math.abs(viewport().scrollTop - top) <= 2, '调整高度抢走了阅读位置');
	}],
	['短回复首次超出一屏后继续跟随', async () => {
		render(1); await settle(); await move(100);
		render(1, 700); await settle(); atBottom();
	}],
	['切换会话后重置为新会话底部', async () => {
		render(); await settle(); await move(-240);
		render(12, 100, 320, 'another-session'); await settle(); atBottom();
	}],
];

run.addEventListener('click', async () => {
	run.disabled = true; results.replaceChildren(); summary.textContent = '测试中';
	if (root) flushSync(() => root.unmount());
	let passed = 0;
	for (const [name, test] of tests) {
		root = createRoot(harness);
		const row = document.createElement('li');
		try { await test(); row.textContent = `通过：${name}`; passed++; }
		catch (error) { row.textContent = `失败：${name} — ${String(error)}`; row.className = 'text-destructive'; }
		finally { flushSync(() => root.unmount()); }
		results.append(row);
	}
	summary.textContent = `${passed}/${tests.length} 通过`; run.disabled = false;
	root = createRoot(harness); demoCount = 8; render();
});
document.querySelector<HTMLButtonElement>('#append')!.addEventListener('click', () => {
	if (run.disabled) return;
	if (!root) root = createRoot(harness);
	render(++demoCount);
});
