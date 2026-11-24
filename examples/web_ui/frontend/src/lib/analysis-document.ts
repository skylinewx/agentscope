export interface FreeAnalysisDocument {
    version: 'free-html-1'; title: string; html: string; contentDigest: string; frameKey: string; bindingCount: number; datasetCount?: number;
}

export function parseFreeDocument(value: unknown): FreeAnalysisDocument {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_VIEW');
    const d = value as Record<string, unknown>;
    if (typeof d.version !== 'string' || !d.version.trim()) throw new Error('INVALID_VIEW');
    if (d.version !== 'free-html-1') throw new Error('UNSUPPORTED_VERSION');
    // The server verifies source evidence. A compiled Markdown report or trend can use zero dynamic bindings.
    if (typeof d.title !== 'string' || !d.title.trim() || d.title.length > 300
        || typeof d.html !== 'string' || !d.html.trim() || new TextEncoder().encode(d.html).byteLength > 768 * 1024
        || typeof d.contentDigest !== 'string' || !/^[0-9a-f]{64}$/.test(d.contentDigest)
        || typeof d.frameKey !== 'string' || !/^[0-9a-f]{64}$/.test(d.frameKey)
        || typeof d.bindingCount !== 'number' || !Number.isInteger(d.bindingCount) || d.bindingCount < 0 || d.bindingCount > 2000
        || (d.datasetCount !== undefined && (typeof d.datasetCount !== 'number' || !Number.isInteger(d.datasetCount) || d.datasetCount < 0 || d.datasetCount > 50))) throw new Error('INVALID_VIEW');
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
        const { hostRenderedText, ...diagnostics } = m.measurements as Record<string, unknown>;
        if (new TextEncoder().encode(JSON.stringify(diagnostics)).length > 60000) return null;
        if (new TextEncoder().encode(JSON.stringify(m.measurements)).length > 2 * 1024 * 1024) return null;
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
