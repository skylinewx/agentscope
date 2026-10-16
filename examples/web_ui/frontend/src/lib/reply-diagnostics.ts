export type ModelDiagnostics = {callId:string; phase:string; failureKind:string; elapsedMs:number; attempt:number};
export function modelDiagnostics(error: unknown): ModelDiagnostics | null {
    if (!error || typeof error !== 'object' || !('diagnostics' in error)) return null;
    const d = error.diagnostics;
    if (!d || typeof d !== 'object') return null;
    const v = d as Record<string,unknown>;
    if (typeof v.callId !== 'string' || !/^[a-f0-9]{32}$/.test(v.callId) ||
        !['request','stream','structured_output','stream_cleanup'].includes(String(v.phase)) ||
        !['http','timeout','protocol','connection','structured_output','unknown'].includes(String(v.failureKind)) ||
        typeof v.elapsedMs !== 'number' || !Number.isFinite(v.elapsedMs) || v.elapsedMs < 0 ||
        typeof v.attempt !== 'number' || !Number.isSafeInteger(v.attempt) || v.attempt < 1) return null;
    return {callId:v.callId,phase:String(v.phase),failureKind:String(v.failureKind),elapsedMs:v.elapsedMs,attempt:v.attempt};
}
export function replyErrorKey(error: unknown): string {
    const type = error && typeof error === 'object' && 'type' in error ? error.type : 'unknown';
    const detail = modelDiagnostics(error);
    if (type === 'connection' && detail) {
        if (detail.failureKind === 'timeout') return 'connection_timeout';
        if (detail.phase === 'stream') return 'connection_stream';
        if (detail.failureKind === 'protocol') return 'connection_protocol';
    }
    return typeof type === 'string' ? type : 'unknown';
}
