/** 使用真实消息和报告组件验证交付路径；仅加载合成快照，不请求模型或预算接口。 */
import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';

import fixture from './analysis-closeout.fixture.json';
import { analysisApi } from '../src/api/analysis';
import { ASMessageBubble } from '../src/components/chat/ASMessageBubble';
import { Button } from '../src/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../src/components/ui/card';
import { TooltipProvider } from '../src/components/ui/tooltip';
import i18n from '../src/i18n';
import { groupReplyMessages } from '../src/lib/reply-presentation';
import { parseSnapshot } from '../src/lib/workspace-analysis';
import '../src/index.css';

await i18n.changeLanguage('zh');
const analysisRef = fixture.default.analysisRef;
const analysisId = analysisRef.split(':')[0];
const path = `./analysis/${analysisId}/revisions/1/analysis.json`;
const at = '2026-11-01T10:00:00Z';
const text = (id: string, value: string) => ({ type: 'text' as const, id, text: value, created_at: at });
const response = { analysisRef, publicationState: 'PUBLISHED',
  delivery: { type: 'workspace_analysis', registered: true, path, analysisRef } };
const metadata = { process_summary: { kind: 'analysis', id: analysisId }, analysisTurnKey: 'fixture-turn' };
const call: ContentBlock = { type: 'tool_call', id: 'publish', name: 'AnalysisPublish',
  input: JSON.stringify({ analysisRef, finalize: true }), state: 'finished', created_at: at };
const result: ContentBlock = { type: 'tool_result', id: 'publish', name: 'AnalysisPublish', state: 'success',
  output: [text('receipt', JSON.stringify(response))], metadata, created_at: at };
const placeholder: ContentBlock = { ...result, output: [text('pending', '后台执行中')], metadata: { backgroundToolCallId: 'publish' } };
const notification: ContentBlock = { type: 'hint', id: 'notification', created_at: at,
  source: JSON.stringify({ label: 'tool_output', backgroundToolCallId: 'publish', backgroundToolName: 'AnalysisPublish',
    backgroundState: 'success', toolResponseMetadata: metadata }),
  hint: [text('background-receipt', `<system-notification>Tool 'AnalysisPublish' running in background (id=publish) has finished (state=success).\n\nResult:\n\n${JSON.stringify(response)}</system-notification>`)] };
