// Run through Vite at /tests/workspace-mcp.html. Uses existing project
// dependencies and the real browser/React lifecycle, without a test framework.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, useEffect } from 'react';
import { createRoot } from 'react-dom/client';

import { workspaceApi } from '@/api';
import type { MCPClientStatus, Skill } from '@/api';
import { useWorkspace } from '@/hooks/useWorkspace';
import { notifyResourceChanged } from '@/lib/resource-events';

type Props = {
	agentId: string | null;
	sessionId: string | null;
	isRunning: boolean;
	mcpPanelOpen: boolean;
};

const initialProps: Props = {
	agentId: 'agent',
	sessionId: 'session',
	isRunning: false,
	mcpPanelOpen: true,
};
const mcp = (status: MCPClientStatus['status'], name = 'budget'): MCPClientStatus => ({
	name,
	status,
	enabled: status !== 'disabled',
	is_stateful: false,
	transport: 'http_mcp',
	resource_id: name,
	source: 'custom',
	version: null,
	binding_sources: ['agent'],
	binding_id: 'generation',
	agent_enabled: true,
	session_override: null,
	effective_enabled: status !== 'disabled',
	available: true,
	is_healthy: status === 'ready',
	tools: status === 'ready' ? [{ name: 'get_context', description: 'Query context' }] : [],
	error: status === 'error' ? 'Connection refused' : null,
});
const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}
async function until(check: () => boolean, message: string, timeout = 4_000) {
	const deadline = Date.now() + timeout;
	while (!check()) {
		if (Date.now() > deadline) throw new Error(message);
		await delay(20);
	}
}

let root: ReturnType<typeof createRoot>;
let queryClient: QueryClient;
let workspace: ReturnType<typeof useWorkspace> | undefined;
let responder: typeof workspaceApi.mcp.list;
let calls: [string, string][];
let renderVersion: number;
const container = document.querySelector<HTMLDivElement>('#harness')!;

function WorkspaceHarness({ agentId, sessionId, ...activity }: Props) {
	const value = useWorkspace(agentId, sessionId, activity);
	useEffect(() => {
		workspace = value;
	}, [value]);
	return createElement(
		'output',
		null,
		value.mcps.map((item) => `${item.name}: ${item.status}`).join(', '),
	);
}
async function render(props: Partial<Props> = {}) {
	const version = ++renderVersion;
	root.render(
		createElement(
			QueryClientProvider,
			{ client: queryClient },
			createElement(
				'section',
				{ 'data-version': version },
				createElement(WorkspaceHarness, { ...initialProps, ...props }),
			),
		),
	);
	await until(
		() => container.querySelector('section')?.getAttribute('data-version') === String(version),
		'The updated React view did not render',
	);
	// Flush passive effects and QueryObserver's scheduled notifications.
	await delay(30);
}
async function expectStatus(status: string) {
	await until(
		() => container.textContent === status,
		`Expected "${status}", received "${container.textContent}"`,
	);
}

