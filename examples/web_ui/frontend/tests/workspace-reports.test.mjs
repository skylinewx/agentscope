import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Streamdown, defaultRemarkPlugins } from 'streamdown';
import { workspaceFilePath, remarkWorkspaceReports, formatReportSize } from '../src/lib/workspace-reports.ts';

function render(markdown) {
  return renderToStaticMarkup(React.createElement(Streamdown, {
    mode: 'static',
    remarkPlugins: [...Object.values(defaultRemarkPlugins), remarkWorkspaceReports],
    allowedTags: { 'workspace-report': ['path'] },
    components: { 'workspace-report': ({ path, children }) => React.createElement('span', { 'data-report': workspaceFilePath(path) }, children) },
  }, markdown));
}

test('workspace-relative reports preserve Unicode, spaces, and encoded filename characters', () => {
  assert.equal(workspaceFilePath('./分析/report/预算.HTML'), './分析/report/预算.HTML');
  assert.equal(workspaceFilePath('./report/a%20b%23c%25.html'), './report/a b#c%.html');
  assert.equal(workspaceFilePath('./report/report.htm#summary'), './report/report.htm');
});

test('only the new report contract is recognized', () => {
  for (const path of ['file:///C:/report.html', 'C:/report.html', '/report.html', 'report/report.html', 'https://example.com/report.html', '//example.com/report.html', 'javascript:alert(1)', 'data:text/html,x', './report/readme.md', './report/chart.png']) {
    assert.equal(workspaceFilePath(path), null, path);
  }
});

test('report references reject traversal, network roots, controls and malformed escaping', () => {
  for (const path of ['./../secret.html', './%2e%2e/secret.html', './report/%2e%2e/secret.html', './%2fhost/report.html', './%5chost/report.html', './C:/report.html', './report/%00.html', './report/%ZZ.html']) {
    assert.equal(workspaceFilePath(path), null, path);
  }
});

test('the real Markdown pipeline produces a card marker without blocked or file navigation', () => {
  const result = render('[预算分析报告](./report/预算%20分析.html)');
  assert.match(result, /data-report="\.\/report\/预算 分析.html"/);
  assert.doesNotMatch(result, /\[blocked\]|href=/);
  assert.match(result, /预算分析报告/);
});

test('encoded punctuation is decoded once through the Markdown pipeline', () => {
  const result = render('[查看报告](./report/a%23b%25.html)');
  assert.match(result, /data-report="\.\/report\/a#b%.html"/);
});

test('code samples, images, and external links retain their original behavior', () => {
  assert.doesNotMatch(render('`[示例](./report/example.html)`'), /data-report=/);
  assert.doesNotMatch(render('![示例](./report/example.html)'), /data-report=/);
  assert.match(render('[官网](https://example.com)'), /data-streamdown="link"/);
  assert.doesNotMatch(render('[旧报告](file:///C:/report.html)'), /data-report=/);
});

test('ordinary tables still render with the report plugin enabled', () => {
  assert.match(render('| 指标 | 金额 |\n| --- | --- |\n| EVA | 100 |'), /<table/);
});

test('file sizes reflect only known metadata', () => {
  assert.equal(formatReportSize(0), '0 B');
  assert.equal(formatReportSize(13619), '13.3 KB');
  assert.equal(formatReportSize(null), null);
  assert.equal(formatReportSize(-1), null);
});
