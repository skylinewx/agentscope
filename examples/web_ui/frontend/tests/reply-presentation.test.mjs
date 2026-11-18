import assert from 'node:assert/strict';
import test from 'node:test';
import { applyReplyPresentation, backgroundWaitStates, groupReplyMessages, pendingCalls, presentReply } from '../src/lib/reply-presentation.ts';

const text = (id, value = id) => ({ type: 'text', id, text: value });
const thought = (id) => ({ type: 'thinking', id, thinking: 'detail' });
const call = (id, state = 'finished') => ({ type: 'tool_call', id, name: 'Read', state, input: '{}' });
const result = (id, metadata = {}) => ({ type: 'tool_result', id, name: 'Read', state: 'success', output: [], metadata });
const message = (content, extra = {}) => ({ id: 'reply', role: 'assistant', metadata: {}, content, ...extra });
const ids = (blocks) => blocks.map((block) => block.id);

test('host factual closeout appears once after background continuation and reload', () => {
	const waiting = message([call('publish'), result('publish'), text('draft')], {
		id: 'waiting', finished_reason: 'completed', metadata: {
			analysisCompletion: { status: 'WAITING', turnKey: 'same-question' },
			presentation: { final_only: true, answer_block_ids: [] },
		},
	});
	const finished = message([text('facts', '已整理查得的数据。')], {
		id: 'finished', finished_reason: 'completed', metadata: {
			analysisCompletion: { status: 'PARTIAL', semanticReview: 'UNAVAILABLE', turnKey: 'same-question' },
			presentation: { final_only: true, answer_block_ids: ['facts'] },
		},
	});
	for (const input of [[waiting, finished], JSON.parse(JSON.stringify([waiting, finished]))]) {
		const groups = groupReplyMessages(input);
		assert.equal(groups.length, 1);
		const presentation = presentReply(groups[0].message, false);
		assert.deepEqual(ids(presentation.answer), ['facts']);
		assert.equal(presentation.pending, null);
		assert.ok(!ids(presentation.process).includes('draft'));
	}
});

test('terminal host summary is visible while rejected drafts remain hidden', () => {
	const value = message([text('draft', '不可靠结论'), text('status', '本次处理尚未完成。')], {
		finished_reason: 'error', finished_at: 'done',
		metadata: { analysisCompletion: { status: 'INCOMPLETE' },
			presentation: { final_only: true, terminal_summary: true, answer_block_ids: ['status'] } },
	});
	for (const input of [value, JSON.parse(JSON.stringify(value))]) {
		assert.deepEqual(ids(presentReply(input).answer), ['status']);
		assert.ok(!ids(presentReply(input).process).includes('draft'));
	}
});

test('background continuations form one process and one final answer after reload', () => {
	const waiting = (id) => message([call(id), result(id), text(`draft-${id}`)], {
		id, created_at: id, finished_at: id, finished_reason: 'completed',
		usage: { input_tokens: 100, output_tokens: 10 },
		metadata: { analysisCompletion: { status: 'WAITING', turnKey: 'question' }, presentation: { final_only: true, answer_block_ids: [] } },
	});
	const final = message([call('last-tool'), result('last-tool'), text('final')], {
		id: 'last', created_at: 'last', finished_at: 'done', finished_reason: 'completed',
		usage: { input_tokens: 200, output_tokens: 20 },
		metadata: { analysisCompletion: { status: 'DIRECT', turnKey: 'question' }, presentation: { final_only: true, answer_block_ids: ['final'] } },
	});
	const messages = [waiting('first'), waiting('second'), waiting('third'), final];
	const before = structuredClone(messages);
	for (const input of [messages, JSON.parse(JSON.stringify(messages))]) {
		const groups = groupReplyMessages(input);
		assert.equal(groups.length, 1);
		assert.equal(groups[0].key, 'first');
		assert.equal(groups[0].message.created_at, 'first');
		assert.equal(groups[0].message.finished_at, 'done');
		assert.equal(groups[0].message.usage.input_tokens, 500);
		assert.equal(presentReply(groups[0].message, false).toolCount, 4);
		assert.deepEqual(ids(presentReply(groups[0].message, false).answer), ['final']);
	}
	assert.deepEqual(messages, before);
	const streaming = groupReplyMessages([messages[0], message([thought('live')], { id: 'live' })]);
	assert.equal(streaming.length, 1);
	assert.equal(streaming[0].key, 'first');
	assert.equal(streaming[0].message.finished_at, undefined);
	assert.deepEqual(presentReply(streaming[0].message).answer, []);
});

