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
assertIncludes('id="androidAnalysisBtn"', 'Android analysis button');
assertIncludes('id="androidAnalysisFileInput"', 'Android analysis upload input');
assertIncludes('上传日志包', 'Android analysis upload button');
assertIncludes('openAndroidAnalysisPanel', 'Android analysis panel');
assertIncludes('handleAndroidAnalysisDrop', 'Android analysis drag-drop handler');
assertIncludes('startAndroidAnalysis', 'Android analysis start action');
assertIncludes("sendBtn.textContent = '分析中...'", 'Android analysis busy button state');
assertIncludes('runAndroidDeepAnalysis', 'Android analysis Deep action');
assertIncludes('generateAndroidCaseDraft', 'Android analysis case draft action');
assertIncludes('confirmAndroidCaseDraft', 'Android analysis case confirm action');
assertIncludes('/case-draft/confirm', 'Android analysis case confirm endpoint');
assertIncludes('waitAndroidAnalysisJob', 'Android analysis async job wait helper');
assertIncludes('streamAndroidAnalysisEvents', 'Android analysis SSE stream helper');
assertIncludes('android-analysis-process', 'Android analysis process panel');
assertIncludes('analysis_metrics_recorded', 'Android analysis metrics event');
assertIncludes('androidAnalysisAutoDeepThreshold', 'Android analysis confidence threshold state');
assertIncludes('first_pass_confidence', 'Android analysis first-pass confidence event');
assertIncludes('auto_deep_triggered', 'Android analysis automatic Deep event');
assertIncludes('/api/android-analysis/jobs/latest', 'Android analysis latest job endpoint');
assertIncludes('isAndroidDeepRequest', 'Android analysis chat-box Deep trigger');
assertIncludes('hydrateAndroidAnalysisMessage', 'Android analysis persisted button hydration');
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
assertIncludes('/static/assets/mermaid.min.js', 'local Mermaid renderer');
assertIncludes('function preRenderMermaidForExport', 'Mermaid export pre-render');
assertIncludes('function getMermaidSvgBaseWidth', 'vector-safe Mermaid zoom sizing');
assertIncludes('svg.style.width = `${nextWidth}px`', 'Mermaid zoom updates SVG width');
assertIncludes('viewport.scrollLeft = drag.left', 'Mermaid drag pans viewport');
assertIncludes("viewport.addEventListener('dragstart'", 'Mermaid drag blocks file upload overlay');
assertIncludes('function buildExportHtml', 'HTML export builder');
assertIncludes('session-menu', 'session action menu');
assertIncludes('safe-area-inset-top', 'mobile safe-area top support');
assertIncludes('safe-area-inset-bottom', 'mobile safe-area bottom support');
assertIncludes('100dvh', 'mobile dynamic viewport support');

const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
assert.ok(scriptMatch, 'Expected inline application script');
const script = scriptMatch[1];

const androidStart = script.slice(
  script.indexOf('async function startAndroidAnalysis'),
  script.indexOf('function renderAndroidAnalysisResult'),
);
assert.match(androidStart, /isStreaming = true;/, 'Android analysis should enter streaming lock state');
assert.match(androidStart, /setStreamingLocked\(true\);/, 'Android analysis should lock page interactions');
assert.match(androidStart, /setStreamingLocked\(false\);/, 'Android analysis should unlock page interactions');

const start = Math.min(
  script.indexOf('function cleanThinkingForDisplay'),
  script.indexOf('function escapeHtml'),
);
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

const mermaidRendered = context.renderMarkdown([
  '```mermaid',
  'graph TD',
  '  A --> B',
  '```',
].join('\n'));
assert.match(mermaidRendered, /class="mermaid-diagram"/);
assert.match(mermaidRendered, /data-mermaid-key="mmd-/);
assert.match(mermaidRendered, /<pre data-lang="mermaid" class="mermaid-source">/);
assert.match(mermaidRendered, /A --&gt; B/);

const mermaidSource = 'graph TD\n  A --> B';
context.getMermaidSvgCache().set(context.mermaidSourceKey(mermaidSource), '<svg><g></g></svg>');
const cachedMermaidRendered = context.renderMarkdown(`\`\`\`mermaid\n${mermaidSource}\n\`\`\``);
assert.match(cachedMermaidRendered, /mermaid-toolbar/);
assert.match(cachedMermaidRendered, /mermaid-viewport/);
assert.match(cachedMermaidRendered, /mermaid-canvas/);

const repairedState = context.repairMermaidSource([
  'stateDiagram-v2',
  '    state LOCK {',
  '        [*] --> Locked',
  '        note right of LOCK : Lock Task mode active',
  '    }',
  '    LOCK --> LOCK: EVENT_LOCK',
].join('\n'));
assert.match(repairedState, /state LOCK \{\n        \[\*\] --> Locked\n    \}\n        note right of LOCK : Lock Task mode active/);

const exported = context.buildExportHtml(
  { title: 'Smoke Export' },
  [
    { role: 'user', content: '| A | B |\n|---|---|\n| 1 | 2 |' },
    {
      role: 'assistant',
      content: 'Done\n\n```mermaid\ngraph TD\n  A --> B\n```',
      thinking: [
        'Android 分析过程',
        '- 阶段：planning',
        '- ai_thinking_delta',
        '- ai_thinking_delta',
        '- ai_text_delta',
        '- verifier_completed',
      ].join('\n'),
      metadata: { android_analysis_job_id: 'job-1' },
      android_process_details: {
        process_overview_title: 'Android RealtimeDeviceManager问题分析过程概览',
        process_detail_title: 'Android RealtimeDeviceManager问题分析过程详情',
        stages: [
          {
            id: 'planning',
            title: 'Planner 路由',
            item_count: 1,
            duration_seconds: 1.2,
            items: [
              {
                event: 'ai_text_stream',
                title: 'AI 输出流',
                data: { content: '| A | B |\n|---|---|\n| 1 | 2 |' },
              },
            ],
          },
        ],
      },
    },
  ],
);
assert.match(exported, /<!DOCTYPE html>/);
assert.match(exported, /Smoke Export - Claude Chat Export/);
assert.match(exported, /--bg-primary: #faf9f5/);
assert.match(exported, /markdown-table-wrap/);
assert.match(exported, /mermaid-diagram/);
assert.match(exported, /mermaid-viewport/);
assert.match(exported, /thinking-block/);
assert.match(exported, /Android RealtimeDeviceManager问题分析过程概览/);
assert.match(exported, /Android RealtimeDeviceManager问题分析过程详情/);
assert.match(exported, /AI 可见思考流：2 个片段/);
assert.match(exported, /AI 输出流：1 个片段/);
assert.match(exported, /<details class="thinking-block">/);
assert.doesNotMatch(exported, /ai_thinking_delta/);
assert.doesNotMatch(exported, /ai_text_delta/);
assert.doesNotMatch(exported, /<script>/i, 'Offline export should not depend on app scripts');

console.log('Frontend smoke checks passed');
