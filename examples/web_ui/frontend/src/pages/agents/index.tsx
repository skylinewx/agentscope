import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
	Bot,
	Copy,
	Pencil,
	Plus,
	Search,
	Trash2,
	MessageSquare,
	ArrowLeft,
	Save,
	Loader2,
} from 'lucide-react';
import { useCallback, useRef, useState } from 'react';
import {
	useBeforeUnload,
	useBlocker,
	useNavigate,
	useParams,
	useSearchParams,
} from 'react-router-dom';
import { toast } from 'sonner';

import { CopyAgentDialog } from './CopyAgentDialog';
import { Check, Field, JsonField, ModelField } from './fields';
import { MemoryFiles } from './MemoryFiles';
import { CredentialEditor, ResourceBindings } from './Resources';
import { TemplateUpdateDialog } from './TemplateUpdateDialog';
import {
	managementApi,
	type AgentDefaults,
	type AgentDraft,
	type AgentSession,
	type ManagedAgent,
	type MemoryConfig,
	type Owner,
	type ResourceCatalog,
} from '@/api/agent-management';
import type { ContextConfig, InviteConfig, PermissionMode, ReActConfig } from '@/api/types';
import {
	AgentFormFields,
	defaultAgentFormValues,
	type AgentFormValues,
} from '@/components/form/AgentFormFields';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Disclosure } from '@/components/ui/disclosure';
import {
	Empty,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
	EmptyDescription,
} from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { SelectField, SelectOption } from '@/components/ui/select-field';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useAuth } from '@/context/AuthContext';
import { useAgentSchema } from '@/hooks/useAgentSchema';
import { notifyResourceChanged } from '@/lib/resource-events';

const emptyDefaults = (): AgentDefaults => ({
	chat_model_config: null,
	fallback_chat_model_config: null,
	tts_model_config: null,
	realtime_model_config: null,
	knowledge_config: null,
	permission_mode: 'default',
});
const emptyMemory = (): MemoryConfig => ({
	enabled: true,
	memory_max_tokens: 4000,
	retrieval_async: true,
	retrieval_max_tokens_per_md: 2000,
	retrieval_max_files: 200,
	retrieval_max_tokens_per_frontmatter: 256,
	memory_instructions: null,
	retrieval_instructions: null,
	retrieval_model_config: null,
});
const agentPath = (agent: ManagedAgent) =>
	`/agents/${agent.id}?owner=${encodeURIComponent(agent.user_id)}`;
const ownerName = (users: Owner[], id: string) => {
	const u = users.find((u) => u.id === id);
	return u ? `${u.display_name || u.username}${u.display_name ? `（${u.username}）` : ''}` : id;
};

