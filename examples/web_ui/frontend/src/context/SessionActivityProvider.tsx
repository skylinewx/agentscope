import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import { useAuth } from './AuthContext';
import { SessionActivityContext } from './session-activity';
import { getBaseUrl } from '@/api/client';
import type { ReplyAddress, SessionActivityEvent, SessionAddress } from '@/api/session';
import type { SessionStatus } from '@/api/types';
import { ReplyCompletionNotice } from '@/components/chat/ReplyCompletionNotice';
import { useSessionUpdates } from '@/hooks/useSessions';
import {
	appendReplyReceipts, chatPath, markReplyReceiptsRead, readReplyReceipts, replyKey, sessionKey,
	type ReplyReceipt,
} from '@/lib/session-activity';

const EMPTY_STATUSES = new Map<string, SessionStatus>();
const toastId = (reply: ReplyAddress) => `session-reply:${replyKey(reply)}`;

function ActivityScope({ storageKey, children }: { storageKey: string; children: ReactNode }) {
	const navigate = useNavigate();
	const [receipts, setReceipts] = useState(() => readReplyReceipts(storageKey) ?? []);
	const [statuses, setStatuses] = useState(EMPTY_STATUSES);
	const receiptSnapshot = useRef(receipts);
	const storageAvailable = useRef(true);
	const viewed = useRef<SessionAddress | null>(null);
	const replay = useRef(new Map<string, ReplyAddress>());
	const openedBeforeReplay = useRef<Set<string> | undefined>(new Set());
	const shownToasts = useRef(new Set<string>());

	const updateReceipts = useCallback((update: (current: ReplyReceipt[]) => ReplyReceipt[]) => {
		const current = (storageAvailable.current ? readReplyReceipts(storageKey) : null) ?? receiptSnapshot.current;
		const next = update(current);
		if (next === current) return false;
		receiptSnapshot.current = next;
		setReceipts(next);
		if (storageAvailable.current) {
			try { localStorage.setItem(storageKey, JSON.stringify(next)); }
			catch { storageAvailable.current = false; }
		}
		return true;
	}, [storageKey]);

	useEffect(() => {
		const toasts = shownToasts.current;
		const sync = (event: StorageEvent) => {
			if (event.key !== storageKey || !storageAvailable.current) return;
			const next = readReplyReceipts(storageKey) ?? [];
			receiptSnapshot.current = next;
			setReceipts(next);
			for (const reply of next) {
				if (reply.read && toasts.has(toastId(reply))) toast.dismiss(toastId(reply));
			}
		};
		window.addEventListener('storage', sync);
		return () => {
			window.removeEventListener('storage', sync);
			for (const id of toasts) toast.dismiss(id);
		};
	}, [storageKey]);

	const markRead = useCallback((target: SessionAddress | ReplyAddress) => {
		const toDismiss = new Set<string>();
		if ('reply_id' in target) toDismiss.add(toastId(target));
		updateReceipts((current) => {
			const { receipts: next, readReplies } = markReplyReceiptsRead(current, target);
			for (const entry of readReplies) toDismiss.add(toastId(entry));
			return next;
		});
		for (const id of toDismiss) {
			if (shownToasts.current.delete(id)) toast.dismiss(id);
		}
	}, [updateReceipts]);

	const registerView = useCallback((session: SessionAddress) => {
		viewed.current = session;
		openedBeforeReplay.current?.add(sessionKey(session));
		markRead(session);
		return () => { if (viewed.current === session) viewed.current = null; };
	}, [markRead]);

	function receive(event: SessionActivityEvent) {
		if (event.type === 'ready') {
			const readSessions = openedBeforeReplay.current;
			openedBeforeReplay.current = undefined;
			updateReceipts((current) => appendReplyReceipts(current, replay.current.values(), readSessions));
			replay.current.clear();
			setStatuses(EMPTY_STATUSES);
			return;
		}
		if (event.type === 'reply_completed' && event.replayed) {
			replay.current.set(replyKey(event), event);
			return;
		}

		const key = sessionKey(event);
		const status = event.type === 'session_status' ? event.status : 'idle';
		setStatuses((current) => current.get(key) === status ? current : new Map(current).set(key, status));
		if (event.type !== 'reply_completed') return;
		const isNew = updateReceipts((current) => appendReplyReceipts(current, [event]));
		if (!isNew || (viewed.current && sessionKey(viewed.current) === key)) return;

		const id = toastId(event);
		shownToasts.current.add(id);
		const dismiss = () => {
			shownToasts.current.delete(id);
			toast.dismiss(id);
		};
		toast(
			<ReplyCompletionNotice question={event.question} onExpire={dismiss}
				onOpen={() => {
					dismiss();
					navigate(chatPath(event, event.reply_id));
				}} />,
			{
				id, duration: Infinity, closeButton: true,
				classNames: { content: 'w-full min-w-0', title: 'w-full min-w-0' },
				onDismiss: () => shownToasts.current.delete(id),
			},
		);
	}

	const connected = useSessionUpdates(receive);
	const unread = useMemo(() => {
		const entries = new Map<string, ReplyAddress[]>();
		for (const reply of receipts) {
			if (reply.read) continue;
			const key = sessionKey(reply);
			const group = entries.get(key);
			if (group) group.push(reply);
			else entries.set(key, [reply]);
		}
		return entries;
	}, [receipts]);
	const value = useMemo(() => ({ connected, statuses, unread, registerView, markRead }),
		[connected, statuses, unread, registerView, markRead]);
	return <SessionActivityContext.Provider value={value}>{children}</SessionActivityContext.Provider>;
}

export function SessionActivityProvider({ children }: { children: ReactNode }) {
	const auth = useAuth();
	if (!auth) return children;
	const storageKey = `agentscope:reply-receipts:${JSON.stringify([getBaseUrl(), auth.user.id])}`;
	return <ActivityScope key={storageKey} storageKey={storageKey}>{children}</ActivityScope>;
}
