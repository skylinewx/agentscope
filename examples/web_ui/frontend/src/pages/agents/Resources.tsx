import { useQuery } from '@tanstack/react-query';
import { Copy, Pencil, Plus, PlugZap } from 'lucide-react';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';

import { Check, Field } from './fields';
import type { ResourceCatalog } from '@/api/agent-management';
import { client } from '@/api/client';
import { credentialApi } from '@/api/credential';
import { createMCPApi } from '@/api/mcp';
import { createSkillApi } from '@/api/skill';
import type {
	AgentResourceConfig,
	SkillResourceBinding,
	SkillView,
	MCPView,
	CredentialSchema,
} from '@/api/types';
import { ManageMCPDialog } from '@/components/dialog/ManageMCPDialog';
import { ManageSkillDialog } from '@/components/dialog/ManageSkillDialog';
import {
	SchemaForm,
	defaultValuesFromSchema,
	type SchemaFormValue,
} from '@/components/form/SchemaForm';
import { SkillVersion } from '@/components/hub/SkillVersion';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Empty, EmptyDescription } from '@/components/ui/empty';
import { SelectField, SelectOption } from '@/components/ui/select-field';

function ResourceImpact({
	owner,
	kind,
	id,
	onCopy,
}: {
	owner: string;
	kind: string;
	id: string;
	onCopy: () => void;
}) {
	const usage = useQuery({
		queryKey: ['agent-resource-usage', owner, kind, id],
		queryFn: () =>
			client.get<{ agents: { id: string; name: string }[] }>(
				`/agent-management/resources/${kind}/${id}/usage`,
				{ owner_id: owner },
			),
	});
	return (
		<div className="rounded-lg border border-primary/20 bg-primary/5 p-3 text-sm leading-6">
			<p>
				修改公共资源会影响使用它的智能体。Skill 的已有绑定版本会保留，可在管理页显式更新。
			</p>
			{usage.data && (
				<p className="text-xs text-muted-foreground">
					当前关联：{usage.data.agents.map((a) => a.name).join('、') || '暂无智能体'}
				</p>
			)}
			<Button type="button" variant="outline" size="sm" className="mt-2" onClick={onCopy}>
				<Copy className="size-3.5" />
				先创建独立副本
			</Button>
		</div>
	);
}