export function AgentsPage() {
	const auth = useAuth();
	const navigate = useNavigate();
	const cache = useQueryClient();
	const listing = useQuery({
		queryKey: ['managed-agents', auth?.user.id],
		queryFn: managementApi.list,
	});
	const [search, setSearch] = useState('');
	const [owner, setOwner] = useState('');
	const [copy, setCopy] = useState<ManagedAgent | null>(null);
	const [remove, setRemove] = useState<ManagedAgent | null>(null);
	const [deleting, setDeleting] = useState(false);
	const users = listing.data?.users ?? [];
	const agents = (listing.data?.agents ?? []).filter(
		(a) =>
			(!owner || a.user_id === owner) &&
			`${a.data.name} ${a.data.description || ''} ${ownerName(users, a.user_id)}`
				.toLocaleLowerCase()
				.includes(search.toLocaleLowerCase()),
	);
	return (
		<main className="h-full overflow-auto p-5 md:p-8">
			<div className="mx-auto max-w-7xl space-y-7">
				<header className="flex flex-wrap items-start justify-between gap-4">
					<div>
						<div className="flex items-center gap-3">
							<span className="rounded-xl bg-primary/10 p-2.5 text-primary">
								<Bot className="size-6" />
							</span>
							<h1 className="text-2xl font-semibold tracking-tight">智能体管理</h1>
						</div>
						<p className="mt-3 text-sm text-muted-foreground">
							{listing.data?.admin
								? '管理所有用户的智能体、能力配置与长期记忆。'
								: '配置你的智能体，组合资源，创建独立副本。'}
						</p>
					</div>
					<Button
						onClick={() =>
							navigate(
								`/agents/new${owner ? `?owner=${encodeURIComponent(owner)}` : ''}`,
							)
						}
					>
						<Plus className="size-4" />
						新建智能体
					</Button>
				</header>
				<div className="flex flex-wrap items-center gap-3">
					<div className="relative min-w-0 flex-1 max-w-md">
						<Search className="pointer-events-none absolute left-3 top-3 size-4 text-muted-foreground" />
						<Input
							aria-label="搜索智能体"
							className="h-10 pl-9"
							placeholder="搜索名称、简介或所属用户"
							value={search}
							onChange={(e) => setSearch(e.target.value)}
						/>
					</div>
					{listing.data?.admin && (
						<SelectField
							aria-label="按所属用户筛选"
							className="!w-auto max-w-full"
							value={owner}
							onValueChange={(nextValue) => setOwner(nextValue)}
						>
							<SelectOption value="">所有用户</SelectOption>
							{users.map((u) => (
								<SelectOption key={u.id} value={u.id}>
									{ownerName(users, u.id)}
								</SelectOption>
							))}
						</SelectField>
					)}
					<span className="text-xs text-muted-foreground">
						共 {agents.length} 个智能体
					</span>
				</div>
				{listing.isPending && <p role="status">正在加载智能体…</p>}
				{listing.error && (
					<p role="alert" className="text-destructive">
						{listing.error.message}
					</p>
				)}
				{listing.data && !agents.length && (
					<Empty className="rounded-2xl border border-dashed bg-card p-12">
						<EmptyHeader>
							<EmptyMedia variant="icon">
								<Bot />
							</EmptyMedia>
							<EmptyTitle>
								{search || owner ? '没有符合条件的智能体' : '创建你的第一个智能体'}
							</EmptyTitle>
							<EmptyDescription>
								从提示词开始，逐步添加模型、技能、工具和记忆。
							</EmptyDescription>
						</EmptyHeader>
					</Empty>
				)}
				<div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
					{agents.map((agent) => (
						<article
							key={`${agent.user_id}:${agent.id}`}
							className="flex min-w-0 flex-col rounded-2xl border bg-card p-5 shadow-sm transition-shadow hover:shadow-md"
						>
							<div className="flex items-start justify-between gap-3">
								<Button
									variant="ghost"
									type="button"
									onClick={() => navigate(agentPath(agent))}
									className="h-auto min-w-0 flex-1 flex-col items-start gap-0 p-0 text-left whitespace-normal"
								>
									<h2 className="font-semibold text-lg break-words hover:text-primary">
										{agent.data.name}
									</h2>
									<p className="mt-1 text-xs text-muted-foreground">
										{ownerName(users, agent.user_id)}
									</p>
								</Button>
								<Badge
									variant={
										agent.data.defaults?.chat_model_config
											? 'secondary'
											: 'outline'
									}
								>
									{agent.data.defaults?.chat_model_config
										? '已配置'
										: agent.data.defaults
											? '待配置模型'
											: '待设默认值'}
								</Badge>
							</div>
							<p className="mt-4 min-h-10 line-clamp-2 text-sm leading-5 text-muted-foreground">
								{agent.data.description || '尚未填写简介'}
							</p>
							<div className="mt-5 rounded-xl bg-muted/40 p-3">
								<p className="truncate text-sm font-medium">
									{agent.data.defaults?.chat_model_config?.model ||
										'未设置默认模型'}
								</p>
								<div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
									<span>
										Skill{' '}
										{Object.keys(agent.data.resource_config.skills).length}
									</span>
									<span>
										MCP {Object.keys(agent.data.resource_config.mcps).length}
									</span>
									<span>
										记忆{' '}
										{agent.data.memory_config?.enabled === false
											? '关闭'
											: '开启'}
									</span>
								</div>
							</div>
							<div className="mt-5 flex flex-wrap items-center gap-1 border-t pt-3">
								<Button
									variant="ghost"
									size="sm"
									onClick={() => navigate(agentPath(agent))}
								>
									<Pencil className="size-3.5" />
									配置
								</Button>
								<Button variant="ghost" size="sm" onClick={() => setCopy(agent)}>
									<Copy className="size-3.5" />
									复制
								</Button>
								{agent.user_id === auth?.user.id && (
									<Button
										variant="ghost"
										size="sm"
										onClick={() => navigate(`/chat/${agent.id}`)}
									>
										<MessageSquare className="size-3.5" />
										对话
									</Button>
								)}
								<Button
									variant="ghost"
									size="icon-sm"
									className="ml-auto text-muted-foreground hover:text-destructive"
									aria-label={`删除 ${agent.data.name}`}
									onClick={() => setRemove(agent)}
								>
									<Trash2 className="size-4" />
								</Button>
							</div>
						</article>
					))}
				</div>
				{copy && (
					<CopyAgentDialog
						agent={copy}
						users={users}
						admin={listing.data?.admin ?? false}
						onClose={() => setCopy(null)}
						onCopied={(agent) => {
							setCopy(null);
							void cache.invalidateQueries({ queryKey: ['managed-agents'] });
							navigate(agentPath(agent));
							toast.success('智能体副本已创建');
						}}
					/>
				)}
				<Dialog
					open={!!remove}
					onOpenChange={(open) => {
						if (!open && !deleting) setRemove(null);
					}}
				>
					<DialogContent>
						<DialogHeader>
							<DialogTitle>删除「{remove?.data.name}」？</DialogTitle>
							<DialogDescription>
								所属用户：{remove ? ownerName(users, remove.user_id) : ''}
								。此操作会删除智能体及其会话和相关定时任务。公共 Skill、MCP 和 AI
								资源保留在所属用户资源库中。
							</DialogDescription>
						</DialogHeader>
						<Button
							variant="destructive"
							disabled={deleting}
							onClick={async () => {
								if (!remove) return;
								setDeleting(true);
								try {
									await managementApi.delete(remove.user_id, remove.id);
									setRemove(null);
									await listing.refetch();
									toast.success('智能体已删除');
								} catch {
									/* API toast. */
								} finally {
									setDeleting(false);
								}
							}}
						>
							{deleting ? '正在删除…' : '确认删除'}
						</Button>
					</DialogContent>
				</Dialog>
			</div>
		</main>
	);
}

