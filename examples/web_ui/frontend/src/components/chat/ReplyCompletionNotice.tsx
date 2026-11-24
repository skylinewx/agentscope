import { ArrowRight, CircleCheck } from 'lucide-react';
import { useEffect, useRef } from 'react';

import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';

export function ReplyCompletionNotice({ question, onOpen, onExpire }: {
	question: string;
	onOpen: () => void;
	onExpire: () => void;
}) {
	const { t } = useTranslation();
	const buttonRef = useRef<HTMLButtonElement>(null);

	useEffect(() => {
		const notice = buttonRef.current?.closest('[data-sonner-toast]');
		if (!notice) return;
		// Sonner pauses fixed-duration toasts on hover, but focus via Tab
		// does not pause them. Keep the clock on this card for both inputs.
		let remaining = 8_000;
		let started = 0;
		let timer: ReturnType<typeof setTimeout> | undefined;
		let mounted = true;
		const updateTimer = () => {
			if (!mounted) return;
			const paused = document.hidden || notice.matches(':hover') || notice.contains(document.activeElement);
			if (paused && timer !== undefined) {
				clearTimeout(timer);
				timer = undefined;
				remaining = Math.max(0, remaining - (performance.now() - started));
			} else if (!paused && timer === undefined) {
				started = performance.now();
				timer = setTimeout(onExpire, remaining);
			}
		};
		const afterBlur = () => queueMicrotask(updateTimer);
		notice.addEventListener('pointerenter', updateTimer);
		notice.addEventListener('pointerleave', updateTimer);
		notice.addEventListener('focusin', updateTimer);
		notice.addEventListener('focusout', afterBlur);
		document.addEventListener('visibilitychange', updateTimer);
		updateTimer();
		return () => {
			mounted = false;
			clearTimeout(timer);
			notice.removeEventListener('pointerenter', updateTimer);
			notice.removeEventListener('pointerleave', updateTimer);
			notice.removeEventListener('focusin', updateTimer);
			notice.removeEventListener('focusout', afterBlur);
			document.removeEventListener('visibilitychange', updateTimer);
		};
	}, [onExpire]);

	return (
		<Button ref={buttonRef} type="button" variant="ghost"
			className="h-auto min-h-11 w-full justify-start whitespace-normal p-1 text-left" onClick={onOpen}>
			<CircleCheck className="self-start text-teal-700 dark:text-teal-400" aria-hidden="true" />
			<span className="grid min-w-0 gap-0.5">
				<span className="flex min-w-0 items-baseline gap-1.5">
					<span className="line-clamp-2 min-w-0 break-words">{question || t('chat.session.untitledQuestion')}</span>{' '}
					<span className="shrink-0">{t('chat.session.replyCompleted')}</span>
				</span>
				<span className="inline-flex items-center gap-1 text-xs text-primary">
					{t('chat.session.openReply')} <ArrowRight className="size-3" aria-hidden="true" />
				</span>
			</span>
		</Button>
	);
}