test('continuation grouping preserves user, turn, failure and interruption boundaries', () => {
	const waiting = message([call('query')], { id: 'first', finished_reason: 'completed',
		metadata: { analysisCompletion: { status: 'WAITING', turnKey: 'one' } } });
	for (const boundary of [
		message([], { role: 'user' }),
		message([], { metadata: { analysisCompletion: { status: 'DIRECT', turnKey: 'two' } } }),
	]) assert.equal(groupReplyMessages([waiting, boundary]).length, 2);
	for (const reason of ['error', 'interrupted', 'exceed_max_iters']) {
		const groups = groupReplyMessages([waiting, message([], { id: 'terminal', finished_reason: reason }), message([], { id: 'next' })]);
		assert.equal(groups.length, 2);
		assert.equal(groups[0].message.finished_reason, reason);
	}
});

test('server selection preserves all final paragraphs and hides progress after the last tool', () => {
	const value = message([text('progress'), call('tool'), result('tool'), text('checking'), text('answer-1'), text('answer-2')], {
		metadata: { presentation: { answer_block_ids: ['answer-1', 'answer-2'] } },
	});
	const before = structuredClone(value);
	assert.deepEqual(ids(presentReply(value).answer), ['answer-1', 'answer-2']);
	assert.deepEqual(ids(presentReply(value).steps), ['progress', 'checking']);
	assert.deepEqual(value, before);
});

test('streaming and saved unclassified replies retain the complete answer tail', () => {
	const value = message([thought('t1'), text('progress'), call('c'), result('c'), thought('t2'), text('a1'), text('a2')]);
	assert.deepEqual(ids(presentReply(value).answer), ['a1', 'a2']);
	assert.equal(presentReply(value).toolCount, 1);
	assert.equal(presentReply(value).thinkingCount, 2);
});

test('explicit empty final output does not promote a progress sentence to an answer', () => {
	const value = message([text('still-working')], { metadata: { presentation: { answer_block_ids: [] } } });
	assert.deepEqual(presentReply(value).answer, []);
});

test('images and delivered report/analysis cards survive later tool calls', () => {
	const value = message([
		text('report', '[报告](./reports/example.html)'),
		text('analysis', `[图表](./analysis/ana_${'a'.repeat(32)}/revisions/1/analysis.json)`),
		{ type: 'data', id: 'image', source: { type: 'url', url: './image.png', media_type: 'image/png' } },
		call('later'), result('later'), text('answer'),
	], { metadata: { presentation: { answer_block_ids: ['answer'] } } });
	assert.deepEqual(ids(presentReply(value).answer), ['report', 'analysis', 'image', 'answer']);
});

test('completion revision removes rejected prose and cards from answer and process', () => {
	const draft = text('old', `错误结论 [结果](./analysis/ana_${'a'.repeat(32)}/revisions/1/analysis.json)`);
	const hint = { type: 'hint', id: 'review', source: JSON.stringify({ label: 'System', sublabel: 'Completion check' }), hint: '修正' };
	const value = message([draft, hint, text('corrected', '[结果](./reports/corrected.html)')], {
		finished_reason: 'completed',
		metadata: { analysisCompletion: { status: 'DIRECT', semanticReview: 'PASS' }, presentation: { answer_block_ids: ['old', 'corrected'] } },
	});
	const before = structuredClone(value);
	assert.deepEqual(ids(presentReply(value).answer), ['corrected']);
	assert.deepEqual(ids(presentReply(value).process), ['review']);
	assert.deepEqual(value, before);
});

test('text-only streaming correction resets the draft but unrelated hints do not', () => {
	const hint = { type: 'hint', id: 'review', source: JSON.stringify({ label: 'System', sublabel: 'Completion check' }), hint: '修正' };
	assert.deepEqual(ids(presentReply(message([text('old'), hint])).answer), []);
	assert.deepEqual(ids(presentReply(message([text('old'), hint, text('new')])).answer), []);
	assert.deepEqual(ids(presentReply(message([text('old'), {...hint, source:'unrelated'}, text('new')])).answer), ['old', 'new']);
});

