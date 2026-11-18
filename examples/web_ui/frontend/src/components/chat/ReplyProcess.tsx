import type { Msg } from '@agentscope-ai/agentscope/message';
import { Check, ChevronRight, Loader2, Square, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';

import { ProgressNarrative } from './ProgressNarrative';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { useTranslation } from '@/i18n/useI18n';
import { progressSteps } from '@/lib/agent-progress';
import type { ReplyPresentation } from '@/lib/reply-presentation';

/** Public narratives stay visible; legacy raw process details retain disclosure. */
export function ReplyProcess({
	message,
	presentation,
	elapsed,
	running,
	disconnected = false,
	steps,
	details,
}: {
	message: Msg;
	presentation: ReplyPresentation;
	elapsed: string;
	running: boolean;
	disconnected?: boolean;
	steps: ReactNode[];
	details: ReactNode;
}) {
	const { t } = useTranslation();
	const narratives = progressSteps(message);
	const [manualOpen, setManualOpen] = useState<boolean | null>(null);
	const [manualDetailsOpen, setManualDetailsOpen] = useState<boolean | null>(null);
	const trigger = useRef<HTMLButtonElement>(null);
	const content = useRef<HTMLDivElement>(null);
	const open = manualOpen ?? running;
	const detailsOpen = manualDetailsOpen ?? (running && narratives.length === 0);
	const finished = !!message.finished_at && !running && !disconnected && !presentation.pending;
	useEffect(() => {
		if (!finished || narratives.length > 0) return;
		// Reading during generation must not pin a completed reply open. Reset
		// once on completion, so users can still reopen it afterwards.
		if (content.current?.contains(document.activeElement)) {
			trigger.current?.focus({ preventScroll: true });
		}
		setManualOpen(null);
		setManualDetailsOpen(null);
	}, [finished, narratives.length]);
	const failed = message.finished_reason === 'error';
	const interrupted = message.finished_reason === 'interrupted';
	const limited = message.finished_reason === 'exceed_max_iters';
	const Icon =
		disconnected || failed || limited ? TriangleAlert : interrupted ? Square : running ? Loader2 : Check;
	let title: string;
	if (disconnected) title = t('replyProcess.disconnected');
	else if (presentation.pending) title = t(`replyProcess.${presentation.pending}`);
	else if (running) title = t('replyProcess.running');
	else if (failed) title = t('replyProcess.failed');
	else if (interrupted) title = t('replyProcess.interrupted');
	else if (limited) title = t('replyProcess.limited');
	else if (presentation.reportCount) {
		title = presentation.topicCount
			? t('replyProcess.reportTopics', {
					count: presentation.reportCount,
					topics: presentation.topicCount,
				})
			: t('replyProcess.reports', { count: presentation.reportCount });
	} else if (presentation.analysisCount)
		title = t('replyProcess.analyses', { count: presentation.analysisCount });
	else if (presentation.toolCount)
		title = t('replyProcess.tools', { count: presentation.toolCount });
	else title = t('replyProcess.completed');

	const changeOpen = (next: boolean) => {
		const button = trigger.current;
		const viewport = button?.closest<HTMLElement>('[data-slot="message-scroller-viewport"]');
		const offset =
			button && viewport
				? Math.max(
						0,
						button.getBoundingClientRect().top - viewport.getBoundingClientRect().top,
					)
				: 0;
		setManualOpen(next);
		// Keep the summary at its reading position, including when using the
		// bottom "collapse" button after scrolling through a long process.
		requestAnimationFrame(() => {
			if (button?.isConnected && viewport) {
				viewport.scrollTop +=
					button.getBoundingClientRect().top -
					viewport.getBoundingClientRect().top -
					offset;
			}
			if (!next) button?.focus({ preventScroll: true });
		});
	};
	const keepReading = () => {
		if (running && manualOpen === null) setManualOpen(true);
	};
	const changeDetailsOpen = (next: boolean) => {
		setManualDetailsOpen(next);
		keepReading();
	};
	const keepDetailsReading = () => {
		if (running && manualDetailsOpen === null) setManualDetailsOpen(true);
	};
	const detailDisclosure = (
		<Collapsible open={detailsOpen} onOpenChange={changeDetailsOpen}>
			<CollapsibleTrigger asChild>
				<Button variant="ghost" type="button" data-testid="reply-process-details-summary"
					className="h-auto justify-start p-0 whitespace-normal group flex max-w-full flex-wrap items-center gap-1 py-2 text-left text-xs text-muted-foreground hover:text-foreground">
					<ChevronRight aria-hidden="true" className="size-3 shrink-0 group-data-[state=open]:rotate-90" />
					{t('replyProcess.details')}
					<span>{t('replyProcess.counts', { thoughts: presentation.thinkingCount, tools: presentation.toolCount })}</span>
				</Button>
			</CollapsibleTrigger>
			<CollapsibleContent data-testid="reply-process-details-content" className="space-y-3 pt-2 pb-3"
				onPointerDownCapture={keepDetailsReading} onFocusCapture={keepDetailsReading} onWheelCapture={keepDetailsReading}>
				{details}
			</CollapsibleContent>
		</Collapsible>
	);

	if (narratives.length > 0) {
		return <section data-testid="reply-process" className="min-w-0">
			<Badge variant="secondary" data-testid="reply-process-summary" role="status"
				className="h-auto max-w-full flex-wrap gap-1.5 py-1 text-sm whitespace-normal">
				<Icon aria-hidden="true" className={running && !presentation.pending ? 'animate-spin motion-reduce:animate-none' : ''} />
				{title}<span className="whitespace-nowrap tabular-nums">· {elapsed}</span>
			</Badge>
			<div data-testid="reply-process-content" className="space-y-5 pt-3">
				{narratives.map((step) => <ProgressNarrative key={step.id} step={step} />)}
				{detailDisclosure}
			</div>
		</section>;
	}

	return (
		<Collapsible open={open} onOpenChange={changeOpen} data-testid="reply-process">
			<CollapsibleTrigger asChild>
				<Button
					variant="ghost"
					ref={trigger}
					type="button"
					className="h-auto justify-start p-0 whitespace-normal group flex min-h-8 max-w-full flex-wrap items-center gap-1.5 rounded-md py-1 text-left text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
					data-testid="reply-process-summary"
				>
					<Icon
						aria-hidden="true"
						className={`size-3.5 shrink-0 ${running && !presentation.pending ? 'animate-spin motion-reduce:animate-none' : ''}`}
					/>
					<span>{title}</span>
					<span className="whitespace-nowrap tabular-nums">· {elapsed}</span>
					<ChevronRight
						aria-hidden="true"
						className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90 motion-reduce:transition-none"
					/>
				</Button>
			</CollapsibleTrigger>
			<CollapsibleContent
				ref={content}
				onPointerDownCapture={keepReading}
				onFocusCapture={keepReading}
				onWheelCapture={keepReading}
			>
				<div
					className="my-2 ml-1.5 min-w-0 border-l border-border pl-4"
					data-testid="reply-process-content"
				>
					{steps.length > 0 && (
						<ol className="space-y-3 pb-3" aria-label={t('replyProcess.steps')}>
							{steps.map((step, index) => (
								<li
									key={presentation.steps[index].id}
									className="relative min-w-0 text-sm text-muted-foreground before:absolute before:-left-[19px] before:top-2.5 before:size-1 before:rounded-full before:bg-muted-foreground"
								>
									{step}
								</li>
							))}
						</ol>
					)}
					{detailDisclosure}
					<Button
						variant="ghost"
						size="xs"
						className="mt-1 px-0 text-muted-foreground"
						onClick={() => changeOpen(false)}
					>
						{t('replyProcess.collapse')}
					</Button>
				</div>
			</CollapsibleContent>
		</Collapsible>
	);
}
