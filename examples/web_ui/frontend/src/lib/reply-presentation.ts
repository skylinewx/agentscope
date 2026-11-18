import type { AgentEvent } from '@agentscope-ai/agentscope/event';
import type { ContentBlock, Msg, TextBlock, ToolCallBlock } from '@agentscope-ai/agentscope/message';

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue | null =>
	typeof value === 'object' && value !== null && !Array.isArray(value)
		? value as RecordValue : null;

function answerIds(metadata: unknown): string[] | null {
	const ids = record(record(metadata)?.presentation)?.answer_block_ids;
	return Array.isArray(ids) && ids.every((id) => typeof id === 'string') ? ids : null;
}

/** The installed SDK forwards block metadata, but does not yet keep reply metadata. */
export function applyReplyPresentation(message: Msg, event: AgentEvent): void {
	if (event.type !== 'REPLY_END' || event.reply_id !== message.id) return;
	// Match the server's message aggregation for every reply middleware.
	// Consumers validate their own metadata instead of losing unknown keys here.
	// Wire events are JSON; the SDK types their metadata values as unknown.
	const metadata = structuredClone(event.metadata ?? {}) as Msg['metadata'];
	message.metadata = { ...message.metadata, ...metadata };
}

function hasDeliveredCard(block: ContentBlock): boolean {
	// Keep user-facing result links visible even when a middleware publishes
	// an artifact before the final model invocation. Never inspect tool output.
	return block.type === 'text' && /\]\(\.\/(?:[^\s)]+\.html?|analysis\/ana_[\da-f]{32}\/revisions\/[1-9]\d*\/analysis\.json)\)/i.test(block.text);
}

export function isCompletionRevision(block: ContentBlock): boolean {
	if (block.type !== 'hint') return false;
	try {
		const source = record(JSON.parse(block.source ?? 'null'));
		return source?.label === 'System' && source.sublabel === 'Completion check';
	} catch { return false; }
}

export function pendingCalls(message: Msg): ToolCallBlock[] {
	if (message.finished_at) return [];
	const finished = new Set(message.content
		.filter((block) => block.type === 'tool_result' && block.state !== 'running')
		.map((block) => block.id));
	return message.content.filter((block): block is ToolCallBlock =>
		block.type === 'tool_call' && !finished.has(block.id) &&
		(block.state === 'asking' || block.state === 'submitted'));
}

export type ReplyPresentation = {
	process: ContentBlock[];
	answer: ContentBlock[];
	steps: TextBlock[];
	toolCount: number;
	thinkingCount: number;
	reportCount: number;
	analysisCount: number;
	topicCount: number;
	pending: 'confirmation' | 'external' | null;
};

/** Resolve background waits across reply segments without rewriting saved messages. */
export function backgroundWaitStates(messages: Msg[]): Map<string, boolean> {
	const settled = new Set<string>();
	const turns: Msg[][] = [[]];
	for (const message of messages) {
		if (message.role === 'user') turns.push([]);
		if (message.role !== 'assistant') continue;
		turns.at(-1)!.push(message);
		for (const block of message.content) {
			if (block.type === 'tool_result' && block.state !== 'running' &&
				!record(block.metadata)?.backgroundToolCallId) settled.add(block.id);
			if (block.type !== 'hint') continue;
			try {
				const source = record(JSON.parse(block.source ?? 'null'));
				if (source?.label === 'tool_output' && typeof source.backgroundToolCallId === 'string' &&
					['success', 'error', 'denied', 'cancelled'].includes(String(source.backgroundState))) {
					settled.add(source.backgroundToolCallId);
				}
			} catch { /* An unrelated or incomplete hint cannot settle a tool call. */ }
		}
	}

	const waiting = new Map<string, boolean>();
	for (const replies of turns) {
		const calls = new Set<string>();
		let latestWaiting: string | null = null;
		for (const message of replies) {
			waiting.set(message.id, false);
			const completion = record(record(message.metadata)?.analysisCompletion);
			for (const block of message.content) {
				if (block.type !== 'tool_result') continue;
				const callId = record(block.metadata)?.backgroundToolCallId;
				if (typeof callId === 'string' && callId) calls.add(callId);
			}
			if (message.finished_reason && message.finished_reason !== 'completed') {
				latestWaiting = null;
				calls.clear();
			} else if (completion?.status === 'WAITING') {
				// A waiting snapshot can include jobs started by an earlier reply.
				// The synthetic successful tool result only acknowledges offloading.
				const pendingIds = completion.pendingToolCallIds;
				const hasSnapshot = Array.isArray(pendingIds) && pendingIds.every((id) => typeof id === 'string' && id.length > 0);
				const knownIds = hasSnapshot ? pendingIds : [...calls];
				const hasPending = knownIds.some((id) => !settled.has(id));
				latestWaiting = hasPending || (knownIds.length === 0 && !hasSnapshot) ? message.id : null;
			} else if (message.finished_reason === 'completed' &&
				['DIRECT', 'READY', 'PARTIAL', 'BLOCKED'].includes(String(completion?.status))) {
				// A terminal result ends this user turn, including cancellation/error.
				// A later user turn's result must never clear an earlier turn's wait.
				latestWaiting = null;
				calls.clear();
			}
		}
		if (latestWaiting) waiting.set(latestWaiting, true);
	}
	return waiting;
}

