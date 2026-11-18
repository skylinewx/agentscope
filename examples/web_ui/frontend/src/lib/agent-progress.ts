import type { AgentEvent } from '@agentscope-ai/agentscope/event';
import type { Msg } from '@agentscope-ai/agentscope/message';

export type ProgressStep = {
	id: string;
	title?: string;
	status: 'running' | 'completed' | 'waiting' | 'error' | 'interrupted';
	summary: string;
	nextAction: string;
};
export type AgentProgress = { version: 1; replyId: string; revision: number; steps: ProgressStep[] };

export function readAgentProgress(value: unknown): AgentProgress | null {
	if (!value || typeof value !== 'object') return null;
	const progress = value as AgentProgress;
	if (progress.version !== 1 || typeof progress.replyId !== 'string' ||
		!Number.isSafeInteger(progress.revision) || progress.revision < 0 ||
		!Array.isArray(progress.steps) || progress.steps.length > 96 ||
		!progress.steps.every((step) => step && typeof step.id === 'string' &&
			(step.title === undefined || (typeof step.title === 'string' && step.title.length <= 200)) &&
			typeof step.summary === 'string' && step.summary.length <= 4000 &&
			typeof step.nextAction === 'string' && step.nextAction.length <= 2000 &&
			['running', 'completed', 'waiting', 'error', 'interrupted'].includes(step.status))) return null;
	if (new Set(progress.steps.map((step) => step.id)).size !== progress.steps.length) return null;
	return progress;
}

/** Full snapshots make replay idempotent; a late update cannot reopen a reply. */
export function applyAgentProgress(message: Msg, event: AgentEvent): boolean {
	if (event.type !== 'CUSTOM' || event.name !== 'agent_progress') return false;
	const next = readAgentProgress(event.value);
	if (!next || next.replyId !== message.id || message.finished_at) return false;
	const old = readAgentProgress(message.metadata?.agentProgress);
	if (old && next.revision <= old.revision) return false;
	message.metadata = { ...message.metadata, agentProgress: structuredClone(next) };
	return true;
}

export function progressSteps(message: Msg): ProgressStep[] {
	return readAgentProgress(message.metadata?.agentProgress)?.steps ?? [];
}
