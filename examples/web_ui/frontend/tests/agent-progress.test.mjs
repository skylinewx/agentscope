import assert from 'node:assert/strict';
import test from 'node:test';
import { applyAgentProgress, progressSteps, readAgentProgress } from '../src/lib/agent-progress.ts';
import { groupReplyMessages, presentReply, applyReplyPresentation } from '../src/lib/reply-presentation.ts';

const step = (id, status = 'running') => ({ id, status, summary: '已读取 42 条明细，返回 5 条汇总结果。', nextAction: '接下来核对数据范围。' });
const value = (revision = 1, id = 'reply') => ({ version: 1, replyId: id, revision, steps: [step('stage')] });
const event = (payload) => ({ type: 'CUSTOM', name: 'agent_progress', value: payload });
const msg = (id = 'reply') => ({ id, role: 'assistant', name: 'agent', metadata: {}, content: [] });

test('snapshots are correlated, deduplicated and final states cannot reopen', () => {
	const message = msg();
	assert.equal(applyAgentProgress(message, event(value(4))), true);
	assert.equal(applyAgentProgress(message, event(value(3))), false);
	assert.equal(applyAgentProgress(message, event(value(4))), false);
	assert.equal(applyAgentProgress(message, event(value(9, 'other'))), false);
	assert.equal(progressSteps(message).length, 1);
	message.finished_at = '2026-10-31T00:00:00Z';
	assert.equal(applyAgentProgress(message, event(value(10))), false);
});

test('terminal metadata restores prose and never leaks it into final answer', () => {
	const message = msg();
	const terminal = value(5); terminal.steps[0].status = 'completed';
	applyReplyPresentation(message, { type: 'REPLY_END', reply_id: 'reply', metadata: { agentProgress: terminal } });
	const restored = JSON.parse(JSON.stringify(message));
	assert.deepEqual(progressSteps(restored), terminal.steps);
	assert.equal(presentReply(restored).answer.length, 0);
});

test('background continuation keeps both replies progress in history', () => {
	const previous = { ...msg('first'), finished_reason: 'completed', metadata: {
		agentProgress: { ...value(3, 'first'), steps: [step('first-step', 'waiting')] },
		analysisCompletion: { status: 'WAITING', turnKey: 'turn' },
	} };
	const next = { ...msg('second'), metadata: { agentProgress: { ...value(2, 'second'), steps: [step('second-step')] } } };
	const grouped = groupReplyMessages([previous, next]);
	assert.equal(grouped.length, 1);
	assert.deepEqual(progressSteps(grouped[0].message).map((entry) => entry.id), ['first-step', 'second-step']);
});

test('unknown versions and malformed payloads are ignored', () => {
	assert.equal(readAgentProgress({ ...value(), version: 99 }), null);
	assert.equal(readAgentProgress({ ...value(), steps: [{ id: 'missing-prose' }] }), null);
	assert.equal(readAgentProgress({ ...value(), steps: [step('duplicate'), step('duplicate')] }), null);
	const previous = { ...msg('first'), metadata: { analysisCompletion: { status: 'WAITING', turnKey: 'turn' } } };
	const next = { ...msg('second'), metadata: { agentProgress: { ...value(2, 'second'), steps: {} } } };
	assert.doesNotThrow(() => groupReplyMessages([previous, next]));
});
