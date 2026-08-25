const INFORMATION_START = '<!-- clippers:information:start -->';
const INFORMATION_END = '<!-- clippers:information:end -->';
const WORK_START = '<!-- clippers:worklog:start -->';
const WORK_END = '<!-- clippers:worklog:end -->';

function localDate(value = new Date()) {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 10);
}

function informationBlock(knowledgeDir = '知识库') {
  return `${INFORMATION_START}\n## 当日消息\n\n![[${knowledgeDir}/知识库.base#当日消息]]\n${INFORMATION_END}`;
}

function newDailyNote(date, knowledgeDir = '知识库') {
  return `---\ntitle: "${date}"\ndate: ${date}\ntype: daily-note\ntags:\n  - 日志\n---\n\n# ${date}\n\n${informationBlock(knowledgeDir)}\n`;
}

function unwrapLegacyWorkBlock(text) {
  const pattern = new RegExp(`${escapeRegExp(WORK_START)}\\s*([\\s\\S]*?)\\s*${escapeRegExp(WORK_END)}`, 'g');
  return text.replace(pattern, (_match, body) => body.trim());
}

function ensureInformationBlock(text, knowledgeDir = '知识库') {
  const block = informationBlock(knowledgeDir);
  const pattern = new RegExp(`${escapeRegExp(INFORMATION_START)}[\\s\\S]*?${escapeRegExp(INFORMATION_END)}`);
  if (pattern.test(text)) return text.replace(pattern, block);
  return `${text.trimEnd()}\n\n${block}\n`;
}

function normalizeDailyNote(text, knowledgeDir = '知识库') {
  return ensureInformationBlock(unwrapLegacyWorkBlock(text), knowledgeDir);
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseReadingList(clippersPath, envFile) {
  const { exec } = require('child_process');
  return new Promise((resolve, reject) => {
    exec(
      `set -a; . ${envFile} 2>/dev/null; set +a; cd ${clippersPath} && ./.venv/bin/clippers knowledge sync --safari-only 2>/dev/null`,
      { timeout: 600000, maxBuffer: 10 * 1024 * 1024 },
      (error, stdout) => {
        if (error) {
          reject(new Error(String(error.message || error).split('\n')[0].slice(0, 200)));
          return;
        }
        const match = stdout.match(/\{[\s\S]*\}\s*$/);
        if (match) {
          try { resolve(JSON.parse(match[0])); return; } catch (_) {}
        }
        resolve({ safari_outputs: [], raw: stdout.slice(-500) });
      },
    );
  });
}

module.exports = {
  INFORMATION_START,
  INFORMATION_END,
  WORK_START,
  WORK_END,
  localDate,
  informationBlock,
  newDailyNote,
  unwrapLegacyWorkBlock,
  ensureInformationBlock,
  normalizeDailyNote,
  parseReadingList,
};
