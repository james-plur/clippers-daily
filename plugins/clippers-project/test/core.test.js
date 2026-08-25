const test = require('node:test');
const assert = require('node:assert/strict');
const {
  selectionTitle,
  safeFilename,
  taskNote,
  CALLOUT_TYPES,
  calloutTitle,
  wrapCallout,
  calloutPrompt,
  parseCalloutResponse,
} = require('../core');

test('selection title removes checkbox and Tasks metadata', () => {
  assert.equal(selectionTitle('- [ ] 设计网络模型 📅 2026-08-20'), '设计网络模型');
});

test('filename is safe for vault paths', () => {
  assert.equal(safeFilename('A/B: C?'), 'A-B- C-');
});

test('task note contains Obsidian properties and source link metadata', () => {
  const note = taskNote({ title: '任务 A', taskId: 'id-1', projectId: 'p-1', sourcePath: '项目/P.md', created: '2026-08-18 12:00' });
  assert.match(note, /type: task/);
  assert.match(note, /project_id: p-1/);
  assert.match(note, /source_path: 项目\/P.md/);
  assert.match(note, /# 任务 A/);
});

test('CALLOUT_TYPES includes common Obsidian callout types', () => {
  assert.ok(Array.isArray(CALLOUT_TYPES));
  assert.ok(CALLOUT_TYPES.includes('note'));
  assert.ok(CALLOUT_TYPES.includes('warning'));
  assert.ok(CALLOUT_TYPES.includes('tip'));
  assert.ok(CALLOUT_TYPES.includes('danger'));
  assert.ok(CALLOUT_TYPES.includes('quote'));
  assert.ok(CALLOUT_TYPES.includes('example'));
  assert.ok(CALLOUT_TYPES.includes('bug'));
});

test('calloutTitle uses first line of selection', () => {
  assert.equal(calloutTitle('性能优化建议\n具体方案如下'), '性能优化建议');
});

test('calloutTitle truncates long lines at 20 chars', () => {
  const input = '一二三四五六七八九十一二三四五六七八九十超';
  const result = calloutTitle(input);
  assert.equal(Array.from(result).length, 21); // 20 chars + ellipsis
});

test('calloutTitle falls back to 备注 for empty input', () => {
  assert.equal(calloutTitle(''), '备注');
  assert.equal(calloutTitle('   \n'), '备注');
});

test('calloutTitle strips markdown formatting', () => {
  assert.equal(calloutTitle('## 标题'), '标题');
  assert.equal(calloutTitle('**粗体**'), '粗体');
  assert.equal(calloutTitle('`代码`'), '代码');
  assert.equal(calloutTitle('> [!note] 旧标题'), '旧标题');
  assert.equal(calloutTitle('- 列表项'), '列表项');
});

test('wrapCallout produces valid Obsidian callout syntax', () => {
  const text = '第一行\n第二行\n第三行';
  const result = wrapCallout(text, 'note', '备注');
  assert.ok(result.startsWith('> [!note] 备注'));
  assert.match(result, /> 第一行/);
  assert.match(result, /> 第二行/);
  assert.match(result, /> 第三行/);
});

test('wrapCallout handles empty lines', () => {
  const text = '第一行\n\n第三行';
  const result = wrapCallout(text, 'note', '备注');
  const lines = result.split('\n');
  assert.equal(lines.length, 4);
  assert.equal(lines[2], '>');
});

test('wrapCallout unwraps existing callout markers', () => {
  const text = '> 已有的引用行';
  const result = wrapCallout(text, 'quote', '引用');
  assert.doesNotMatch(result, />>/);
  assert.match(result, /> \[!quote\] 引用/);
  assert.match(result, /> 已有的引用行/);
});

test('calloutPrompt includes selection text', () => {
  const prompt = calloutPrompt('这是一段测试文本');
  assert.match(prompt, /这是一段测试文本/);
  assert.match(prompt, /callout/);
});

test('calloutPrompt truncates long selections', () => {
  const long = 'A'.repeat(3000);
  const prompt = calloutPrompt(long);
  assert.ok(prompt.length < long.length + 200);
});

test('parseCalloutResponse parses valid JSON', () => {
  const result = parseCalloutResponse('{"type":"warning","title":"注意事项"}');
  assert.equal(result.type, 'warning');
  assert.equal(result.title, '注意事项');
});

test('parseCalloutResponse falls back for unknown type', () => {
  const result = parseCalloutResponse('{"type":"foobar","title":"测试"}');
  assert.equal(result.type, 'note');
  assert.equal(result.title, '测试');
});

test('parseCalloutResponse falls back for invalid JSON', () => {
  const result = parseCalloutResponse('not json at all');
  assert.equal(result.type, 'note');
  assert.equal(result.title, '备注');
});

test('parseCalloutResponse falls back for empty title', () => {
  const result = parseCalloutResponse('{"type":"tip","title":""}');
  assert.equal(result.type, 'tip');
  assert.equal(result.title, '备注');
});
