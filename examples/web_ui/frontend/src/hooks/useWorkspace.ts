import { useQuery } from '@tanstack/react-query';
import { useEffect, useCallback } from 'react';

import { workspaceApi } from '@/api';
import type { MCPClient, MCPClientStatus, Skill } from '@/api';
import { getUserId } from '@/api/client';
import type { UploadOptions } from '@/api/workspace';
import { notifyResourceChanged, RESOURCE_CHANGED } from '@/lib/resource-events';

const NO_MCPS: MCPClientStatus[] = [];
const NO_SKILLS: Skill[] = [];

interface WorkspaceActivity {
	isRunning?: boolean;
	mcpPanelOpen?: boolean;
}

export function useWorkspace(
	agentId: string | null,
	sessionId: string | null,
	{ isRunning = false, mcpPanelOpen = false }: WorkspaceActivity = {},
) {
	const enabled = Boolean(agentId && sessionId);
	const {
		data: mcpData,
		isPending: mcpsPending,
		error: mcpError,
		refetch: fetchMcps,
	} = useQuery({
		// A late response from another session must never replace this panel.
		queryKey: ['workspace-mcps', getUserId(), agentId, sessionId],
		queryFn: () => workspaceApi.mcp.list(agentId as string, sessionId as string),
		enabled,
		staleTime: 0,
		retry: false,
		// Joining a run can precede tool initialization. While the panel is
		// visible, re-read cached server health until initialization settles.
		// This endpoint does not connect to MCP servers or execute tools.
		refetchInterval: (query) =>
			mcpPanelOpen &&
			isRunning &&
			!query.state.error &&
			query.state.data?.some((mcp) => mcp.enabled && mcp.status === 'not_loaded')
				? 2_000
				: false,
	});
	const mcps = enabled ? (mcpData ?? NO_MCPS) : NO_MCPS;
	const loading = enabled && mcpsPending;

	const refetch = useCallback(async () => {
		if (enabled) await fetchMcps();
	}, [enabled, fetchMcps]);

	const {
		data: skillData,
		isPending: skillPending,
		error: skillError,
		refetch: fetchSkills,
	} = useQuery({
		queryKey: ['workspace-skills', getUserId(), agentId, sessionId],
		queryFn: () => workspaceApi.skill.list(agentId as string, sessionId as string),
		enabled,
		staleTime: 0,
		retry: false,
	});
	const skills = enabled ? (skillData ?? NO_SKILLS) : NO_SKILLS;
	const skillsLoading = enabled && skillPending;
	const refetchSkills = useCallback(async () => {
		if (enabled) await fetchSkills();
	}, [enabled, fetchSkills]);
	const changeResource = useCallback(
		async (kind: 'mcp' | 'skill', id: string, value: boolean | null) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			await workspaceApi.setResourceEnabled(kind, id, agentId, sessionId, value);
			await (kind === 'mcp' ? refetch() : refetchSkills());
		},
		[agentId, sessionId, refetch, refetchSkills],
	);
	const changeMcp = useCallback(
		(id: string, value: boolean | null) => changeResource('mcp', id, value),
		[changeResource],
	);
	const changeSkill = useCallback(
		(id: string, value: boolean | null) => changeResource('skill', id, value),
		[changeResource],
	);

	useEffect(() => {
		// Refresh at run start/end and when the MCP panel opens/closes.
		void refetch();
	}, [refetch, isRunning, mcpPanelOpen]);
	useEffect(() => {
		refetchSkills();
	}, [refetchSkills, isRunning, mcpPanelOpen]);
	useEffect(() => {
		const refresh = () => {
			void refetch();
			void refetchSkills();
		};
		window.addEventListener(RESOURCE_CHANGED, refresh);
		return () => window.removeEventListener(RESOURCE_CHANGED, refresh);
	}, [refetch, refetchSkills]);
	const upgradeSkill = useCallback(
		async (name: string) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			await workspaceApi.skill.upgrade(name, agentId, sessionId);
			notifyResourceChanged();
		},
		[agentId, sessionId],
	);

	const addMcps = useCallback(
		async (clients: MCPClient[]) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			const existingNames = new Set(mcps.map((m) => m.name));
			for (const mcp of clients) {
				if (existingNames.has(mcp.name)) {
					throw new Error(`MCP server "${mcp.name}" already exists in this workspace.`);
				}
			}
			const batchNames = new Set<string>();
			for (const mcp of clients) {
				if (batchNames.has(mcp.name)) {
					throw new Error(`Duplicate MCP server name "${mcp.name}" in configuration.`);
				}
				batchNames.add(mcp.name);
			}
			for (const mcp of clients) {
				await workspaceApi.mcp.add(agentId, sessionId, mcp);
			}
			await refetch();
		},
		[agentId, sessionId, mcps, refetch],
	);

	const addMcpsFromLibrary = useCallback(
		async (mcpIds: string[]) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			const result = await workspaceApi.mcp.addFromLibrary(agentId, sessionId, mcpIds);
			await refetch();
			// Reported per MCP, so a partial success is still a success for
			// what landed; surface only what did not.
			const failures = Object.entries(result.failed);
			if (failures.length > 0) {
				throw new Error(failures.map(([name, why]) => `${name}: ${why}`).join('\n'));
			}
		},
		[agentId, sessionId, refetch],
	);

	const removeMcp = useCallback(
		async (mcpName: string) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			await workspaceApi.mcp.remove(mcpName, agentId, sessionId);
			await refetch();
		},
		[agentId, sessionId, refetch],
	);

	const uploadSkill = useCallback(
		async (files: File[], options: UploadOptions = {}) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			await workspaceApi.skill.upload(agentId, sessionId, files, options);
			notifyResourceChanged();
			await refetchSkills();
		},
		[agentId, sessionId, refetchSkills],
	);

	const addSkillsFromLibrary = useCallback(
		async (skillIds: string[]) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			const result = await workspaceApi.skill.addFromLibrary(agentId, sessionId, skillIds);
			await refetchSkills();
			// Reported per skill, so a partial success is still a success
			// for what landed; surface only what did not.
			const failures = Object.entries(result.failed);
			if (failures.length > 0) {
				throw new Error(failures.map(([name, why]) => `${name}: ${why}`).join('\n'));
			}
		},
		[agentId, sessionId, refetchSkills],
	);

	const removeSkill = useCallback(
		async (skillName: string) => {
			if (!agentId || !sessionId) throw new Error('No agent/session selected');
			await workspaceApi.skill.remove(skillName, agentId, sessionId);
			await refetchSkills();
		},
		[agentId, sessionId, refetchSkills],
	);

	return {
		mcps,
		loading,
		error: mcpError ?? skillError,
		changeMcp,
		changeSkill,
		refetch,
		addMcps,
		addMcpsFromLibrary,
		removeMcp,
		skills,
		skillsLoading,
		uploadSkill,
		addSkillsFromLibrary,
		removeSkill,
		upgradeSkill,
	};
}
