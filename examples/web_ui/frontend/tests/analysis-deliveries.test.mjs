import assert from 'node:assert/strict';
import test from 'node:test';
import { finalAnalysisDeliveries, latestAnalysisRevisions, registeredAnalyses } from '../src/lib/analysis-deliveries.ts';
import { applyReplyPresentation, groupReplyMessages, presentReply } from '../src/lib/reply-presentation.ts';

const id = 'ana_0123456789abcdef0123456789abcdef';
const path = `./analysis/${id}/revisions/4/analysis.json`;
const analysisRef = `${id}:4`;
const delivery = { type: 'workspace_analysis', registered: true, analysisRef, path };
const response = { analysisRef, publicationState: 'PUBLISHED', delivery };
const call = { type: 'tool_call', id: 'publish', name: 'AnalysisPublish', state: 'finished', input: { analysisRef } };
const result = { type: 'tool_result', id: 'publish', name: 'AnalysisPublish', state: 'success', metadata: { process_summary: { kind: 'analysis', id } }, output: [{ type: 'text', text: JSON.stringify(response) }] };
const message = (content = [call, result], extra = {}) => ({ role: 'assistant', content, ...extra });
const output = (value) => ({ ...result, output: [{ type: 'text', text: JSON.stringify(value) }] });
const expected = [{ analysisRef, path }];
const rejected = { type: 'hint', source: JSON.stringify({ label: 'System', sublabel: 'Completion check' }), hint: '修正' };

// 与 ToolOffloadMiddleware 保存的宿主通知格式一致；后台占位结果本身不是发布回执。
function background(value = response, metadata = result.metadata, name = call.name, callId = call.id) {
  return [
    { ...result, id: callId, name, metadata: { backgroundToolCallId: callId }, output: [{ type: 'text', text: '后台执行中' }] },
    { type: 'hint', source: JSON.stringify({ label: 'tool_output', backgroundToolCallId: callId,
      backgroundState: 'success', backgroundToolName: name, toolResponseMetadata: metadata }),
      hint: [{ type: 'text', text: `<system-notification>Tool '${name}' running in background (id=${callId}) has finished (state=success).\n\nResult:\n\n${JSON.stringify(value)}</system-notification>` }] },
  ];
}

function publication(analysisId, revision, callId) {
  const ref = `${analysisId}:${revision}`, target = `./analysis/${analysisId}/revisions/${revision}/analysis.json`;
  return [
    { ...call, id: callId, input: JSON.stringify({ analysisRef: ref }) },
    { ...result, id: callId, metadata: { process_summary: { kind: 'analysis', id: analysisId } },
      output: [{ type: 'text', text: JSON.stringify({ analysisRef: ref, publicationState: 'PUBLISHED',
        delivery: { ...delivery, analysisRef: ref, path: target } }) }] },
  ];
}

test('a successful receipt is tracked without a model link, live and after reload', () => {
  for (const tail of [[], [{ type: 'text', text: '[查看结果](./analysis/ana_misspelled/revisions/4/analysis.json)' }]]) {
    const m = message([call, result, ...tail]); const before = structuredClone(m);
    assert.deepEqual(registeredAnalyses(m), expected);
    assert.deepEqual(registeredAnalyses(JSON.parse(JSON.stringify({ ...m, finished_at: '2026-10-13' }))), expected);
    assert.deepEqual(m, before);
  }
});

test('duplicate receipts register one card while distinct actual deliveries survive', () => {
  assert.deepEqual(registeredAnalyses(message([call, result, result])), expected);
  const nextRef = `${id}:5`, nextPath = path.replace('/4/', '/5/');
  const nextCall = { ...call, id: 'next', input: JSON.stringify({ analysisRef: nextRef }) };
  const nextResult = { ...output({ ...response, analysisRef: nextRef, delivery: { ...delivery, analysisRef: nextRef, path: nextPath } }), id: 'next' };
  assert.deepEqual(registeredAnalyses(message([call, result, nextCall, nextResult])), [...expected, { analysisRef: nextRef, path: nextPath }]);
});

