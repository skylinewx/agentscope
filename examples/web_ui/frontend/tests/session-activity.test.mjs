import assert from 'node:assert/strict';
import test from 'node:test';

import { appendReplyReceipts, markReplyReceiptsRead, sessionKey } from '../src/lib/session-activity.ts';

const target = { agent_id: 'agent', session_id: 'target' };
const first = { ...target, reply_id: 'first' };
const opened = { ...target, reply_id: 'opened' };
const later = { ...target, reply_id: 'later' };
const newCompletion = { ...target, reply_id: 'new' };
const other = { ...first, session_id: 'other' };
const completions = [first, other, opened, later];

test('entering the session clears every existing completion in that session', () => {
	const before = appendReplyReceipts([], completions);
	const { receipts, readReplies } = markReplyReceiptsRead(before, target);

	assert.deepEqual(readReplies.map(({ reply_id }) => reply_id), ['first', 'opened', 'later']);
	assert.deepEqual(receipts.map(({ read }) => read), [true, false, true, true]);
	assert.equal(appendReplyReceipts(receipts, completions), receipts);
	assert.equal(markReplyReceiptsRead(receipts, target).receipts, receipts);
	assert.equal(appendReplyReceipts(receipts, [newCompletion]).at(-1).read, false);
});

test('passively viewing a result marks just that reply as read', () => {
	const before = appendReplyReceipts([], completions);
	const { receipts, readReplies } = markReplyReceiptsRead(before, opened);
	assert.deepEqual(receipts.map(({ read }) => read), [false, false, true, false]);
	assert.deepEqual(readReplies.map(({ reply_id }) => reply_id), ['opened']);
});

test('initial replay honors the session opened before its receipts arrived', () => {
	const before = appendReplyReceipts([], [newCompletion]);
	const receipts = appendReplyReceipts(before, completions, new Set([sessionKey(target)]));

	assert.deepEqual(receipts.map(({ read }) => read), [false, true, false, true, true]);
	assert.equal(appendReplyReceipts(receipts, completions), receipts);
});