export function AgentEditorPage() {
	const { agentId } = useParams();
	const [params] = useSearchParams();
	const auth = useAuth();
	const navigate = useNavigate();
	const cache = useQueryClient();
	const fresh = agentId === 'new';
	const owner = params.get('owner') || auth!.user.id;
	const listing = useQuery({
		queryKey: ['managed-agents', auth?.user.id],
		queryFn: managementApi.list,
		refetchOnWindowFocus: false,
	});
	const detail = useQuery({
		queryKey: ['managed-agent', owner, agentId],
		enabled: !fresh && !!agentId,
		queryFn: () => managementApi.detail(owner, agentId!),
		refetchOnWindowFocus: false,
	});
	const resources = useQuery({
		queryKey: ['managed-resources', owner],
		queryFn: () => managementApi.resources(owner),
		refetchOnWindowFocus: false,
	});
	const { schema } = useAgentSchema();
	const problem = detail.error || resources.error || listing.error;
	if (problem)
		return (
			<main className="p-8">
				<Button variant="ghost" onClick={() => navigate('/agents')}>
					<ArrowLeft className="size-4" />
					返回
				</Button>
				<p role="alert" className="mt-4 text-destructive">
					{problem.message}
				</p>
			</main>
		);
	if (!schema || !resources.data || !listing.data || (!fresh && !detail.data))
		return (
			<main className="p-8" role="status">
				正在加载智能体配置…
			</main>
		);
	return (
		<AgentEditor
			key={`${owner}:${agentId}:${detail.data?.agent.revision || 'new'}`}
			owner={owner}
			agent={fresh ? undefined : detail.data?.agent}
			sessions={detail.data?.sessions ?? []}
			users={listing.data.users}
			admin={listing.data.admin}
			catalog={resources.data}
			schema={schema}
			refresh={async () => {
				const result = await resources.refetch();
				if (!result.data) throw result.error;
				return result.data;
			}}
			onSaved={(agent) => {
				cache.setQueryData(['managed-agent', owner, agent.id], {
					agent,
					sessions: detail.data?.sessions ?? [],
				});
				void cache.invalidateQueries({ queryKey: ['managed-agents'] });
				notifyResourceChanged();
				if (fresh) navigate(agentPath(agent), { replace: true });
			}}
		/>
	);
}

const tabs = [
	['basic', '基础信息'],
	['ai', 'AI 资源'],
	['skill', 'Skill'],
	['mcp', 'MCP'],
	['knowledge', '知识库'],
	['memory', '记忆'],
	['behavior', '运行配置'],
	['workspace', '工作空间'],
] as const;
type Tab = (typeof tabs)[number][0];

