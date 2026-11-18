import * as React from 'react';

import { cn } from '@/lib/utils';

function MessageGroup({ className, ...props }: React.ComponentProps<'div'>) {
	return (
		<div
			data-slot="message-group"
			className={cn('flex min-w-0 flex-col gap-2', className)}
			{...props}
		/>
	);
}

function Message({
	className,
	align = 'start',
	...props
}: React.ComponentProps<'div'> & { align?: 'start' | 'end' }) {
	return (
		<div
			data-slot="message"
			data-align={align}
			className={cn(
				'group/message relative flex w-full min-w-0 gap-2 text-sm data-[align=end]:flex-row-reverse',
				className,
			)}
			{...props}
		/>
	);
}

function MessageAvatar({ className, ...props }: React.ComponentProps<'div'>) {
	return (
		<div
			data-slot="message-avatar"
			className={cn(
				'flex w-fit min-w-8 shrink-0 items-center justify-center self-end overflow-hidden rounded-full bg-muted group-has-data-[slot=message-footer]/message:-translate-y-8',
				className,
			)}
			{...props}
		/>
	);
}

function MessageContent({ className, ...props }: React.ComponentProps<'div'>) {
	return (
		<div
			data-slot="message-content"
			className={cn(
				'flex w-full min-w-0 flex-col gap-2.5 wrap-break-word group-data-[align=end]/message:*:data-slot:self-end',
				className,
			)}
			{...props}
		/>
	);
}

function MessageHeader({ className, ...props }: React.ComponentProps<'div'>) {
	return (
		<div
			data-slot="message-header"
			className={cn(
				'flex max-w-full min-w-0 items-center px-3 text-xs font-medium text-muted-foreground group-has-data-[variant=ghost]/message:px-0',
				className,
			)}
			{...props}
		/>
	);
}

function MessageFooter({ className, ...props }: React.ComponentProps<'div'>) {
	return (
		<div
			data-slot="message-footer"
			className={cn(
				'flex max-w-full min-w-0 items-center px-3 text-xs font-medium text-muted-foreground group-has-data-[variant=ghost]/message:px-0 group-data-[align=end]/message:justify-end',
				className,
			)}
			{...props}
		/>
	);
}

/** Reveal the local time on message hover/focus; keep it visible on touch devices. */
function MessageTime({
	dateTime,
	locale,
	className,
	...props
}: Omit<React.ComponentProps<'time'>, 'dateTime'> & { dateTime: string; locale?: string }) {
	const date = new Date(dateTime);
	if (!Number.isFinite(date.getTime())) return null;
	const fullDate = new Intl.DateTimeFormat(locale, {
		dateStyle: 'medium', timeStyle: 'medium', hour12: false,
	}).format(date);
	return (
		<time
			data-slot="message-time"
			dateTime={date.toISOString()}
			title={fullDate}
			aria-label={fullDate}
			className={cn(
				'shrink-0 text-xs font-normal tabular-nums text-muted-foreground opacity-0 group-hover/message:opacity-100 group-focus-within/message:opacity-100 [@media(hover:none)]:opacity-100',
				className,
			)}
			{...props}
		>
			{new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', hour12: false }).format(date)}
		</time>
	);
}

export { MessageGroup, Message, MessageAvatar, MessageContent, MessageFooter, MessageHeader, MessageTime };
