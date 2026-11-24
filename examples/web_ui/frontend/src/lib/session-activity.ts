import type { ReplyAddress, SessionAddress } from '@/api/session';

export type ReplyReceipt = ReplyAddress & { read: boolean };

export function sessionKey({ agent_id, session_id }: SessionAddress) {
	return `${agent_id}/${session_id}`;
}

export function replyKey(reply: ReplyAddress) {
	return `${sessionKey(reply)}/${reply.reply_id}`;
}

export function chatPath(session: SessionAddress, replyId?: string) {
	const path = `/chat/${encodeURIComponent(session.agent_id)}/${encodeURIComponent(session.session_id)}`;
	return replyId ? `${path}?${new URLSearchParams({ reply: replyId })}` : path;
}

export function readReplyReceipts(key: string): ReplyReceipt[] | null {
	try {
		const raw = localStorage.getItem(key);
		if (!raw) return null;
		const value: unknown = JSON.parse(raw);
		return Array.isArray(value) ? value.filter((entry: unknown): entry is ReplyReceipt =>
			!!entry && typeof entry === 'object' &&
			'agent_id' in entry && typeof entry.agent_id === 'string' &&
			'session_id' in entry && typeof entry.session_id === 'string' &&
			'reply_id' in entry && typeof entry.reply_id === 'string' &&
			'read' in entry && typeof entry.read === 'boolean',
		) : null;
	} catch {
		return null;
	}
}

function retainReadReceipts(receipts: ReplyReceipt[]) {
	let readToKeep = 600; // Covers the bounded server replay window, including Redis near-trimming.
	return receipts.toReversed().filter((entry) => !entry.read || readToKeep-- > 0).reverse();
}

export function appendReplyReceipts(
	current: ReplyReceipt[],
	replies: Iterable<ReplyAddress>,
	readSessions?: ReadonlySet<string>,
) {
	const seen = new Set(current.map(replyKey));
	const added: ReplyReceipt[] = [];
	for (const reply of replies) {
		const key = replyKey(reply);
		if (seen.has(key)) continue;
		seen.add(key);
		const { agent_id, session_id, reply_id } = reply;
		added.push({ agent_id, session_id, reply_id, read: readSessions?.has(sessionKey(reply)) ?? false });
	}
	if (!added.length) return current;
	const receipts = [...current, ...added];
	return readSessions?.size ? retainReadReceipts(receipts) : receipts;
}

export function markReplyReceiptsRead(
	current: ReplyReceipt[],
	target: SessionAddress | ReplyAddress,
) {
	const session = sessionKey(target);
	const replyId = 'reply_id' in target ? target.reply_id : undefined;
	const readReplies: ReplyAddress[] = [];
	const next = current.map((entry) => {
		if (entry.read || sessionKey(entry) !== session || (replyId !== undefined && entry.reply_id !== replyId)) {
			return entry;
		}
		readReplies.push(entry);
		return { ...entry, read: true };
	});
	return { receipts: readReplies.length ? retainReadReceipts(next) : current, readReplies };
}
