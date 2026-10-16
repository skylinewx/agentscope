import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Streamdown, defaultRemarkPlugins } from 'streamdown';
import { analysisPath, remarkWorkspaceAnalyses, parseDelivery, parseSnapshot, validateScene } from '../src/lib/workspace-analysis.ts';
import { remarkWorkspaceReports } from '../src/lib/workspace-reports.ts';

const path = './analysis/ana_0123456789abcdef0123456789abcdef/revisions/3/analysis.json';
function render(text, source = text) {
  return renderToStaticMarkup(React.createElement(Streamdown, {
    mode: 'static', remarkPlugins: [...Object.values(defaultRemarkPlugins), remarkWorkspaceReports, [remarkWorkspaceAnalyses, { source }]],
    allowedTags: { 'workspace-analysis': ['path'], 'workspace-report': ['path'], 'workspace-analysis-link': ['path'], 'workspace-analysis-unavailable': ['standalone'] },
    components: { 'workspace-analysis': ({ path }) => React.createElement('div', { 'data-analysis': path }), 'workspace-report': ({ path }) => React.createElement('span', { 'data-report': path }), 'workspace-analysis-link': ({ path, children }) => React.createElement('button', { 'data-analysis-link': path }, children), 'workspace-analysis-unavailable': ({ children }) => React.createElement('span', { 'data-invalid-analysis': true }, children) },
  }, text));
}

test('only the canonical registered path shape is promoted', () => {
  assert.equal(analysisPath(path), path);
  for (const p of [path.toUpperCase(), path.replaceAll('/', '\\'), path.replace('analysis/', '%61nalysis/'), path + '?x=1', path.replace('/3/', '/03/'), './other.json', '../' + path, 'file://' + path]) assert.equal(analysisPath(p), null, p);
});
test('standalone links become cards and inline links become explicit view controls', () => {
  assert.match(render(`[查看分析](${path})`), /data-analysis=/);
  const inline = render(`该结果已登记为可查看的分析记录：[查看分析结果](${path})`);
  assert.match(inline, /data-analysis-link=/);
  assert.match(inline, /查看分析结果/);
  assert.doesNotMatch(inline, /data-analysis=|href=/);
  for (const text of ['`[查看分析](' + path + ')`', `![图](${path})`, '```md\n[查看分析](' + path + ')\n```']) assert.doesNotMatch(render(text), /data-analysis(?:-link)?=/);
});
test('inline reference and formatted links work without turning ordinary links into analysis controls', () => {
  for (const text of [`查看 **[结果](${path})**。`, `结果：[查看][analysis]\n\n[analysis]: ${path}`, `| 项目 | 入口 |\n|---|---|\n| 分析 | [查看](${path}) |`]) {
    assert.match(render(text), /data-analysis-link=/);
    assert.doesNotMatch(render(text), /data-analysis=|href=/);
  }
  for (const href of ['https://example.com/', './other.json', path + '?x=1', path.replace('/3/', '/03/')]) {
    assert.doesNotMatch(render(`结果：[查看](${href})`), /data-analysis(?:-link)?=/);
  }
});
test('inline stream prefixes remain inert until the original closing delimiter arrives', () => {
  const full = `结果：[查看分析](${path})`;
  for (let end = 0; end < full.length; end++) assert.doesNotMatch(render(full.slice(0, end)), /data-analysis(?:-link)?=/, `prefix ${end}`);
  assert.doesNotMatch(render(full, full.slice(0, -1)), /data-analysis(?:-link)?=/);
  assert.match(render(full), /data-analysis-link=/);
});
test('stream prefixes never produce a card until the closing delimiter arrives', () => {
  const full = `[查看分析](${path})`;
  for (let end = 0; end < full.length; end++) assert.doesNotMatch(render(full.slice(0, end)), /data-analysis=/, `prefix ${end}`);
  // Streamdown may repair an incomplete link; original-source gating still prevents a request.
  assert.doesNotMatch(render(full, full.slice(0, -1)), /data-analysis=/);
  assert.equal((render(full + '\n\n' + full).match(/data-analysis=/g) ?? []).length, 1);
});
test('analysis reference links and workspace HTML reports coexist', () => {
  assert.match(render(`[结果][analysis]\n\n[analysis]: ${path}`), /data-analysis=/);
  assert.match(render('[报告](./report/current.html)'), /data-report=/);
});