function AgentEditor({
	owner,
	agent,
	sessions,
	users,
	admin,
	catalog,
	schema,
	refresh,
	onSaved,
}: {
	owner: string;
	agent?: ManagedAgent;
	sessions: AgentSession[];
	users: Owner[];
	admin: boolean;
	catalog: ResourceCatalog;
	schema: NonNullable<ReturnType<typeof useAgentSchema>['schema']>;
	refresh: () => Promise<ResourceCatalog>;
	onSaved: (agent: ManagedAgent) => void;
}) {
	const navigate = useNavigate();
	const auth = useAuth();
	const [draft, setDraft] = useState<AgentDraft>(() => {
		const base = defaultAgentFormValues(schema);
		return {
			name: agent?.data.name ?? '',
			description: agent?.data.description ?? '',
			system_prompt: agent?.data.system_prompt ?? "You're a helpful assistant.",
			chat_config: agent?.data.chat_config ?? {
				context_config: base.context_config as ContextConfig,
				react_config: base.react_config as ReActConfig,
				invite_config: base.invite_config as InviteConfig,
			},
			resource_config: agent?.data.resource_config ?? { mcps: {}, skills: {} },
			defaults: agent?.data.defaults ?? emptyDefaults(),
			memory_config: agent?.data.memory_config ?? emptyMemory(),
			revision: agent?.revision,
		};
	});
	const [tab, setTab] = useState<Tab>('basic');
	const [dirty, setDirty] = useState(false);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState('');
	const [credentialEditor, setCredentialEditor] = useState<{ id: string | null } | null>(null);
	const [workspace, setWorkspace] = useState(
		agent?.data.workspace_id ||
			(new Set(sessions.map((s) => s.workspace_id)).size === 1
				? sessions[0]?.workspace_id
				: '') ||
			'',
	);
	const [copy, setCopy] = useState(false);
	const [templateUpdate, setTemplateUpdate] = useState(false);
	const templateTrigger = useRef<HTMLButtonElement>(null);
	const allowNavigate = useRef(false);
	const blocker = useBlocker(() => dirty && !allowNavigate.current);
	useBeforeUnload(
		useCallback(
			(event: BeforeUnloadEvent) => {
				if (dirty) {
					event.preventDefault();
					event.returnValue = '';
				}
			},
			[dirty],
		),
	);
	const change = <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) => {
		setDraft((v) => ({ ...v, [key]: value }));
		setDirty(true);
		setError('');
	};
	const defaults = (key: keyof AgentDefaults, value: AgentDefaults[typeof key]) =>
		change('defaults', { ...draft.defaults, [key]: value });
	const memory = (key: keyof MemoryConfig, value: MemoryConfig[typeof key]) =>
		change('memory_config', { ...draft.memory_config, [key]: value });
	const behaviorValues = { identity: {}, ...draft.chat_config } as unknown as AgentFormValues;
	const workspaces = [
		...new Set(
			[agent?.data.workspace_id, ...sessions.map((s) => s.workspace_id)].filter(
				(id): id is string => !!id,
			),
		),
	];
	return (
		<main className="h-full overflow-auto">
			{agent && templateUpdate && (
				<TemplateUpdateDialog
					agent={agent}
					onClose={() => setTemplateUpdate(false)}
					onReturnFocus={() => templateTrigger.current?.focus()}
					onUpdated={(saved) => {
						setTemplateUpdate(false);
						onSaved(saved);
						void refresh();
						toast.success('智能体已更新，历史记录和个人文件已保留');
					}}
				/>
			)}
			<form
				onInput={(e) => {
					if (e.currentTarget.contains(e.target as Node)) setDirty(true);
				}}
				noValidate
				className="mx-auto max-w-6xl p-5 pb-0 md:px-8 md:pt-8"
				onSubmit={async (e) => {
					e.preventDefault();
					e.stopPropagation();
					const form = e.currentTarget;
					const invalid = form.querySelector<
						HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
					>('input:invalid, textarea:invalid, select:invalid');
					if (invalid) {
						setTab(
							(invalid
								.closest('[data-agent-tab]')
								?.getAttribute('data-agent-tab') as Tab) || 'basic',
						);
						// Reveal a retained draft inside a collapsed section before focusing it.
						for (
							let parent = invalid.parentElement;
							parent && parent !== form;
							parent = parent.parentElement
						) {
							if (parent.matches('[data-slot="collapsible"][data-state="closed"]')) {
								parent
									.querySelector<HTMLButtonElement>(
										':scope > [data-slot="collapsible-trigger"]',
									)
									?.click();
							}
						}
						setTimeout(() => {
							invalid.focus();
							invalid.reportValidity();
						}, 0);
						return;
					}
					setBusy(true);
					setError('');
					try {
						const saved = await managementApi.save(owner, draft, agent?.id);
						setDirty(false);
						allowNavigate.current = true;
						onSaved(saved);
						toast.success('智能体配置已保存');
					} catch (err) {
						setError((err as Error).message);
					} finally {
						setBusy(false);
					}
				}}
			>
				<header className="mb-6 space-y-4">
					<Button
						type="button"
						variant="ghost"
						size="sm"
						className="-ml-3"
						onClick={() => navigate('/agents')}
					>
						<ArrowLeft className="size-4" />
						智能体管理
					</Button>
					<div className="flex flex-wrap items-start justify-between gap-4">
						<div>
							<h1 className="text-2xl font-semibold">
								{agent ? agent.data.name : '新建智能体'}
							</h1>
							<p className="mt-2 text-sm text-muted-foreground">
								所属用户：{ownerName(users, owner)} ·{' '}
								{agent ? '配置、资源与记忆' : '先定义角色，再组合智能体能力'}
							</p>
						</div>
						{agent && (
							<div className="flex gap-2">
								{agent.template && (
									<Button
										ref={templateTrigger}
										type="button"
										variant="outline"
										disabled={dirty || busy}
										tooltip={dirty ? '请先保存配置再检查更新' : ''}
										onClick={() => setTemplateUpdate(true)}
									>
										检查更新
									</Button>
								)}
								<Button
									type="button"
									variant="outline"
									disabled={dirty}
									tooltip={dirty ? '请先保存配置再复制' : ''}
									onClick={() => setCopy(true)}
								>
									<Copy className="size-4" />
									复制
								</Button>
								{owner === auth?.user.id && (
									<Button
										type="button"
										variant="outline"
										onClick={() => navigate(`/chat/${agent.id}`)}
									>
										<MessageSquare className="size-4" />
										对话
									</Button>
								)}
							</div>
						)}
					</div>
				</header>
				{!agent && admin && (
					<div className="mb-6 max-w-sm">
						<Field label="所属用户">
							<SelectField
								value={owner}
								onValueChange={(nextValue) =>
									navigate(`/agents/new?owner=${encodeURIComponent(nextValue)}`)
								}
							>
								{users
									.filter((u) => u.status === 'active')
									.map((u) => (
										<SelectOption value={u.id} key={u.id}>
											{ownerName(users, u.id)}
										</SelectOption>
									))}
							</SelectField>
						</Field>
					</div>
				)}
				<Tabs value={tab} onValueChange={(next) => setTab(next as Tab)} className="min-w-0">
					<TabsList
						aria-label="智能体配置分组"
						variant="line"
						className="sticky top-0 z-10 mb-6 w-full justify-start overflow-x-auto overflow-y-hidden rounded-none border-b bg-canvas/95 py-2 backdrop-blur group-data-horizontal/tabs:h-auto"
					>
						{tabs.map(([key, label]) => (
							<TabsTrigger
								key={key}
								value={key}
								className="h-auto shrink-0 flex-none px-3 py-2"
							>
								{label}
							</TabsTrigger>
						))}
					</TabsList>
					<fieldset disabled={busy} className="min-w-0">
						<TabsContent
							forceMount
							value="basic"
							data-agent-tab="basic"
							hidden={tab !== 'basic'}
							className="space-y-6"
						>
							<section className="space-y-5 rounded-2xl border bg-card p-5 md:p-7">
								<Field label="名称">
									<Input
										autoFocus={!agent}
										aria-label="智能体名称"
										value={draft.name}
										maxLength={100}
										required
										onChange={(e) => change('name', e.target.value)}
									/>
								</Field>
								<Field label="简介">
									<Textarea
										aria-label="智能体简介"
										rows={2}
										maxLength={2000}
										value={draft.description}
										placeholder="这个智能体能帮助用户完成什么？"
										onChange={(e) => change('description', e.target.value)}
									/>
								</Field>
								<Field
									label="系统提示词"
									hint="定义角色、工作流程、输出格式和约束。"
								>
									<Textarea
										aria-label="系统提示词"
										className="min-h-64 leading-6"
										value={draft.system_prompt}
										onChange={(e) => change('system_prompt', e.target.value)}
									/>
								</Field>
							</section>
							{!!sessions.length && (
								<section className="rounded-xl border p-5 space-y-3">
									<h2 className="font-medium">从已有会话导入默认配置</h2>
									<p className="text-sm text-muted-foreground">
										选择模型、知识库和权限配置的来源。导入后需要保存，原会话配置保留。
									</p>
									<SelectField
										aria-label="导入会话配置"
										value=""
										onValueChange={(nextValue) => {
											const session = sessions.find(
												(s) => s.id === nextValue,
											);
											if (session) {
												change('defaults', session.defaults);
												toast.success('配置已导入，请检查并保存');
											}
										}}
									>
										<SelectOption value="">选择一个会话…</SelectOption>
										{sessions.map((s) => (
											<SelectOption key={s.id} value={s.id}>
												{s.name}
											</SelectOption>
										))}
									</SelectField>
								</section>
							)}
						</TabsContent>
						<TabsContent
							forceMount
							value="ai"
							data-agent-tab="ai"
							hidden={tab !== 'ai'}
							className="space-y-5"
						>
							<div className="flex flex-wrap items-center justify-between gap-3">
								<div>
									<h2 className="font-semibold text-lg">默认模型</h2>
									<p className="mt-2 text-sm text-muted-foreground">
										新会话继承这些配置。会话中单独选择模型后，使用会话自定义值。
									</p>
								</div>
								<Button
									type="button"
									variant="outline"
									onClick={() => setCredentialEditor({ id: null })}
								>
									<Plus className="size-4" />
									新建 AI 资源
								</Button>
							</div>
							<div className="grid gap-5 lg:grid-cols-2">
								<ModelField
									label="主模型"
									value={draft.defaults.chat_model_config}
									onChange={(v) => defaults('chat_model_config', v)}
									catalog={catalog}
								/>
								<ModelField
									label="备用模型"
									value={draft.defaults.fallback_chat_model_config}
									onChange={(v) => defaults('fallback_chat_model_config', v)}
									catalog={catalog}
								/>
								<ModelField
									label="语音合成模型"
									kind="tts"
									value={draft.defaults.tts_model_config}
									onChange={(v) => defaults('tts_model_config', v)}
									catalog={catalog}
								/>
								<ModelField
									label="实时语音模型"
									kind="realtime"
									value={draft.defaults.realtime_model_config}
									onChange={(v) => defaults('realtime_model_config', v)}
									catalog={catalog}
								/>
							</div>
							<section className="rounded-xl border p-5">
								<h3 className="font-medium mb-3">所属用户的 AI 资源</h3>
								{catalog.credentials.map((c) => (
									<div
										className="flex justify-between items-center gap-3 border-b last:border-0 py-2"
										key={c.id}
									>
										<span className="text-sm">
											{c.name}
											<span className="ml-2 text-xs text-muted-foreground">
												{c.type}
											</span>
										</span>
										<Button
											type="button"
											variant="ghost"
											size="sm"
											disabled={!c.editable}
											onClick={() => setCredentialEditor({ id: c.id })}
										>
											编辑
										</Button>
									</div>
								))}
								{!catalog.credentials.length && (
									<p className="text-sm text-muted-foreground">
										暂无 AI 资源，请先添加提供商凭据。
									</p>
								)}
							</section>
						</TabsContent>
						<TabsContent
							forceMount
							value="skill"
							data-agent-tab="skill"
							hidden={tab !== 'skill'}
						>
							<ResourceBindings
								owner={owner}
								kind="skill"
								catalog={catalog}
								value={draft.resource_config}
								onChange={(v) => change('resource_config', v)}
								refresh={refresh}
							/>
						</TabsContent>
						<TabsContent
							forceMount
							value="mcp"
							data-agent-tab="mcp"
							hidden={tab !== 'mcp'}
						>
							<ResourceBindings
								owner={owner}
								kind="mcp"
								catalog={catalog}
								value={draft.resource_config}
								onChange={(v) => change('resource_config', v)}
								refresh={refresh}
							/>
						</TabsContent>
						<TabsContent
							forceMount
							value="knowledge"
							data-agent-tab="knowledge"
							hidden={tab !== 'knowledge'}
							className="space-y-5"
						>
							<h2 className="font-semibold text-lg">知识库关联与检索</h2>
							<section className="space-y-4 rounded-xl border bg-card p-5">
								{[
									...new Set([
										...(draft.defaults.knowledge_config?.knowledge_base_ids ??
											[]),
										...catalog.knowledge_bases.map((k) => k.id),
									]),
								].map((id) => {
									const kb = catalog.knowledge_bases.find((k) => k.id === id);
									const config = draft.defaults.knowledge_config ?? {
										knowledge_base_ids: [],
										parameters: {},
									};
									return (
										<Check
											key={id}
											label={kb?.name || `知识库不可用 · ${id}`}
											checked={config.knowledge_base_ids.includes(id)}
											onChange={(yes) =>
												defaults('knowledge_config', {
													...config,
													knowledge_base_ids: yes
														? [...config.knowledge_base_ids, id]
														: config.knowledge_base_ids.filter(
																(k) => k !== id,
															),
												})
											}
										/>
									);
								})}
								{!catalog.knowledge_bases.length && (
									<p className="text-sm text-muted-foreground">
										所属用户暂无知识库，可在知识库页面创建。
									</p>
								)}
							</section>
							<JsonField
								label="检索参数"
								value={draft.defaults.knowledge_config?.parameters ?? {}}
								onChange={(parameters) =>
									defaults('knowledge_config', {
										knowledge_base_ids:
											draft.defaults.knowledge_config?.knowledge_base_ids ??
											[],
										parameters,
									})
								}
							/>
						</TabsContent>
						<TabsContent
							forceMount
							value="memory"
							data-agent-tab="memory"
							hidden={tab !== 'memory'}
							className="space-y-5"
						>
							<section className="space-y-5 rounded-xl border bg-card p-5">
								<h2 className="text-lg font-semibold">记忆策略</h2>
								<Check
									label="启用长期记忆"
									checked={draft.memory_config.enabled}
									onChange={(v) => memory('enabled', v)}
								/>
								<Check
									label="异步检索相关记忆"
									checked={draft.memory_config.retrieval_async}
									onChange={(v) => memory('retrieval_async', v)}
								/>
								<div className="grid gap-4 sm:grid-cols-2">
									{(
										[
											['memory_max_tokens', '记忆索引最大 Token', 256, 64000],
											[
												'retrieval_max_tokens_per_md',
												'单文件读取最大 Token',
												128,
												32000,
											],
											['retrieval_max_files', '检索文件数量上限', 1, 2000],
											[
												'retrieval_max_tokens_per_frontmatter',
												'文件摘要最大 Token',
												32,
												4096,
											],
										] as const
									).map(([key, label, min, max]) => (
										<Field key={key} label={label}>
											<Input
												type="number"
												required
												min={min}
												max={max}
												value={draft.memory_config[key]}
												onChange={(e) =>
													memory(key, e.target.valueAsNumber)
												}
											/>
										</Field>
									))}
								</div>
								<Disclosure
									summary={<>高级记忆提示词</>}
									summaryClassName="text-sm cursor-pointer"
								>
									<div className="mt-4 space-y-4">
										<Field label="记忆写入指令" hint="留空使用系统默认指令。">
											<Textarea
												rows={4}
												value={
													draft.memory_config.memory_instructions ?? ''
												}
												onChange={(e) =>
													memory(
														'memory_instructions',
														e.target.value || null,
													)
												}
											/>
										</Field>
										<Field label="记忆检索指令">
											<Textarea
												rows={4}
												value={
													draft.memory_config.retrieval_instructions ?? ''
												}
												onChange={(e) =>
													memory(
														'retrieval_instructions',
														e.target.value || null,
													)
												}
											/>
										</Field>
									</div>
								</Disclosure>
							</section>
							<ModelField
								label="记忆检索模型（不配置时使用会话主模型）"
								value={draft.memory_config.retrieval_model_config}
								onChange={(v) => memory('retrieval_model_config', v)}
								catalog={catalog}
							/>
							{agent ? (
								<>
									<WorkspaceSelect
										value={workspace}
										onChange={setWorkspace}
										ids={workspaces}
										sessions={sessions}
									/>
									<MemoryFiles
										key={workspace}
										agent={agent}
										workspace={workspace}
									/>
								</>
							) : (
								<p className="text-sm text-muted-foreground">
									保存智能体后即可管理记忆内容。
								</p>
							)}
						</TabsContent>
						<TabsContent
							forceMount
							value="behavior"
							data-agent-tab="behavior"
							hidden={tab !== 'behavior'}
							className="space-y-6 rounded-xl border bg-card p-5"
						>
							<Field label="默认工具权限">
								<SelectField
									value={draft.defaults.permission_mode}
									onValueChange={(nextValue) =>
										defaults('permission_mode', nextValue as PermissionMode)
									}
								>
									<SelectOption value="default">默认 · 按需询问</SelectOption>
									<SelectOption value="accept_edits">
										允许工作目录内的编辑
									</SelectOption>
									<SelectOption value="explore">探索 · 只读</SelectOption>
									<SelectOption value="dont_ask">
										无人值守 · 拒绝需要询问的操作
									</SelectOption>
									<SelectOption value="bypass">跳过常规审批</SelectOption>
								</SelectField>
							</Field>
							<AgentFormFields
								schema={schema}
								sections={['context_config', 'react_config', 'invite_config']}
								values={behaviorValues}
								onChange={(section, key, value) => {
									if (section !== 'identity')
										change('chat_config', {
											...draft.chat_config,
											[section]: {
												...draft.chat_config[section],
												[key]: value,
											},
										});
								}}
							/>
						</TabsContent>
						<TabsContent
							forceMount
							value="workspace"
							data-agent-tab="workspace"
							hidden={tab !== 'workspace'}
							className="space-y-5"
						>
							<h2 className="font-semibold text-lg">工作空间与会话</h2>
							<WorkspaceSelect
								value={workspace}
								onChange={setWorkspace}
								ids={workspaces}
								sessions={sessions}
							/>
							{agent ? (
								<WorkspaceFiles
									agent={agent}
									workspace={workspace}
									active={tab === 'workspace'}
								/>
							) : (
								<p className="text-sm text-muted-foreground">
									保存后自动创建独立工作空间。
								</p>
							)}
							<section className="rounded-xl border p-5">
								<h3 className="font-medium mb-3">关联会话（{sessions.length}）</h3>
								{sessions.map((s) => (
									<div
										className="flex items-center justify-between border-b last:border-0 py-3 text-sm"
										key={s.id}
									>
										<span>{s.name}</span>
										{owner === auth?.user.id && (
											<Button
												type="button"
												variant="ghost"
												size="sm"
												onClick={() =>
													navigate(`/chat/${agent!.id}/${s.id}`)
												}
											>
												打开会话
											</Button>
										)}
									</div>
								))}
							</section>
						</TabsContent>
					</fieldset>
				</Tabs>
				<footer className="sticky bottom-0 z-20 mt-8 flex flex-wrap items-center justify-between gap-3 border-t bg-canvas/95 py-4 backdrop-blur">
					<div className="min-w-0 text-sm">
						{error ? (
							<p
								role="alert"
								className="max-w-2xl whitespace-pre-wrap break-words text-destructive"
							>
								{error}
							</p>
						) : (
							<span className="text-muted-foreground">
								{dirty
									? '有未保存的更改'
									: agent
										? '配置已保存'
										: '完成后保存智能体'}
							</span>
						)}
					</div>
					<Button type="submit" disabled={busy || !draft.name.trim()}>
						{busy ? (
							<Loader2 className="size-4 animate-spin" />
						) : (
							<Save className="size-4" />
						)}
						{busy ? '正在保存…' : '保存配置'}
					</Button>
				</footer>
			</form>
			{credentialEditor && (
				<CredentialEditor
					key={credentialEditor.id ?? 'new'}
					owner={owner}
					id={credentialEditor.id}
					onClose={() => {
						setCredentialEditor(null);
						void refresh().catch(() => {});
					}}
					onCopied={(newId) => {
						const oldId = credentialEditor.id;
						const next = { ...draft.defaults };
						for (const key of [
							'chat_model_config',
							'fallback_chat_model_config',
							'tts_model_config',
							'realtime_model_config',
						] as const) {
							if (next[key]?.credential_id === oldId)
								next[key] = { ...next[key]!, credential_id: newId };
						}
						change('defaults', next);
						if (draft.memory_config.retrieval_model_config?.credential_id === oldId)
							memory('retrieval_model_config', {
								...draft.memory_config.retrieval_model_config,
								credential_id: newId,
							});
						setCredentialEditor({ id: newId });
						void refresh().catch(() => {});
					}}
				/>
			)}
			{agent && copy && (
				<CopyAgentDialog
					agent={agent}
					users={users}
					admin={admin}
					onClose={() => setCopy(false)}
					onCopied={(newAgent) => {
						setCopy(false);
						navigate(agentPath(newAgent));
					}}
				/>
			)}
			<Dialog
				open={blocker.state === 'blocked'}
				onOpenChange={(open) => {
					if (!open && blocker.state === 'blocked') blocker.reset();
				}}
			>
				<DialogContent>
					<DialogHeader>
						<DialogTitle>还有未保存的配置</DialogTitle>
						<DialogDescription>
							离开页面会丢弃这次对智能体配置的修改。已单独保存的资源和记忆不受影响。
						</DialogDescription>
					</DialogHeader>
					<div className="flex justify-end gap-3">
						<Button
							variant="outline"
							onClick={() => blocker.state === 'blocked' && blocker.reset()}
						>
							继续编辑
						</Button>
						<Button
							variant="destructive"
							onClick={() => blocker.state === 'blocked' && blocker.proceed()}
						>
							放弃修改并离开
						</Button>
					</div>
				</DialogContent>
			</Dialog>
		</main>
	);
}

