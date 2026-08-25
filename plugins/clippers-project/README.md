# Clippers Project

提供两个命令：

- **从选定文字增加 task 笔记**：当前文档必须有 `project_id` property。插件会在与项目文档同名的子目录中创建 task 笔记，并把选中文字替换为 wikilink。
- **从选定文字生成 callout**：选中一段文字，插件调用 LLM 网关分析内容，自动选择最合适的 callout 类型（note / tip / warning / danger / quote 等）和中文标题，包装为 `> [!type] 标题` callout 块。LLM 网关不可用时回退到本地规则（note 类型，标题取首行）。

## 设置

插件设置页可配置 LLM Gateway URL，默认 `http://127.0.0.1:8767/v1/chat/completions`。网关需提供 OpenAI 兼容接口。未配置或不可用时自动回退到本地规则，不影响基本使用。

除此之外没有文件监听、状态回写、日志同步、附件归档或附件清理。