test('guarded candidates stay hidden during streaming, waiting, failures and history reload', () => {
	for (const status of [undefined, 'WAITING', 'INCOMPLETE']) {
		const value = message([{ ...call('declare'), name: 'AnalysisTask' }, text('draft', '|单位|净利润|'),
			{ type: 'data', id: 'chart', source: { type: 'url', url: './image.png' } }], {
			finished_reason: status ? 'completed' : undefined,
			metadata: status ? { analysisCompletion: { status } } : {},
		});
		for (const sample of [value, JSON.parse(JSON.stringify(value))]) {
			const shown = presentReply(sample);
			assert.deepEqual(shown.answer, []);
			assert.deepEqual(shown.steps, []);
			assert.deepEqual(ids(shown.process), ['declare']);
			assert.equal(shown.pending, status === 'WAITING' ? 'external' : null);
		}
	}
});

test('approved guarded reply publishes only selected final blocks once', () => {
	const value = message([text('old'), text('final'), { type: 'data', id: 'image' }], {
		finished_reason: 'completed', metadata: {
			analysisCompletion: { status: 'DIRECT', semanticReview: 'PASS' },
			presentation: { final_only: true, answer_block_ids: ['final', 'image'] },
		},
	});
	assert.deepEqual(ids(presentReply(value).answer), ['final', 'image']);
	assert.deepEqual(presentReply(value).steps, []);
	for (const finished_reason of ['error', 'interrupted']) {
		assert.deepEqual(presentReply({ ...value, finished_reason }).answer, []);
	}
});

test('tool output cannot become a user-facing report card', () => {
	const value = message([call('c'), { ...result('c'), output: [text('link', '[报告](./reports/example.html)')] }]);
	assert.deepEqual(presentReply(value).answer, []);
});

test('calls, replayed results and thoughts are counted once by ID', () => {
	const value = message([call('c'), result('c'), result('c'), thought('t'), thought('t'), result('orphan')]);
	assert.equal(presentReply(value).toolCount, 2);
	assert.equal(presentReply(value).thinkingCount, 1);
});

test('pending confirmations/external execution ignore resolved stale call states', () => {
	const value = message([call('resolved', 'asking'), result('resolved'), call('ask', 'asking'), call('external', 'submitted')]);
	assert.deepEqual(ids(pendingCalls(value)), ['ask', 'external']);
	assert.equal(presentReply(value).pending, 'confirmation');
	assert.equal(presentReply(message([call('external', 'submitted')])).pending, 'external');
	assert.deepEqual(pendingCalls(message([call('stale', 'asking')], { finished_at: '2026-10-13T10:00:00Z', finished_reason: 'error' })), []);
});

test('errors and interruptions keep partial output even if thinking resumes after it', () => {
	for (const finished_reason of ['error', 'interrupted']) {
		const value = message([call('c'), result('c'), text('partial'), thought('unfinished')], { finished_reason });
		assert.deepEqual(ids(presentReply(value).answer), ['partial']);
	}
});

test('report retries do not inflate counts; failed results do not claim success', () => {
	const meta = { process_summary: { kind: 'report', id: 'export-1', topicCount: 3 } };
	const value = message([result('one', meta), result('retry', meta), { ...result('failed', {
		process_summary: { kind: 'report', id: 'export-2', topicCount: 9 },
	}), state: 'error' }]);
	assert.equal(presentReply(value).reportCount, 1);
	assert.equal(presentReply(value).topicCount, 3);
});

test('multiple reports do not add potentially overlapping topic counts', () => {
	const value = message(['a', 'b'].map((id) => result(id, { process_summary: { kind: 'report', id, topicCount: 3 } })));
	assert.equal(presentReply(value).reportCount, 2);
	assert.equal(presentReply(value).topicCount, 0);
});

test('terminal event metadata survives live assembly and a history round trip', () => {
	const value = message([text('p'), text('a')]);
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'reply', metadata: { presentation: { answer_block_ids: ['a'] } } });
	assert.deepEqual(ids(presentReply(JSON.parse(JSON.stringify(value))).answer), ['a']);
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'another-reply', metadata: { presentation: { answer_block_ids: [] } } });
	assert.deepEqual(ids(presentReply(value).answer), ['a']);
});

