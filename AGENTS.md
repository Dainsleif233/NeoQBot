# AGENTS.md — NeoQBot

面向 AI 助手与人类协作者的工作指引。改动代码、配置或文档前请先通读本文件，
尤其是「安全边界」「代码约定」和「改动指南」三节。

---

## 1. 项目概览

NeoQBot 是一个**自托管的、可审计的多平台机器人 / 群组 / 知识编排控制面**。

- 通过 **OneBot 11 / NapCat** 接入 QQ，通过 **飞书 CLI** 接入飞书，通过 **OpenAI-compatible LLM**
  做入群审核与群聊风控决策。
- 用一个可视化的**资源编排画布**连接多个 QQ Bot、飞书 Bot、QQ 群、飞书群与知识库，并表达
  「谁管理谁、数据流向哪里、某个 Bot 在某群承担哪些事务」。
- 核心设计目标是把易变的 IM/CLI 接口与稳定的治理流程分离：**所有决策、阈值、幂等、审计、重试
  都在核心服务层**，外部平台适配器只做协议转换。
- 默认 **fail-closed**：模型或外部接口异常时不自动批准、不自动处罚；首次联调默认 `dry_run: true`。

仓库名 / Python 包 / CLI / 服务名 / 环境变量前缀统一为 **NeoQBot**（变量前缀 `NEOQBOT_`）。
当前版本 `0.4.0`，许可证 **GPL-3.0-or-later**。

---

## 2. 技术栈与环境

