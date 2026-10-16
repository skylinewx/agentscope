import { useEffect, useRef, useState } from 'react';
import { documentHeight, documentInspection, type FreeAnalysisDocument as Document } from '@/lib/analysis-document';
import { prepareReportPreview } from '@/lib/workspace-reports';

export function FreeAnalysisDocument({ document, inspectionRequest, onInspection }: {
    document: Document; inspectionRequest?: string; onInspection?: (measurements: unknown) => void;
}) {
    const frame = useRef<HTMLIFrameElement>(null);
    const [height, setHeight] = useState(480);
    // Policy enforcement is outside the model's document. No same-origin access.
    const [html] = useState(() => prepareReportPreview(document.html));
    useEffect(() => {
        const receive = (event: MessageEvent) => {
            if (event.source !== frame.current?.contentWindow) return;
            const next = documentHeight(event.data, document.frameKey);
            if (next !== null) setHeight(current => current === next ? current : next);
            const measurements = documentInspection(event.data, document.frameKey, inspectionRequest);
            if (measurements !== null) onInspection?.(measurements);
        };
        window.addEventListener('message', receive);
        return () => window.removeEventListener('message', receive);
    }, [document.frameKey, inspectionRequest, onInspection]);
    useEffect(() => {
        if (inspectionRequest) frame.current?.contentWindow?.postMessage({type:'analysis-document-inspect',key:document.frameKey,requestId:inspectionRequest}, '*');
    }, [document.frameKey, inspectionRequest]);
    return <iframe ref={frame} title={document.title} sandbox="allow-scripts" referrerPolicy="no-referrer"
        srcDoc={html} className="block w-full min-w-0 border-0 bg-transparent" style={{ height }}
        onLoad={() => frame.current?.contentWindow?.postMessage({ type: 'analysis-document-measure', key: document.frameKey }, '*')} />;
}
