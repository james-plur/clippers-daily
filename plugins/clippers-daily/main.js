const { Plugin, PluginSettingTab, Setting, Notice, TFile, normalizePath } = require('obsidian');
const { exec } = require('child_process');

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

function parseReadingList(clippersPath, envFile) {
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

const DEFAULTS = { journalDir: '日志', knowledgeDir: '知识库', clippersPath: '/Users/luchenda/tools/clippers', envFile: '/Users/luchenda/.config/clippers/knowledge.env' };

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
    this.addCommand({
      id: 'parse-reading-list',
      name: '解析 Safari 阅读列表',
      callback: () => void this.parseReadingListCommand(),
    });
    this.addSettingTab(new ClippersDailySettingTab(this.app, this));
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

  async parseReadingListCommand() {
    new Notice('Clippers Daily：正在解析阅读列表…', 0);
    try {
      const result = await parseReadingList(this.settings.clippersPath, this.settings.envFile);
      const count = (result.safari_outputs || []).length;
      new Notice(`Clippers Daily：阅读列表解析完成，已生成 ${count} 篇笔记`);
    } catch (err) {
      new Notice(`Clippers Daily：阅读列表解析失败：${String(err).slice(0, 150)}`);
    }
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

class ClippersDailySettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl('h2', { text: 'Clippers Daily 设置' });

    new Setting(containerEl)
      .setName('Clippers 项目路径')
      .setDesc('clippers CLI 所在的项目根目录（含 .venv/bin/clippers）')
      .addText(text => text
        .setPlaceholder('/Users/luchenda/tools/clippers')
        .setValue(this.plugin.settings.clippersPath)
        .onChange(async value => {
          this.plugin.settings.clippersPath = value.trim() || DEFAULTS.clippersPath;
          await this.plugin.saveData(this.plugin.settings);
        }));

    new Setting(containerEl)
      .setName('环境变量文件')
      .setDesc('包含 ZHIPU/DEEPSEEK API Key 的 env 文件路径')
      .addText(text => text
        .setPlaceholder('/Users/luchenda/.config/clippers/knowledge.env')
        .setValue(this.plugin.settings.envFile)
        .onChange(async value => {
          this.plugin.settings.envFile = value.trim() || DEFAULTS.envFile;
          await this.plugin.saveData(this.plugin.settings);
        }));
  }
}