export function ResourceBindings({
	owner,
	kind,
	catalog,
	value,
	onChange,
	refresh,
}: {
	owner: string;
	kind: 'skill' | 'mcp';
	catalog: ResourceCatalog;
	value: AgentResourceConfig;
	onChange: (value: AgentResourceConfig) => void;
	refresh: () => Promise<ResourceCatalog>;
}) {
	const [editor, setEditor] = useState<{ record: SkillView | MCPView | null } | null>(null);
	const [busy, setBusy] = useState('');
	const skillApi = useMemo(() => createSkillApi(owner), [owner]);
	const mcpApi = useMemo(() => createMCPApi(owner), [owner]);
	const field = kind === 'skill' ? 'skills' : 'mcps';
	const bindings = value[field];
	const rows = catalog[field];
	const allIds = [...new Set([...Object.keys(bindings), ...rows.map((r) => r.id)])];
	const change = (id: string, next: SkillResourceBinding | null) => {
		const updated = { ...bindings };
		if (next) updated[id] = next;
		else delete updated[id];
		onChange({ ...value, [field]: updated });
	};
	const copyResource = async (id: string) => {
		setBusy(id);
		try {
			const saved = await client.post<{ id: string }>(
				`/agent-management/resources/${kind}/${id}/copy`,
				undefined,
				{ owner_id: owner },
			);
			const updated = await refresh();
			if (bindings[id]) {
				const selected = { ...bindings };
				delete selected[id];
				selected[saved.id] = { enabled: bindings[id].enabled };
				onChange({ ...value, [field]: selected });
			}
			setEditor({ record: updated[field].find((r) => r.id === saved.id) ?? null });
			toast.success('已创建独立资源副本，请保存智能体关联');
		} finally {
			setBusy('');
		}
	};
	const closeEditor = async () => {
		const wasNew = editor?.record === null;
		setEditor(null);
		const latest = await refresh();
		if (wasNew) {
			const additions = latest[field].filter((r) => !rows.some((old) => old.id === r.id));
			if (additions.length)
				onChange({
					...value,
					[field]: {
						...bindings,
						...Object.fromEntries(additions.map((r) => [r.id, { enabled: true }])),
					},
				});
		}
	};
	const notice = editor?.record ? (
		<ResourceImpact
			owner={owner}
			kind={kind}
			id={editor.record.id}
			onCopy={() => {
				void copyResource(editor.record!.id).catch(() => {});
			}}
		/>
	) : undefined;
	return (
		<section className="space-y-4">
			<div className="flex flex-wrap items-start justify-between gap-3">
				<div>
					<h2 className="text-lg font-semibold">
						{kind === 'skill' ? 'Skill 技能' : 'MCP 服务'}
					</h2>
					<p className="mt-2 text-sm text-muted-foreground">
						勾选资源以关联智能体，使用开关控制是否启用。资源属于当前智能体的所属用户。
					</p>
				</div>
				<Button type="button" variant="outline" onClick={() => setEditor({ record: null })}>
					<Plus className="size-4" />
					新建{kind === 'skill' ? ' Skill' : ' MCP'}
				</Button>
			</div>
			{!allIds.length && (
				<Empty className="rounded-xl border border-dashed p-10">
					<EmptyDescription>资源库为空，可以在这里新建或从资源库安装。</EmptyDescription>
				</Empty>
			)}
			{allIds.map((id) => {
				const record = rows.find((r) => r.id === id);
				const binding = bindings[id] as SkillResourceBinding | undefined;
				const skill = kind === 'skill' ? (record as SkillView | undefined) : undefined;
				return (
					<div
						key={id}
						className={`rounded-xl border p-4 space-y-3 ${binding ? 'border-primary/25 bg-primary/3' : 'bg-card'}`}
					>
						<div className="flex flex-wrap items-center justify-between gap-3">
							<Check
								label={record?.display_name || record?.name || `资源已失效 · ${id}`}
								checked={!!binding}
								disabled={!record && !binding}
								onChange={(yes) => change(id, yes ? { enabled: true } : null)}
							/>
							{binding && (
								<Check
									label="启用"
									checked={binding.enabled}
									disabled={!record}
									onChange={(enabled) => change(id, { ...binding, enabled })}
								/>
							)}
						</div>
						{record?.description && (
							<p className="line-clamp-3 text-xs leading-5 text-muted-foreground">
								{record.description}
							</p>
						)}
						<div className="flex flex-wrap items-center gap-2">
							{kind === 'skill' ? (
								<SkillVersion
									className="mr-auto"
									version={
										binding
											? binding.version || '保存时绑定当前版本'
											: skill?.version || '未指定'
									}
									versionLabel={binding ? '绑定版本' : '资源版本'}
									updatedAt={
										binding?.artifact_id
											? (binding.version_updated_at ?? null)
											: (skill?.version_updated_at ?? null)
									}
								/>
							) : (
								<span className="mr-auto text-xs text-muted-foreground">
									资源版本：{record?.version || '未指定'}
								</span>
							)}
							{record &&
								kind === 'skill' &&
								binding?.artifact_id &&
								binding.version !== record.version && (
									<Button
										type="button"
										size="sm"
										variant="outline"
										onClick={() =>
											change(id, {
												...binding,
												artifact_id: null,
												version: record.version,
											})
										}
									>
										更新到 {record.version}
									</Button>
								)}
							{record && (
								<>
									<Button
										type="button"
										size="sm"
										variant="ghost"
										onClick={() => setEditor({ record })}
									>
										<Pencil className="size-3.5" />
										编辑
									</Button>
									<Button
										type="button"
										size="sm"
										variant="ghost"
										disabled={!!busy}
										onClick={() => {
											void copyResource(id).catch(() => {});
										}}
									>
										<Copy className="size-3.5" />
										独立副本
									</Button>
								</>
							)}
							{record && kind === 'mcp' && (
								<Button
									type="button"
									size="sm"
									variant="outline"
									disabled={!!busy}
									onClick={async () => {
										setBusy(id);
										try {
											const result = await client.post<{ tools: string[] }>(
												`/agent-management/resources/mcp/${id}/test`,
												undefined,
												{ owner_id: owner },
											);
											toast.success(
												`连接成功，可用工具 ${result.tools.length} 个`,
											);
										} catch {
											/* API toast reports the failure. */
										} finally {
											setBusy('');
										}
									}}
								>
									<PlugZap className="size-3.5" />
									{busy === id ? '处理中…' : '测试连接'}
								</Button>
							)}
						</div>
					</div>
				);
			})}
			{editor && kind === 'skill' && (
				<ManageSkillDialog
					key={editor.record?.id || 'new-skill'}
					record={editor.record as SkillView | null}
					api={skillApi}
					notice={notice}
					onClose={() => {
						void closeEditor().catch(() => {});
					}}
				/>
			)}
			{editor && kind === 'mcp' && (
				<ManageMCPDialog
					key={editor.record?.id || 'new-mcp'}
					record={editor.record as MCPView | null}
					api={mcpApi}
					notice={notice}
					onClose={() => {
						void closeEditor().catch(() => {});
					}}
				/>
			)}
		</section>
	);
}