test('automatic revision shows only the latest card and retains all paths to suppress repeated model links', () => {
  const m = message([
    ...publication(id, 5, 'draft'),
    { type: 'text', id: 'draft-link', text: `[查看结果](./analysis/${id}/revisions/5/analysis.json)` },
    { type: 'hint', source: JSON.stringify({ label: 'System', sublabel: 'Completion check' }), hint: '修正' },
    ...publication(id, 6, 'final'),
    { type: 'text', id: 'final-link', text: `[查看结果](./analysis/${id}/revisions/6/analysis.json)` },
  ]);
  const before = structuredClone(m);
  for (const value of [m, JSON.parse(JSON.stringify({ ...m, finished_at: '2026-10-21' }))]) {
    const deliveries = registeredAnalyses(value);
    assert.deepEqual(latestAnalysisRevisions(deliveries), [
      { analysisRef: `${id}:6`, path: `./analysis/${id}/revisions/6/analysis.json` },
    ]);
    assert.deepEqual(deliveries.map((item) => item.path), [5, 6].map((revision) => `./analysis/${id}/revisions/${revision}/analysis.json`));
  }
  assert.deepEqual(m, before);
});

test('late old delivery inspections and repeated receipts cannot downgrade the final revision', () => {
  const inspect = { ...call, id: 'inspect-old', name: 'AnalysisInspect', input: { section: 'delivery', analysisRef } };
  const inspectResult = { ...output(delivery), id: 'inspect-old', name: 'AnalysisInspect' };
  const deliveries = registeredAnalyses(message([
    ...publication(id, 9, 'nine'), ...publication(id, 10, 'ten'),
    ...publication(id, 9, 'replay'), inspect, inspectResult,
  ]));
  const before = structuredClone(deliveries);
  assert.deepEqual(latestAnalysisRevisions(deliveries), [
    { analysisRef: `${id}:10`, path: `./analysis/${id}/revisions/10/analysis.json` },
  ]);
  assert.deepEqual(deliveries, before);
});

test('distinct analyses and separate replies retain their own cards', () => {
  const otherId = 'ana_fedcba9876543210fedcba9876543210';
  const m = message([...publication(id, 5, 'first'), ...publication(otherId, 1, 'other'), ...publication(id, 6, 'revised')]);
  assert.deepEqual(latestAnalysisRevisions(registeredAnalyses(m)).map((item) => item.analysisRef), [`${id}:6`, `${otherId}:1`]);
  assert.deepEqual(latestAnalysisRevisions(registeredAnalyses(message())), expected);
});

test('an unsuccessful newer revision does not replace the last successful delivery', () => {
  const [newCall, newResult] = publication(id, 5, 'new');
  for (const changed of [{ ...newResult, state: 'error' }, { ...newResult, state: 'running' }]) {
    assert.deepEqual(latestAnalysisRevisions(registeredAnalyses(message([call, result, newCall, changed]))), expected);
  }
  assert.deepEqual(latestAnalysisRevisions([]), []);
});

test('streaming publications wait for final approval before any rejection hint exists', () => {
  const m = message([], { id: 'reply', finished_reason: null });
  for (const revision of [5, 6, 7]) {
    m.content.push(...publication(id, revision, `publish-${revision}`));
    const before = structuredClone(m);
    assert.equal(registeredAnalyses(m).length, revision - 4, 'Keep all receipts in the process');
    assert.deepEqual(finalAnalysisDeliveries(m), [], 'Publishing is not final approval');
    assert.deepEqual(finalAnalysisDeliveries(JSON.parse(JSON.stringify(m))), [], 'Reloading a running reply must not expose a card');
    assert.deepEqual(m, before);
  }
  m.content.push({ type: 'text', id: 'answer', text: '最终说明' });
  const completion = { status: 'READY', analysisRef: `${id}:7`, semanticReview: 'PASS' };
  applyReplyPresentation(m, { type: 'REPLY_END', reply_id: 'reply', metadata: { analysisCompletion: completion } });
  assert.deepEqual(finalAnalysisDeliveries(m), [], 'A terminal verdict alone must not bypass the reply state');
  m.finished_reason = 'completed';
  assert.deepEqual(finalAnalysisDeliveries(m).map(item => item.analysisRef), [`${id}:7`]);
  assert.deepEqual(finalAnalysisDeliveries(JSON.parse(JSON.stringify(m))).map(item => item.analysisRef), [`${id}:7`]);
});