test('completion verdict is carried independently of presentation and scoped to its reply', () => {
	const value = message([text('answer')], { metadata: { keep: true } });
	const metadata = { analysisCompletion: { status: 'INCOMPLETE', analysisRef: 'fixture:1', semanticReview: 'REVISE' } };
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'another', metadata });
	assert.deepEqual(value.metadata, { keep: true });
	applyReplyPresentation(value, { type: 'TEXT', reply_id: 'reply', metadata });
	assert.deepEqual(value.metadata, { keep: true });
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'reply', metadata });
	metadata.analysisCompletion.status = 'READY';
	assert.equal(value.metadata.analysisCompletion.status, 'INCOMPLETE', 'Keep a detached terminal snapshot');
	assert.deepEqual(JSON.parse(JSON.stringify(value)).metadata, { keep: true, analysisCompletion: { status: 'INCOMPLETE', analysisRef: 'fixture:1', semanticReview: 'REVISE' } });
});

test('terminal metadata preserves arbitrary middleware values and replaces whole verdicts', () => {
	const value = message([], { metadata: { keep: true, extensionReview: { oldRef: 'old' } } });
	const metadata = { extensionReview: { status: 'incomplete', references: ['source'] } };
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'reply', metadata });
	metadata.extensionReview.references.push('later mutation');
	assert.deepEqual(value.metadata, { keep: true, extensionReview: { status: 'incomplete', references: ['source'] } });
	applyReplyPresentation(value, { type: 'REPLY_END', reply_id: 'reply', metadata: { extensionReview: null } });
	assert.deepEqual(value.metadata, { keep: true, extensionReview: null });
});

test('missing and malformed metadata degrade to readable output', () => {
	for (const metadata of [null, {}, { presentation: { answer_block_ids: [1] } }]) {
		assert.deepEqual(ids(presentReply(message([text('answer')], { metadata })).answer), ['answer']);
	}
});

test('plain replies need no summary; questions and separate turns are unchanged', () => {
	assert.equal(presentReply(message([text('answer')])).process.length, 0);
	assert.deepEqual(ids(presentReply(message([text('question')], { role: 'user' })).answer), ['question']);
	presentReply(message([call('one'), result('one')]));
	assert.equal(presentReply(message([text('next-answer')])).toolCount, 0);
});

const offloaded = (id) => result(id, { backgroundToolCallId: id });
const waitingReply = (id, calls = []) => message(calls.map(offloaded), {
	id, finished_at: '2026-10-26T00:35:28Z', finished_reason: 'completed',
	metadata: { analysisCompletion: { status: 'WAITING' }, presentation: { final_only: true, answer_block_ids: [] } },
});
const backgroundDone = (id, state = 'success') => ({
	type: 'hint', id: `notice-${id}`,
	source: JSON.stringify({ label: 'tool_output', backgroundToolCallId: id, backgroundState: state }),
	hint: 'Background result',
});
const finalReply = (id) => message([text(`${id}-answer`, 'Final answer')], {
	id, finished_at: '2026-10-26T00:37:09Z', finished_reason: 'completed',
	metadata: { analysisCompletion: { status: 'DIRECT', semanticReview: 'PASS' },
		presentation: { final_only: true, answer_block_ids: [`${id}-answer`] } },
});
const shownWaiting = (messages) => {
	const waits = backgroundWaitStates(messages);
	return messages.filter((item) => presentReply(item, waits.get(item.id) ?? false).pending === 'external').map((item) => item.id);
};

test('completed reply segment keeps its real background wait; synthetic success is not completion', () => {
	const waiting = waitingReply('wait', ['job']);
	assert.deepEqual(pendingCalls(waiting), []);
	assert.deepEqual(shownWaiting([waiting]), ['wait']);
});

