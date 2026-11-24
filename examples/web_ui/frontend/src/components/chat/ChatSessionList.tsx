import { format, isToday } from 'date-fns';
import {
	BotMessageSquare,
	Cable,
	CalendarClock,
	CircleAlert,
	Loader2,
	MessageSquareDashed,
	Plus,
	Trash2,
	Users,
	X,
	type LucideIcon,
} from 'lucide-react';
import { useId, useRef, useState } from 'react';
import { toast } from 'sonner';

import type { SessionRecord, SessionSourceKind, SessionView } from '@/api';
import type { SessionAddress } from '@/api/session';
import { SessionListItem } from '@/components/chat/SessionListItem';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import { Label } from '@/components/ui/label';
import {
	SidebarGroup,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarMenu,
} from '@/components/ui/sidebar';
import { useSessionActivity } from '@/context/session-activity';
import { useTranslation } from '@/i18n/useI18n';
import { sessionKey } from '@/lib/session-activity';

// Show an origin icon only when the list mixes sources.
const SOURCE_ICON: Record<SessionSourceKind, LucideIcon> = {
	user: BotMessageSquare,
	schedule: CalendarClock,
	channel: Cable,
	team: Users,
};

interface Props {
	sessions: SessionView[];
	currentSessionId?: string;
	hasAgent: boolean;
	creatingSession: boolean;
	onCreate: () => void;
	onOpen: (session: SessionAddress, replyId?: string) => void;
	onRename: (session: SessionRecord) => void;
	onDelete: (session: SessionRecord) => void;
	onDeleteSelected: (sessionIds: string[]) => Promise<ReadonlySet<string>>;
}

