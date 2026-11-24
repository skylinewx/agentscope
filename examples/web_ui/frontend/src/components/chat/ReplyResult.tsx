import { useEffect, useRef, type RefObject } from 'react';

import { useMessageScroller } from '@/components/ui/message-scroller';

/** Mark a delivered result only when the start of its answer is on screen. */
export function ReplyResult({ replyId, onVisible }: { replyId: string; onVisible?: (id: string) => void }) {
	const ref = useRef<HTMLSpanElement>(null);
	useEffect(() => {
		const marker = ref.current;
		if (!onVisible || !marker) return;
		const viewport = marker.closest('[data-slot="message-scroller-viewport"]');
		const observer = new IntersectionObserver(([entry]) => {
			if (entry.intersectionRatio >= 0.5 && document.visibilityState === 'visible') onVisible(replyId);
		}, { root: viewport, threshold: 0.5 });
		const check = () => {
			observer.unobserve(marker);
			if (document.visibilityState === 'visible') observer.observe(marker);
		};
		check();
		document.addEventListener('visibilitychange', check);
		return () => {
			observer.disconnect();
			document.removeEventListener('visibilitychange', check);
		};
	}, [onVisible, replyId]);
	return <span ref={ref} data-reply-result={replyId} className="pointer-events-none absolute size-px" aria-hidden="true" />;
}

/** Navigate once on a link, after any needed history page has rendered. */
export function FocusReply({ replyId, navigationKey, viewport }: {
	replyId: string;
	navigationKey?: string;
	viewport: RefObject<HTMLDivElement | null>;
}) {
	const { scrollToMessage } = useMessageScroller();
	const lastJump = useRef<string | null>(null);
	useEffect(() => {
		const jump = `${navigationKey ?? ''}:${replyId}`;
		if (lastJump.current === jump) return;
		let alignFrame = 0;
		const frame = requestAnimationFrame(() => {
			if (!scrollToMessage(replyId, { behavior: 'instant' })) return;
			alignFrame = requestAnimationFrame(() => {
				const scrollArea = viewport.current;
				const anchor = scrollArea?.querySelector(`[data-reply-result="${CSS.escape(replyId)}"]`);
				if (scrollArea && anchor) {
					const bounds = scrollArea.getBoundingClientRect();
					const y = anchor.getBoundingClientRect().top;
					if (y < bounds.top || y > bounds.bottom - 24) {
						scrollArea.scrollBy({ top: y - bounds.top - 12, behavior: 'instant' });
					}
				}
				lastJump.current = jump;
			});
		});
		return () => { cancelAnimationFrame(frame); cancelAnimationFrame(alignFrame); };
	}, [navigationKey, replyId, scrollToMessage, viewport]);
	return null;
}
