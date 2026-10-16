/** Isolated host-generated fixtures; no model or business requests. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';

import fixtures from './analysis-expression.fixture.json';
import followup from './analysis-formula-followup.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { WorkspaceAnalysis } from '../src/components/chat/WorkspaceAnalysis';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import '../src/index.css';
import { parseSnapshot } from '../src/lib/workspace-analysis';

await i18n.changeLanguage('zh');
const snapshot = (name: keyof typeof fixtures) => parseSnapshot(structuredClone(fixtures[name]));
analysisApi.view = async (workspace) => workspace.sessionId === 'formula'
  ? parseSnapshot(structuredClone(followup)) : snapshot('default');
analysisApi.project = async (_workspace, _ref, params, offset, _signal, limit = 12) => {
  if (params.filter === 'negative') return snapshot('negative');
  if (params.sort === 'asc') return snapshot('asc');
  return snapshot(offset ? 'next' : limit > 12 ? 'large' : 'default');
};
const refuse = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
analysisApi.render = analysisApi.restore = analysisApi.preview = analysisApi.downloadUrl = analysisApi.resolveAction = refuse;
const path = (ref: string) => `./analysis/${ref.split(':')[0]}/revisions/1/analysis.json`;
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient()}><TooltipProvider>
  <main style={{ maxWidth: 1100, margin: '24px auto', padding: 12 }}>
    <h1>表达与展示选择：合成数据组件检查</h1>
    <p>指标字段、标题、结论和初始顺序均来自显式计划；未调用真实模型或预算查询。</p>
    <WorkspaceAnalysis path={path(fixtures.default.analysisRef)} workspace={{ agentId: 'synthetic-expression', sessionId: 'expression' }} />
    <h2>公式行动：核对完成且存在差额</h2>
    <WorkspaceAnalysis path={path(followup.analysisRef)} workspace={{ agentId: 'synthetic-expression', sessionId: 'formula' }} />
  </main>
</TooltipProvider></QueryClientProvider>);
