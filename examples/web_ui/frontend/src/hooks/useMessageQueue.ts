import type { Msg } from '@agentscope-ai/agentscope/message';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { getAuthSignal } from '@/api/auth-state';
import { messageQueueApi, type MessageQueueState, type QueuedMessage, type QueueOperation } from '@/api/message-queue';

export function useMessageQueue(agentId: string | null, sessionId: string | null) {
	const key = `${agentId}:${sessionId}`;
	const scope = useRef(key);
	useLayoutEffect(() => { scope.current = key; }, [key]);
	const [saved, setSaved] = useState<{ key: string; queue: MessageQueueState } | null>(null);
	const [error, setError] = useState<Error | null>(null);
	const fetching = useRef<AbortController | null>(null);

	const accept = useCallback((queue: MessageQueueState) => {
		if (scope.current !== key || getAuthSignal().aborted) return;
		setSaved((previous) => previous?.key === key && previous.queue.revision > queue.revision ? previous : { key, queue });
		setError(null);
	}, [key]);

	const refresh = useCallback(async () => {
		if (!agentId || !sessionId) return;
		if (fetching.current && !fetching.current.signal.aborted) return;
		const controller = new AbortController();
		fetching.current = controller;
		const signal = AbortSignal.any([controller.signal, getAuthSignal()]);
		try {
			accept(await messageQueueApi.get(agentId, sessionId, signal));
		} catch (e) {
			if (!signal.aborted && scope.current === key) setError(e as Error);
		} finally {
			if (fetching.current === controller) fetching.current = null;
		}
	}, [agentId, sessionId, key, accept]);

	useEffect(() => {
		setError(null);
		void refresh();
		// Reconciles missed notifications and other tabs, including after sleep.
		const timer = setInterval(() => void refresh(), 3000);
		const onVisible = () => { if (!document.hidden) void refresh(); };
		document.addEventListener('visibilitychange', onVisible);
		return () => { clearInterval(timer); fetching.current?.abort(); document.removeEventListener('visibilitychange', onVisible); };
	}, [refresh]);

	const submit = useCallback(async (message: Msg) => {
		if (!agentId || !sessionId) throw new Error('No session selected.');
		accept(await messageQueueApi.add(agentId, sessionId, message));
	}, [agentId, sessionId, accept]);

	const action = useCallback(async (operation: QueueOperation, item?: QueuedMessage, message?: Msg) => {
		if (!agentId || !sessionId) throw new Error('No session selected.');
		try {
			const result = await messageQueueApi.action(agentId, sessionId, operation, item, message);
			accept(result);
			return result;
		} catch (e) {
			void refresh();
			throw e;
		}
	}, [agentId, sessionId, accept, refresh]);

	return { state: saved?.key === key ? saved.queue : null, error, refresh, submit, action };
}

export type MessageQueueController = ReturnType<typeof useMessageQueue>;
