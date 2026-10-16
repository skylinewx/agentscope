import {useCallback, useEffect, useRef, useState} from 'react';
import {useParams} from 'react-router-dom';
import {useQuery} from '@tanstack/react-query';
import {analysisApi, type PreviewCheck} from '@/api/analysis';
import {getBaseUrl, getUserId} from '@/api/client';
import {AnalysisVisuals} from '@/components/chat/analysis/AnalysisVisuals';
import {FreeAnalysisDocument} from '@/components/chat/analysis/FreeAnalysisDocument';

export function AnalysisPreviewPage() {
    const {agentId = '', sessionId = '', previewRef = ''} = useParams();
    return <PreviewContent key={`${getBaseUrl()}|${getUserId()}|${agentId}|${sessionId}|${previewRef}`} agentId={agentId} sessionId={sessionId} previewRef={previewRef} />;
}

function PreviewContent({agentId,sessionId,previewRef}: {agentId:string;sessionId:string;previewRef:string}) {
    const [report, setReport] = useState(false);
    const [narrow, setNarrow] = useState(false);
    const [inspectionRequest, setInspectionRequest] = useState<string>();
    const [checking, setChecking] = useState(false);
    const [check, setCheck] = useState<PreviewCheck>();
    const [checkError, setCheckError] = useState('');
    const pending = useRef<string | undefined>(undefined);
    const query = useQuery({
        queryKey: ['analysis-preview', getBaseUrl(), getUserId(), agentId, sessionId, previewRef],
        queryFn: ({signal}) => analysisApi.publicationPreview({agentId, sessionId}, previewRef, signal),
        enabled: Boolean(agentId && sessionId && /^proposal_[0-9a-f]{64}$/.test(previewRef)),
        retry: false, staleTime: 0, gcTime: 0,
    });
    const free = query.data?.view.freeDocument;
    useEffect(() => {
        if (!inspectionRequest) return;
        const timer = setTimeout(() => {
            if (pending.current === inspectionRequest) {
                pending.current = undefined; setChecking(false); setCheckError('未收到当前页面的检查数据，不能判定为已检查。');
            }
        }, 10000);
        return () => clearTimeout(timer);
    }, [inspectionRequest]);
    const record = useCallback(async (measurements: unknown) => {
        if (!free || !pending.current || pending.current !== inspectionRequest) return;
        pending.current = undefined;
        try { setCheck(await analysisApi.recordPreviewCheck({agentId,sessionId},previewRef,free.contentDigest,free.frameKey,measurements)); }
        catch (e) { setCheckError(e instanceof Error ? e.message : '检查记录保存失败'); }
        finally { setChecking(false); }
    }, [agentId,sessionId,previewRef,free,inspectionRequest]);
    const inspect = () => {
        const id = crypto.randomUUID(); pending.current = id; setCheck(undefined); setCheckError(''); setChecking(true); setInspectionRequest(id);
    };
    return <div className="flex h-full min-h-0 flex-col p-4">
        <header className="mb-4 flex flex-wrap items-center gap-3">
            <h1 className="text-lg font-semibold">发布前预览 · 尚未发布</h1>
            <button className="rounded border px-3 py-1" aria-pressed={!report} onClick={()=>setReport(false)}>分析预览</button>
            <button className="rounded border px-3 py-1" aria-pressed={report} onClick={()=>setReport(true)}>报告预览</button>
            {free && <>
                <button className="rounded border px-3 py-1" aria-pressed={narrow} disabled={checking} onClick={()=>{setNarrow(v=>!v);setCheck(undefined);}}> {narrow ? '恢复可用宽度' : '窄屏 420 px'} </button>
                <button className="rounded border px-3 py-1" disabled={checking} onClick={inspect}>{checking ? '检查中…' : '运行页面检查'}</button>
            </>}
        </header>
        {free && <p className="mb-2 text-sm">页面检查记录当前宽度的顺序、溢出、标签重叠和脚本错误；图形含义与业务解读仍需自审。</p>}
        {checkError && <p role="alert">{checkError}</p>}
        {check && check.contentDigest === free?.contentDigest && <details className="mb-3 text-sm" open><summary role="status">检查已记录 · {check.viewport.width} px · {check.status === 'MEASURED' ? '已测量，仍需视觉自审' : check.status === 'ERRORS' ? '存在必须修复的问题' : '存在待复核项'}</summary>
            <pre className="max-h-52 overflow-auto whitespace-pre-wrap">{JSON.stringify(check,null,2)}</pre></details>}
        {query.isLoading && <p role="status">正在读取预览…</p>}
        {query.error && <p role="alert">预览无法读取：{query.error.message}</p>}
        {query.data && (free ? <main className="min-h-0 flex-1 overflow-auto"><div style={{width:narrow?'min(420px,100%)':'100%'}}>
            <FreeAnalysisDocument key={free.contentDigest} document={free} inspectionRequest={inspectionRequest} onInspection={record} />
        </div></main> : report ? <iframe className="min-h-0 w-full flex-1 border-0" sandbox="allow-scripts" referrerPolicy="no-referrer" title="尚未发布的报告预览" srcDoc={query.data.html} /> :
            <main className="min-h-0 flex-1 overflow-auto"><AnalysisVisuals view={query.data.view} /></main>)}
    </div>;
}
