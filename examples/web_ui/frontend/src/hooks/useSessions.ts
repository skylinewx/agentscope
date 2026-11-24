import { type QueryFilters, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useEffectEvent, useState } from 'react';

import { sessionApi } from '../api';
import type { SessionView, CreateSessionRequest, UpdateSessionRequest } from '../api';
import { getAuthSignal } from '../api/auth-state';
import { ApiError } from '../api/client';
import type { SessionActivityEvent } from '../api/session';

/** Stable empty list, so `sessions` keeps its identity between renders. */
const NO_SESSIONS: SessionView[] = [];

/** Every agent's session list, so one mutation refreshes all of them. */
const SESSIONS_KEY = ['sessions'];
const CONCURRENT_SESSION_DELETES = 4;

/** One subscription for this account, independent of the selected session. */
export function useSessionUpdates(onActivity?: (event: SessionActivityEvent) => void) {
	const queryClient = useQueryClient();
	const authSignal = getAuthSignal();
	const [connectedAuth, setConnectedAuth] = useState<AbortSignal | null>(null);
	const receiveActivity = useEffectEvent((event: SessionActivityEvent) => onActivity?.(event));

	useEffect(() => {
		const controller = new AbortController();
		const signal = AbortSignal.any([authSignal, controller.signal]);
		let retryTimer: ReturnType<typeof setTimeout> | undefined;
		let attempts = 0;

		async function refreshLists(filters: QueryFilters) {
			const wasFetching = queryClient.isFetching(filters) > 0;
			await queryClient.invalidateQueries(filters, { cancelRefetch: false });
			// A read already in flight may have captured the old title. Let
			// its callers receive the result, then reconcile after the update.
			if (wasFetching && !signal.aborted) {
				await queryClient.invalidateQueries(filters, { cancelRefetch: false });
			}
		}

		async function connect() {
			if (signal.aborted) return;
			try {
				for await (const event of sessionApi.streamUpdates(signal)) {
					if (signal.aborted) return;
					if (event.type === 'ready') {
						attempts = 0;
						setConnectedAuth(authSignal);
						// Recover status and title changes missed while offline.
						void refreshLists({ queryKey: SESSIONS_KEY, type: 'active' });
						receiveActivity(event);
					} else if (event.type === 'title_updated') {
						void refreshLists({
							queryKey: [...SESSIONS_KEY, event.agent_id], exact: true, type: 'active',
						});
					} else {
						receiveActivity(event);
						if (event.type === 'reply_completed' && !event.replayed) {
							const key = [...SESSIONS_KEY, event.agent_id];
							const views = queryClient.getQueryData<SessionView[]>(key);
							if (views && !views.some(({ session }) => session.id === event.session_id)) {
								void refreshLists({ queryKey: key, exact: true, type: 'active' });
							}
						}
					}
				}
			} catch (error) {
				if (signal.aborted) return;
				if (error instanceof ApiError && [401, 403, 404, 405].includes(error.status)) {
					setConnectedAuth(null);
					return;
				}
			}
			if (signal.aborted) return;
			setConnectedAuth(null);
			retryTimer = setTimeout(connect, Math.min(1000 * 2 ** Math.min(attempts++, 4), 10_000));
		}

		signal.addEventListener('abort', () => clearTimeout(retryTimer), { once: true });
		void connect();
		return () => controller.abort();
	}, [authSignal, queryClient]);

	return connectedAuth === authSignal;
}

/**
 * Manages session views for a given agent.
 *
 * Each entry is a `SessionView` (record + is_running + optional team
 * detail) — the same shape the backend returns.
 *
 * Backed by the shared query cache, keyed on `agentId`. The chat page
 * mounts this hook twice — once for the outer sidebar's list, once
 * inside the viewport — and both now read the same entry, so a rename
 * from the sidebar reaches the viewport's header without either side
 * knowing about the other. Drilling into a team member gives the
 * viewport a different `agentId`, hence its own entry, which is what
 * that view wants.
 *
 * @param agentId - The agent whose sessions to load. Pass null to skip fetching.
 * @returns Object with the loaded `sessions` array plus `loading` /
 *   `error` flags and `refetch` / `create` / `update` / `remove`
 *   helpers that all keep the list in sync.
 */