/** Keep process events inspectable; guarded candidate answers require approval. */
export function presentReply(message: Msg, backgroundWaiting?: boolean): ReplyPresentation {
	const ids = answerIds(message.metadata);
	const selected = ids === null ? null : new Set(ids);
	const completion = record(record(message.metadata)?.analysisCompletion);
	const guarded = record(record(message.metadata)?.presentation)?.final_only === true || !!completion ||
		message.content.some((block) => isCompletionRevision(block) ||
			(block.type === 'tool_call' && block.name === 'AnalysisTask'));
	const approved = (!!completion && ['DIRECT', 'READY', 'PARTIAL', 'BLOCKED'].includes(String(completion.status)) &&
		message.finished_reason === 'completed') || (record(record(message.metadata)?.presentation)?.terminal_summary === true &&
		message.finished_reason === 'error');
	// Unclassified/streaming messages retain the complete text tail, not just
	// the last TextBlock. This also presents existing saved conversations.
	const keepPartial = message.finished_reason === 'error' || message.finished_reason === 'interrupted';
	const lastBoundary = message.content.findLastIndex((block) =>
		block.type === 'tool_call' || block.type === 'tool_result' || (!keepPartial && block.type === 'thinking'));
	// Rejected drafts and embedded cards belong in neither answer nor process.
	const lastRevision = message.content.findLastIndex(isCompletionRevision);
	const process: ContentBlock[] = [];
	const answer: ContentBlock[] = [];
	const calls = new Set<string>();
	const thoughts = new Set<string>();
	const reports = new Map<string, number>();
	const analyses = new Set<string>();
	for (const [index, block] of message.content.entries()) {
		const deliverable = block.type === 'text' || block.type === 'data';
		if (message.role === 'assistant' && guarded && deliverable) {
			if (approved && index > lastRevision && selected?.has(block.id)) answer.push(block);
			continue;
		}
		const visible = message.role !== 'assistant' || (index > lastRevision &&
			(block.type === 'data' || hasDeliveredCard(block) ||
				(block.type === 'text' && (selected ? selected.has(block.id) : index > lastBoundary))));
		(visible ? answer : process).push(block);
		if (block.type === 'tool_call' || block.type === 'tool_result') calls.add(block.id);
		if (block.type === 'thinking') thoughts.add(block.id);
		if (block.type !== 'tool_result' || block.state !== 'success') continue;
		const summary = record(record(block.metadata)?.process_summary);
		if (typeof summary?.id !== 'string' || !summary.id) continue;
		if (summary.kind === 'report') {
			const topics = typeof summary.topicCount === 'number' && Number.isSafeInteger(summary.topicCount) && summary.topicCount > 0
				? summary.topicCount : 0;
			reports.set(summary.id, topics);
		} else if (summary.kind === 'analysis') analyses.add(summary.id);
	}
	const pending = pendingCalls(message);
	return {
		process, answer,
		steps: process.filter((block): block is TextBlock => block.type === 'text' && !!block.text.trim()),
		toolCount: calls.size, thinkingCount: thoughts.size,
		reportCount: reports.size, analysisCount: analyses.size,
		// A topic total is unambiguous for one report; multiple reports may overlap.
		topicCount: reports.size === 1 ? [...reports.values()][0] : 0,
		pending: pending.some((call) => call.state === 'asking') ? 'confirmation'
			: pending.length || (backgroundWaiting ?? completion?.status === 'WAITING') ? 'external' : null,
	};
}

/** 后台续跑仍属于同一次提问；只合并等待后的连续回复，保留真实终止边界。 */
export function groupReplyMessages(messages: Msg[]): { key: string; message: Msg }[] {
	const groups: { key: string; message: Msg }[] = [];
	for (const message of messages) {
		const previous = groups.at(-1);
		const previousCompletion = record(record(previous?.message.metadata)?.analysisCompletion);
		const completion = record(record(message.metadata)?.analysisCompletion);
		const sameTurn = !previousCompletion?.turnKey || !completion?.turnKey ||
			previousCompletion.turnKey === completion.turnKey;
		if (!previous || previous.message.role !== 'assistant' || message.role !== 'assistant' ||
			previous.message.name !== message.name || !sameTurn || previousCompletion?.status !== 'WAITING' ||
			previous.message.finished_reason !== 'completed') {
			groups.push({ key: message.id, message });
			continue;
		}
		const usage = { input_tokens: 0, output_tokens: 0, ...previous.message.usage };
		for (const [key, value] of Object.entries(message.usage ?? {})) {
			if (typeof value === 'number') usage[key as keyof typeof usage] =
				(usage[key as keyof typeof usage] ?? 0) + value;
		}
		previous.message = {
			...message,
			metadata: {
				...previous.message.metadata, ...message.metadata,
				...(Array.isArray(record(message.metadata?.agentProgress)?.steps) ? { agentProgress: {
					...record(message.metadata.agentProgress),
					steps: [...(Array.isArray(record(previous.message.metadata?.agentProgress)?.steps)
						? record(previous.message.metadata?.agentProgress)!.steps as unknown[] : []),
						...(record(message.metadata.agentProgress)!.steps as unknown[] ?? [])].slice(-96),
				} as Msg['metadata'][string] } : {}),
			},
			created_at: previous.message.created_at,
			content: [...previous.message.content, ...message.content],
			usage,
		};
	}
	return groups;
}
