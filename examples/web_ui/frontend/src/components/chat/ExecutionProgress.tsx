import type { Msg, ToolCallBlock } from '@agentscope-ai/agentscope/message';
import { ChevronDown, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from '@/i18n/useI18n';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Button } from '@/components/ui/button';

type Operation = { operationId: string; stage: string; status: string; startedAt: string; endedAt?: string; attempt?: number };

/** 独立于候选答案的执行状态；不等待模型输出，也不展示未经交付的业务内容。 */
export function ExecutionProgress({ message, background = false }: { message: Msg; background?: boolean }) {
    const { t } = useTranslation();
    const [now, setNow] = useState(Date.now());
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, []);
    const progress = message.metadata?.executionProgress as { stage?: string } | undefined;
    const analysis = message.metadata?.analysisExecutionProgress as { stage?: string; status?: string; startedAt?: string; attempt?: number; operations?: Operation[] } | undefined;
    const calls = message.content.filter((block): block is ToolCallBlock => block.type === 'tool_call' &&
        !message.content.some((result) => result.type === 'tool_result' && result.id === block.id &&
            ['success', 'error'].includes(result.state)));
    const operation = calls.some((call) => call.name === 'AnalysisPublish') ? 'PUBLISHING'
        : calls.some((call) => call.name === 'AnalysisPython') ? 'CALCULATING' : 'EXECUTING';
    const stage = analysis?.status === 'RUNNING' ? analysis.stage : background ? operation : progress?.stage ?? 'PREPARING';
    const started = Date.parse(analysis?.status === 'RUNNING' && analysis.startedAt ? analysis.startedAt : message.created_at);
    const elapsed = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : 0;
    return <Collapsible className="w-full min-w-0"><div role="status" aria-live="polite" data-testid="execution-progress"
        className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
        <Loader2 aria-hidden="true" className="size-4 animate-spin shrink-0" />
        <span>{t(`chat.execution.${stage}`)}</span>
        {analysis?.status === 'RUNNING' && (analysis.attempt ?? 1) > 1 && <span>{t('chat.execution.retry', { attempt: analysis.attempt! })}</span>}
        {elapsed >= 3 && <span aria-live="off" className="tabular-nums text-xs">{t('chat.execution.elapsed', { seconds: elapsed })}</span>}
        {!!analysis?.operations?.length && <CollapsibleTrigger asChild><Button variant="ghost" size="sm" className="ml-auto shrink-0" aria-label={t('chat.execution.details')}>
            <ChevronDown className="size-4" /><span className="sr-only">{t('chat.execution.details')}</span>
        </Button></CollapsibleTrigger>}
    </div><CollapsibleContent><ol className="mb-3 space-y-1 pl-6 text-xs text-muted-foreground" aria-label={t('chat.execution.details')}>
        {analysis?.operations?.map(item => <li key={item.operationId} className="flex flex-wrap gap-x-2">
            <span>{t(`chat.execution.${item.stage}`)}</span>
            <span>{t(`chat.execution.status.${item.status}`)}</span>
            {(item.attempt ?? 1) > 1 && <span>{t('chat.execution.retry', { attempt: item.attempt! })}</span>}
            <span className="tabular-nums">{t('chat.execution.elapsed', { seconds: Math.max(0, Math.floor(((item.endedAt ? Date.parse(item.endedAt) : now) - Date.parse(item.startedAt)) / 1000)) })}</span>
        </li>)}
    </ol></CollapsibleContent></Collapsible>;
}
