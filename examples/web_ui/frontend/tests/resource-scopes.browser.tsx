import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

import { mcpApi, skillApi } from '@/api';
import type { AgentResourceConfig, MCPView, SkillView, ResourceScope } from '@/api';
import { AgentResources } from '@/components/form/AgentResources';
import { ResourceScopeControls } from '@/components/panel/ResourceScopeControls';
import i18n from '@/i18n';
import '@/index.css';

const container = document.querySelector<HTMLDivElement>('#harness')!;
const summary = document.querySelector<HTMLElement>('#summary')!;
const results = document.querySelector<HTMLOListElement>('#results')!;
const run = document.querySelector<HTMLButtonElement>('#run')!;
let root: ReturnType<typeof createRoot>;
let config: AgentResourceConfig;
let changes: (boolean | null)[];
const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const assert = (condition: unknown, message: string) => {
	if (!condition) throw new Error(message);
};
async function until(condition: () => unknown) {
	for (let i = 0; i < 100; i++) {
		if (condition()) return;
		await delay(20);
	}
	throw new Error('Timed out waiting for component update');
}
export function Draft() {
	const [value, setValue] = useState<AgentResourceConfig>({ mcps: {}, skills: {} });
	useEffect(() => {
		config = value;
	}, [value]);
	return <AgentResources value={value} onChange={setValue} />;
}
export function Scope({ local = false, unavailable = false, fail = false }) {
	const [override, setOverride] = useState<boolean | null>(null);
	const resource: ResourceScope & { enabled: boolean } = {
		binding_sources: local ? ['session'] : ['agent'],
		binding_id: 'binding',
		agent_enabled: local ? null : false,
		session_override: local ? null : override,
		effective_enabled: !unavailable && (override ?? false),
		available: !unavailable,
		enabled: override ?? false,
	};
	return (
		<ResourceScopeControls
			resource={resource}
			name="Browser"
			onChange={async (enabled) => {
				changes.push(enabled);
				if (fail) throw new Error('保存失败');
				setOverride(enabled);
			}}
		/>
	);
}
const tests: [string, () => Promise<void>][] = [
	[
		'智能体按 ID 绑定资源，默认开关独立，取消绑定可保存',
		async () => {
			root.render(<Draft />);
			await until(() => container.querySelectorAll('[role="checkbox"]').length === 2);
			(container.querySelector('[aria-label="绑定 Browser"]') as HTMLElement).click();
			await until(() => config.mcps['mcp-id']?.enabled);
			(container.querySelector('[role="switch"]') as HTMLElement).click();
			await until(() => config.mcps['mcp-id']?.enabled === false);
			(container.querySelector('[aria-label="绑定 Reports"]') as HTMLElement).click();
			await until(() => config.skills['skill-id']?.enabled);
			assert(
				config.mcps['mcp-id'].enabled === false,
				'Skill binding must not reset MCP default',
			);
			(container.querySelector('[aria-label="绑定 Browser"]') as HTMLElement).click();
			await until(() => !config.mcps['mcp-id']);
		},
	],
	[
		'会话可开启智能体默认停用的资源，并恢复继承',
		async () => {
			root.render(<Scope />);
			await until(() => container.textContent?.includes('继承智能体'));
			(container.querySelector('[role="switch"]') as HTMLElement).click();
			await until(() => container.textContent?.includes('本会话覆盖'));
			assert(changes[0] === true, 'Explicit true override expected');
			const reset = [...container.querySelectorAll('button')].find(
				(el) => el.textContent === '恢复继承',
			)!;
			reset.click();
			await until(
				() => changes.length === 2 && container.textContent?.includes('继承智能体'),
			);
			assert(changes[1] === null, 'Reset must send null, not current default');
			assert(
				container.querySelector('[role="switch"]')?.getAttribute('aria-checked') ===
					'false',
				'Reset must restore disabled default',
			);
		},
	],
	[
		'会话额外资源没有恢复继承入口',
		async () => {
			root.render(<Scope local />);
			await until(() => container.textContent?.includes('本会话添加'));
			(container.querySelector('[role="switch"]') as HTMLElement).click();
			await until(() => changes.length === 1);
			assert(
				!container.textContent?.includes('恢复继承'),
				'Local additions have no override reset',
			);
		},
	],
	[
		'失败保留原开关状态并显示错误',
		async () => {
			root.render(<Scope fail />);
			await until(() => container.querySelector('[role="switch"]'));
			(container.querySelector('[role="switch"]') as HTMLElement).click();
			await until(() => container.querySelector('[role="alert"]'));
			assert(container.textContent?.includes('保存失败'), 'Failure should be visible');
			assert(
				container.querySelector('[role="switch"]')?.getAttribute('aria-checked') ===
					'false',
				'Failure must not appear saved',
			);
		},
	],
	[
		'无权限或已删除资源无法切换',
		async () => {
			root.render(<Scope unavailable />);
			await until(() => container.querySelector('[role="switch"]'));
			assert(
				(container.querySelector('[role="switch"]') as HTMLButtonElement).disabled,
				'Unavailable resource must be disabled',
			);
		},
	],
];
run.addEventListener('click', async () => {
	run.disabled = true;
	results.replaceChildren();
	summary.textContent = '运行中…';
	await i18n.changeLanguage('zh');
	const originalMcp = mcpApi.list,
		originalSkill = skillApi.list;
	mcpApi.list = async () => [{ id: 'mcp-id', name: 'Browser', source: 'custom' } as MCPView];
	skillApi.list = async () => [
		{
			id: 'skill-id',
			name: 'Reports',
			source: 'custom',
			package_saved: true,
			version: '1',
		} as SkillView,
	];
	let passed = 0;
	try {
		for (const [name, test] of tests) {
			root = createRoot(container);
			changes = [];
			const item = document.createElement('li');
			results.append(item);
			try {
				await test();
				passed++;
				item.textContent = `PASS: ${name}`;
			} catch (error) {
				item.textContent = `FAIL: ${name}: ${String(error)}`;
			} finally {
				root.unmount();
			}
		}
	} finally {
		mcpApi.list = originalMcp;
		skillApi.list = originalSkill;
		summary.textContent = `${passed}/${tests.length} passed`;
		run.disabled = false;
	}
});
