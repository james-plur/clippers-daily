const { Plugin, Notice, TFile, normalizePath } = require('obsidian');

const INFORMATION_START = '<!-- clippers:information:start -->';
const INFORMATION_END = '<!-- clippers:information:end -->';
const WORK_START = '<!-- clippers:worklog:start -->';
const WORK_END = '<!-- clippers:worklog:end -->';

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

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

function normalizeDailyNote(text, knowledgeDir = '知识库') {
  const unwrapped = unwrapLegacyWorkBlock(text);
  const block = informationBlock(knowledgeDir);
  const pattern = new RegExp(`${escapeRegExp(INFORMATION_START)}[\\s\\S]*?${escapeRegExp(INFORMATION_END)}`);
  if (pattern.test(unwrapped)) return unwrapped.replace(pattern, block);
  return `${unwrapped.trimEnd()}\n\n${block}\n`;
}

const DEFAULTS = { journalDir: '日志', knowledgeDir: '知识库' };

module.exports = class ClippersDaily extends Plugin {
  async onload() {
    this.settings = Object.assign({}, DEFAULTS, await this.loadData());
    this.addCommand({
      id: 'open-today',
      name: '打开/创建今日日记',
      callback: () => this.openDay(localDate()),
    });
    this.addCommand({
      id: 'repair-current-note',
      name: '修复当前日记结构',
      callback: () => this.repairCurrent(),
    });
    this.app.workspace.onLayoutReady(() => void this.migrateLegacyWorkBlocks());
  }

  async openDay(date) {
    const path = normalizePath(`${this.settings.journalDir}/${date}.md`);
    let file = this.app.vault.getAbstractFileByPath(path);
    if (!(file instanceof TFile)) {
      if (!this.app.vault.getAbstractFileByPath(this.settings.journalDir)) {
        await this.app.vault.createFolder(this.settings.journalDir);
      }
      file = await this.app.vault.create(path, newDailyNote(date, this.settings.knowledgeDir));
    } else {
      await this.normalizeFile(file);
    }
    await this.app.workspace.getLeaf(false).openFile(file);
  }

  async repairCurrent() {
    const file = this.app.workspace.getActiveFile();
    if (!(file instanceof TFile) || !file.path.startsWith(`${this.settings.journalDir}/`)) {
      new Notice('Clippers Daily：请先打开一篇日记');
      return;
    }
    await this.normalizeFile(file);
    new Notice('Clippers Daily：日记结构已修复');
  }

  async normalizeFile(file) {
    const before = await this.app.vault.read(file);
    const after = normalizeDailyNote(before, this.settings.knowledgeDir);
    if (after !== before) await this.app.vault.modify(file, after);
  }

  async migrateLegacyWorkBlocks() {
    for (const file of this.app.vault.getMarkdownFiles()) {
      if (!file.path.startsWith(`${this.settings.journalDir}/`)) continue;
      const before = await this.app.vault.read(file);
      const after = unwrapLegacyWorkBlock(before);
      if (after !== before) await this.app.vault.modify(file, after);
    }
  }
};
