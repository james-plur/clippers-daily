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

module.exports = { selectionTitle, safeFilename, taskNote };