test('missing completion metadata cannot promote unfinished or aborted publications', () => {
  for (const finished_reason of [undefined, null, 'error', 'interrupted', 'exceed_max_iters']) {
    const m = message([call, result], { finished_reason, finished_at: '2026-10-21' });
    assert.deepEqual(finalAnalysisDeliveries(m), []);
    assert.deepEqual(registeredAnalyses(m), expected, 'Retain the published artifact in process history');
    assert.ok(presentReply(m).process.includes(result));
  }
});

test('rejected publication is hidden during correction and after failed/interrupted reloads', () => {
  for (const reason of [null, 'error', 'interrupted', 'exceed_max_iters', 'completed']) {
    const m = message([call, result, rejected, { type: 'text', id: 'answer', text: '修正未完成' }], { finished_reason: reason });
    assert.deepEqual(finalAnalysisDeliveries(m), []);
    assert.deepEqual(finalAnalysisDeliveries(JSON.parse(JSON.stringify(m))), []);
    assert.deepEqual(registeredAnalyses(m), expected, 'Keep receipts for history and Markdown link suppression');
    assert.ok(presentReply(m).process.includes(result));
  }
});

test('a terminal incomplete verdict hides publication even without a revision hint', () => {
  for (const status of ['INCOMPLETE', 'BLOCKED']) {
    const m = message([call, result], { finished_reason: 'completed', metadata: { analysisCompletion: { status, analysisRef } } });
    assert.deepEqual(finalAnalysisDeliveries(m), []);
  }
});

test('text-only correction can approve the same published artifact, live and after reload', () => {
  const m = message([call, result, rejected, { type: 'text', id: 'final', text: '已纠正措辞' }], { id: 'reply', finished_reason: 'completed' });
  const completion = { status: 'READY', analysisRef, semanticReview: 'PASS' };
  applyReplyPresentation(m, { type: 'REPLY_END', reply_id: 'reply', metadata: { analysisCompletion: completion } });
  assert.deepEqual(finalAnalysisDeliveries(m), expected);
  assert.deepEqual(finalAnalysisDeliveries(JSON.parse(JSON.stringify(m))), expected);
  assert.deepEqual(m.metadata.analysisCompletion, completion);
});

test('only the final approved revision is presented and new repair receipts wait for approval', () => {
  const other = 'ana_fedcba9876543210fedcba9876543210';
  const m = message([call, result, rejected, ...publication(id, 5, 'repair'), ...publication(other, 1, 'other')]);
  assert.deepEqual(finalAnalysisDeliveries(m), []);
  m.finished_reason = 'completed';
  m.metadata = { analysisCompletion: { status: 'READY', analysisRef: `${id}:5`, semanticReview: 'PASS' } };
  assert.deepEqual(finalAnalysisDeliveries(m).map(d => d.analysisRef), [`${id}:5`]);
  m.content.push(...publication(id, 6, 'unreviewed'));
  assert.deepEqual(finalAnalysisDeliveries(m).map(d => d.analysisRef), [`${id}:5`], 'Largest revision is not necessarily approved');
});

test('partial approved results survive but missing receipts, failed reviews and stale passes do not', () => {
  for (const status of ['READY', 'PARTIAL']) {
    const m = message([call, result, rejected], { finished_reason: 'completed', metadata: { analysisCompletion: { status, analysisRef, semanticReview: 'PASS' } } });
    assert.deepEqual(finalAnalysisDeliveries(m), expected);
    for (const reason of [null, 'error', 'interrupted', 'exceed_max_iters']) {
      assert.deepEqual(finalAnalysisDeliveries({ ...m, finished_reason: reason }), []);
    }
    for (const semanticReview of [undefined, 'REVISE', 'UNAVAILABLE']) {
      assert.deepEqual(finalAnalysisDeliveries({ ...m, metadata: { analysisCompletion: { status, analysisRef, semanticReview } } }), []);
    }
    assert.deepEqual(finalAnalysisDeliveries({ ...m, content: [] }), [], 'A verdict cannot fabricate a delivery');
  }
});

test('completed conversations without completion protocol retain deduplication and distinct results', () => {
  const other = 'ana_fedcba9876543210fedcba9876543210';
  const m = message([...publication(id, 4, 'old'), ...publication(id, 5, 'latest'), ...publication(other, 1, 'other')], { finished_reason: 'completed' });
  const before = structuredClone(m);
  assert.deepEqual(finalAnalysisDeliveries(m).map(d => d.analysisRef), [`${id}:5`, `${other}:1`]);
  assert.deepEqual(m, before);
});

