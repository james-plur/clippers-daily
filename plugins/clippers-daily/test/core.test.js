const test = require('node:test');
const assert = require('node:assert/strict');
const { newDailyNote, normalizeDailyNote, unwrapLegacyWorkBlock } = require('../core');

test('new daily note contains only the information managed block', () => {
  const note = newDailyNote('2026-08-18');
  assert.match(note, /clippers:information:start/);
  assert.doesNotMatch(note, /clippers:worklog:start/);
  assert.doesNotMatch(note, /工作信息/);
});

test('legacy work block is unwrapped without changing its content', () => {
  const note = '# day\n\n<!-- clippers:worklog:start -->\n## 工作信息\n\n手写内容\n<!-- clippers:worklog:end -->\n';
  const migrated = unwrapLegacyWorkBlock(note);
  assert.doesNotMatch(migrated, /clippers:worklog/);
  assert.match(migrated, /## 工作信息\n\n手写内容/);
});

test('normalization is idempotent', () => {
  const once = normalizeDailyNote('# day\n');
  assert.equal(normalizeDailyNote(once), once);
});
