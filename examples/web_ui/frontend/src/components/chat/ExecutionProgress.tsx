import type { Msg, ToolCallBlock } from '@agentscope-ai/agentscope/message';
import { Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from '@/i18n/useI18n';

/** 独立于候选答案的执行状态；不等待模型输出，也不展示未经交付的业务内容。 */
export function ExecutionProgress({ message, background = false }: { message: Msg; background?: boolean }) {
    const { t } = useTranslation();
    const [now, setNow] = useState(Date.now());
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, []);
    const progress = message.metadata?.executionProgress as { stage?: string } | undefined;
    const calls = message.content.filter((block): block is ToolCallBlock => block.type === 'tool_call' &&
        !message.content.some((result) => result.type === 'tool_result' && result.id === block.id &&
            ['success', 'error'].includes(result.state)));
    const operation = calls.some((call) => call.name === 'AnalysisPublish') ? 'PUBLISHING'
        : calls.some((call) => call.name === 'AnalysisPython') ? 'CALCULATING' : 'EXECUTING';
    const stage = background ? operation : progress?.stage ?? 'PREPARING';
    const started = Date.parse(message.created_at);
    const elapsed = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : 0;
    return <div role="status" aria-live="polite" data-testid="execution-progress"
        className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
        <Loader2 aria-hidden="true" className="size-4 animate-spin shrink-0" />
        <span>{t(`chat.execution.${stage}`)}</span>
        {elapsed >= 3 && <span aria-live="off" className="tabular-nums text-xs">{t('chat.execution.elapsed', { seconds: elapsed })}</span>}
    </div>;
}
