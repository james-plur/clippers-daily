const { Plugin, PluginSettingTab, Setting, Notice, TFile, normalizePath } = require('obsidian');
const {
  selectionTitle,
  safeFilename,
  taskNote,
  calloutTitle,
  wrapCallout,
  generateCallout,
} = require('./core');

const DEFAULT_SETTINGS = {
  gatewayUrl: 'http://127.0.0.1:8767/v1/chat/completions',
};

module.exports = class ClippersProject extends Plugin {
  async onload() {
    await this.loadSettings();

    this.addCommand({
      id: 'create-task-note-from-selection',
      name: '从选定文字增加 task 笔记',
      editorCallback: (editor, view) => void this.createTaskNote(editor, view),
    });
    this.addCommand({
      id: 'create-callout-from-selection',
      name: '从选定文字生成 callout',
      editorCallback: (editor, view) => void this.createCallout(editor, view),
    });

    this.addSettingTab(new ClippersProjectSettingTab(this.app, this));
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  async createTaskNote(editor, view) {
    const source = view?.file;
    if (!(source instanceof TFile)) {
      new Notice('Clippers Project：请先打开项目文档');
      return;
    }
    const properties = this.app.metadataCache.getFileCache(source)?.frontmatter || {};
    if (!properties.project_id) {
      new Notice('Clippers Project：当前文档需要 project_id 属性');
      return;
    }
    const title = selectionTitle(editor.getSelection());
    if (!title) {
      new Notice('Clippers Project：请先选中要创建为 task 笔记的文字');
      return;
    }
    const folder = source.path.replace(/\.md$/, '');
    const desired = normalizePath(`${folder}/${safeFilename(title)}.md`);
    if (!this.app.vault.getAbstractFileByPath(folder)) await this.app.vault.createFolder(folder);
    let task = this.app.vault.getAbstractFileByPath(desired);
    if (!(task instanceof TFile)) {
      const created = new Date().toISOString().slice(0, 16).replace('T', ' ');
      task = await this.app.vault.create(desired, taskNote({
        title,
        taskId: crypto.randomUUID(),
        projectId: String(properties.project_id),
        sourcePath: source.path,
        created,
      }));
    }
    editor.replaceSelection(`[[${task.path.replace(/\.md$/, '')}|${title}]]`);
    new Notice(`Clippers Project：已创建 task 笔记 ${task.basename}`);
  }

  async createCallout(editor, view) {
    const selection = editor.getSelection();
    if (!selection.trim()) {
      new Notice('Clippers Project：请先选中要包装为 callout 的文字');
      return;
    }

    const from = editor.getCursor('from');
    const to = editor.getCursor('to');
    const startsAtLineBegin = from.ch === 0;
    const endsAtLineEnd = to.ch === editor.getLine(to.line).length;

    let type = 'note';
    let title = calloutTitle(selection);

    // LLM 提供智能类型和标题；不可用时回退到本地规则
    try {
      const result = await generateCallout(selection, this.settings.gatewayUrl);
      type = result.type;
      title = result.title;
    } catch (err) {
      console.warn('Clippers Project: LLM callout 生成失败，使用本地规则', err);
    }

    let callout = wrapCallout(selection, type, title);
    if (!startsAtLineBegin) callout = '\n' + callout;
    if (!endsAtLineEnd) callout = callout + '\n';
    editor.replaceSelection(callout);
    new Notice(`Clippers Project：已生成 ${type} callout「${title}」`);
  }
};

class ClippersProjectSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();

    containerEl.createEl('h2', { text: 'Clippers Project' });

    new Setting(containerEl)
      .setName('LLM Gateway URL')
      .setDesc('生成 callout 时调用的 LLM 网关地址（OpenAI 兼容接口）。不可用时自动回退到本地规则（note 类型）。')
      .addText(text => text
        .setPlaceholder('http://127.0.0.1:8767/v1/chat/completions')
        .setValue(this.plugin.settings.gatewayUrl)
        .onChange(async value => {
          this.plugin.settings.gatewayUrl = value.trim() || DEFAULT_SETTINGS.gatewayUrl;
          await this.plugin.saveSettings();
        }));
  }
}
