import assert from 'node:assert/strict';
import test from 'node:test';
import { registeredAnalyses } from '../src/lib/analysis-deliveries.ts';

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

test('a successful receipt supplies the card without a model link, live and after reload', () => {
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
