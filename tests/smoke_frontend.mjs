import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '..');
const htmlPath = path.join(root, 'static', 'index.html');
const html = fs.readFileSync(htmlPath, 'utf8');

function assertIncludes(needle, label = needle) {
  assert.ok(html.includes(needle), `Expected static/index.html to contain ${label}`);
}

assertIncludes('id="messageInput"', 'message input');
assertIncludes('id="markdownToggle"', 'Markdown toggle');
assertIncludes('id="webSearchToggle"', 'web search toggle');
assertIncludes('id="devProjectBtn"', 'development project button');
assertIncludes('id="devProjectBar"', 'development project status bar');
assertIncludes('id="providerPicker"', 'provider picker');
assertIncludes('data-provider="gemini"', 'Gemini provider option');
assertIncludes('session-provider-badge', 'session provider badge');
assertIncludes('currentSessionProvider', 'current session provider helper');
assertIncludes('activeInfoEl', 'single active streaming info status');
assertIncludes('处理完成', 'completed streaming info status');
assertIncludes('openDevProjectPicker', 'development project picker');
assertIncludes('showDevDiff', 'development diff action');
assertIncludes('runDevPresetTest', 'development test action');
assertIncludes('function renderMarkdown', 'Markdown renderer');
assertIncludes('function buildExportHtml', 'HTML export builder');
assertIncludes('session-menu', 'session action menu');
assertIncludes('safe-area-inset-top', 'mobile safe-area top support');
assertIncludes('safe-area-inset-bottom', 'mobile safe-area bottom support');
assertIncludes('100dvh', 'mobile dynamic viewport support');

const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
assert.ok(scriptMatch, 'Expected inline application script');
const script = scriptMatch[1];

const start = script.indexOf('function escapeHtml');
const end = script.indexOf('async function saveHtmlFile');
assert.ok(start >= 0 && end > start, 'Expected exportable Markdown and HTML functions');

const functionBlock = script.slice(start, end);
const context = {
  document: {
    createElement() {
      return {
        _text: '',
        set textContent(value) {
          this._text = String(value ?? '');
        },
        get innerHTML() {
          return this._text
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;');
        },
      };
    },
  },
  Date,
};
vm.createContext(context);
vm.runInContext(functionBlock, context, { filename: 'static/index.html#markdown' });

const markdown = [
  '## Title',
  '',
  '| Name | Score | Note |',
  '|:-----|------:|:----:|',
  '| **Alice** | 10 | `ok` |',
  '',
  '- item',
  '',
  '```js',
  'console.log("<safe>");',
  '```',
].join('\n');

const rendered = context.renderMarkdown(markdown);
assert.match(rendered, /<h2>Title<\/h2>/);
assert.match(rendered, /<div class="markdown-table-wrap"><table>/);
assert.match(rendered, /<th style="text-align:left">Name<\/th>/);
assert.match(rendered, /<td style="text-align:right">10<\/td>/);
assert.match(rendered, /<strong>Alice<\/strong>/);
assert.match(rendered, /<code>ok<\/code>/);
assert.match(rendered, /<ul><li>item<\/li><\/ul>/);
assert.match(rendered, /&lt;safe&gt;/);

const exported = context.buildExportHtml(
  { title: 'Smoke Export' },
  [
    { role: 'user', content: '| A | B |\n|---|---|\n| 1 | 2 |' },
    { role: 'assistant', content: 'Done', thinking: 'checked files' },
  ],
);
assert.match(exported, /<!DOCTYPE html>/);
assert.match(exported, /Smoke Export - Claude Chat Export/);
assert.match(exported, /markdown-table-wrap/);
assert.match(exported, /thinking-block/);
assert.match(exported, /checked files/);
assert.doesNotMatch(exported, /<script>/i, 'Offline export should not depend on app scripts');

console.log('Frontend smoke checks passed');
