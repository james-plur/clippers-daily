const { Plugin, Notice, TFile, normalizePath } = require('obsidian');

function selectionTitle(selection) {
  return selection
    .replace(/^\s*- \[[^\]]\]\s*/, '')
    .replace(/\s+(?:⏫|🔼|🔽|📅|⏳|🛆|➕|✅|❌|🔁|🆔|⛔).*$/, '')
    .trim();
}

function safeFilename(title) {
  return title.replace(/[\\/:*?"<>|#[\]]/g, '-').replace(/\s+/g, ' ').trim();
}

function taskNote({ title, taskId, projectId, sourcePath, created }) {
  return `---\ntitle: ${JSON.stringify(title)}\ntype: task\ntask_id: ${taskId}\nproject_id: ${projectId}\nsource_path: ${sourcePath}\nstatus: todo\ncreated: ${created}\ncompleted:\n---\n\n# ${title}\n`;
}

module.exports = class ClippersProject extends Plugin {
  onload() {
    this.addCommand({
      id: 'create-task-note-from-selection',
      name: '从选定文字增加 task 笔记',
      editorCallback: (editor, view) => void this.createTaskNote(editor, view),
    });
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
};
