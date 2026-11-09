import { client } from './client';
import type {
	AgentData,
	AgentView,
	ChatModelConfig,
	PermissionMode,
	SessionKnowledgeConfig,
	SkillView,
	MCPView,
} from './types';

export interface AgentDefaults {
	chat_model_config: ChatModelConfig | null;
	fallback_chat_model_config: ChatModelConfig | null;
	tts_model_config: ChatModelConfig | null;
	realtime_model_config: ChatModelConfig | null;
	knowledge_config: SessionKnowledgeConfig | null;
	permission_mode: PermissionMode;
}
export interface MemoryConfig {
	enabled: boolean;
	memory_max_tokens: number;
	retrieval_async: boolean;
	retrieval_max_tokens_per_md: number;
	retrieval_max_files: number;
	retrieval_max_tokens_per_frontmatter: number;
	memory_instructions: string | null;
	retrieval_instructions: string | null;
	retrieval_model_config: ChatModelConfig | null;
}
export interface ManagedAgent extends AgentView {
	revision: string;
	template?: {
		source_owner_id: string;
		source_agent_id: string;
		source_name: string;
		synced_at: string;
	} | null;
	data: AgentData & {
		description?: string;
		defaults?: AgentDefaults | null;
		memory_config?: MemoryConfig | null;
		workspace_id?: string | null;
	};
}
export interface Owner {
	id: string;
	username: string;
	display_name: string;
	status: string;
}
export interface AgentDraft {
	name: string;
	description: string;
	system_prompt: string;
	chat_config: AgentData['chat_config'];
	resource_config: AgentData['resource_config'];
	defaults: AgentDefaults;
	memory_config: MemoryConfig;
	revision?: string;
}
export interface AgentSession {
	id: string;
	name: string;
	workspace_id: string;
	defaults: AgentDefaults;
}
export interface ResourceCatalog {
	skills: SkillView[];
	mcps: MCPView[];
	credentials: { id: string; name: string; type: string; editable: boolean }[];
	knowledge_bases: { id: string; name: string }[];
}
export interface FileRow {
	path: string;
	size: number;
	mtime?: number;
}
export interface CopyItem {
	id: string;
	name: string;
	available: boolean;
	enabled?: boolean;
	version?: string;
}
export interface CopyPreview {
	agent: ManagedAgent;
	sessions: AgentSession[];
	defaults: AgentDefaults;
	workspace_id?: string;
	skills: CopyItem[];
	mcps: CopyItem[];
	ai: CopyItem[];
	knowledge_bases: CopyItem[];
	memory: FileRow[];
	files: FileRow[];
	file_error: string | null;
}
export interface CopyOptions {
	name: string;
	request_id: string;
	revision: string;
	target_user_id: string;
	source_session_id?: string;
	workspace_id?: string;
	skills: string[];
	mcps: string[];
	ai_fields: string[];
	knowledge_base_ids: string[];
	skill_mode: 'link' | 'copy';
	mcp_mode: 'link' | 'copy';
	ai_mode: 'link' | 'copy';
	memory: boolean;
	files: string[];
}
const scope = (owner: string, extra: Record<string, string> = {}) => ({
	owner_id: owner,
	...extra,
});
export interface TemplatePreview {
	source_name: string;
	synced_at: string;
	source_revision: string;
	target_revision: string;
	changes: {
		path: string;
		conflict: boolean;
		current: unknown;
		incoming: unknown;
		removed: boolean;
	}[];
}
export const managementApi = {
	templatePreview: (owner: string, id: string) =>
		client.get<TemplatePreview>(`/agent-management/${id}/template-preview`, scope(owner), {
			silent: true,
		}),
	templateUpdate: (owner: string, id: string, preview: TemplatePreview, takeSource: string[]) =>
		client.post<ManagedAgent>(
			`/agent-management/${id}/template-update`,
			{
				source_revision: preview.source_revision,
				target_revision: preview.target_revision,
				take_source: takeSource,
			},
			scope(owner),
			{ silent: true },
		),
	list: () =>
		client.get<{ agents: ManagedAgent[]; users: Owner[]; admin: boolean; total: number }>(
			'/agent-management',
		),
	detail: (owner: string, id: string) =>
		client.get<{ agent: ManagedAgent; sessions: AgentSession[] }>(
			`/agent-management/${id}`,
			scope(owner),
		),
	resources: (owner: string) =>
		client.get<ResourceCatalog>('/agent-management/resources', scope(owner)),
	save: (owner: string, data: AgentDraft, id?: string) =>
		id
			? client.patch<ManagedAgent>(`/agent-management/${id}`, data, scope(owner), {
					silent: true,
				})
			: client.post<ManagedAgent>('/agent-management', data, scope(owner), { silent: true }),
	delete: (owner: string, id: string) => client.delete(`/agent-management/${id}`, scope(owner)),
	preview: (owner: string, id: string, extra: Record<string, string> = {}) =>
		client.get<CopyPreview>(`/agent-management/${id}/copy-preview`, scope(owner, extra)),
	copy: (owner: string, id: string, options: CopyOptions) =>
		client.post<ManagedAgent>(`/agent-management/${id}/copy`, options, scope(owner), {
			silent: true,
		}),
	files: (owner: string, id: string, workspace: string, memory = true) =>
		client.get<{ workspace_id: string; files: FileRow[] }>(
			`/agent-management/${id}/files`,
			scope(owner, {
				...(workspace ? { workspace_id: workspace } : {}),
				memory: String(memory),
			}),
		),
	memory: (owner: string, id: string, workspace: string, path: string) =>
		client.get<{ content: string; revision: string }>(
			`/agent-management/${id}/memory`,
			scope(owner, { workspace_id: workspace, path }),
		),
	writeMemory: async (
		owner: string,
		id: string,
		workspace: string,
		path: string,
		content: string,
		revision: string | null,
	) => {
		const response = await client.stream(`/agent-management/${id}/memory`, {
			method: 'PUT',
			body: { path, content, revision, workspace_id: workspace || null },
			params: scope(owner),
		});
		return response.json() as Promise<{ revision: string }>;
	},
	deleteMemory: (owner: string, id: string, workspace: string, path: string, revision: string) =>
		client.delete(
			`/agent-management/${id}/memory`,
			scope(owner, { workspace_id: workspace, path, revision }),
		),
	clearMemory: (owner: string, id: string, workspace: string) =>
		client.post(
			`/agent-management/${id}/memory/clear`,
			undefined,
			scope(owner, { workspace_id: workspace }),
		),
};
