import { useCallback, useEffect, useState } from 'react';

import { mcpApi } from '@/api';
import type { MCPView, UpdateMCPRequest } from '@/api';
import { notifyResourceChanged, RESOURCE_CHANGED } from '@/lib/resource-events';

/**
 * The user's library of installed MCPs.
 *
 * User-level, so unlike `useWorkspace` it needs no agent/session — an empty
 * list means the user has installed nothing yet, not that a session is
 * missing.
 */
export function useMCPs() {
	const [mcps, setMcps] = useState<MCPView[]>([]);
	// Starts true: the first paint happens before the effect fires, and
	// a false start would flash the empty state before the spinner.
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState<Error | null>(null);

	const refetch = useCallback(async () => {
		setLoading(true);
		setError(null);
		try {
			setMcps(await mcpApi.list());
		} catch (e) {
			setError(e as Error);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		refetch();
		window.addEventListener(RESOURCE_CHANGED, refetch);
		return () => window.removeEventListener(RESOURCE_CHANGED, refetch);
	}, [refetch]);

	const update = useCallback(
		async (mcpId: string, body: UpdateMCPRequest) => {
			await mcpApi.update(mcpId, body);
			notifyResourceChanged();
		},
		[],
	);

	const remove = useCallback(
		async (mcpId: string) => {
			await mcpApi.remove(mcpId);
			notifyResourceChanged();
		},
		[],
	);

	const upgrade = useCallback(async (mcpId: string) => {
		await mcpApi.upgrade(mcpId);
		notifyResourceChanged();
	}, []);
	return { mcps, loading, error, refetch, update, remove, upgrade };
}
