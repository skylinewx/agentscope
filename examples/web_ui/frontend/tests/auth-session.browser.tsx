import { QueryClientProvider } from '@tanstack/react-query';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import { authApi } from '@/api/auth';
import { getAuth, getAuthNotice, getAuthSignal, setAuth, type AuthSession } from '@/api/auth-state';
import { AuthGate } from '@/context/AuthContext';
import { TooltipProvider } from '@/components/ui/tooltip';
import i18n from '@/i18n';
import { queryClient } from '@/lib/query-client';
import '@/index.css';

const harness = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
const session: AuthSession = {
	user: { id: 'fixture', username: 'fixture', display_name: '测试用户', role: 'user', status: 'active', created_at: '', updated_at: '', last_login_at: null },
	csrf_token: 'fixture-only',
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const assert = (value: unknown, message: string) => { if (!value) throw new Error(message); };
const until = async (check: () => unknown) => { for (let n = 0; n < 100; n++) { if (check()) return; await new Promise((resolve) => setTimeout(resolve, 20)); } throw new Error('等待超时'); };
let status = 200;
let root: ReturnType<typeof createRoot> | null = null;
let defer = false;
let release: (() => void) | null = null;

window.fetch = async (input) => {
	const url = new URL(String(input), location.origin);
	if (url.pathname === '/api/branding') return json({ browser_title: 'BudgetOntology', logo_data_url: null });
	if (url.pathname === '/api/auth/me') {
		const responseStatus = status;
		if (defer) { defer = false; await new Promise<void>((resolve) => { release = resolve; }); }
		return json(responseStatus === 200 ? session : { detail: '测试登录状态响应' }, responseStatus);
	}
	throw new Error(`Unexpected fixture request: ${url.pathname}`);
};

async function mount(initialStatus = 200) {
	if (root) flushSync(() => root!.unmount());
	setAuth(null); status = initialStatus; defer = false; release = null;
	root = createRoot(harness);
	flushSync(() => root!.render(<QueryClientProvider client={queryClient}><TooltipProvider>
		<AuthGate><section data-testid="protected"><p>已登录的聊天页面</p><textarea defaultValue="未发送的草稿" /></section></AuthGate>
	</TooltipProvider></QueryClientProvider>));
	await until(() => initialStatus === 200 ? !!harness.querySelector('[data-testid="protected"]') : !!harness.querySelector('#login-username'));
}

const tests: [string, () => Promise<void>][] = [
	['续期后登录身份、聊天草稿、缓存及连接信号保持不变', async () => {
		await mount(); const signal = getAuthSignal(); const draft = harness.querySelector('textarea');
		queryClient.setQueryData(['renewal-fixture'], 'cached');
		for (let n = 0; n < 3; n++) setAuth(await authApi.me());
		assert(!signal.aborted && getAuthSignal() === signal, '续期中断了现有连接');
		assert(harness.querySelector('textarea') === draft && draft?.value === '未发送的草稿', '聊天界面或草稿被重置');
		assert(queryClient.getQueryData(['renewal-fixture']) === 'cached', '续期清除了缓存');
	}],
	['暂时的服务器错误不会退出登录', async () => {
		await mount(); status = 503; await authApi.me().catch(() => {});
		assert(!!getAuth() && !!harness.querySelector('[data-testid="protected"]'), '临时错误退出了登录');
	}],
	['当前登录失效后显示明确提示并取消原连接', async () => {
		await mount(); const signal = getAuthSignal(); status = 401; await authApi.me().catch(() => {});
		await until(() => !!harness.querySelector('#login-username'));
		assert(signal.aborted && !getAuth(), '失效登录未清除');
		assert(harness.textContent?.includes('登录已失效，请重新登录后继续。'), '缺少失效提示');
	}],
	['旧登录的迟到 401 不会退出新登录', async () => {
		await mount(); status = 401; defer = true;
		const pending = authApi.me().catch(() => {}); await until(() => release !== null);
		setAuth({ ...session, csrf_token: 'new-fixture-session' }); release!(); await pending;
		assert(getAuth()?.csrf_token === 'new-fixture-session' && !getAuthNotice(), '迟到响应清除了新登录');
	}],
	['初次访问登录页不显示会话失效提示', async () => {
		await mount(401);
		assert(!harness.textContent?.includes('登录已失效，请重新登录后继续。'), '首次访问误提示过期');
	}],
];

async function preview() {
	await mount(); status = 401; await authApi.me().catch(() => {});
	await until(() => !!harness.querySelector('#login-username'));
}
run.addEventListener('click', async () => {
	run.disabled = true; results.innerHTML = ''; summary.textContent = '正在运行…'; let passed = 0;
	for (const [name, test] of tests) {
		const row = document.createElement('li');
		try { await test(); row.textContent = `通过：${name}`; passed++; } catch (error) { row.textContent = `失败：${name} — ${error}`; }
		results.append(row);
	}
	summary.textContent = `${passed}/${tests.length} 通过`; run.disabled = false; await preview();
});
await i18n.changeLanguage('zh');
await preview();
