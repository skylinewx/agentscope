import * as React from 'react';
import {
	MessageScroller as MessageScrollerPrimitive,
	useMessageScroller,
	useMessageScrollerScrollable,
	useMessageScrollerVisibility,
} from '@shadcn/react/message-scroller';

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { ArrowDownIcon } from 'lucide-react';

const ScrollOptionsContext = React.createContext({ autoScroll: false, scrollEdgeThreshold: 8 });
const SCROLL_KEYS = new Set(['ArrowDown', 'ArrowUp', 'End', 'Home', 'PageDown', 'PageUp', ' ']);

function MessageScrollerProvider({
	autoScroll = false,
	scrollEdgeThreshold = 8,
	children,
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Provider>) {
	const options = React.useMemo(() => ({ autoScroll, scrollEdgeThreshold }), [autoScroll, scrollEdgeThreshold]);
	return <ScrollOptionsContext.Provider value={options}>
		<MessageScrollerPrimitive.Provider autoScroll={autoScroll} scrollEdgeThreshold={scrollEdgeThreshold} {...props}>
			{children}
		</MessageScrollerPrimitive.Provider>
	</ScrollOptionsContext.Provider>;
}

function MessageScroller({
	className,
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Root>) {
	return (
		<MessageScrollerPrimitive.Root
			data-slot="message-scroller"
			className={cn(
				'group/message-scroller relative flex size-full min-h-0 flex-col overflow-hidden',
				className,
			)}
			{...props}
		/>
	);
}

function MessageScrollerViewport({
	className,
	onWheel,
	onTouchMove,
	onKeyDown,
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Viewport>) {
	const { autoScroll, scrollEdgeThreshold } = React.useContext(ScrollOptionsContext);
	const { scrollToEnd } = useMessageScroller();
	const frame = React.useRef<number | null>(null);
	React.useEffect(() => () => {
		if (frame.current !== null) cancelAnimationFrame(frame.current);
	}, []);
	const resumeIfAtBottom = (viewport: HTMLDivElement) => {
		if (!autoScroll) return;
		if (frame.current !== null) cancelAnimationFrame(frame.current);
		const beforeTop = viewport.scrollTop;
		const wasAtBottom = viewport.scrollHeight - viewport.clientHeight - beforeTop <= scrollEdgeThreshold;
		// The primitive pauses on every input gesture, even a downward wheel
		// at the bottom or a gesture consumed by a nested scroll area. Wait
		// for native scrolling, then resume only if the viewport stayed at end.
		// Streaming can increase scrollHeight in the same frame; that alone
		// must not be mistaken for the user scrolling up.
		frame.current = requestAnimationFrame(() => {
			frame.current = null;
			const atBottom = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= scrollEdgeThreshold;
			const stayedAtBottom = wasAtBottom && viewport.scrollTop >= beforeTop - 0.5;
			if (viewport.isConnected && (atBottom || stayedAtBottom)) {
				scrollToEnd({ behavior: 'auto' });
			}
		});
	};
	return (
		<MessageScrollerPrimitive.Viewport
			data-slot="message-scroller-viewport"
			onWheel={(event) => { onWheel?.(event); resumeIfAtBottom(event.currentTarget); }}
			onTouchMove={(event) => { onTouchMove?.(event); resumeIfAtBottom(event.currentTarget); }}
			onKeyDown={(event) => { onKeyDown?.(event); if (SCROLL_KEYS.has(event.key)) resumeIfAtBottom(event.currentTarget); }}
			className={cn(
				// `no-scrollbar`: the stock thin scrollbar is hidden while
				// autoscrolling and shown otherwise, so a streaming reply
				// blinks it on and off down the right edge.
				'size-full min-h-0 min-w-0 scroll-fade-b no-scrollbar overflow-y-auto overscroll-contain contain-content',
				className,
			)}
			{...props}
		/>
	);
}

function MessageScrollerContent({
	className,
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Content>) {
	return (
		<MessageScrollerPrimitive.Content
			data-slot="message-scroller-content"
			className={cn('flex h-max min-h-full flex-col gap-6', className)}
			{...props}
		/>
	);
}

function MessageScrollerItem({
	className,
	scrollAnchor = false,
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Item>) {
	return (
		<MessageScrollerPrimitive.Item
			data-slot="message-scroller-item"
			scrollAnchor={scrollAnchor}
			className={cn(
				'min-w-0 shrink-0 [contain-intrinsic-size:auto_10rem] [content-visibility:auto]',
				className,
			)}
			{...props}
		/>
	);
}

function MessageScrollerButton({
	direction = 'end',
	className,
	children,
	render,
	variant = 'secondary',
	size = 'icon-sm',
	...props
}: React.ComponentProps<typeof MessageScrollerPrimitive.Button> &
	Pick<React.ComponentProps<typeof Button>, 'variant' | 'size'>) {
	return (
		<MessageScrollerPrimitive.Button
			data-slot="message-scroller-button"
			data-direction={direction}
			data-variant={variant}
			data-size={size}
			direction={direction}
			className={cn(
				'absolute inset-s-1/2 -translate-x-1/2 border-border bg-background text-foreground transition-[translate,scale,opacity] duration-200 hover:bg-muted hover:text-foreground data-[active=false]:pointer-events-none data-[active=false]:scale-95 data-[active=false]:opacity-0 data-[active=false]:duration-400 data-[active=false]:ease-[cubic-bezier(0.7,0,0.84,0)] data-[active=true]:translate-y-0 data-[active=true]:scale-100 data-[active=true]:opacity-100 data-[active=true]:ease-[cubic-bezier(0.23,1,0.32,1)] data-[direction=end]:bottom-4 data-[direction=end]:data-[active=false]:translate-y-full data-[direction=start]:top-4 data-[direction=start]:data-[active=false]:-translate-y-full rtl:translate-x-1/2 data-[direction=start]:[&_svg]:rotate-180',
				className,
			)}
			render={render ?? <Button variant={variant} size={size} />}
			{...props}
		>
			{children ?? (
				<>
					<ArrowDownIcon />
					<span className="sr-only">
						{direction === 'end' ? 'Scroll to end' : 'Scroll to start'}
					</span>
				</>
			)}
		</MessageScrollerPrimitive.Button>
	);
}

export {
	MessageScrollerProvider,
	MessageScroller,
	MessageScrollerViewport,
	MessageScrollerContent,
	MessageScrollerItem,
	MessageScrollerButton,
	useMessageScroller,
	useMessageScrollerScrollable,
	useMessageScrollerVisibility,
};
