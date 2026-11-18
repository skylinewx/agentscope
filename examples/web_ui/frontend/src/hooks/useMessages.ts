import { EventType } from '@agentscope-ai/agentscope/event';
import type {
	AgentEvent,
	CustomEvent,
	DataBlockStartEvent,
	DataBlockDeltaEvent,
	DataBlockEndEvent,
	ReplyStartEvent,
	UserConfirmResultEvent,
} from '@agentscope-ai/agentscope/event';
import { appendEvent, AssistantMsg, UserMsg } from '@agentscope-ai/agentscope/message';
import type { Msg, ContentBlock } from '@agentscope-ai/agentscope/message';
import type { ToolCallBlock } from '@agentscope-ai/agentscope/message';
import { useState, useCallback, useRef, useEffect } from 'react';

import { sessionApi, takeFreshlyCreated } from '@/api';
import { chatApi } from '@/api';
import { getAuthSignal } from '@/api/auth-state';
import { ApiError } from '@/api/client';
import { useAudioManager } from '@/context/AudioContext';
import { useMessageQueue } from '@/hooks/useMessageQueue';
import { applyAgentProgress, readAgentProgress } from '@/lib/agent-progress';
import { applyReplyPresentation, pendingCalls } from '@/lib/reply-presentation';

/**
 * One pending subagent HITL request, projected from a team *member*
 * session onto its *leader* session so the leader UI can render and
 * resolve it. Mirrors the Python payload written by
 * ``SubagentHitlProjector`` and pushed/replayed as a ``CustomEvent``
 * (``name="subagent_require_user_confirm"``).
 */
export type SubagentHitlEntry = {
	worker_session_id: string;
	worker_agent_id: string;
	worker_agent_name: string;
	reply_id: string;
	event_type: 'require_user_confirm' | 'require_external_execution';
	/** The original ``RequireUserConfirmEvent`` payload (serialized). */
	event: { tool_calls?: ToolCallBlock[] } & Record<string, unknown>;
	created_at: string;
};

/**
 * Return true if ``msg`` is an assistant reply currently parked on a
 * pending tool_call (awaiting user confirmation or an external
 * execution result). Used both to detect the "in-flight reply on page
 * load" case and as the SDK-gap workaround that hides the confirm
 * card once the paired tool_result lands.
 */
const hasPendingToolCall = (msg: Msg | undefined): boolean => {
	return !!msg && msg.role === 'assistant' && pendingCalls(msg).length > 0;
};

const hitlKey = (e: { worker_session_id: string; reply_id: string }) =>
	`${e.worker_session_id}:${e.reply_id}`;

/**
 * Lifecycle phase of the reply currently owned by this session.
 *
 * - ``idle`` — no in-flight reply; the send button is enabled.
 * - ``streaming`` — a reply is in progress (either actively producing
 *   events or parked awaiting HITL). The send button is replaced by a
 *   Stop button. The parked-vs-generating distinction is not tracked
 *   here; HITL cards render themselves from message content when a
 *   ``RequireUserConfirmEvent`` block is present.
 * - ``interrupting`` — the user has requested a stop and we are
 *   waiting for the backend's terminating ``ReplyEndEvent``. Stop
 *   button is shown but disabled so users cannot spam it. Falls back
 *   to ``idle`` after a 10s safety timeout in case the terminating
 *   event never arrives (dropped SSE frame, backend bug, etc.).
 */
export type ReplyPhase = 'idle' | 'streaming' | 'interrupting';

export type ConnectionNotice = 'reconnecting' | 'interrupted' | 'failed' | null;

/** A session switch or logout must also cancel an outstanding retry delay. */
function waitForReconnect(signal: AbortSignal, delay: number): Promise<void> {
	return new Promise((resolve) => {
		const finish = () => {
			clearTimeout(timer);
			signal.removeEventListener('abort', finish);
			resolve();
		};
		const timer = setTimeout(finish, delay);
		signal.addEventListener('abort', finish, { once: true });
		if (signal.aborted) finish();
	});
}

/** Safety fallback: force phase back to idle if REPLY_END is not seen. */
const INTERRUPT_TIMEOUT_MS = 10_000;