const link = text('link', `[查看结果](${path})`);
const message = (content: ContentBlock[], status = 'READY'): Msg => ({
  id: 'reply', name: 'fixture-agent', role: 'assistant', content, created_at: at, finished_at: at,
  finished_reason: 'completed' as Msg['finished_reason'],
  metadata: { analysisCompletion: { status, turnKey: 'fixture-turn', semanticReview: 'PASS', analysisRef },
    presentation: { answer_block_ids: content.filter((block) => block.type === 'text').map((block) => block.id) } },
});
const snapshot = parseSnapshot({ ...fixture.default, columns: [], rows: [], scenes: [], sections: [], tables: [],
  memberKeys: [], objectCount: 0, total: 0, chartsRequested: false, activeColumnKey: null,
  claims: [], summaryClaimIndexes: [],
  viewParameters: { ...fixture.default.viewParameters, columnKey: null },
  page: { offset: 0, limit: 100, total: 0, nextOffset: null },
  freeDocument: { version: 'free-html-1', title: '合成报告', contentDigest: 'a'.repeat(64), frameKey: 'b'.repeat(64),
    bindingCount: 1, datasetCount: 0,
    html: '<!doctype html><html lang="zh"><head><meta charset="UTF-8" /></head><body><h1>合成报告</h1><p>金额 -18,660.23 元</p><p>用于验证报告交付后的可用宽度。</p></body></html>' },
});
analysisApi.view = async () => snapshot;
const refuse = async () => { throw new Error('NO_REAL_SERVICE_CALLS'); };
analysisApi.project = analysisApi.restore = analysisApi.render = analysisApi.preview = analysisApi.downloadUrl = analysisApi.resolveAction = refuse;
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const assert = (value: unknown, reason: string) => { if (!value) throw new Error(reason); };
const until = async (predicate: () => unknown) => {
  for (let index = 0; index < 100; index++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('等待界面更新超时');
};

export function Harness() {
  const [current, setCurrent] = useState(message([call, placeholder, notification, link]));
  const [width, setWidth] = useState(920);
  const [outcomes, setOutcomes] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [summary, setSummary] = useState('准备就绪');
  const render = (next: Msg, size: number) => flushSync(() => { setCurrent(next); setWidth(size); });
  const area = () => document.getElementById('delivery-harness')!;
  const cards = () => area().querySelectorAll<HTMLElement>('[data-slot="workspace-analysis"]');
  const checkCard = async (inBubble: boolean) => {
    await until(() => cards().length === 1 && area().querySelector('iframe'));
    const card = cards()[0];
    const available = area().getBoundingClientRect().width;
    assert(Math.abs(card.getBoundingClientRect().width - available) <= 3, '报告没有占满消息可用宽度');
    assert(!!card.closest('[data-slot="bubble"]') === inBubble, '正式交付与链接回退路径错误');
    assert(area().scrollWidth <= area().clientWidth + 1, '聊天消息容器发生横向溢出');
  };
  const run = async () => {
    setRunning(true); setOutcomes([]); setSummary('运行中');
    const passed: string[] = [];
    const record = (label: string) => { passed.push(label); setOutcomes([...passed]); };
    try {
      for (const size of [920, 420]) {
        render(message([call, result, link]), size);
        await checkCard(false); record(`${size}px：同步交付只展示一个全宽报告`);
        render(message([call, placeholder, notification, link], 'WAITING'), size);
        assert(cards().length === 0, '续跑尚未结束时提前展示报告');
        record(`${size}px：后台等待阶段不提前交付`);
        const grouped = groupReplyMessages([
          message([call, placeholder], 'WAITING'), message([notification, link]),
        ])[0].message;
        render(grouped, size);
        await checkCard(false); record(`${size}px：后台续跑完成后正式交付，模型链接不重复展示`);
        render(JSON.parse(JSON.stringify(grouped)), size);
        await checkCard(false); record(`${size}px：历史消息重载保留正式交付路径`);
        render(message([link]), size);
        await checkCard(true); record(`${size}px：单独链接回退同样占满可用宽度`);
        render(message([text('plain', '简短回复')]), size);
        assert(cards().length === 0, '普通回复出现报告');
        const bubble = area().querySelector<HTMLElement>('[data-slot="bubble"]')!;
        assert(bubble.getBoundingClientRect().width < area().clientWidth / 2, '普通文字气泡被无条件撑宽');
        record(`${size}px：普通文字仍按内容宽度展示`);
      }
      render(message([call, placeholder, notification, link]), 920);
      await checkCard(false);
      setSummary(`${passed.length} 项通过`);
    } catch (error) { setSummary(`失败：${String(error)}`); }
    finally { setRunning(false); }
  };
  return <QueryClientProvider client={client}><TooltipProvider>
    <Card className="m-5"><CardHeader><CardTitle>报告交付与容器回归（合成数据）</CardTitle></CardHeader><CardContent className="space-y-3">
      <Button onClick={() => void run()} disabled={running}>运行测试</Button>
      <p id="summary" role="status">{summary}</p>
      <ol>{outcomes.map((item) => <li key={item}>{item}</li>)}</ol>
    </CardContent></Card>
    <section id="delivery-harness" style={{ width, maxWidth: 'calc(100% - 40px)', margin: '20px auto' }}>
      <ASMessageBubble message={current} agentId="fixture-agent" sessionId="fixture-session" backgroundWaiting={false} onUserConfirm={() => {}} />
    </section>
  </TooltipProvider></QueryClientProvider>;
}
createRoot(document.getElementById('root')!).render(<Harness />);