test('three background resumes clear old notices and retain only the approved final answer', () => {
	const first = waitingReply('first', ['one']);
	const second = waitingReply('second', ['two']);
	second.content.unshift(backgroundDone('one', 'error'));
	const third = waitingReply('third', ['three']);
	third.content.unshift(backgroundDone('two'));
	const final = finalReply('final');
	final.content.unshift(backgroundDone('three'));
	const history = [message([text('question')], { id: 'user', role: 'user' }), first, second, third, final];
	const before = structuredClone(history);
	assert.deepEqual(shownWaiting(history.slice(0, 2)), ['first']);
	assert.deepEqual(shownWaiting(history.slice(0, 3)), ['second']);
	assert.deepEqual(shownWaiting(history.slice(0, 4)), ['third']);
	for (const messages of [history, JSON.parse(JSON.stringify(history))]) {
		assert.deepEqual(shownWaiting(messages), []);
		const waits = backgroundWaitStates(messages);
		assert.deepEqual(messages.filter((item) => item.role === 'assistant')
			.flatMap((item) => presentReply(item, waits.get(item.id)).answer).map((block) => block.text), ['Final answer']);
	}
	assert.deepEqual(history, before);
});

test('partial completion keeps outstanding parallel jobs; last notice clears while resumed reply streams', () => {
	const waiting = waitingReply('waiting', ['one', 'two']);
	const resumed = message([backgroundDone('one')], { id: 'resumed' });
	assert.deepEqual(shownWaiting([waiting, resumed]), ['waiting']);
	resumed.content.push(backgroundDone('two'));
	assert.deepEqual(shownWaiting([waiting, resumed]), []);
});

test('success, failure, denial and cancellation settle only the matching background call', () => {
	for (const state of ['success', 'error', 'denied', 'cancelled']) {
		const waiting = waitingReply('waiting', ['job']);
		assert.deepEqual(shownWaiting([waiting, message([backgroundDone('other', state)], { id: 'other' })]), ['waiting']);
		assert.deepEqual(shownWaiting([waiting, message([backgroundDone('job', state)], { id: 'done' })]), []);
	}
});

test('incomplete, unrelated and user-supplied hints cannot settle background calls', () => {
	const waiting = waitingReply('waiting', ['job']);
	for (const block of [
		backgroundDone('job', 'running'),
		{ ...backgroundDone('job'), source: '{' },
		{ ...backgroundDone('job'), source: JSON.stringify({ label: 'other', backgroundToolCallId: 'job', backgroundState: 'success' }) },
	]) assert.deepEqual(shownWaiting([waiting, message([block], { id: 'unrelated' })]), ['waiting']);
	assert.deepEqual(shownWaiting([waiting, message([backgroundDone('job')], { id: 'user', role: 'user' })]), ['waiting']);
});

test('one user turn shows one wait, and another user turn cannot clear its pending work', () => {
	const waiting = waitingReply('first', ['one']);
	const laterWait = waitingReply('second');
	assert.deepEqual(shownWaiting([waiting, laterWait]), ['second']);
	const history = [waiting, message([text('new request')], { id: 'new-user', role: 'user' }), finalReply('new-final')];
	assert.deepEqual(shownWaiting(history), ['first']);
	history.push(message([backgroundDone('one')], { id: 'old-job-completed' }));
	assert.deepEqual(shownWaiting(history), []);
});

test('terminal completion and interruption end waits without altering permission requests', () => {
	for (const reason of ['error', 'interrupted', 'exceed_max_iters']) {
		const stopped = { ...waitingReply('stopped'), finished_reason: reason };
		assert.deepEqual(shownWaiting([waitingReply('wait', ['job']), stopped]), []);
	}
	assert.deepEqual(shownWaiting([waitingReply('wait'), finalReply('final')]), []);
	const confirmation = message([call('permission', 'asking')], { id: 'confirmation' });
	assert.equal(presentReply(confirmation, false).pending, 'confirmation');
});

test('authoritative pending IDs and real tool results survive replay and partial history', () => {
	const waiting = waitingReply('wait');
	waiting.metadata.analysisCompletion.pendingToolCallIds = ['job'];
	assert.deepEqual(shownWaiting([waiting]), ['wait']);
	assert.deepEqual(shownWaiting([waiting, message([result('job')], { id: 'done' })]), []);
	waiting.metadata.analysisCompletion.pendingToolCallIds = [];
	assert.deepEqual(shownWaiting([waiting]), []);
	waiting.metadata.analysisCompletion.pendingToolCallIds = [null];
	assert.deepEqual(shownWaiting([waiting]), ['wait']);
});
