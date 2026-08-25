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

const CALLOUT_TYPES = [
  'note', 'abstract', 'summary', 'tldr',
  'info', 'todo',
  'tip', 'hint', 'important',
  'success', 'check', 'done',
  'question', 'help', 'faq',
  'warning', 'caution', 'attention',
  'failure', 'fail', 'missing',
  'danger', 'error',
  'bug',
  'example',
  'quote', 'cite',
];

const CALLOUT_SYSTEM = `你是一个 Obsidian callout 分类助手。根据用户提供的文本内容，判断最适合的 callout 类型并生成简短标题。

可用的 callout 类型：
- note/abstract/summary/tldr: 一般注释、摘要、总结
- info/todo: 信息说明、待办
- tip/hint/important: 提示、技巧、重要说明
- success/check/done: 成功、完成、检查项
- question/help/faq: 问题、求助、常见问答
- warning/caution/attention: 警告、注意
- failure/fail/missing: 失败、缺失
- danger/error: 危险、错误
- bug: 缺陷
- example: 示例
- quote/cite: 引用

规则：
1. 标题不超过 20 个中文字符，简洁概括文本主题
2. 如果文本本身就是引用或转述，优先选择 quote
3. 如果文本包含警告、风险或注意事项，优先选 warning
4. 如果文本是纯粹的说明、描述或笔记，选 note
5. 标题不能和 callout 类型同名（比如类型选了 warning，标题不能是"警告"）

返回 JSON：{"type": "callout类型", "title": "中文标题"}`;

function calloutTitle(selection) {
  let firstLine = selection.trim().split('\n')[0].trim();
  firstLine = firstLine
    .replace(/^#{1,6}\s*/, '')
    .replace(/\*{1,3}(.+?)\*{1,3}/g, '$1')
    .replace(/_/g, '')
    .replace(/^\s*>\s*\[!.*?\]\s*/, '')
    .replace(/^\s*>\s*/, '')
    .replace(/^\s*[-*+]\s*/, '')
    .replace(/`(.+?)`/g, '$1')
    .trim();
  if (!firstLine) return '备注';
  const chars = Array.from(firstLine);
  return chars.length > 20 ? chars.slice(0, 20).join('') + '…' : firstLine;
}

function wrapCallout(text, type, title) {
  const header = `> [!${type}] ${title}`;
  const body = text
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map(line => line.replace(/^\s*> ?/, ''))
    .map(line => line ? `> ${line}` : '>')
    .join('\n');
  return `${header}\n${body}`;
}

function calloutPrompt(selection) {
  const snippet = selection.length > 2000
    ? selection.slice(0, 2000) + '\n...'
    : selection;
  return `分析以下文本，选择最合适的 Obsidian callout 类型和标题：\n\n${snippet}`;
}

function parseCalloutResponse(content) {
  try {
    const parsed = JSON.parse(content);
    const type = CALLOUT_TYPES.includes(parsed.type) ? parsed.type : 'note';
    const title = String(parsed.title || '').trim().slice(0, 20) || '备注';
    return { type, title };
  } catch {
    return { type: 'note', title: '备注' };
  }
}

async function generateCallout(selection, gatewayUrl) {
  const response = await fetch(gatewayUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: 'clippers-default',
      messages: [
        { role: 'system', content: CALLOUT_SYSTEM },
        { role: 'user', content: calloutPrompt(selection) },
      ],
      response_format: { type: 'json_object' },
      temperature: 0.2,
      max_tokens: 256,
    }),
  });
  if (!response.ok) {
    throw new Error(`LLM gateway returned ${response.status}`);
  }
  const data = await response.json();
  const content = data?.choices?.[0]?.message?.content;
  if (!content) {
    throw new Error('LLM 返回空内容');
  }
  return parseCalloutResponse(content);
}

module.exports = {
  selectionTitle,
  safeFilename,
  taskNote,
  CALLOUT_TYPES,
  calloutTitle,
  wrapCallout,
  calloutPrompt,
  parseCalloutResponse,
  generateCallout,
};