test('malformed internal analysis references never become external link controls', () => {
  for (const href of [path.replace('0123456789abcdef0123456789abcdef', 'd61759818c465794aee1ffa53a3131'), path.replace('./', '/'), path + '?x=1']) {
    for (const value of [`[查看结果](${href})`, `参见 [查看结果](${href})。`, `[结果][analysis]\n\n[analysis]: ${href}`]) {
      const markup = render(value);
      assert.match(markup, /data-invalid-analysis/);
      assert.doesNotMatch(markup, /data-streamdown="link"|href=|data-analysis=/);
    }
  }
  assert.match(render('[网站](https://example.com/)'), /data-streamdown="link"/);
  assert.doesNotMatch(render('`[结果](./analysis/invalid/analysis.json)`'), /data-invalid-analysis/);
});
test('future protocol and unbounded or executable scene primitives fail closed', () => {
  assert.throws(() => parseSnapshot({ schemaVersion: 2 }), /UNSUPPORTED/);
  const scene = { viewId: 'test-chart', type: 'BAR_RANK', width: 600, height: 100, zeroBaseline: 0, description: 'test', rowKeys: [], primitives: [] };
  validateScene(scene);
  for (const primitive of [{ kind: 'script', text: 'x', tone: 'label' }, { kind: 'rect', x: Infinity, y: 0, width: 1, height: 1, tone: 'positive' }, { kind: 'text', x: 0, y: 0, text: 'x', tone: 'url(x)' }]) assert.throws(() => validateScene({ ...scene, primitives: [primitive] }));
});
test('registered export metadata is bound to this revision and a canonical file', () => {
  const analysisRef = 'ana_0123456789abcdef0123456789abcdef:3';
  const exportRef = 'export_' + 'a'.repeat(64);
  const delivery = { analysisRef, exportRef, path: path.replace('analysis.json', `reports/${exportRef}.html`), contentDigest: 'b'.repeat(64), rendererVersion: '1.10.1', layout: 'standard', scopeLabel: '全部', isFiltered: false, sizeBytes: 1024, registered: true, viewParameters: { filter: 'all', sort: 'published', topN: null, columnKey: null } };
  assert.equal(parseDelivery(delivery, analysisRef), delivery);
  assert.equal(parseDelivery({ ...delivery, rendererVersion: '1.9.0' }, analysisRef).rendererVersion, '1.9.0');
  for (const change of [{ rendererVersion: '' }, { layout: undefined }, { analysisRef: analysisRef.replace(':3', ':4') }, { path: 'https://example.invalid/report.html' }, { registered: false }, { sizeBytes: 11 * 1024 * 1024 }, { viewParameters: { ...delivery.viewParameters, topN: 0 } }]) assert.throws(() => parseDelivery({ ...delivery, ...change }, analysisRef));
});
test('explicit source-value plots parse without changing the displayed values or comparability', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../../agent_service/analysis_extension/presentation-v1.example.json', import.meta.url), 'utf8'));
  const scene = { viewId: 'test-chart', type: 'BAR_VALUES', width: 600, height: 100, zeroBaseline: 24, description: '来源原值', rowKeys: [fixture.rows[0].rowKey], primitives: [
    { kind: 'rect', x: 23.9, y: 30, width: 0.1, height: 10, tone: 'negative', rowKey: fixture.rows[0].rowKey },
    { kind: 'line', x1: 23.9, x2: 23.9, y1: 27, y2: 43, tone: 'negative' },
  ] };
  const snapshot = { ...fixture, sections: [...fixture.sections.filter(s=>s.blocks.some(b=>b.metricIndexes.length || b.tableIndexes.length)), {groupId:'chart',title:'',description:'',width:'full',claimsPosition:'after',claimIndexes:[],blocks:[{viewId:'test-chart',title:'',description:'',metricIndexes:[],tableIndexes:[],sceneIndexes:[0]}]}], scenes: [scene], chartsRequested: true, statuses: { ...fixture.statuses, comparability: 'UNCONFIRMED' } };
  const parsed = parseSnapshot(snapshot);
  assert.deepEqual(parsed.scenes, [scene]);
  assert.deepEqual(parsed.rows, fixture.rows);
  assert.equal(parsed.statuses.comparability, 'UNCONFIRMED');
});
test('Python-produced public v1 fixture passes, while misleading page bounds fail', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../../agent_service/analysis_extension/presentation-v1.example.json', import.meta.url), 'utf8'));
  assert.equal(parseSnapshot(fixture).total, fixture.rows.length);
  for (const rendererVersion of ['1.0.0', '1.0.1', '1.1.0', '1.5.0', '1.5.0']) assert.throws(() => parseSnapshot({ ...fixture, rendererVersion }), /UNSUPPORTED/);
  for (const field of ['technicalDetails', 'findings', 'actions', 'chartsRequested', 'findingsScopeLabel']) { const missing = { ...fixture }; delete missing[field]; assert.throws(() => parseSnapshot(missing), /INVALID/); }
  assert.throws(() => parseSnapshot({ ...fixture, rendererVersion: '9.0.0' }), /UNSUPPORTED/);
  assert.throws(() => parseSnapshot({ ...fixture, page: { ...fixture.page, nextOffset: 0 } }), /INVALID/);
  assert.throws(() => parseSnapshot({ ...fixture, metrics: [{ valueRef: 'x', label: 'x', display: '2', unit: '项', scopeId: 'another-scope' }] }), /INVALID/);
});