function WorkspaceSelect({
	value,
	onChange,
	ids,
	sessions,
}: {
	value: string;
	onChange: (value: string) => void;
	ids: string[];
	sessions: AgentSession[];
}) {
	if (ids.length < 2) return null;
	return (
		<Field label="选择工作空间">
			<SelectField value={value} onValueChange={(nextValue) => onChange(nextValue)}>
				<SelectOption value="">请选择工作空间</SelectOption>
				{ids.map((id) => (
					<SelectOption value={id} key={id}>
						{sessions.find((s) => s.workspace_id === id)?.name || '默认工作空间'} ·{' '}
						{id.slice(0, 8)}
					</SelectOption>
				))}
			</SelectField>
		</Field>
	);
}
function WorkspaceFiles({
	agent,
	workspace,
	active,
}: {
	agent: ManagedAgent;
	workspace: string;
	active: boolean;
}) {
	const files = useQuery({
		queryKey: ['agent-workspace-files', agent.user_id, agent.id, workspace],
		queryFn: () => managementApi.files(agent.user_id, agent.id, workspace, false),
		enabled: active,
		refetchOnWindowFocus: false,
	});
	return (
		<section className="rounded-xl border p-5">
			<h3 className="font-medium mb-3">业务文件</h3>
			{files.error && (
				<p role="alert" className="text-sm text-destructive">
					{files.error.message}
				</p>
			)}
			{files.data?.files.length ? (
				<div className="divide-y">
					{files.data.files.map((f) => (
						<div className="flex min-w-0 gap-4 py-2 text-sm" key={f.path}>
							<span className="flex-1 break-all">{f.path}</span>
							<span className="text-muted-foreground shrink-0">
								{Math.ceil(f.size / 1024)} KB
							</span>
						</div>
					))}
				</div>
			) : (
				!files.error && (
					<p className="text-sm text-muted-foreground">
						{files.isPending ? '正在加载…' : '暂无业务文件'}
					</p>
				)
			)}
		</section>
	);
}
