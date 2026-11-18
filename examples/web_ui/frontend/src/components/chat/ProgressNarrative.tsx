import type { ProgressStep } from '@/lib/agent-progress';

/** Only prose: factual observations and the next action share one reading flow. */
export function ProgressNarrative({ step }: { step: ProgressStep }) {
	return <div data-testid="progress-narrative" data-status={step.status}
		className="space-y-2 leading-relaxed text-sm text-muted-foreground">
		{step.summary && <p className="whitespace-pre-wrap break-words">{step.summary}</p>}
		{step.nextAction && <p className="whitespace-pre-wrap break-words">{step.nextAction}</p>}
	</div>;
}