export function ChatSessionList({
	sessions,
	currentSessionId,
	hasAgent,
	creatingSession,
	onCreate,
	onOpen,
	onRename,
	onDelete,
	onDeleteSelected,
}: Props) {
	const { t, i18n } = useTranslation();
	const activity = useSessionActivity();
	const [selection, setSelection] = useState<Set<string> | null>(null);
	const [confirmOpen, setConfirmOpen] = useState(false);
	const selectAllId = useId();
	const modeButton = useRef<HTMLButtonElement>(null);
	const deleteButton = useRef<HTMLButtonElement>(null);
	const newSessionButton = useRef<HTMLButtonElement>(null);
	const selecting = selection !== null;
	const selected = selection ? sessions.filter(({ session }) => selection.has(session.id)) : [];
	const count = selected.length;
	const selectAllState = count === 0 ? false : count === sessions.length ? true : 'indeterminate';
	const showSourceIcons = new Set(sessions.map(({ session }) => session.origin.type)).size > 1;
	const sessionDateFormatter = new Intl.DateTimeFormat(i18n.resolvedLanguage || i18n.language, {
		month: 'short',
		day: '2-digit',
	});
	const groups = [
		{
			id: 'today',
			views: sessions.filter(({ session }) => isToday(new Date(session.created_at))),
		},
		{
			id: 'earlier',
			views: sessions.filter(({ session }) => !isToday(new Date(session.created_at))),
		},
	] as const;

	function getStatus(view: SessionView) {
		const key = sessionKey({ agent_id: view.session.agent_id, session_id: view.session.id });
		return activity?.statuses.get(key) ?? view.status ?? (view.is_running ? 'running' : 'idle');
	}

	const runningCount = selected.filter((view) => getStatus(view) === 'running').length;
	const deletingTeam = selected.some(({ session, team }) => team?.team.session_id === session.id);
	const current = selected.find(({ session }) => session.id === currentSessionId);
	const preview = current
		? [current, ...selected.filter((view) => view !== current).slice(0, 2)]
		: selected.slice(0, 3);

	function selectSession(id: string, checked: boolean) {
		setSelection((previous) => {
			if (!previous) return previous;
			const next = new Set(previous);
			if (checked) next.add(id);
			else next.delete(id);
			return next;
		});
	}

	async function deleteSelected() {
		const ids = selected.map(({ session }) => session.id);
		const removed = await onDeleteSelected(ids);
		const remaining = ids.filter((id) => !removed.has(id));
		setSelection(remaining.length > 0 ? new Set(remaining) : null);
		if (remaining.length === 0) {
			toast.success(t('chat.session.bulkDelete.success', { count: removed.size }));
		} else if (removed.size > 0) {
			toast.warning(
				t('chat.session.bulkDelete.partial', {
					removed: removed.size,
					failed: remaining.length,
				}),
			);
		} else {
			toast.error(t('chat.session.bulkDelete.failed', { count: remaining.length }));
		}
	}

	function restoreFocus(event: Event) {
		const target = [deleteButton.current, modeButton.current, newSessionButton.current].find(
			(button) => button && !button.disabled,
		);
		if (target) {
			event.preventDefault();
			target.focus();
		}
	}

	return (
		<SidebarGroup className="mt-5 min-h-0 flex-1 px-2 py-0">
			<SidebarGroupLabel className="justify-between gap-2">
				<span className="flex min-w-0 items-center gap-2">
					{t('chat.session.label')}
					<span className="text-[10px] text-text-data">{sessions.length}</span>
				</span>
				<Button
					ref={modeButton}
					type="button"
					variant="secondary"
					size="sm"
					className="border-border font-sans text-xs font-normal normal-case enabled:cursor-pointer aria-pressed:bg-sidebar-accent aria-pressed:text-sidebar-accent-foreground"
					aria-pressed={selecting}
					disabled={!selecting && sessions.length === 0}
					onClick={() => setSelection(selecting ? null : new Set())}
				>
					{selecting ? <X aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
					{t(
						selecting
							? 'chat.session.bulkDelete.exit'
							: 'chat.session.bulkDelete.enter',
					)}
				</Button>
			</SidebarGroupLabel>
			<SidebarGroupContent className="flex min-h-0 flex-1 flex-col">
				<SidebarGroup>
					{selecting ? (
						<Label
							htmlFor={selectAllId}
							className="h-8 cursor-pointer px-2 text-muted-foreground"
						>
							<Checkbox
								id={selectAllId}
								className="after:hidden"
								checked={selectAllState}
								disabled={sessions.length === 0}
								aria-label={t('chat.session.bulkDelete.selectAllLabel')}
								onCheckedChange={(checked) =>
									setSelection(
										new Set(
											checked === true
												? sessions.map(({ session }) => session.id)
												: [],
										),
									)
								}
							/>
							{t('chat.session.bulkDelete.selectAll')}
						</Label>
					) : (
						<SidebarMenu className="mb-2">
							<Button
								id="tour-create-session"
								ref={newSessionButton}
								disabled={!hasAgent || creatingSession}
								aria-busy={creatingSession}
								onClick={onCreate}
							>
								{creatingSession ? <Loader2 className="animate-spin" /> : <Plus />}
								{t(creatingSession ? 'common.creating' : 'chat.newSession')}
							</Button>
						</SidebarMenu>
					)}
				</SidebarGroup>

				<div className="no-scrollbar min-h-0 flex-1 overflow-y-auto">
					{sessions.length === 0 ? (
						<Empty className="min-h-50 border-none py-4">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<MessageSquareDashed />
								</EmptyMedia>
								<EmptyTitle>{t('chat.session.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t(
										hasAgent
											? 'chat.session.emptyHasAgent'
											: 'chat.session.emptyNoAgent',
									)}
								</EmptyDescription>
							</EmptyHeader>
						</Empty>
					) : (
						groups
							.filter(({ views }) => views.length > 0)
							.map(({ id, views }) => (
								<SidebarGroup key={id}>
									<SidebarGroupLabel>{t(`chat.session.${id}`)}</SidebarGroupLabel>
									<SidebarGroupContent>
										<SidebarMenu>
											{views.map((view) => {
												const session = view.session;
												const address = {
													agent_id: session.agent_id,
													session_id: session.id,
												};
												const unread = activity?.unread.get(
													sessionKey(address),
												);
												const date = new Date(session.created_at);
												return (
													<SessionListItem
														key={session.id}
														name={session.config.name || session.id}
														timestamp={
															id === 'today'
																? format(date, 'HH:mm')
																: sessionDateFormatter.format(date)
														}
														status={getStatus(view)}
														unread={!!unread?.length}
														active={currentSessionId === session.id}
														icon={
															showSourceIcons
																? (SOURCE_ICON[
																		session.origin.type
																	] ?? BotMessageSquare)
																: undefined
														}
														selection={
															selection
																? {
																		checked: selection.has(
																			session.id,
																		),
																		onChange: (checked) =>
																			selectSession(
																				session.id,
																				checked,
																			),
																	}
																: undefined
														}
														onOpen={() =>
															onOpen(
																address,
																unread?.at(-1)?.reply_id,
															)
														}
														onRename={() => onRename(session)}
														onDelete={() => onDelete(session)}
													/>
												);
											})}
										</SidebarMenu>
									</SidebarGroupContent>
								</SidebarGroup>
							))
					)}
				</div>

				{selecting && (
					<div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-2 py-2">
						<span role="status" className="text-xs text-muted-foreground">
							{t('chat.session.bulkDelete.selected', { count })}
						</span>
						<Button
							ref={deleteButton}
							type="button"
							size="sm"
							variant="destructive"
							disabled={count === 0}
							onClick={() => setConfirmOpen(true)}
						>
							<Trash2 aria-hidden="true" />
							{t('chat.session.bulkDelete.deleteSelected')}
						</Button>
					</div>
				)}
			</SidebarGroupContent>
			<DeleteDialog
				open={confirmOpen}
				onOpenChange={setConfirmOpen}
				title={t('chat.session.bulkDelete.title', { count })}
				description={t('chat.session.bulkDelete.description')}
				confirmLabel={t('chat.session.bulkDelete.confirm')}
				pendingLabel={t('chat.session.bulkDelete.deleting')}
				confirmDisabled={count === 0}
				onCloseAutoFocus={restoreFocus}
				onConfirm={deleteSelected}
			>
				<div className="min-w-0 space-y-3 overflow-y-auto max-h-[45dvh]">
					<ul
						aria-label={t('chat.session.bulkDelete.preview')}
						className="space-y-1 rounded-lg bg-muted/50 px-3 py-2"
					>
						{preview.map(({ session }) => (
							<li key={session.id} className="flex items-baseline gap-2">
								<span className="min-w-0 truncate">
									{session.config.name || session.id}
								</span>
								{session.id === currentSessionId && (
									<span className="shrink-0 text-xs text-muted-foreground">
										{t('chat.session.bulkDelete.current')}
									</span>
								)}
							</li>
						))}
						{count > preview.length && (
							<li className="text-xs text-muted-foreground">
								{t('chat.session.bulkDelete.more', {
									count: count - preview.length,
								})}
							</li>
						)}
					</ul>
					{(runningCount > 0 || deletingTeam) && (
						<Alert>
							<CircleAlert aria-hidden="true" />
							<AlertDescription>
								{runningCount > 0 && (
									<p>
										{t('chat.session.bulkDelete.running', {
											count: runningCount,
										})}
									</p>
								)}
								{deletingTeam && <p>{t('chat.session.bulkDelete.team')}</p>}
							</AlertDescription>
						</Alert>
					)}
				</div>
			</DeleteDialog>
		</SidebarGroup>
	);
}
