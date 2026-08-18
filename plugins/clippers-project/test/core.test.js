const test = require('node:test');
const assert = require('node:assert/strict');
const { selectionTitle, safeFilename, taskNote } = require('../core');

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
