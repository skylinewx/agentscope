import { ApiError, client, getBaseUrl } from './client';
import { ANALYSIS_VIEW_MAX_BYTES, parseDelivery, parseSnapshot, parseActionResolution, type ExportDelivery, type ViewParameters } from '@/lib/workspace-analysis';
import { REPORT_PREVIEW_MAX_BYTES } from '@/lib/workspace-reports';

export type AnalysisWorkspace = { agentId: string; sessionId: string };
export type PreviewCheck = {contentDigest: string; status: 'ERRORS' | 'REVIEW_REQUIRED' | 'MEASURED'; viewport: {width:number;height:number}; visualSemanticsValidated: false};
const params = (w: AnalysisWorkspace) => ({ agent_id: w.agentId, session_id: w.sessionId });

async function bounded(response: Response, maximum: number) {
	if (Number(response.headers.get('content-length')) > maximum) { await response.body?.cancel(); throw new ApiError(413, 'Analysis content is too large.'); }
	const reader = response.body?.getReader();
	if (!reader) throw new Error('EMPTY_VIEW');
	const decoder = new TextDecoder('utf-8', { fatal: true });
	let size = 0, content = '';
	try {
		while (true) {
			const { done, value } = await reader.read(); if (done) break;
			size += value.byteLength;
			if (size > maximum) { await reader.cancel(); throw new ApiError(413, 'Analysis content is too large.'); }
			content += decoder.decode(value, { stream: true });
		}
		return content + decoder.decode();
	} finally { reader.releaseLock(); }
}

export const analysisApi = {
	async publicationPreview(w: AnalysisWorkspace, previewRef: string, signal: AbortSignal) {
		const query = { ...params(w), preview_ref: previewRef };
		const [data, html] = await Promise.all([
			bounded(await client.stream('/workspace/analyses/preview', {params: {...query, format:'json'}, signal, silent:true}), ANALYSIS_VIEW_MAX_BYTES),
			bounded(await client.stream('/workspace/analyses/preview', {params: {...query, format:'html'}, signal, silent:true}), REPORT_PREVIEW_MAX_BYTES),
		]);
		const result = JSON.parse(data);
		if (result.previewOnly !== true || result.publicationState !== 'DRAFT') throw new Error('INVALID_PREVIEW');
		return { view: parseSnapshot(result.document), html, diagnostics: result.diagnostics as Record<string, unknown> };
	},
	async recordPreviewCheck(w: AnalysisWorkspace, previewRef: string, contentDigest: string, frameKey: string, measurements: unknown): Promise<PreviewCheck> {
		const response = await client.stream('/workspace/analyses/preview-check', {method:'POST', params:params(w),
			body:{previewRef,contentDigest,frameKey,measurements}, silent:true, timeoutMs:10000});
		const result = JSON.parse(await bounded(response, 65536));
		if (result.contentDigest !== contentDigest || !['ERRORS','REVIEW_REQUIRED','MEASURED'].includes(result.status)
			|| result.visualSemanticsValidated !== false || !Number.isInteger(result.viewport?.width)) throw new Error('INVALID_PREVIEW_CHECK');
		return result;
	},
	async resolveAction(w: AnalysisWorkspace, actionRef: string, signal: AbortSignal) {
		const response = await client.stream('/workspace/analyses/resolve-action', { method: 'POST', body: { actionRef }, params: params(w), signal, silent: true });
		return parseActionResolution(JSON.parse(await bounded(response, 65536)), actionRef);
	},
	async view(w: AnalysisWorkspace, path: string, signal: AbortSignal) {
		const response = await client.stream('/workspace/analyses/view', { params: { ...params(w), path }, signal, silent: true });
		return parseSnapshot(JSON.parse(await bounded(response, ANALYSIS_VIEW_MAX_BYTES)));
	},
	async project(w: AnalysisWorkspace, analysisRef: string, viewParameters: ViewParameters, offset: number, signal: AbortSignal, limit = 12) {
		const response = await client.stream('/workspace/analyses/project-view', { method: 'POST', body: { analysisRef, viewParameters, offset, limit }, params: params(w), signal, silent: true });
		return parseSnapshot(JSON.parse(await bounded(response, ANALYSIS_VIEW_MAX_BYTES)));
	},
	async restore(w: AnalysisWorkspace, path: string, signal: AbortSignal) {
		const response = await client.stream('/workspace/analyses/restore', { method: 'POST', body: { path }, params: params(w), signal, silent: true });
		return parseSnapshot(JSON.parse(await bounded(response, ANALYSIS_VIEW_MAX_BYTES)));
	},
	async render(w: AnalysisWorkspace, analysisRef: string, viewParameters: ViewParameters, signal: AbortSignal): Promise<ExportDelivery> {
		const response = await client.stream('/workspace/analyses/render-report', { method: 'POST',
			body: { analysisRef, requestKey: `ui:1:${analysisRef}:${JSON.stringify(viewParameters)}`, viewParameters }, params: params(w), signal, silent: true });
		return parseDelivery(JSON.parse(await bounded(response, ANALYSIS_VIEW_MAX_BYTES)), analysisRef);
	},
	async preview(w: AnalysisWorkspace, ref: ExportDelivery, signal: AbortSignal) {
		return bounded(await client.stream('/workspace/analyses/report', { params: { ...params(w), analysis_ref: ref.analysisRef, export_ref: ref.exportRef }, signal, silent: true }), REPORT_PREVIEW_MAX_BYTES);
	},
	async downloadUrl(w: AnalysisWorkspace, ref: ExportDelivery, signal: AbortSignal) {
		const query = { ...params(w), analysis_ref: ref.analysisRef, export_ref: ref.exportRef };
		const response = await client.stream('/workspace/analyses/report-token', { method: 'POST', params: query, signal, silent: true });
		const token = JSON.parse(await bounded(response, 4096)) as { token: string };
		const url = new URL('/workspace/analyses/report', getBaseUrl());
		Object.entries({ ...query, token: token.token, download: 'true' }).forEach(([key, value]) => url.searchParams.set(key, value));
		return url.toString();
	},
};