test('preview, failed, incomplete and unregistered results cannot publish cards', () => {
  for (const changed of [
    { ...result, state: 'error' }, { ...result, state: 'running' },
    output({ ...response, previewOnly: true }), output({ ...response, publicationState: 'DRAFT' }),
    output({ ...response, error: { code: 'FAILED' } }),
    output({ ...response, delivery: { ...delivery, registered: false } }),
    { ...result, output: [{ type: 'text', text: '{' }] },
    { ...result, output: JSON.stringify(response) },
    { ...result, output: [{ type: 'text', text: `[查看结果](${path})` }] },
  ]) assert.deepEqual(registeredAnalyses(message([call, changed])), []);
  assert.deepEqual(registeredAnalyses(message([{ ...call, input: { analysisRef, previewOnly: true } }, result])), []);
});

test('only paired built-in tool calls with analysis metadata supply cards', () => {
  assert.deepEqual(registeredAnalyses(message([result])), []);
  assert.deepEqual(registeredAnalyses(message([call, { ...result, metadata: {} }])), []);
  assert.deepEqual(registeredAnalyses(message(undefined, { role: 'user' })), []);
  for (const name of ['Read', 'mcp__external__AnalysisPublish', 'AnalysisBuild']) {
    assert.deepEqual(registeredAnalyses(message([{ ...call, name }, { ...result, name }])), []);
  }
  assert.deepEqual(registeredAnalyses(message([call, { ...result, name: 'Read' }])), []);
});

test('canonical path, revision and analysis identity must agree', () => {
  for (const changed of [
    { ...delivery, path: path.replace('/4/', '/04/') }, { ...delivery, path: 'https://other.invalid/' + path },
    { ...delivery, path: path + '?x=1' }, { ...delivery, path: path.replace(id, 'ana_short') },
    { ...delivery, analysisRef: `${id}:5` }, { ...delivery, type: 'tool_markdown' },
  ]) assert.deepEqual(registeredAnalyses(message([call, output({ ...response, delivery: changed })])), []);
  assert.deepEqual(registeredAnalyses(message([call, output({ ...response, analysisRef: `${id}:5` })])), []);
  assert.deepEqual(registeredAnalyses(message([{ ...call, input: { analysisRef: `${id}:5` } }, result])), []);
  assert.deepEqual(registeredAnalyses(message([call, { ...result, metadata: { process_summary: { kind: 'analysis', id: 'another' } } }])), []);
});

test('an explicit delivery inspection can restore the card but ordinary inspection cannot', () => {
  const inspectCall = { ...call, name: 'AnalysisInspect', input: JSON.stringify({ section: 'delivery', analysisRef }) };
  const inspectResult = { ...output(delivery), name: 'AnalysisInspect' };
  assert.deepEqual(registeredAnalyses(message([inspectCall, inspectResult])), expected);
  assert.deepEqual(registeredAnalyses(message([{ ...inspectCall, input: { section: 'facts' } }, inspectResult])), []);
  assert.deepEqual(registeredAnalyses(message([{ ...inspectCall, input: { section: 'delivery', analysisRef: `${id}:5` } }, inspectResult])), []);
});

test('background publish is registered after continuation and reload, without a model link', () => {
  const turnKey = 'user-turn';
  const [placeholder, notification] = background(response, { ...result.metadata, analysisTurnKey: turnKey });
  const waiting = message([call, placeholder], { id: 'waiting', name: 'agent', finished_reason: 'completed',
    metadata: { analysisCompletion: { status: 'WAITING', turnKey, pendingToolCallIds: [call.id] } } });
  assert.deepEqual(registeredAnalyses(waiting), []);
  const continued = message([notification], { id: 'continued', name: 'agent', finished_reason: 'completed',
    metadata: { analysisCompletion: { status: 'READY', turnKey, semanticReview: 'PASS', analysisRef } } });
  const before = structuredClone([waiting, continued]);
  for (const segments of [[waiting, continued], JSON.parse(JSON.stringify([waiting, continued]))]) {
    const grouped = groupReplyMessages(segments);
    assert.equal(grouped.length, 1);
    assert.deepEqual(registeredAnalyses(grouped[0].message), expected);
    assert.deepEqual(finalAnalysisDeliveries(grouped[0].message), expected);
    assert.deepEqual(finalAnalysisDeliveries({ ...grouped[0].message, finished_reason: null }), []);
  }
  assert.deepEqual([waiting, continued], before, 'Do not rewrite saved messages');
});

