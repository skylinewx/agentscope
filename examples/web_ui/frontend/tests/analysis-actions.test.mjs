import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { draftScope, readDraft, writeDraft, insertDraft } from '../src/lib/analysis-draft.ts';
import { parseSnapshot, parseActionResolution } from '../src/lib/workspace-analysis.ts';

const base = JSON.parse(readFileSync(new URL('../../../agent_service/analysis_extension/presentation-v1.example.json', import.meta.url), 'utf8'));
const actionRef = base.analysisRef + '#action_' + 'a'.repeat(24);
const finding = { findingId: 'finding_x', statement: '原值为负', support: 'SUPPORTED', type: 'sign', basis: '原值', ruleVersion: '1', applicability: '符号', sourceType: 'observation', previousAnalysisRef: null,
  explanations: [{ kind: 'observation', statement: '来源', support: 'SUPPORTED', missingEvidence: ['指标含义'] }] };
const action = { actionRef, findingRefs: ['finding_x'], goal: '核对同口径新值', scopeLabel: '本对象', requiredEvidence: ['新值'], expectedDecision: '确认原观察', availability: 'AVAILABLE', availabilityReason: '已发现只读来源', priorityBasis: '先核对来源' };
const run = { actionRunId: 'run_a', actionRef, status: 'RUNNING', resultAnalysisRef: null, updatedAt: '2026-10-11', resultNote: '' };
const fixture = { ...base, findings: [finding], actions: [action], actionRuns: [run], findingsScopeLabel: '原发布范围' };

test('a completed formula check retains its difference note and requires the current field', () => {
  const fixture = JSON.parse(readFileSync(new URL('./analysis-formula-followup.fixture.json', import.meta.url), 'utf8'));
  const parsed = parseSnapshot(fixture);
  assert.equal(parsed.actionRuns[0].status, 'COMPLETED');
  assert.match(parsed.actionRuns[0].resultNote, /存在差额.*不代表公式一致/);
  const incomplete = structuredClone(fixture);
  delete incomplete.actionRuns[0].resultNote;
  assert.throws(() => parseSnapshot(incomplete), /INVALID/);
});

test('empty draft inserts text without a send callback', () => {
  const scope = draftScope('server', 'user', 'agent', 'session');
  writeDraft(scope, '');
  assert.deepEqual(insertDraft(scope, scope, '建议'), { kind: 'inserted', value: '建议' });
  assert.equal(readDraft(scope), '建议');
});
test('nonempty draft requires an explicit append, preserving edits made while confirming', () => {
  const scope = 'confirm-case'; writeDraft(scope, '原草稿');
  assert.equal(insertDraft(scope, scope, '建议').kind, 'confirm');
  assert.equal(readDraft(scope), '原草稿');
  writeDraft(scope, '用户随后编辑');
  assert.equal(insertDraft(scope, scope, '建议', true).value, '用户随后编辑\n\n建议');
});
test('late resolution from another server/user/agent/session cannot overwrite current draft', () => {
  const current = draftScope('s', 'u', 'a', 'chat'); writeDraft(current, '保留');
  for (const parts of [['other', 'u', 'a', 'chat'], ['s', 'other', 'a', 'chat'], ['s', 'u', 'other', 'chat'], ['s', 'u', 'a', 'other']]) {
    assert.equal(insertDraft(current, draftScope(...parts), '迟到建议', true).kind, 'stale');
    assert.equal(readDraft(current), '保留');
  }
});
test('switching sessions preserves independent drafts', () => {
  const first = draftScope('s', 'u', 'a', 'one'), second = draftScope('s', 'u', 'a', 'two');
  writeDraft(first, '一'); writeDraft(second, '二');
  assert.equal(readDraft(first), '一'); assert.equal(readDraft(second), '二');
});
test('registered actions parse but cross-revision or missing finding references fail', () => {
  assert.equal(parseSnapshot(fixture).actions[0].actionRef, actionRef);
  for (const changed of [{ ...action, actionRef: actionRef.replace(/:[0-9]+#/, ':99#') }, { ...action, findingRefs: ['absent'] }, { ...action, availability: 'COMPLETED' }]) {
    assert.throws(() => parseSnapshot({ ...fixture, actions: [changed] }), /INVALID/);
  }
});
test('causal hypotheses cannot claim support and completed runs need a new result', () => {
  assert.throws(() => parseSnapshot({ ...fixture, findings: [{ ...finding, explanations: [{ ...finding.explanations[0], kind: 'business_cause_hypothesis' }] }] }), /INVALID/);
  assert.throws(() => parseSnapshot({ ...fixture, actionRuns: [{ ...run, status: 'COMPLETED' }] }), /INVALID/);
  assert.throws(() => parseSnapshot({ ...fixture, actionRuns: [{ ...run, status: 'COMPLETED', resultAnalysisRef: base.analysisRef }] }), /INVALID/);
});
test('action resolution is tied to requested action and bounded draft content', () => {
  assert.equal(parseActionResolution({ actionRef, text: '核对', availability: 'AVAILABLE' }, actionRef).text, '核对');
  assert.throws(() => parseActionResolution({ actionRef: actionRef + 'x', text: '核对', availability: 'AVAILABLE' }, actionRef));
  assert.throws(() => parseActionResolution({ actionRef, text: 'x'.repeat(16001), availability: 'AVAILABLE' }, actionRef));
});
test('current snapshots require explicit empty findings and actions', () => {
  assert.deepEqual(parseSnapshot(base).actions, []);
  for (const field of ['findings', 'actions']) { const incomplete = { ...base }; delete incomplete[field]; assert.throws(() => parseSnapshot(incomplete), /INVALID/); }
});