test('explicit host snapshots preserve empty plans and selected findings without client inference', () => {
  const fixtures = JSON.parse(readFileSync(new URL('./analysis-capabilities.fixture.json', import.meta.url), 'utf8'));
  const details = parseSnapshot(fixtures.details.snapshot);
  assert.equal(details.rendererVersion, '1.11.1');
  assert.equal(details.chartsRequested, false);
  assert.deepEqual(details.metrics, []);
  assert.deepEqual(details.findings, []);
  assert.deepEqual(details.actions, []);
  assert.deepEqual(details.scenes, []);
  const selected = parseSnapshot(fixtures.planned.snapshot);
  assert.equal(selected.findings.length, 1);
  assert.equal(selected.actions.length, 1);
  assert.deepEqual(selected.scenes, []);
  assert.equal(selected.metrics.length, 1);
  assert.throws(() => parseSnapshot({ ...details, chartsRequested: 'false' }), /INVALID/);
});

test('host expression plans retain selected metric fields, prose and explicit display order', () => {
  const fixtures = JSON.parse(readFileSync(new URL('./analysis-expression.fixture.json', import.meta.url), 'utf8'));
  for (const fixture of Object.values(fixtures)) parseSnapshot(fixture);
  const view = parseSnapshot(fixtures.default);
  assert.deepEqual(view.metrics.map((m) => [m.label, m.display]), [['本次对象数', '14'], ['需要核对', '7']]);
  assert.match(view.claims[0], /^2026 年本轮先看所选范围：/);
  assert.match(view.claims[0], /业务原因尚不能确定/);
  assert.equal(view.rows[0].exact, '20');
  assert.equal(fixtures.asc.rows[0].exact, '-5');
  assert.deepEqual(fixtures.negative.metrics.map((m) => [m.label, m.display]), [['本次对象数', '7'], ['需要核对', '7']]);
});

test('host table projections preserve member groups, formula columns and page counts', () => {
  const fixtures = JSON.parse(readFileSync(new URL('./analysis-closeout.fixture.json', import.meta.url), 'utf8'));
  for (const fixture of Object.values(fixtures)) parseSnapshot(fixture);
  const whole = parseSnapshot(fixtures.default), filtered = parseSnapshot(fixtures.negative), next = parseSnapshot(fixtures.next);
  assert.equal(whole.objectCount, 14);
  assert.equal(whole.total, 42);
  assert.equal(whole.rows.length, 36);
  assert.equal(next.rows.length, 6);
  assert.equal(whole.tables.filter((t) => t.kind === 'values').length, 2);
  assert.deepEqual(whole.tables.find((t) => t.kind === 'formula').columns.map((c) => c.label), ['来源值', '本层计算', '本层差额', '逐层展开计算', '展开后差额']);
  assert.equal(filtered.objectCount, 7);
  assert.equal(filtered.rows.length, 21);
  for (const member of filtered.memberKeys) assert.deepEqual(filtered.rows.filter((r) => r.memberKey === member).map((r) => r.exact), ['-5', '10', '15']);
  assert.equal(fixtures.top.rows.length, 3);
  for (const field of ['tables', 'memberKeys', 'objectCount', 'originalObjectCount', 'columns', 'activeColumnKey']) {
    const incomplete = { ...whole }; delete incomplete[field]; assert.throws(() => parseSnapshot(incomplete), /INVALID/);
  }
  const foreign = structuredClone(whole); foreign.tables[0].rows[0].memberKey = 'member_' + '0'.repeat(24);
  assert.throws(() => parseSnapshot(foreign), /INVALID/);
  assert.throws(() => parseSnapshot({ ...whole, page: { ...whole.page, total: whole.total } }), /INVALID/);
});

test('model presentation switches remain explicit and invalid table controls are refused', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../../agent_service/analysis_extension/presentation-v1.example.json', import.meta.url), 'utf8'));
  assert.equal(parseSnapshot(fixture).technicalDetails, false);
  assert.equal(parseSnapshot({ ...fixture, technicalDetails: true }).technicalDetails, true);
  for (const technicalDetails of [undefined, 'false', 0]) assert.throws(() => parseSnapshot({ ...fixture, technicalDetails }), /INVALID/);
  for (const edit of [{ collapsed: undefined }, { collapsed: 'yes' }, { pageSize: 0 }, { pageSize: 101 }, { pageSize: 2.5 }]) {
    const changed = structuredClone(fixture); Object.assign(changed.tables[0], edit);
    assert.throws(() => parseSnapshot(changed), /INVALID/);
  }
});

test('model-selected titles units and page settings are not replaced by the client', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../../agent_service/analysis_extension/presentation-v1.example.json', import.meta.url), 'utf8'));
  fixture.tables[0].title = '模型选择的标题';
  fixture.tables[0].collapsed = true;
  fixture.tables[0].pageSize = 3;
  fixture.tables[0].columns.forEach(column => { column.unit = ''; });
  fixture.claims = ['**重点数值**，其余为业务解读。'];
  fixture.summaryClaimIndexes = [0];
  const parsed = parseSnapshot(fixture);
  assert.equal(parsed.tables[0].title, '模型选择的标题');
  assert.equal(parsed.tables[0].pageSize, 3);
  assert.equal(parsed.tables[0].collapsed, true);
  assert.ok(parsed.tables[0].columns.every(column => column.unit === ''));
  assert.deepEqual(parsed.claims, fixture.claims);
});
