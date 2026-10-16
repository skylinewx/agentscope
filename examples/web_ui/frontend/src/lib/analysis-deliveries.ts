import type { Msg } from '@agentscope-ai/agentscope/message';

import { analysisPath } from './workspace-analysis.ts';

export type RegisteredAnalysis = { analysisRef: string; path: string };
const object = (value: unknown): Record<string, unknown> | null =>
	typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null;

function jsonObject(value: unknown): Record<string, unknown> | null {
	if (typeof value !== 'string') return object(value);
	try { return object(JSON.parse(value)); } catch { return null; }
}

/** Actual delivery receipts, independent of whether the model repeats their links. */
export function registeredAnalyses(message: Msg): RegisteredAnalysis[] {
	if (message.role !== 'assistant') return [];
	const calls = new Map(message.content.filter((block) => block.type === 'tool_call').map((block) => [block.id, block]));
	const delivered = new Map<string, RegisteredAnalysis>();
	for (const block of message.content) {
		if (block.type !== 'tool_result' || block.state !== 'success') continue;
		const call = calls.get(block.id);
		if (!call || call.name !== block.name || !['AnalysisPublish', 'AnalysisInspect'].includes(call.name)) continue;
		const input = jsonObject(call.input);
		if (call.name === 'AnalysisPublish' && input?.previewOnly === true) continue;
		if (call.name === 'AnalysisInspect' && input?.section !== 'delivery') continue;
		const summary = object(object(block.metadata)?.process_summary);
		if (summary?.kind !== 'analysis') continue;
		// Only the paired built-in tool's structured receipt can register a card.
		// Ordinary tool text, model links and preview/error responses cannot.
		if (!Array.isArray(block.output) || block.output.length !== 1 || block.output[0].type !== 'text') continue;
		const response = jsonObject(block.output[0].text);
		if (!response || response.error || response.previewOnly === true) continue;
		if (call.name === 'AnalysisPublish' && response.publicationState !== 'PUBLISHED') continue;
		const delivery = object(response.delivery ?? response);
		if (delivery?.type !== 'workspace_analysis' || delivery.registered !== true || typeof delivery.path !== 'string') continue;
		const path = analysisPath(delivery.path);
		if (!path) continue;
		const [, id, revision] = /^\.\/analysis\/(ana_[0-9a-f]{32})\/revisions\/([1-9][0-9]{0,8})\/analysis\.json$/.exec(path)!;
		const analysisRef = `${id}:${revision}`;
		if (delivery.analysisRef !== analysisRef || summary.id !== id) continue;
		if (input?.analysisRef !== analysisRef || (call.name === 'AnalysisPublish' && response.analysisRef !== analysisRef)) continue;
		delivered.set(path, { analysisRef, path });
	}
	return [...delivered.values()];
}