/**
 * Manages messages for a single ``(agentId, sessionId)`` pair.
 *
 * Event delivery has two independent channels:
 *
 * - **History** — ``GET /sessions/{sid}/messages`` fetches persisted
 *   ``Msg`` objects (each a complete reply).
 * - **Live stream** — ``GET /sessions/{sid}/stream`` is a long-lived
 *   SSE connection that pushes ``AgentEvent`` deltas as they are
 *   produced by any chat run on this session (user-triggered,
 *   background retrigger, team member message, …).
 *
 * The hook opens the SSE connection immediately after fetching
 * history. User input is sent via ``POST /chat/``. Human-in-the-loop
 * confirmations use the active realtime control channel during voice
 * mode and ``POST /chat/`` otherwise. Resulting events arrive through
 * the already-open SSE connection.
 *
 * ``phase`` is driven by event content, not HTTP lifecycle: it moves
 * to ``streaming`` on ``ReplyStartEvent`` and back to ``idle`` on
 * ``ReplyEndEvent``. Calling ``interrupt()`` moves it to
 * ``interrupting`` until the terminating ``ReplyEndEvent`` arrives (or
 * a 10s safety timeout fires).
 *
 * @param agentId - The agent whose session to subscribe. ``null`` to
 *   skip.
 * @param sessionId - The session to subscribe. ``null`` to skip.
 * @returns Object with ``msgs``, ``loading``, ``phase``, ``error``,
 *   ``send``, ``onUserConfirm``, and ``abort``. ``loading`` stays true
 *   until the history for the *current* ``(agentId, sessionId)`` has
 *   landed, so ``msgs`` must not be rendered while it is set.
 */
