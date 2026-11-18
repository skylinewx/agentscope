import type { Msg } from '@agentscope-ai/agentscope/message';

import { client } from './client';

export interface QueuedMessage {
	message: Msg;
	sequence: number;
	version: number;
	enqueued_at: string;
}

export interface MessageQueueState {
	revision: number;
	items: QueuedMessage[];
	active_id: string | null;
	activity: 'idle' | 'running' | 'waiting';
	waiting_reason: 'permission' | 'external' | 'background' | null;
	paused: boolean;
	pause_reason: 'manual' | 'stopped' | 'error' | 'interrupted' | null;
	editing_id: string | null;
}

export type QueueOperation = 'pause' | 'resume' | 'begin_edit' | 'save' | 'cancel_edit' | 'delete' | 'restore';

export const messageQueueApi = {
	get: (agentId: string, sessionId: string, signal?: AbortSignal) =>
		client.get<MessageQueueState>(`/sessions/${sessionId}/queue`, { agent_id: agentId }, { silent: true, signal, timeoutMs: 10000 }),
	add: (agentId: string, sessionId: string, message: Msg) =>
		client.post<MessageQueueState>(`/sessions/${sessionId}/queue`, { message }, { agent_id: agentId }, { timeoutMs: 30000 }),
	action: (agentId: string, sessionId: string, operation: QueueOperation, item?: QueuedMessage, message?: Msg) =>
		client.post<MessageQueueState>(`/sessions/${sessionId}/queue/actions`, {
			operation, item_id: item?.message.id, version: item?.version, message,
		}, { agent_id: agentId }, { timeoutMs: 30000 }),
};
