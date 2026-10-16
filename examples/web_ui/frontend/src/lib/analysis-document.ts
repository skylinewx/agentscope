export interface FreeAnalysisDocument {
    version: 'free-html-1'; title: string; html: string; contentDigest: string; frameKey: string; bindingCount: number; datasetCount?: number;
}

export function parseFreeDocument(value: unknown): FreeAnalysisDocument {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_VIEW');
    const d = value as Record<string, unknown>;
    if (d.version !== 'free-html-1' || typeof d.title !== 'string' || !d.title || d.title.length > 300
        || typeof d.html !== 'string' || !d.html || new TextEncoder().encode(d.html).byteLength > 768 * 1024
        || typeof d.contentDigest !== 'string' || !/^[0-9a-f]{64}$/.test(d.contentDigest)
        || typeof d.frameKey !== 'string' || !/^[0-9a-f]{64}$/.test(d.frameKey)
        || typeof d.bindingCount !== 'number' || !Number.isInteger(d.bindingCount) || d.bindingCount < 0 || d.bindingCount > 2000
        || (d.datasetCount !== undefined && (typeof d.datasetCount !== 'number' || !Number.isInteger(d.datasetCount) || d.datasetCount < 0 || d.datasetCount > 50))
        || (d.bindingCount === 0 && !d.datasetCount)) throw new Error('INVALID_VIEW');
    return d as unknown as FreeAnalysisDocument;
}

/** Other frames, stale documents and arbitrary messages cannot resize a card. */
export function documentHeight(data: unknown, expectedKey: string): number | null {
    if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
    const message = data as Record<string, unknown>;
    if (message.type !== 'analysis-document-size' || message.key !== expectedKey || typeof message.height !== 'number'
        || !Number.isInteger(message.height) || message.height < 160 || message.height > 24000) return null;
    return message.height;
}

/** Only the current frame/request may submit bounded observations for server validation. */
export function documentInspection(data: unknown, expectedKey: string, requestId?: string): unknown | null {
    if (!requestId || !data || typeof data !== 'object' || Array.isArray(data)) return null;
    const m = data as Record<string, unknown>;
    if (m.type !== 'analysis-document-inspection' || m.key !== expectedKey || m.requestId !== requestId
        || !m.measurements || typeof m.measurements !== 'object' || Array.isArray(m.measurements)) return null;
    try {
        if (new TextEncoder().encode(JSON.stringify(m.measurements)).length > 60000) return null;
    } catch { return null; }
    return m.measurements;
}

/** Downloaded pages wrap the exact same content in an isolated standalone frame. */
export function freeReportContent(html: string): string {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const inner = doc.querySelector('iframe[data-analysis-document="free-html-1"]')?.getAttribute('srcdoc');
    if (!inner) throw new Error('INVALID_VIEW');
    return inner;
}