export function useMessages(
	agentId: string | null,
	sessionId: string | null,
	options?: {
		/**
		 * Called when a ``CUSTOM`` event with ``name="team_updated"``
		 * arrives — the team membership has changed (TeamCreate /
		 * AgentCreate / TeamDelete ran). The typical response is to
		 * refetch the session list so the team sidebar updates.
		 */
		onTeamUpdated?: () => void;
		/**
		 * Called when a ``CUSTOM`` event with ``name="state_updated"``
		 * arrives — agent state (tasks / permission) changed during a
		 * tool call. The ``value`` payload contains the latest
		 * ``tasks_context`` and ``permission_context``.
		 */
		onStateUpdated?: (value: Record<string, unknown>) => void;
		/**
		 * Called when a ``CUSTOM`` event with ``name="session_updated"``
		 * arrives — the session record changed server-side, currently
		 * only when auto-naming replaced its placeholder name. Refetch
		 * the session list to pick the new one up.
		 */
		onSessionUpdated?: () => void;
		/** True when a dedicated realtime transport already plays PCM audio. */
		isRealtimeAudioActive?: () => boolean;
		/** Send a tool confirmation over the active realtime transport. */
		sendRealtimeUserConfirm?: (event: UserConfirmResultEvent) => Promise<void>;
	},
) {
	const [msgs, setMsgs] = useState<Msg[]>([]);
	const messageQueue = useMessageQueue(agentId, sessionId);
	const queueState = messageQueue.state;
	const refreshQueue = messageQueue.refresh;
	const submitQueued = messageQueue.submit;
	const queueUnavailable = !messageQueue.state && messageQueue.error instanceof ApiError && messageQueue.error.status === 404;
	const refreshQueueRef = useRef(messageQueue.refresh);
	useEffect(() => { refreshQueueRef.current = messageQueue.refresh; }, [messageQueue.refresh]);
	// The (agent, session) pair `msgs` actually belongs to. Loading is
	// derived from it rather than set inside the fetch effect: an effect
	// runs *after* the render that changed `sessionId`, so a flag it owns
	// is still `false` for one frame — long enough to paint the empty
	// state over a session that does have messages.
	const [loadedKey, setLoadedKey] = useState<string | null>(null);
	const [phase, setPhase] = useState<ReplyPhase>('idle');
	const [error, setError] = useState<Error | null>(null);
	const [connectionNotice, setConnectionNotice] = useState<ConnectionNotice>(null);
	// A failed history read is distinct from an empty conversation or a stream error.
	const [historyError, setHistoryError] = useState<Error | null>(null);
	const [historyAttempt, setHistoryAttempt] = useState(0);
	const reloadHistory = useCallback(() => {
		setLoadedKey(null);
		setHistoryAttempt((attempt) => attempt + 1);
	}, []);
	// Pending subagent HITL cards projected onto this (leader) session.
	const [subagentHitl, setSubagentHitl] = useState<SubagentHitlEntry[]>([]);

	const msgsRef = useRef<Msg[]>([]);
	const confirmedUserIds = useRef(new Set<string>());
	const sendAttempt = useRef<{ key: string; signature: string; message: Msg } | null>(null);
	const sendingDirect = useRef<string | null>(null);
	const currentReplyRef = useRef<Msg | null>(null);
	const abortRef = useRef<AbortController | null>(null);
	const rafRef = useRef<number | null>(null);
	// Timer that reverts ``interrupting`` back to ``idle`` if the
	// terminating REPLY_END never arrives (dropped SSE frame, etc.).
	const interruptTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

	const clearInterruptTimer = useCallback(() => {
		if (interruptTimerRef.current !== null) {
			clearTimeout(interruptTimerRef.current);
			interruptTimerRef.current = null;
		}
	}, []);

	const audioManager = useAudioManager();

	const optionsRef = useRef(options);
	useEffect(() => {
		optionsRef.current = options;
	}, [options]);
	const scheduleUpdate = useCallback(() => {
		if (rafRef.current !== null) return;
		rafRef.current = requestAnimationFrame(() => {
			rafRef.current = null;
			setMsgs([...msgsRef.current]);
		});
	}, []);

	/** Apply a single AgentEvent to the in-progress reply. */
	const processEvent = useCallback(
		(event: AgentEvent) => {
			// Custom events are service-layer notifications, not agent
			// reply content — route them to callbacks and skip appendEvent.
			if (event.type === EventType.CUSTOM) {
				const custom = event as CustomEvent;
				if (custom.name === 'agent_progress') {
					const replyId = readAgentProgress(custom.value)?.replyId;
					const reply = msgsRef.current.find((message) => message.id === replyId);
					if (reply && applyAgentProgress(reply, event)) {
						const updated = { ...reply };
						msgsRef.current = msgsRef.current.map((message) => message === reply ? updated : message);
						if (currentReplyRef.current?.id === reply.id) currentReplyRef.current = updated;
						scheduleUpdate();
					}
				} else if (custom.name === 'message_queue_updated') {
					void refreshQueueRef.current();
				} else if (custom.name === 'queued_message_started') {
					const message = (custom.value as { message: Msg }).message;
					confirmedUserIds.current.add(message.id);
					if (!msgsRef.current.some((existing) => existing.id === message.id)) {
						msgsRef.current = [...msgsRef.current, message];
						scheduleUpdate();
					}
					setConnectionNotice(null);
					setPhase('streaming');
				} else if (custom.name === 'execution_progress') {
                    const progress = custom.value as { messageId?: string; stage?: string };
                    if (progress.messageId && typeof progress.stage === 'string') {
                        msgsRef.current = msgsRef.current.map((message) => message.id === progress.messageId
                            ? { ...message, metadata: { ...message.metadata, executionProgress: progress } } : message);
                        scheduleUpdate();
                    }
                } else if (custom.name === 'team_updated') {
					optionsRef.current?.onTeamUpdated?.();
				} else if (custom.name === 'state_updated' && custom.value) {
					optionsRef.current?.onStateUpdated?.(custom.value as Record<string, unknown>);
				} else if (custom.name === 'session_updated') {
					optionsRef.current?.onSessionUpdated?.();
				} else if (custom.name === 'subagent_require_user_confirm') {
					// A team member is asking for confirmation; show (or
					// refresh) its card on this leader view. Dedup by
					// (worker_session_id, reply_id).
					const e = custom.value as unknown as SubagentHitlEntry;
					setSubagentHitl((prev) => [
						...prev.filter((x) => hitlKey(x) !== hitlKey(e)),
						e,
					]);
				} else if (custom.name === 'subagent_user_confirm_result') {
					// The member resolved (or its run ended); clear the card.
					const v = custom.value as { worker_session_id: string; reply_id: string };
					setSubagentHitl((prev) => prev.filter((x) => hitlKey(x) !== hitlKey(v)));
				}
				return;
			}
			if (event.type === EventType.REPLY_START) {
				setConnectionNotice(null);
				const e = event as ReplyStartEvent;
				// A continuation (the run resuming after a confirmation or an
				// external execution result) re-emits REPLY_START with the
				// *same* reply_id. Re-point at the existing reply instead of
				// appending a second msg under that id: a duplicate would
				// collide on React keys, strand the original bubble in its
				// running state, and hide any still-pending confirmation card
				// — those are read off the tail msg, which would be the empty
				// duplicate.
				const existing = msgsRef.current.find((m) => m.id === e.reply_id);
				if (existing) {
					currentReplyRef.current = existing;
				} else {
					audioManager?.stopAllPlayback();
					const msg =
						e.role === 'user'
							? UserMsg({ id: e.reply_id, name: e.name, content: [], created_at: e.created_at })
							: AssistantMsg({ id: e.reply_id, name: e.name, content: [], created_at: e.created_at });
					msgsRef.current = [...msgsRef.current, msg];
					currentReplyRef.current = msg;
				}
				clearInterruptTimer();
				setPhase('streaming');
			} else {
				// Realtime providers may start the assistant response before the
				// user's final transcription arrives. Route every identified event
				// to its own reply instead of assuming event streams never overlap.
				const reply = event.reply_id
					? currentReplyRef.current?.id === event.reply_id
						? currentReplyRef.current
						: (msgsRef.current.find((message) => message.id === event.reply_id) ?? null)
					: currentReplyRef.current;
				if (reply) {
					appendEvent(reply, event);
					applyReplyPresentation(reply, event);
					// ``appendEvent`` mutates in place, which would leave
					// every Msg identical across renders and force the whole
					// list to re-render on each delta. Republish just the
					// reply that changed under a fresh identity, so the
					// memoised bubbles of the other messages can skip the
					// render. Anything holding a Msg reference across events
					// must re-read it from here — ``currentReplyRef`` below,
					// everything else looks the reply up by id.
					const updated = { ...reply, content: [...reply.content] };
					msgsRef.current = msgsRef.current.map((m) => (m === reply ? updated : m));
					if (currentReplyRef.current?.id === reply.id) {
						currentReplyRef.current = updated;
					}
				}
				if (
					event.type === EventType.REPLY_END &&
					(!event.reply_id ||
						currentReplyRef.current === null ||
						currentReplyRef.current.id === event.reply_id)
				) {
					clearInterruptTimer();
					setPhase('idle');
					currentReplyRef.current = null;
				}
			}

			// Route streaming audio DataBlocks to the audio manager. They still
			// flow through `appendEvent` above (which builds up `source.data`
			// in the Msg), but MessageBubble reads playback state from the
			// manager so it can show progress and autoplay on completion.
			if (audioManager) {
				if (event.type === EventType.DATA_BLOCK_START) {
					const e = event as DataBlockStartEvent;
					if (e.media_type.startsWith('audio/')) {
						audioManager.start(e.block_id, e.media_type);
					}
				} else if (event.type === EventType.DATA_BLOCK_DELTA) {
					const e = event as DataBlockDeltaEvent;
					if (e.media_type.startsWith('audio/')) {
						audioManager.append(e.block_id, e.data);
					}
				} else if (event.type === EventType.DATA_BLOCK_END) {
					const e = event as DataBlockEndEvent;
					// `end` is a no-op when the block isn't being tracked, so
					// we can call it unconditionally.
					audioManager.end(e.block_id);
				}
			}

			scheduleUpdate();
		},
		[scheduleUpdate, audioManager, clearInterruptTimer],
	);

	// ── Lifecycle: fetch history + open SSE stream ──────────────────
	useEffect(() => {
		setLoadedKey(null);
		msgsRef.current = [];
		confirmedUserIds.current = new Set();
		currentReplyRef.current = null;
		setMsgs([]);
		setError(null);
		setConnectionNotice(null);
		setHistoryError(null);
		clearInterruptTimer();
		setPhase('idle');
		setSubagentHitl([]);
		audioManager?.disposeAll();

		if (!agentId || !sessionId) return;

		const controller = new AbortController();
		const signal = AbortSignal.any([controller.signal, getAuthSignal()]);
		abortRef.current = controller;

		// Rebuild from the persisted snapshot AND its cursor before each new
		// subscription. Reusing partial live messages would append replayed
		// deltas twice, and keeping the old phase would strand crashed replies.
		const readHistory = async (reconnecting = false) => {
			const { messages, is_running, event_cursor } = await sessionApi.messages(
				sessionId, agentId, undefined,
				{ signal, silent: reconnecting, timeoutMs: 15_000 },
			);
			if (signal.aborted) return null;
			const tail = messages.at(-1);
			const pending = hasPendingToolCall(tail);
			msgsRef.current = messages;
			confirmedUserIds.current = new Set(messages.filter((message) => message.role === 'user').map((message) => message.id));
			currentReplyRef.current = pending ? tail! : null;
			clearInterruptTimer();
			setPhase(is_running || pending ? 'streaming' : 'idle');
			setSubagentHitl([]); // Pending cards are replayed by the new stream.
			setMsgs([...messages]);
			return { cursor: event_cursor, interrupted: !is_running && !pending && tail?.role === 'user' };
		};

		(async () => {
			let eventCursor: string | null = null;
			let interrupted = false;
			// 1. Fetch persisted history — unless this tab just created the
			// session, in which case there is provably none.
			if (takeFreshlyCreated(sessionId)) {
				if (!signal.aborted) setLoadedKey(`${agentId}:${sessionId}`);
			} else {
				try {
					const snapshot = await readHistory();
					if (signal.aborted) return;
					eventCursor = snapshot!.cursor;
					interrupted = snapshot!.interrupted;
				} catch (e) {
					if (!signal.aborted) {
						setError(e as Error);
						setHistoryError(e as Error);
					}
					return;
				} finally {
					// Marks the load done whether it succeeded or threw —
					// an error surfaces through `historyError`, and leaving
					// `loading` stuck on would hide it behind a spinner
					// forever.
					if (!signal.aborted) setLoadedKey(`${agentId}:${sessionId}`);
				}
			}

			// 2. Recover both rejected reads and a clean EOF. Retry only reads:
			// replaying POST /chat would execute the user's request twice.
			let reconnecting = false;
			let attempts = 0;
			while (!signal.aborted) {
				try {
					if (reconnecting) {
						const snapshot = await readHistory(true);
						if (signal.aborted) return;
						eventCursor = snapshot!.cursor;
						interrupted = snapshot!.interrupted;
					}
					for await (const event of sessionApi.streamEvents(
						sessionId, agentId, signal, eventCursor,
						() => {
							if (signal.aborted) return;
							setError(null);
							setConnectionNotice(interrupted ? 'interrupted' : null);
						},
					)) {
						if (signal.aborted) return;
						attempts = 0;
						processEvent(event);
					}
					if (signal.aborted) return;
					throw new Error('Session stream disconnected.');
				} catch (e) {
					if (signal.aborted) return;
					setError(e as Error);
					clearInterruptTimer();
					if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
						setConnectionNotice('failed');
						setPhase('idle');
						return;
					}
					setConnectionNotice('reconnecting');
				}
				reconnecting = true;
				await waitForReconnect(signal, Math.min(1000 * 2 ** Math.min(attempts++, 4), 10_000));
			}
		})();

		return () => {
			controller.abort();
			abortRef.current = null;
			clearInterruptTimer();
			if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
			rafRef.current = null;
		};
	}, [agentId, sessionId, historyAttempt, scheduleUpdate, processEvent, audioManager, clearInterruptTimer]);

	/**
	 * Idle messages use /chat immediately; only busy/backlogged conversations
	 * use the waiting queue. The server rechecks admission across tabs.
	 *
	 * @param content - The message content blocks.
	 */
	const send = useCallback(
		async (content: ContentBlock[]) => {
			if (!agentId || !sessionId || connectionNotice === 'reconnecting' || connectionNotice === 'failed') throw new Error('Session is not connected.');
			const key = `${agentId}:${sessionId}`;
			const signal = abortRef.current?.signal;
			const authSignal = getAuthSignal();
			const current = () => !signal?.aborted && !authSignal.aborted;
			// Keep the same ID across an ambiguous failure, including a retry
			// which now sees the first submission running on the server.
			const signature = JSON.stringify(content, (field, value) => ['id', 'created_at', 'finished_at'].includes(field) ? undefined : value);
			if (sendAttempt.current?.key !== key || sendAttempt.current.signature !== signature) {
				sendAttempt.current = { key, signature, message: UserMsg({ name: 'user', content }) };
			}
			const attempt = sendAttempt.current;
			const message = attempt.message;
			const queue = queueState;
			const busy = sendingDirect.current === key || phase !== 'idle' || !!(queue && (
				queue.activity !== 'idle' || queue.items.length || queue.editing_id || (queue.paused && queue.pause_reason === 'manual')
			));
			const direct = queueUnavailable || !busy;
			const removeOptimistic = () => {
				if (!current() || confirmedUserIds.current.has(message.id)) return;
				msgsRef.current = msgsRef.current.filter((existing) => existing.id !== message.id);
				scheduleUpdate();
			};
			setConnectionNotice(null);
			try {
				if (direct) {
					sendingDirect.current = key;
					if (!msgsRef.current.some((existing) => existing.id === message.id)) msgsRef.current = [...msgsRef.current, { ...message, metadata: { ...message.metadata, executionProgress: { stage: 'SENDING' } } }];
					scheduleUpdate();
					setPhase('streaming');
					try {
						const result = await chatApi.trigger({ agent_id: agentId, session_id: sessionId, input: message });
						if (result.status === 'queued') removeOptimistic();
                        else if (current()) {
                            msgsRef.current = msgsRef.current.map((item) => item.id === message.id &&
                                (!item.metadata?.executionProgress || (item.metadata.executionProgress as {stage?: string}).stage === 'SENDING')
                                ? { ...item, metadata: { ...item.metadata, executionProgress: { stage: 'PREPARING' } } } : item);
                            scheduleUpdate();
                        }
						if (current() && !queueUnavailable) void refreshQueue();
					} catch (e) {
						removeOptimistic();
						if (current()) {
							if (!confirmedUserIds.current.has(message.id)) setPhase('idle');
							if (!queueUnavailable) void refreshQueue();
						}
						throw e;
					} finally {
						if (sendingDirect.current === key) sendingDirect.current = null;
					}
				} else {
					await submitQueued(message);
				}
				if (sendAttempt.current === attempt) sendAttempt.current = null;
			} catch (e) {
				if (current()) setError(e as Error);
				throw e;
			}
		},
		[agentId, sessionId, submitQueued, connectionNotice, queueUnavailable, scheduleUpdate, phase, queueState, refreshQueue],
	);

	/**
	 * Confirm or deny a tool call (human-in-the-loop). Sends a
	 * ``UserConfirmResultEvent`` through realtime control while voice mode
	 * is active, or through ``POST /chat/`` otherwise. Events arrive via
	 * SSE in both cases.
	 *
	 * @param toolCall - The tool call block to confirm/deny.
	 * @param confirm - Whether the user confirmed.
	 * @param replyId - The reply id the tool call belongs to.
	 * @param rules - Optional permission rules to attach.
	 */
	const onUserConfirm = useCallback(
		async (
			toolCall: ToolCallBlock,
			confirm: boolean,
			replyId: string,
			rules?: ToolCallBlock['suggested_rules'],
		) => {
			if (!agentId || !sessionId) return;

			// Restore the ref so continuation events (no REPLY_START)
			// have a target.
			currentReplyRef.current = msgsRef.current.find((m) => m.id === replyId) ?? null;

			const event: UserConfirmResultEvent = {
				type: EventType.USER_CONFIRM_RESULT,
				id: crypto.randomUUID(),
				created_at: new Date().toISOString(),
				reply_id: replyId,
				confirm_results: [
					{ confirmed: confirm, tool_call: toolCall, rules: rules ?? null },
				],
			};

			try {
				if (
					optionsRef.current?.isRealtimeAudioActive?.() &&
					optionsRef.current.sendRealtimeUserConfirm
				) {
					await optionsRef.current.sendRealtimeUserConfirm(event);
				} else {
					await chatApi.trigger({
						agent_id: agentId,
						session_id: sessionId,
						input: event,
					});
				}
			} catch (e) {
				setError(e as Error);
				throw e;
			}
		},
		[agentId, sessionId],
	);

	/** Abort the current SSE connection. */
	const abort = useCallback(() => {
		abortRef.current?.abort();
	}, []);

	/**
	 * Request interruption of the in-progress reply (running or parked
	 * on HITL). Optimistically moves ``phase`` to ``interrupting`` so
	 * the UI can disable the Stop button; the phase reverts to
	 * ``idle`` when the backend's terminating ``ReplyEndEvent``
	 * arrives via SSE (or after a 10s safety timeout, in case that
	 * event is lost).
	 *
	 * Backend contract:
	 * - 202: interrupt was accepted (cancel signal broadcast for a
	 *   running reply, or wakeup enqueued for a parked one). The
	 *   resulting ``ReplyEndEvent`` arrives through the SSE stream and
	 *   drives the phase transition.
	 * - Idle sessions are a silent no-op at the agent layer, so
	 *   spamming this callback is safe.
	 */
	const interrupt = useCallback(async () => {
		if (!agentId || !sessionId) return;
		// Only escalate to ``interrupting`` if a reply is actually in
		// flight; if we're already idle (SSE completed just before the
		// click) leave the phase alone.
		setPhase((prev) => (prev === 'streaming' ? 'interrupting' : prev));
		clearInterruptTimer();
		interruptTimerRef.current = setTimeout(() => {
			interruptTimerRef.current = null;
			setPhase((prev) => (prev === 'interrupting' ? 'idle' : prev));
		}, INTERRUPT_TIMEOUT_MS);
		try {
			await sessionApi.interrupt(sessionId, agentId);
		} catch (e) {
			clearInterruptTimer();
			setPhase((prev) => (prev === 'interrupting' ? 'idle' : prev));
			setError(e as Error);
		}
	}, [agentId, sessionId, clearInterruptTimer]);

	/**
	 * Confirm or deny a tool call that a *team member* is awaiting,
	 * from this leader view (design §3.6 — backend routing).
	 *
	 * The result is POSTed to the **leader** session (the
	 * ``(agentId, sessionId)`` this hook is bound to), NOT the worker.
	 * The backend resolves ``reply_id`` → worker session via the
	 * leader's pending hash and forwards the event to the worker's
	 * continuation. The client never addresses the worker directly —
	 * ``entry.worker_*`` ids are used only for local dedup / clearing.
	 *
	 * @param entry - The pending subagent HITL entry being resolved.
	 * @param toolCall - The tool call block to confirm/deny.
	 * @param confirm - Whether the user confirmed.
	 * @param rules - Optional permission rules to attach.
	 */
	const onSubagentConfirm = useCallback(
		async (
			entry: SubagentHitlEntry,
			toolCall: ToolCallBlock,
			confirm: boolean,
			rules?: ToolCallBlock['suggested_rules'],
		) => {
			if (!agentId || !sessionId) return;

			const event: UserConfirmResultEvent = {
				type: EventType.USER_CONFIRM_RESULT,
				id: crypto.randomUUID(),
				created_at: new Date().toISOString(),
				reply_id: entry.reply_id, // worker's reply_id; backend maps it
				confirm_results: [
					{ confirmed: confirm, tool_call: toolCall, rules: rules ?? null },
				],
			};

			try {
				// Post to the leader front door — backend routes to the
				// worker session (§3.6). Do NOT address the worker here.
				await chatApi.trigger({
					agent_id: agentId,
					session_id: sessionId,
					input: event,
				});
			} catch (e) {
				setError(e as Error);
				// Rethrow so the card can re-enable itself and be retried.
				throw e;
			}

			// Drop only the call just answered — an entry can carry several
			// pending tool calls, and clearing the whole entry would take the
			// unanswered siblings' cards down with it. The backend's clear
			// event removes whatever is left.
			setSubagentHitl((prev) =>
				prev.flatMap((x) => {
					if (hitlKey(x) !== hitlKey(entry)) return [x];
					const remaining = (x.event.tool_calls ?? []).filter(
						(tc) => tc.id !== toolCall.id,
					);
					return remaining.length > 0
						? [{ ...x, event: { ...x.event, tool_calls: remaining } }]
						: [];
				}),
			);
		},
		[agentId, sessionId],
	);

	const currentKey = agentId !== null && sessionId !== null ? `${agentId}:${sessionId}` : null;
	const ownsConversation = currentKey !== null && loadedKey === currentKey;
	const loading = currentKey !== null && !ownsConversation;

	return {
		msgs: ownsConversation ? msgs : [],
		loading,
		phase: ownsConversation ? (phase === 'idle' && messageQueue.state && messageQueue.state.activity !== 'idle' ? 'streaming' as ReplyPhase : phase) : ('idle' as ReplyPhase),
		messageQueue,
		error: ownsConversation ? error : null,
		connectionNotice: ownsConversation ? connectionNotice : null,
		historyError: ownsConversation ? historyError : null,
		reloadHistory,
		send,
		onUserConfirm,
		onSubagentConfirm,
		subagentHitl: ownsConversation ? subagentHitl : [],
		abort,
		interrupt,
	};
}
