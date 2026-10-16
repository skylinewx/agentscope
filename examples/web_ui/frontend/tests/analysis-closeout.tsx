/** Isolated regression page; APIs return host-generated synthetic snapshots only. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';

import fixtures from './analysis-closeout.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { WorkspaceAnalysis } from '../src/components/chat/WorkspaceAnalysis';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import '../src/index.css';
import { parseSnapshot } from '../src/lib/workspace-analysis';

await i18n.changeLanguage('zh');
const snapshot = (name: keyof typeof fixtures) => parseSnapshot(structuredClone(fixtures[name]));
analysisApi.view = async () => snapshot('default');
analysisApi.project = async (_workspace, _ref, params, offset, _signal, limit = 12) => {
  if (params.filter === 'negative') return snapshot('negative');
  if (params.sort === 'desc') return snapshot('desc');
  return snapshot(offset ? 'next' : limit > 12 ? 'large' : 'default');
};
const refuse = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
analysisApi.render = analysisApi.restore = analysisApi.preview = analysisApi.downloadUrl = analysisApi.resolveAction = refuse;
const path = `./analysis/${fixtures.default.analysisRef.split(':')[0]}/revisions/1/analysis.json`;
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient()}><TooltipProvider>
  <main style={{ maxWidth: 1100, margin: '24px auto', padding: 12 }}>
    <h1>分析评审修复：合成数据组件检查</h1>
    <p>仅验证分组、公式、筛选、排序和分页；未调用真实模型或预算查询。</p>
    <WorkspaceAnalysis path={path} workspace={{ agentId: 'synthetic-closeout', sessionId: 'synthetic-closeout' }} />
  </main>
</TooltipProvider></QueryClientProvider>);