test('background and synchronous deliveries share deduplication and final completion gating', () => {
  const m = message([call, ...background(), result, ...background(), { type: 'text', text: `[查看结果](${path})` }],
    { finished_reason: 'completed', metadata: { analysisCompletion: { status: 'READY', analysisRef, semanticReview: 'PASS' } } });
  assert.deepEqual(registeredAnalyses(m), expected);
  assert.deepEqual(finalAnalysisDeliveries(m), expected);
  for (const semanticReview of ['REVISE', 'NOT_REVIEWED']) {
    assert.deepEqual(finalAnalysisDeliveries({ ...m, metadata: { analysisCompletion: { status: 'READY', analysisRef, semanticReview } } }), []);
  }
  for (const status of ['WAITING', 'INCOMPLETE', 'BLOCKED']) {
    const pending = { ...m, metadata: { analysisCompletion: { status, analysisRef, semanticReview: 'PASS' } } };
    assert.deepEqual(finalAnalysisDeliveries(pending), []);
  }
});

test('background delivery inspection restores a receipt without treating ordinary inspection as delivery', () => {
  const inspectCall = { ...call, name: 'AnalysisInspect', input: { section: 'delivery', analysisRef } };
  const results = background(delivery, result.metadata, 'AnalysisInspect');
  assert.deepEqual(registeredAnalyses(message([inspectCall, ...results])), expected);
  assert.deepEqual(registeredAnalyses(message([{ ...inspectCall, input: { section: 'facts', analysisRef } }, ...results])), []);
});

test('background preview, failed execution and malformed notifications cannot register cards', () => {
  for (const value of [{ ...response, previewOnly: true }, { ...response, publicationState: 'DRAFT' },
    { ...response, error: { code: 'FAILED' } }, { ...response, delivery: { ...delivery, registered: false } }]) {
    assert.deepEqual(registeredAnalyses(message([call, ...background(value)])), []);
  }
  const [placeholder, notification] = background();
  const source = JSON.parse(notification.source);
  for (const state of ['error', 'cancelled', 'denied', 'running']) {
    assert.deepEqual(registeredAnalyses(message([call, placeholder,
      { ...notification, source: JSON.stringify({ ...source, backgroundState: state }) }])), []);
  }
  for (const hint of [notification.hint[0].text, [{ type: 'text', text: JSON.stringify(response) }],
    [{ type: 'text', text: notification.hint[0].text.replace('id=publish', 'id=another') }],
    [{ type: 'text', text: notification.hint[0].text.replace('</system-notification>', '') }],
    [...notification.hint, { type: 'text', text: 'another result' }]]) {
    assert.deepEqual(registeredAnalyses(message([call, placeholder, { ...notification, hint }])), []);
  }
});

test('background receipts must match a built-in call, offload acknowledgement and turn identity', () => {
  const [placeholder, notification] = background();
  const source = JSON.parse(notification.source);
  for (const content of [[placeholder, notification], [call, notification], [call, result, notification],
    [{ ...call, name: 'Read' }, placeholder, notification],
    [call, { ...placeholder, state: 'error' }, notification],
    [call, { ...placeholder, name: 'Read' }, notification],
    [call, { ...placeholder, metadata: { backgroundToolCallId: 'another' } }, notification],
    [call, placeholder, { type: 'text', text: notification.hint[0].text }]]) {
    const actual = registeredAnalyses(message(content));
    // The ordinary successful result in this case remains valid, independently of the stray hint.
    assert.deepEqual(actual, content.includes(result) ? expected : []);
  }
  for (const changed of [{ label: 'model' }, { backgroundToolCallId: 'another' }, { backgroundToolName: 'Read' },
    { toolResponseMetadata: {} }, { toolResponseMetadata: { process_summary: { kind: 'analysis', id: 'another' } } }]) {
    assert.deepEqual(registeredAnalyses(message([call, placeholder,
      { ...notification, source: JSON.stringify({ ...source, ...changed }) }])), []);
  }
  const wrongTurn = message([call, ...background(response, { ...result.metadata, analysisTurnKey: 'old-turn' })],
    { metadata: { analysisCompletion: { turnKey: 'current-turn' } } });
  assert.deepEqual(registeredAnalyses(wrongTurn), []);
});