const tests: [string, () => Promise<void>][] = [
	[
		'A late Skill response cannot overwrite another conversation',
		async () => {
			let release: (skills: Skill[]) => void = () => {};
			const original = workspaceApi.skill.list;
			try {
				workspaceApi.skill.list = async (_agent, session) =>
					session === 'old'
						? await new Promise<Skill[]>((resolve) => {
								release = resolve;
							})
						: [{ name: 'current', resource_id: 'current' } as Skill];
				await render({ sessionId: 'old' });
				await render({ sessionId: 'current' });
				await until(
					() => workspace?.skills[0]?.name === 'current',
					'Current Skill did not load',
				);
				release([{ name: 'old', resource_id: 'old' } as Skill]);
				await delay(50);
				assert(
					workspace?.skills[0]?.name === 'current',
					'Late Skill response crossed the session boundary',
				);
			} finally {
				workspaceApi.skill.list = original;
			}
		},
	],
	[
		'Switches and inheritance reset include resource ID and current scope',
		async () => {
			const original = workspaceApi.setResourceEnabled;
			const writes: unknown[][] = [];
			try {
				workspaceApi.setResourceEnabled = async (...args) => {
					writes.push(args);
				};
				await render();
				await workspace!.changeMcp('stable-mcp-id', false);
				await workspace!.changeSkill('stable-skill-id', true);
				await workspace!.changeMcp('stable-mcp-id', null);
				assert(
					JSON.stringify(writes) ===
						JSON.stringify([
							['mcp', 'stable-mcp-id', 'agent', 'session', false],
							['skill', 'stable-skill-id', 'agent', 'session', true],
							['mcp', 'stable-mcp-id', 'agent', 'session', null],
						]),
					'A scoped write lost its ID, override, or conversation',
				);
			} finally {
				workspaceApi.setResourceEnabled = original;
			}
		},
	],

	[
		'Reply start refreshes an already-open panel without changing sessions',
		async () => {
			await render();
			await expectStatus('budget: not_loaded');
			responder = async () => [mcp('ready')];
			await render({ isRunning: true });
			await expectStatus('budget: ready');
			assert(workspace?.mcps[0].tools.length === 1, 'Ready tools should be shown');
			assert(!workspace.loading, 'Background refresh should not show initial loading');
		},
	],
	[
		'Reply end refreshes the panel and shows initialization errors',
		async () => {
			await render({ isRunning: true });
			await expectStatus('budget: not_loaded');
			responder = async () => [mcp('error')];
			await render({ isRunning: false });
			await expectStatus('budget: error');
			assert(workspace?.mcps[0].error === 'Connection refused', 'Error details must survive');
		},
	],
	[
		'Pending initialization is refreshed during a run; polling stops once ready',
		async () => {
			await render({ isRunning: true });
			await expectStatus('budget: not_loaded');
			responder = async () => [mcp('ready')];
			await expectStatus('budget: ready');
			const count = calls.length;
			await delay(2_200);
			assert(calls.length === count, 'A ready MCP should not be polled');
		},
	],
	[
		'Idle sessions and hidden MCP panels do not poll',
		async () => {
			await render();
			await expectStatus('budget: not_loaded');
			let count = calls.length;
			await delay(2_200);
			assert(calls.length === count, 'An idle session should not be polled');
			await render({ isRunning: true, mcpPanelOpen: false });
			count = calls.length;
			await delay(2_200);
			assert(calls.length === count, 'A hidden panel should not be polled');
		},
	],
	[
		'Opening the MCP panel refreshes tools loaded while it was closed',
		async () => {
			await render({ mcpPanelOpen: false });
			await expectStatus('budget: not_loaded');
			responder = async () => [mcp('ready')];
			await render({ mcpPanelOpen: true });
			await expectStatus('budget: ready');
		},
	],
	[
		'A late response from a previous session cannot overwrite the current session',
		async () => {
			let resolveOld!: (value: MCPClientStatus[]) => void;
			responder = (_agent, session) =>
				session === 'session'
					? new Promise((resolve) => {
							resolveOld = resolve;
						})
					: Promise.resolve([mcp('ready', 'second')]);
			await render();
			await render({ sessionId: 'second-session' });
			await expectStatus('second: ready');
			resolveOld([mcp('not_loaded', 'first')]);
			await delay(100);
			await expectStatus('second: ready');
		},
	],
	[
		'An older overlapping refresh cannot regress the newer ready state',
		async () => {
			await render();
			await expectStatus('budget: not_loaded');
			let resolveOld!: (value: MCPClientStatus[]) => void;
			responder = () =>
				new Promise((resolve) => {
					resolveOld = resolve;
				});
			const pending = workspace!.refetch();
			await until(() => Boolean(resolveOld), 'The old request should have started');
			responder = async () => [mcp('ready')];
			await workspace!.refetch();
			await expectStatus('budget: ready');
			resolveOld([mcp('not_loaded')]);
			await pending;
			await delay(100);
			await expectStatus('budget: ready');
		},
	],
	[
		'No selected session clears the panel and disables requests',
		async () => {
			responder = async () => [mcp('ready')];
			await render();
			await expectStatus('budget: ready');
			const count = calls.length;
			await render({ sessionId: null, isRunning: true });
			await expectStatus('');
			assert(!workspace?.loading, 'No session must not show loading');
			assert(calls.length === count, 'No requests should run without a session');
		},
	],
	[
		'Resource configuration changes refresh the open panel',
		async () => {
			await render();
			await expectStatus('budget: not_loaded');
			responder = async () => [mcp('disabled')];
			notifyResourceChanged();
			await expectStatus('budget: disabled');
		},
	],
	[
		'A failed status request stops polling and a later run retries',
		async () => {
			await render({ isRunning: true });
			await expectStatus('budget: not_loaded');
			responder = async () => {
				throw new Error('Status endpoint unavailable');
			};
			await workspace!.refetch();
			await until(
				() => workspace?.error?.message === 'Status endpoint unavailable',
				'Fetch error must be exposed',
			);
			const count = calls.length;
			await delay(2_200);
			assert(calls.length === count, 'A failed endpoint should not be polled repeatedly');
			responder = async () => [mcp('ready')];
			await render({ isRunning: false });
			await expectStatus('budget: ready');
			assert(workspace?.error === null, 'A successful refresh should clear its error');
		},
	],
];

const runButton = document.querySelector<HTMLButtonElement>('#run')!;
const summary = document.querySelector<HTMLParagraphElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
runButton.addEventListener('click', async () => {
	runButton.disabled = true;
	results.replaceChildren();
	summary.textContent = 'Running…';
	const originalList = workspaceApi.mcp.list;
	const originalSkills = workspaceApi.skill.list;
	let passed = 0;
	try {
		workspaceApi.mcp.list = (agent, session) => {
			calls.push([agent, session]);
			return responder(agent, session);
		};
		workspaceApi.skill.list = async () => [];
		for (const [name, test] of tests) {
			const item = document.createElement('li');
			item.textContent = `Running: ${name}`;
			results.append(item);
			calls = [];
			workspace = undefined;
			renderVersion = 0;
			responder = async () => [mcp('not_loaded')];
			queryClient = new QueryClient({
				defaultOptions: {
					queries: { retry: false, gcTime: Infinity, refetchOnWindowFocus: false },
				},
			});
			root = createRoot(container);
			try {
				await test();
				passed++;
				item.dataset.result = 'passed';
				item.textContent = `PASS: ${name}`;
			} catch (error) {
				item.dataset.result = 'failed';
				item.textContent = `FAIL: ${name} — ${String(error)}`;
			} finally {
				root.unmount();
				queryClient.clear();
			}
		}
	} finally {
		workspaceApi.mcp.list = originalList;
		workspaceApi.skill.list = originalSkills;
		summary.textContent = `${passed}/${tests.length} passed`;
		runButton.disabled = false;
	}
});
