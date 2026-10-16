/** Public snapshots produced by the host from isolated synthetic data. No live API calls. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { createRoot } from 'react-dom/client';

import fixtures from './analysis-capabilities.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { WorkspaceAnalysis } from '../src/components/chat/WorkspaceAnalysis';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import '../src/index.css';
import { parseSnapshot } from '../src/lib/workspace-analysis';

await i18n.changeLanguage('zh');
analysisApi.view = async (_workspace, path) => {
  const fixture = Object.values(fixtures).find((entry) => entry.path === path);
  if (!fixture) throw new Error('UNKNOWN_FIXTURE');
  return parseSnapshot(structuredClone(fixture.snapshot));
};
const refuse = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
analysisApi.project = analysisApi.restore = refuse;
analysisApi.render = analysisApi.preview = analysisApi.downloadUrl = analysisApi.resolveAction = refuse;
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

export function Harness() {
  const [mode, setMode] = useState<'details' | 'planned'>('details');
  return <QueryClientProvider client={client}><TooltipProvider><main style={{ maxWidth: 1000, margin: '24px auto', padding: 12 }}>
    <h1>通用能力与显式计划：合成组件检查</h1>
    <p>同一组数据包含负值、零值、正值。未调用模型、真实查询或发送聊天。</p>
    <div style={{ display: 'flex', gap: 16, margin: '16px 0' }}>
      <button onClick={() => setMode('details')}>仅明细</button>
      <button onClick={() => setMode('planned')}>显式正值检查与计数</button>
    </div>
    <p>当前计划：{mode === 'details' ? '仅保留来源明细' : '正值检查、一项核对建议、计数卡'}；图表未请求。</p>
    <WorkspaceAnalysis path={fixtures[mode].path} workspace={{ agentId: 'synthetic-capabilities', sessionId: mode }} />
  </main></TooltipProvider></QueryClientProvider>;
}

createRoot(document.getElementById('root')!).render(<Harness />);