- **语言**：Python 3.11+（Docker 镜像用 `python:3.12-slim`）。
- **包管理**：[uv](https://docs.astral.sh/uv/)（首选）。`pyproject.toml` 用 setuptools 构建后端，
  `src` 布局（`src/neoqbot`）。
- **核心依赖**：FastAPI、uvicorn[standard]、httpx、pydantic v2、pydantic-settings、PyYAML。
- **开发依赖**（`--extra dev`）：ruff（lint + format）。
- **前端**：`src/neoqbot/web/` 下的 `index.html` / `app.css` / `app.js` 是**原生 JS，无构建步骤**，
  直接由 FastAPI `StaticFiles` 提供。
- **数据库**：本地 SQLite（WAL 模式），含审计表。
- **文档语言**：主文档为中文，另含 `README.en.md` / `README.ja.md`。代码注释以中文为主。

---

## 3. 仓库结构

```text
src/neoqbot/
  __main__.py            `python -m neoqbot` 入口
  cli.py                 argparse 命令：serve / init-db / doctor / run-moderation / ...
  config.py              Pydantic 配置模型、校验、旧配置迁移、Settings 加载
  app.py                 FastAPI 应用、OneBot Webhook、admin API（/api/v1）、中间件
  gui.py                 管理后台路由（/api/gui、/gui、会话、CSRF、配置热加载）
  services.py            事务服务：入群、风控、公告、检索
  runtime.py             事件队列、定时循环（风控/公告）、生命周期
  events.py              事件标准化（申请/群消息/私聊）
  models.py              Pydantic 数据模型（JoinRequest / GroupMessage / Announcement ...）
  ports.py               适配器边界（Protocol）：DecisionEngine / QQGateway / FeishuGateway
  adapters/
    onebot.py            QQ / OneBot 11 客户端
    feishu_cli.py        飞书 CLI 网关（argv 透传，不经 shell）
    llm.py               OpenAI-compatible 与规则决策引擎
  database.py            SQLite 数据 + 审计层
  auth.py                GUI 会话与 PBKDF2 密码
  security.py            FailureLimiter、Host 校验、请求体大小限制
  recording.py           按日 JSONL 消息归档
  napcat.py              NapCat 初始化与诊断
  container.py           DI 容器：组装所有服务与客户端
  web/                   GUI 静态资源（index.html / app.css / app.js / img）
docs/                    ARCHITECTURE.md / ORCHESTRATION.md / FEISHU_CLI.md
integrations/astrbot_plugin_neoqbot_control/   AstrBot 控制插件
tests/                   unittest 测试套件
config.example.yaml      配置字段完整示例
Dockerfile / docker-compose.yml / .env(.example)
```

---

## 4. 常用命令

```bash
# 安装（含 dev 依赖：ruff）
uv sync --extra dev

# 本地初始化与运行（Windows 用 Copy-Item 替代 cp）
cp config.example.yaml config.yaml
uv run neoqbot init-secrets --secret-dir data/secrets
uv run neoqbot --config config.yaml init-db
uv run neoqbot --config config.yaml serve
# 打开 http://127.0.0.1:8080/dashboard

# 质量门禁（提交前务必全部通过）
uv run ruff check .
uv run ruff format --check src
uv run python -m unittest discover -s tests -v
node --check src/neoqbot/web/app.js

# 自动格式化
uv run ruff format src

# 只读/诊断命令
uv run neoqbot --config config.yaml doctor
uv run neoqbot --config config.yaml show-config   # 脱敏后的有效配置
uv run neoqbot --config config.yaml run-moderation
uv run neoqbot --config config.yaml sync-announcements
uv run neoqbot --config config.yaml prune
```

> 仓库**不内置 CI**；维护者在合并前于受控环境手动运行上述门禁。改动后请自行跑全套并检查。

---

## 5. 架构与关键模块

- **端口与适配器**：`ports.py` 仅定义 `Protocol` 接口。QQ/飞书/LLM 的具体实现都在 `adapters/`，
  通过 `container.py` 注入到服务层。**新增或替换平台能力时，优先扩展适配器，不要污染核心服务。**
- **服务层**（`services.py`）：`JoinApprovalService`、`ModerationService`、`AnnouncementService`、
  `SearchService`。每个 QQ Bot 各持有一份服务实例（dict by `bot_id`）。
- **运行时**（`runtime.py`）：单进程 `asyncio` 事件队列（上限 5000），多个事件 worker；按编排边
  分别启动风控 / 公告定时循环。单组异常不应阻断其他组。
- **事件处理**（`events.py`）：把 OneBot 事件标准化为申请 / 群消息 / 私聊，按 `bot_id` 分派。
- **Webhook**（`app.py`）：校验 HMAC（`X-Signature`，HMAC-SHA1，兼容标准 Bearer） → 按
  `bot_id + group_id` 查编排连接 → 未编排群直接丢弃（不写库、只 DEBUG）。队列满返回 503。
- **编排模型**（声明式）：`orchestration.resources`（qq_group / feishu_group / knowledge_base）
  与 `orchestration.edges`（manages / observes / archives_to / searches / syncs）。**每条 QQ Bot→群
  连接的 `tasks` 是「该 Bot 在该群的事务配置」的唯一权威来源**，节点坐标在 `layout` 中且不影响运行。
- **配置热加载**：`reload_runtime` 在 GUI 保存后重建容器；修改 host/port 等字段会返回「需重启」列表。
- **存储**：`database.py` 管理 SQLite，含 `join_requests` / `group_messages` / `moderation_runs` /
  `announcements` / `audit_log` / `admin_users` / `gui_sessions`。

---

## 6. 配置系统

- 主配置 `config.yaml`，完整字段见 `config.example.yaml`。**不要把真实 Token / Cookie / 二维码 /
  数据库 / `config.yaml` 提交进仓库**（`data/` 下均为运行时产物）。
- 环境变量用 `NEOQBOT_` 前缀，嵌套字段用双下划线：`NEOQBOT_APP__ADMIN_API_TOKEN`、
  `NEOQBOT_LLM__API_KEY`。仅 `NEOQBOT_` 前缀的应用配置会被读取。
- 敏感值优先走**只读 secret 文件**：配置里 `*_file` 字段 + `resolve_secret(value, file)` 解析，
  文件存在时优先于明文值（见 `config.py::resolve_secret`）。
- 旧版 `managed_group_ids + bot.tasks` 在 `Settings` 校验前自动迁移到 `orchestration`
  资源与边；保存后只输出新模型。
- `Settings` 提供大量查询方法（`effective_qq_bots`、`qq_group_assignment`、`qq_group_assignments`、
  `join_approval_for_group`、`bundled_qq_bot`、`deployment_security_errors` 等），服务层依赖它们
  而非直接读原始字段。

---

## 7. 安全边界（务必遵守）

1. **fail-closed 默认**：`app.dry_run` 默认 `true`；`auto_approve` / `auto_reject` 默认关闭；
   模型失败 → 转人工（`manual_review`）+ 审计；风控**只通知、不处罚**。
2. **凭据绝不出现在日志/审计明文**：诊断接口只返回布尔（如 `ok`、`signature_present`），
   不返回 token 或其指纹。`show-config` / `status` 都走 `redacted_dict()` 脱敏。
3. **Webhook 鉴权**：OneBot 支持 HMAC-SHA1 `X-Signature`（NapCat Token 即签名密钥）**或**标准
   Bearer；外部适配器还可用独立 `webhook_secret`。任何情况下都拒绝未认证请求。
4. **管理 API**：独立 Bearer token（`app.admin_api_token`，长度 ≥ 32）；未配置时返回 503。
   失败有 `FailureLimiter`（滑动窗口）限速。
5. **GUI**：HttpOnly/SameSite 会话 Cookie、CSRF token、登录限速、安全响应头（CSP / HSTS /
   X-Frame-Options 等）。首次登录强制改密。
6. **部署基线**：公网前必须防火墙来源白名单 + HTTPS；不需要公网时设
   `NEOQBOT_GUI_BIND_IP=127.0.0.1`。生产设 `app.require_https`、`gui.secure_cookie`，配置
   `app.allowed_hosts` / `forwarded_allow_ips`（**绝不要用 `*`**）/ `management_allowed_networks`。
   保持 `app.expose_api_docs=false`、`gui.allow_sensitive_settings_edits=false`。
7. **飞书 CLI**：参数以 argv 列表逐条传递，**绝不经过 shell**；群内容不能被拼接成第二条命令。
   只有显式 QQ 管理员可触发飞书搜索。
8. **容器**：默认非 root（uid 10001）、read-only 根、drop ALL capabilities、no-new-privileges。
9. **Bot 内部 ID 创建后不可改**：它绑定节点地址、Webhook、secret 文件与平台登录身份；只能改显示名。
   更换身份 = 新建 Bot + 迁移连接 + 确认新账号登录 + 删旧节点。
10. **配置并发**：GUI 读取时拿到配置 SHA-256 修订号，保存做乐观并发校验；其他会话已改则返回 409，
    绝不静默覆盖。`/api/gui/settings` 只合并平台域，`/api/gui/orchestration` 只合并 Bot/资源/连接域。

---

## 8. 代码约定

- **每个模块顶部都写 `from __future__ import annotations`**（项目一致要求，保持）。
- **Pydantic v2 模型**：用 `field_validator` / `model_validator` 做校验与旧字段迁移；
  迁移逻辑集中在 `config.py`，不要散落到服务层。
- **异步优先**：服务与适配器方法多为 `async def`，用 `asyncio` 队列/任务；不要阻塞事件循环。
- **类型注解**：开 `from __future__ import annotations` 后可用 `X | None` 联合写法；
  ruff 开启 `UP`/`B`/`ASYNC` 等规则，注意不要引入冗余断言、await 误用等问题。
- **日志**：`logger = logging.getLogger(__name__)`；敏感信息绝不进日志。
- **时间统一 UTC**：模型/服务用 `datetime.now(UTC)`，不要用本地时间；提供 `utc_now()` 辅助。
- **适配器边界用 `Protocol`**（`ports.py`）：核心服务只依赖接口，不直接 import 具体客户端。
- **Secret 解析统一走 `resolve_secret(value, file)`**，不要手写文件读取。
- **行宽 100，target py311**（见 `pyproject.toml [tool.ruff]`）。lint 选择 `E, F, I, UP, B, ASYNC`。
- **注释用中文**描述业务语义（与现有代码保持一致），标识符用英文。

---

## 9. 改动指南（常见任务）

- **新增一个 API 路由**：在 `app.py`（admin 域，`Depends(require_admin)` + `/api/v1/...`）或
  `gui.py`（会话域，`/api/gui/...`）里加。涉及写操作的要保持审计与权限校验。
- **新增一个平台适配器**：在 `adapters/` 实现 `ports.py` 中的某个 Protocol，并在 `container.py`
  `build_container` 中按配置选择注入。不要改动核心服务逻辑。
- **新增一个配置字段**：在 `config.py` 对应 Config 模型加字段 + 校验器；必要时更新
  `config.example.yaml`、`.env` 文档与 `redacted_dict()` 脱敏白名单。
- **新增一个编排事务（edge task）**：在 `config.py` 加 Task 配置模型，在 `services.py` 加对应服务，
  在 `runtime.py` 注册定时循环；记住「每条边的 `tasks` 是唯一权威」，不要再加 Bot 级镜像字段。
- **新增一个数据表 / 审计项**：在 `database.py` 加建表语句与读写；关键动作务必 `database.audit(...)`。
- **改前端**：编辑 `src/neoqbot/web/` 下的原生 HTML/CSS/JS；改完跑 `node --check src/neoqbot/web/app.js`。
  注意 CSP 只允许 `'self'`，不要引入外部脚本/CDN。
- **改文档**：涉及行为/配置/安全的变更要同步更新 `README.md`、`docs/*`、`SECURITY.md`（多语言 README
  视改动范围同步）。

---

## 10. 测试约定

- 框架为 **unittest**（`python -m unittest discover -s tests -v`），不是 pytest。
- 常用辅助：`fastapi.testclient.TestClient`、`unittest.mock.AsyncMock`、`patch`、`Settings.model_validate({...})`
  构造临时配置、`tempfile` 放 secret/DB。
- 测试覆盖配置校验/迁移、编排分配、Webhook 鉴权、GUI 合并与并发等。新增行为请补对应用例。
- 不要在测试里打印或留存真实凭据；临时 `data/` 产物应落在临时目录。

---

## 11. 部署

- **Docker**：`docker compose build && docker compose up -d`。`init-volumes` 服务用镜像内置
  `config.example.yaml` 创建持久化配置并初始化 NapCat secret；`neoqbot` 是主服务（read-only、
  非 root、drop caps）；`qq-bridge` 是可选 NapCat 侧车（仅 Compose 内网可达，不要映射到公网）。
- 升级后若 OneBot 上报 401/404：`docker compose up -d --build --force-recreate init-volumes neoqbot qq-bridge`，
  再跑 `neoqbot doctor` 校对齐配置。
- 健康检查：`/healthz`、`/readyz`（仅应暴露给 LB/监控网络）。
- 飞书 CLI 可选安装：`INSTALL_FEISHU_CLI=true`，`FEISHU_CLI_PACKAGE=<npm 包>`。

---

## 12. 不要做的事（易错点）

- 不要把模型结论直接当平台动作（审核/执行分离）；不要把 `dry_run` 默认关掉。
- 不要把凭据、IP、token 指纹写进日志、审计详情或诊断输出。
- 不要在 Webhook 入口为未编排群写入数据库；保持 `unmanaged_group` 只 DEBUG。
- 不要把 `app.forwarded_allow_ips` 设为 `*`；不要信任外部任意 `X-Forwarded-*` 头。
- 不要把飞书命令拼成 shell 字符串；不要暴露 NapCat WebUI `6099` / OneBot `3000` 到公网。
- 不要新增 Bot 级事务镜像字段绕过编排边；不要修改已有 Bot 内部 ID。
- 不要在提交里带 `data/`、`config.yaml`、二维码或 `.env` 中的真实密钥。
- 不要跳过质量门禁（ruff / unittest / node --check）就提交；仓库无 CI 兜底。

---

## 13. 文档与贡献

- 设计目标与状态机、公告归档、数据表、安全模型见 `docs/ARCHITECTURE.md`；
  编排节点类型、连接语义、旧配置迁移见 `docs/ORCHESTRATION.md`；飞书 CLI 见 `docs/FEISHU_CLI.md`；
  部署基线与泄露处置见 `SECURITY.md`。
- 漏洞请走 GitHub Private vulnerability reporting，**不要**在公开 Issue / 日志 / 截图泄露凭据或漏洞。
- 涉及平台写操作或安全边界的变更，在说明里包含**风险**与**验证方式**。