export function useSessions(agentId: string | null) {
	const queryClient = useQueryClient();

	const {
		data,
		isPending,
		isFetching,
		error,
		refetch: runRefetch,
	} = useQuery({
		queryKey: [...SESSIONS_KEY, agentId],
		queryFn: ({ signal }) => sessionApi.list(agentId as string, signal).then((res) => res.sessions),
		enabled: agentId !== null,
		// This list carries `is_running` and team membership, both of
		// which move underneath the page. Sharing one copy between the
		// two mounts is the point here, not skipping the read — so a
		// view that needs it still re-reads it when it mounts.
		staleTime: 0,
	});

	/** Drop every agent's list, so both mounts re-read after a write. */
	const invalidate = useCallback(
		() => queryClient.invalidateQueries({ queryKey: SESSIONS_KEY }),
		[queryClient],
	);

	/**
	 * Reload the session list.
	 *
	 * Also returns the fresh list, so a caller reacting to an event can
	 * act on it immediately — reading the `sessions` state right after
	 * awaiting would still see the pre-update value from its closure.
	 *
	 * @returns The reloaded views, or an empty array when there is no
	 *   agent or the request failed.
	 */
	const refetch = useCallback(async (): Promise<SessionView[]> => {
		const result = await runRefetch();
		return result.data ?? NO_SESSIONS;
	}, [runRefetch]);

	/** Make the new session available immediately, then reconcile its list. */
	const create = useCallback(
		async (body: CreateSessionRequest) => {
			const authSignal = getAuthSignal();
			const res = await sessionApi.create(body);
			authSignal.throwIfAborted();
			const queryKey = [...SESSIONS_KEY, body.agent_id];
			const target = { queryKey, exact: true };
			// A list read started before the POST may omit the new session.
			await queryClient.cancelQueries(target);
			authSignal.throwIfAborted();
			const view = res.view;
			if (view) {
				queryClient.setQueryData<SessionView[]>(queryKey, (current = NO_SESSIONS) => [
					view,
					...current.filter((item) => item.session.id !== view.session.id),
				]);
				void queryClient.invalidateQueries(target);
			} else {
				// Older servers only send the ID. Load the view before opening it.
				await queryClient.invalidateQueries(target);
				authSignal.throwIfAborted();
			}
			return res;
		},
		[queryClient],
	);

	/** Updates a session's model config and refreshes the list. */
	const update = useCallback(
		async (sessionId: string, body: UpdateSessionRequest) => {
			if (!agentId) throw new Error('No agent selected');
			const res = await sessionApi.update(sessionId, agentId, body);
			await invalidate();
			return res;
		},
		[agentId, invalidate],
	);

	/** Deletes a session and refreshes the list. */
	const remove = useCallback(
		async (sessionId: string) => {
			if (!agentId) throw new Error('No agent selected');
			await sessionApi.delete(sessionId, agentId);
			await invalidate();
		},
		[agentId, invalidate],
	);

	/** Returns the selected ids that are now gone, including already-deleted sessions. */
	const removeMany = useCallback(
		async (sessionIds: string[]) => {
			if (!agentId) throw new Error('No agent selected');
			const authSignal = getAuthSignal();
			const removed = new Set<string>();
			for (let start = 0; start < sessionIds.length; start += CONCURRENT_SESSION_DELETES) {
				authSignal.throwIfAborted();
				const batch = sessionIds.slice(start, start + CONCURRENT_SESSION_DELETES);
				const results = await Promise.allSettled(
					batch.map((id) =>
						sessionApi.delete(id, agentId, { silent: true, signal: authSignal, timeoutMs: 30_000 }),
					),
				);
				results.forEach((result, index) => {
					if (
						result.status === 'fulfilled' ||
						(result.reason instanceof ApiError && result.reason.status === 404)
					) {
						removed.add(batch[index]);
					}
				});
			}

			authSignal.throwIfAborted();
			const key = [...SESSIONS_KEY, agentId];
			// Discard list reads that may have started before the deletions committed.
			await queryClient.cancelQueries({ queryKey: key, exact: true });
			authSignal.throwIfAborted();
			if (removed.size > 0) {
				queryClient.setQueryData<SessionView[]>(key, (views) =>
					views?.filter(({ session }) => !removed.has(session.id)),
				);
			}
			// Team leaders can also remove member sessions from other agents' lists.
			void invalidate();
			return removed;
		},
		[agentId, invalidate, queryClient],
	);

	return {
		sessions: data ?? NO_SESSIONS,
		loaded: data !== undefined,
		loading: agentId !== null && isPending,
		fetching: isFetching,
		error: error as Error | null,
		refetch,
		create,
		update,
		remove,
		removeMany,
	};
}