export function CredentialEditor({
	owner,
	id,
	onClose,
	onCopied,
}: {
	owner: string;
	id: string | null;
	onClose: () => void;
	onCopied: (id: string) => void;
}) {
	const schemas = useQuery({ queryKey: ['credential-schemas'], queryFn: credentialApi.schemas });
	const detail = useQuery({
		queryKey: ['agent-credential', owner, id],
		enabled: !!id,
		queryFn: () =>
			client.get<{ data: Record<string, SchemaFormValue> }>(
				`/agent-management/resources/credential/${id}`,
				{ owner_id: owner },
			),
		refetchOnWindowFocus: false,
	});
	return (
		<Dialog
			open
			onOpenChange={(open) => {
				if (!open) onClose();
			}}
		>
			<DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
				<DialogHeader>
					<DialogTitle>{id ? '编辑 AI 资源' : '新建 AI 资源'}</DialogTitle>
					<DialogDescription>
						配置会保存到智能体所属用户的资源库。已有密钥保持隐藏，填写新值即可替换。
					</DialogDescription>
				</DialogHeader>
				{id && (
					<ResourceImpact
						owner={owner}
						kind="credential"
						id={id}
						onCopy={async () => {
							const copied = await client.post<{ id: string }>(
								`/agent-management/resources/credential/${id}/copy`,
								undefined,
								{ owner_id: owner },
							);
							onCopied(copied.id);
						}}
					/>
				)}
				{(detail.error || schemas.error) && (
					<p role="alert" className="text-destructive">
						{detail.error?.message || schemas.error?.message}
					</p>
				)}
				{schemas.data && (!id || detail.data) ? (
					<CredentialForm
						key={id ?? 'new'}
						owner={owner}
						id={id}
						schemas={schemas.data.schemas}
						initial={detail.data?.data}
						onClose={onClose}
					/>
				) : (
					<p>正在加载…</p>
				)}
			</DialogContent>
		</Dialog>
	);
}

function CredentialForm({
	owner,
	id,
	schemas,
	initial,
	onClose,
}: {
	owner: string;
	id: string | null;
	schemas: CredentialSchema[];
	initial?: Record<string, SchemaFormValue>;
	onClose: () => void;
}) {
	const [data, setData] = useState<Record<string, SchemaFormValue>>(
		() =>
			initial ?? {
				...defaultValuesFromSchema(schemas[0]),
				type: schemas[0].properties.type.const as string,
			},
	);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState('');
	const schema = schemas.find((s) => s.properties.type.const === data.type);
	return (
		<form
			className="space-y-5"
			onSubmit={async (e) => {
				e.preventDefault();
				setBusy(true);
				setError('');
				try {
					const path = `/agent-management/resources/credential${id ? `/${id}` : ''}`;
					if (id)
						await client.patch(path, { data }, { owner_id: owner }, { silent: true });
					else await client.post(path, { data }, { owner_id: owner }, { silent: true });
					toast.success('AI 资源已保存');
					onClose();
				} catch (err) {
					setError((err as Error).message);
				} finally {
					setBusy(false);
				}
			}}
		>
			<fieldset disabled={busy} className="space-y-5">
				<Field label="提供商">
					<SelectField
						disabled={!!id}
						value={data.type as string}
						onValueChange={(nextValue) => {
							const next = schemas.find(
								(s) => s.properties.type.const === nextValue,
							)!;
							setData({ ...defaultValuesFromSchema(next), type: nextValue });
						}}
					>
						{schemas.map((s) => (
							<SelectOption
								key={s.properties.type.const as string}
								value={s.properties.type.const as string}
							>
								{s.title}
							</SelectOption>
						))}
					</SelectField>
				</Field>
				{schema && (
					<SchemaForm
						schema={schema}
						values={data}
						onChange={(key, value) => setData({ ...data, [key]: value })}
						idPrefix="managed-credential"
					/>
				)}
				{error && (
					<p role="alert" className="text-destructive text-sm">
						{error}
					</p>
				)}
				<Button type="submit" disabled={busy}>
					{busy ? '正在保存…' : '保存 AI 资源'}
				</Button>
			</fieldset>
		</form>
	);
}
