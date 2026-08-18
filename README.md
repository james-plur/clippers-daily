# Clippers

Clippers 采用“服务器端 + 本地 Obsidian 插件”的边界：

```text
Clippers
├── 服务器端（本仓库根目录）
│   ├── 信息与代码动态采集、选稿、日报和邮件
│   ├── Web 管理控制台
│   └── PaperLab 云端采集与数据管理入口
└── plugins/
    ├── clippers-daily/    本地日记创建与“当日消息”视图
    └── clippers-project/  从选中文字创建 task 笔记
```

服务器端不管理本地工作日志和附件，也不提供 PaperLab 桌面 App。PaperLab 只保留部署在服务器上的
采集、数据管理和查询能力。本地能力由相互独立、最小权限的 Obsidian 插件提供。

稳定边界是 SQLite/HTTP API 与 JSON/Markdown 日报产物，不向外部插件暴露 Python 内部模块。

## 关键规则

- 默认每天 08:00（Asia/Shanghai）生成最多 10 条日报；重要代码更新可扩展到最多 15 条。
- 合格 DeepSeek 新动态存在时至少选择 1 条；该硬约束高于模型评分。
- 合格中文媒体候选存在时至少选择 1 条，机器之心在中文媒体中为最高优先级。
- 未入选内容在 7 天窗口内继续参与选稿，历史投递按 ID、规范 URL 和标题去重。
- 同一日期默认禁止重复发送，只有 `--force-send` 或 API 的 `force=true` 能显式重发。
- 日报条目支持点赞；点赞会提升对应来源、类别、关键词和标签的推荐权重，取消点赞会撤销增益。权重限制在 `[-5,5]`，每日衰减 `0.995`。
- 代码是与企业、媒体、论文并列的第四类来源。GitHub App 只读取个人账户 Star 的公开仓库和 Following
  中的组织；首次同步只建立 SHA 基线，后续逐仓库分析改动，组织更新合并为一个概览。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
set -a; source .env; set +a
.venv/bin/clippers doctor
.venv/bin/clippers source-test deepseek
.venv/bin/clippers source-test jiqizhixin
.venv/bin/clippers daily --no-send
.venv/bin/clippers-web
```

控制台默认监听 `http://127.0.0.1:8766`。日报输出位于
`data/reports/YYYY-MM-DD/daily.md` 与 `daily.html`，状态和历史保存在 `data/clippers.db`。

## 控制台

控制台提供历史日报与点赞反馈、来源启停和预检、结构化配置与高级 YAML、版本回滚、多个 SMTP
发件人和收件人、主备模型、Git 状态、任务日志及 PaperLab 状态。PaperLab 的云端采集与数据管理
界面由认证网关代理到 `/paperlab/`；不维护本地 App、阅读器或桌面打包产物。

大模型统一由监听 `127.0.0.1:8767` 的 OpenAI 兼容网关提供。Clippers 和 PaperLab 均使用
`http://127.0.0.1:8767/v1` 与模型别名 `clippers-default`；网关默认路由到 SiliconFlow 的
`deepseek-ai/DeepSeek-V4-Pro`，上游 API Key 只需在 Clippers 控制台配置一次。

认证复用 `PAPERLAB_ADMIN_USERNAME` 和 `PAPERLAB_ADMIN_PASSWORD_HASH`。密码哈希使用 Argon2；
模型和 SMTP 密钥只写入 `/srv/clippers/secrets/` 的 0600 文件，API 不回显密钥。

## 配置与 API

配置位于 `config/*.yaml`。常用接口：

- `GET /api/digests`、`GET /api/digests/{date}`
- `PUT /api/digests/{date}/items/{item_id}/like`
- `GET/PUT /api/config/{section}`、`POST /api/config/{section}/rollback`
- `GET/PUT /api/sources/{source_id}`、`POST /api/jobs/source-test`
- `POST /api/jobs/daily-preview`、`POST /api/jobs/daily-send`
- `GET /api/jobs`、`GET /api/logs`
- `GET /api/paperlab/status`、`POST /api/paperlab/sync`
- `GET /api/code/status`、`GET /api/code/repositories`
- `GET /api/code/github/connect`、`GET /api/code/github/callback`、`POST /api/code/github/disconnect`
- `POST /api/jobs/code-sync`、`POST /api/jobs/code-repository-test`

所有修改接口都要求登录会话和 CSRF token；正式发送和 PaperLab 同步还要求二次确认。

## Obsidian 插件

- `clippers-daily`：创建/打开当天日记，只维护“当日消息”区块；历史的“工作信息”保护标记会被
  解除，正文原样保留，之后完全由用户编辑。
- `clippers-project`：仅提供“从选定文字增加 task 笔记”。它不监听文档变化，不同步任务状态，
  不归档、移动或删除附件，也不生成工作日志。

插件目录中的 `main.js` 和 `manifest.json` 可以直接复制到 vault 的 `.obsidian/plugins/<id>/`。

## 生产部署

systemd、Nginx 和发布脚本位于 `deploy/`。生产目录采用：

```text
/srv/clippers/
  current -> releases/<timestamp>
  releases/
  shared/data/
  shared/config/
  secrets/
  .env
```

发布前必须备份 SQLite、配置、日报与 PaperLab 配置。数据库迁移只新增表和索引，不删除旧表；
旧 `records`、`deliveries`、`digest_items` 会保留。

## 开发验证

```bash
.venv/bin/python -m compileall -q src
.venv/bin/pytest -q
node --check src/clippers_daily/static/console.js
```

仓库不提交 `.env`、密钥、数据库、日报产物或服务器日志；两个 Obsidian 插件源码随仓库维护。
