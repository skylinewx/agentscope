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
	const ids = answerIds(event.metadata);
	if (ids !== null) {
		message.metadata = { ...message.metadata, presentation: { answer_block_ids: [...ids] } };
	}
}

function hasDeliveredCard(block: ContentBlock): boolean {
	// Keep user-facing result links visible even when a middleware publishes
	// an artifact before the final model invocation. Never inspect tool output.
	return block.type === 'text' && /\]\(\.\/(?:[^\s)]+\.html?|analysis\/ana_[\da-f]{32}\/revisions\/[1-9]\d*\/analysis\.json)\)/i.test(block.text);
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

/** Partition without changing the stored message or dropping any content. */
export function presentReply(message: Msg): ReplyPresentation {
	const ids = answerIds(message.metadata);
	const selected = ids === null ? null : new Set(ids);
	// Unclassified/streaming messages retain the complete text tail, not just
	// the last TextBlock. This also presents existing saved conversations.
	const keepPartial = message.finished_reason === 'error' || message.finished_reason === 'interrupted';
	const lastBoundary = message.content.findLastIndex((block) =>
		block.type === 'tool_call' || block.type === 'tool_result' || (!keepPartial && block.type === 'thinking'));
	const process: ContentBlock[] = [];
	const answer: ContentBlock[] = [];
	const calls = new Set<string>();
	const thoughts = new Set<string>();
	const reports = new Map<string, number>();
	const analyses = new Set<string>();
	for (const [index, block] of message.content.entries()) {
		const visible = message.role !== 'assistant' || block.type === 'data' || hasDeliveredCard(block) ||
			(block.type === 'text' && (selected ? selected.has(block.id) : index > lastBoundary));
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
			: pending.length ? 'external' : null,
	};
}
