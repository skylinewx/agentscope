/** Local component regression harness. All analysis APIs are synthetic. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';

import fixture from './analysis-review.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { getBaseUrl, getUserId } from '../src/api/client';
import { AnalysisDraftContext } from '../src/components/chat/AnalysisDraftContext';
import { TextInput, type TextInputRef } from '../src/components/chat/TextInput';
import { WorkspaceAnalysis } from '../src/components/chat/WorkspaceAnalysis';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import '../src/index.css';
import { draftScope } from '../src/lib/analysis-draft';
import { parseSnapshot } from '../src/lib/workspace-analysis';

await i18n.changeLanguage('zh');
let holdResolution = false;
let release: (() => void) | null = null;
let resolutions = 0;
const snapshot = () => parseSnapshot(structuredClone(fixture));
analysisApi.view = async () => snapshot();
analysisApi.project = async () => snapshot();
analysisApi.restore = async () => snapshot();
analysisApi.resolveAction = async (workspace, actionRef, signal) => {
  resolutions++;
  if (holdResolution) await new Promise<void>((resolve, reject) => {
    release = resolve;
    signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')), { once: true });
  });
  return { actionRef, availability: 'AVAILABLE', text: `合成核对建议：会话 ${workspace.sessionId}\n行动引用：${actionRef}` };
};
// Fail closed if an unmocked operation is accidentally used in this harness.
analysisApi.render = analysisApi.preview = analysisApi.downloadUrl = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

export function Harness() {
  const [session, setSession] = useState('A');
  const [sent, setSent] = useState(0);
  const [narrow, setNarrow] = useState(false);
  const [held, setHeld] = useState(false);
  const input = useRef<TextInputRef>(null);
  const workspace = { agentId: 'synthetic-review', sessionId: session };
  const scope = draftScope(getBaseUrl(), getUserId(), workspace.agentId, session);
  return <QueryClientProvider client={queryClient}><TooltipProvider>
    <main style={{ maxWidth: narrow ? 390 : 1000, margin: '24px auto', padding: 12 }}>
      <h1>阶段3合成组件自审</h1>
      <p>无真实模型、无预算查询。发送只增加本地计数。</p>
      <p data-testid="review-status">当前会话：{session}；发送次数：{sent}；解析次数：{resolutions}</p>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <button onClick={() => setSession(session === 'A' ? 'B' : 'A')}>切换会话</button>
        <button onClick={() => { holdResolution = !held; setHeld(!held); }}>延迟解析：{held ? '开' : '关'}</button>
        <button onClick={() => { release?.(); release = null; }}>释放解析</button>
        <button onClick={() => setNarrow(!narrow)}>宽度：{narrow ? '390' : '1000'}</button>
      </div>
      <AnalysisDraftContext.Provider value={{ insert: (text, origin) => {
        if (origin.agentId === workspace.agentId && origin.sessionId === session) input.current?.insertDraft(text, scope);
      } }}>
        <WorkspaceAnalysis path={`./analysis/${fixture.analysisRef.split(':')[0]}/revisions/1/analysis.json`} workspace={workspace} />
        <TextInput key={scope} ref={input} draftScope={scope} onSend={() => setSent((value) => value + 1)} />
      </AnalysisDraftContext.Provider>
    </main>
  </TooltipProvider></QueryClientProvider>;
}

createRoot(document.getElementById('root')!).render(<Harness />);
