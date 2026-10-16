/** Isolated UI regression: synthetic data, no model, source query or chat writes. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import fixtures from './analysis-capabilities.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { ReportMarkdown } from '../src/components/chat/WorkspaceReport';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import '../src/index.css';
import { parseSnapshot } from '../src/lib/workspace-analysis';

await i18n.changeLanguage('zh');
let onRead = () => {};
analysisApi.view = async (_workspace, path) => {
  if (path !== fixtures.details.path) throw new Error('UNEXPECTED_ANALYSIS_PATH');
  onRead();
  return parseSnapshot(structuredClone(fixtures.details.snapshot));
};
const refuse = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
analysisApi.project = analysisApi.restore = refuse;
analysisApi.render = analysisApi.preview = analysisApi.downloadUrl = analysisApi.resolveAction = refuse;
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Harness() {
  const [reads, setReads] = useState(0);
  useEffect(() => { onRead = () => setReads(value => value + 1); return () => { onRead = () => {}; }; }, []);
  return <QueryClientProvider client={client}><TooltipProvider><main style={{maxWidth:1000, margin:'32px auto', padding:20}}>
    <h1>分析链接交互验证（合成数据）</h1>
    <p>采用与用户反馈相同的行内链接句式；点击后应在当前页面打开完整分析。</p>
    <p data-testid="analysis-reads">分析读取次数：{reads}</p>
    <ReportMarkdown animated isAnimating={false} agentId="synthetic-inline-agent" sessionId="synthetic-inline-session">
      {`该结果已登记为可查看的分析记录：[查看分析结果](${fixtures.details.path})`}
    </ReportMarkdown>
    <label>未发送的草稿<textarea aria-label="未发送的草稿" defaultValue="保留这份草稿" style={{display:'block',width:'100%',marginTop:24,border:'1px solid #888'}} /></label>
  </main></TooltipProvider></QueryClientProvider>;
}

createRoot(document.getElementById('root')!).render(<Harness />);
