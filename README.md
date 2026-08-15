# Clippers Daily

Clippers Daily 是独立的 AI 基础设施日报服务。它负责采集、选稿、LLM 编辑、渲染、邮件投递、
历史反馈和管理控制台；Obsidian 插件、Safari、日历、项目日志和本地附件自动化不属于本仓库。

稳定边界是 SQLite/HTTP API 与 JSON/Markdown 日报产物，不向外部插件暴露 Python 内部模块。

## 关键规则

- 默认每天 08:00（Asia/Shanghai）生成 7 条日报。
- 合格 DeepSeek 新动态存在时至少选择 1 条；该硬约束高于模型评分。
- 合格中文媒体候选存在时至少选择 1 条，机器之心在中文媒体中为最高优先级。
- 未入选内容在 7 天窗口内继续参与选稿，历史投递按 ID、规范 URL 和标题去重。
- 同一日期默认禁止重复发送，只有 `--force-send` 或 API 的 `force=true` 能显式重发。
- 日报条目支持点赞；点赞会提升对应来源、类别、关键词和标签的推荐权重，取消点赞会撤销增益。权重限制在 `[-5,5]`，每日衰减 `0.995`。

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
发件人和收件人、主备模型、Git 状态、任务日志及 PaperLab 状态。完整 PaperLab 界面由认证网关
代理到 `/paperlab/`。

大模型统一由监听 `127.0.0.1:8767` 的 OpenAI 兼容网关提供。Clippers 和 PaperLab 均使用
`http://127.0.0.1:8767/v1` 与模型别名 `clippers-default`；网关默认路由到 SiliconFlow 的
`Pro/deepseek-ai/DeepSeek-V4`，上游 API Key 只需在 Clippers 控制台配置一次。

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

所有修改接口都要求登录会话和 CSRF token；正式发送和 PaperLab 同步还要求二次确认。

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

仓库不提交 `.env`、密钥、数据库、日报产物、服务器日志或 Obsidian 插件源码。
