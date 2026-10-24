# astrbot_plugin_mailer

`astrbot_plugin_mailer` 是一个为 AstrBot 提供邮件收发能力的插件，用来给 LLM 暴露受控的邮件工具。

它支持：

- 同时挂载多个邮箱账号，每个账号独立配置 SMTP 与 IMAP
- 每个账号可选收信模式：IDLE 推送即时处理，或被提醒后才去查看
- 纯文本邮件
- HTML 富文本邮件
- 通过 `cid:` 引用的文中内嵌图片
- 本地文件附件
- 收件人域名白名单/黑名单限制
- 调用者 ID 白名单限制
- 附件目录沙箱限制
- 可选的任意附件路径放行开关（高风险）

## 功能概览

- 向 AstrBot 注册三个 LLM 工具：`send_email`、`list_emails`、`read_email`
- 通过 SMTP 发送邮件，支持 TLS 和 STARTTLS
- 通过 IMAP 读取收件箱（纯标准库实现，不引入额外依赖）
- `check_mode=idle` 的账号常驻 IDLE，服务端推送到就处理（可选自动回信 + 推送通知）
- `check_mode=manual` 的账号只在被提醒时才查信，适合不想让 bot 自作主张的场景
- 自动构造适合文本、HTML、内嵌图片、附件的 MIME 邮件结构
- 优先引导 LLM 使用 `html_body` 生成富文本邮件
- 通过 `allowed_sender_ids` 限制哪些用户可触发邮件功能
- 通过 `allowed_domains` / `blocked_domains` 限制收件人域名
- 通过 `allowed_attachment_roots` 限制附件来源目录
- 提供仅管理员可用的 `/mail_test`、`/mail_config_check`、`/mail_accounts`、`/mail_check`、`/mail_read`

## 目录结构

```text
astrbot_plugin_mailer/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  README.md
  mailer/
    accounts.py
    imap_client.py
    message_builder.py
    models.py
    smtp_client.py
    state.py
  tests/
```

## 安装方式

将插件放到 AstrBot 插件目录：

```text
data/plugins/astrbot_plugin_mailer/
```

AstrBot 会根据 `requirements.txt` 安装运行时依赖：

- `aiosmtplib`

IMAP 收信使用标准库 `imaplib` / `email`，不需要额外依赖。

## 配置说明

插件配置通过 `_conf_schema.json` 暴露给 AstrBot WebUI，并已按分组组织，便于在界面中分类查看。

### 1. `accounts` 邮箱账号列表

一个可增删的账号列表，每个账号就几个字段：

- `label`：账号标识，命令和工具里用它指定账号，留空则用邮箱地址
- `email`：邮箱地址，同时作为 SMTP / IMAP 用户名，并写入 `From` 头
- `password`：密码或授权码，SMTP 与 IMAP 共用
- `is_default`：不指定账号时用哪个发信
- `enabled`：是否启用
- `check_mode`：
  - `manual`：只有被提醒时（LLM 调 `list_emails` / `read_email`，或 `/mail_check`）才去查
  - `idle`：常驻一条 IMAP 连接挂 IDLE（RFC 2177），服务端一有新邮件就推 `EXISTS`，
    收到推送立刻去取。29 分钟保活重进一次，断线指数退避重连
- `auto_reply_enabled`：是否自动回信。关掉就是"只提醒、不回"
- `auto_reply_whitelist`：留空表示回复所有人；填了则只回复列表里的地址或域名
- `notify_enabled`：新邮件通知开关，关掉后不推送
- `notify_session`：通知推送到哪个会话，格式 `平台:消息类型:会话ID`
- `from_name`：发件人显示名
- `smtp_host` / `smtp_port`：SMTP 服务器，端口 465 自动隐式 TLS，其它端口自动 STARTTLS
- `imap_host` / `imap_port`：IMAP 服务器，留空表示该账号只能发信；993 自动 SSL

### 2. `receive` 收信行为

- `max_body_chars`：单封邮件正文读取上限，超出截断
- `ignore_sender_patterns`：发件人地址命中的关键字，命中后不自动回复（仍会通知）

### 3. `tool` 工具控制

- `enable_llm_tool`：是否向 LLM 暴露邮件相关工具
- `require_tool_confirmation`：【开发中】开启后，LLM 直接调用工具会被拒绝，待后续开发接入人工审核或确认流程。

### 4. `security` 安全控制

- `allowed_sender_ids`：允许调用邮件功能的 AstrBot 用户 ID 列表，空表示不限制
- `allowed_domains`：允许发送的收件人域名白名单，空表示不限制
- `blocked_domains`：禁止发送的收件人域名黑名单
- `allowed_attachment_roots`：允许作为附件和内嵌图片来源的绝对目录列表
- `allow_unsafe_all_attachment_paths`：是否允许任意本地附件路径，默认关闭
- `max_attachment_size_mb`：单个文件大小上限，单位 MB
- `max_total_attachment_size_mb`：所有附件与内嵌图片总大小上限，单位 MB
- `max_html_length`：HTML 正文最大长度

## 附件路径规则

插件默认始终允许以下目录中的文件作为附件或内嵌图片：

```text
data/plugin_data/astrbot_plugin_mailer/attachments/
```

如果传入的是相对路径，会自动按该目录解析。

例如：

```json
{"path": "report.pdf"}
```

会被解析为：

```text
data/plugin_data/astrbot_plugin_mailer/attachments/report.pdf
```

如果传入绝对路径，则必须位于 `allowed_attachment_roots` 配置的目录之一中，否则会被拒绝。

如果你开启 `security.allow_unsafe_all_attachment_paths=true`，则会跳过目录沙箱，允许读取任意本地路径。

这是一个高风险选项，意味着 LLM 可能读取并外发服务器上的任意可访问文件。除非运行环境完全可信且你明确知道后果，否则不要开启。

## LLM 工具说明

插件注册三个工具：

### `send_email`

工具设计上更鼓励生成富文本邮件：

- 如果用户没有明确要求纯文本，优先填写 `html_body`
- 如果邮件里需要展示图片，优先使用 `html_body + inline_images`
- `text_body` 更适合做纯文本兜底，或用于用户明确要求 plain text 的场景

建议的调用参数如下：

```json
{
  "to": ["alice@example.com"],
  "cc": ["bob@example.com"],
  "bcc": ["carol@example.com"],
  "subject": "周报",
  "text_body": "这是纯文本正文",
  "html_body": "<p>你好，见下图：<img src=\"cid:chart-1\"></p>",
  "reply_to": "reply@example.com",
  "attachments": [
    {
      "path": "report.pdf",
      "filename": "Q1-report.pdf"
    }
  ],
  "inline_images": [
    {
      "path": "chart.png",
      "cid": "chart-1"
    }
  ]
}
```

参数约束：

- `to` 必填
- `subject` 必填
- `text_body` 和 `html_body` 至少提供一个
- 使用 `inline_images` 时必须提供 `html_body`
- 默认情况下，所有附件和内嵌图片路径都必须通过目录沙箱检查
- 只有在显式开启 `allow_unsafe_all_attachment_paths` 时，才允许读取任意本地路径

## 手动命令

### `/mail_accounts`

列出所有已配置的邮箱账号、收信模式和收发能力，方便确认哪个账号是默认账号、哪个开了自动回复。

默认仅管理员可用。

### `/mail_check [账号标识] [数量]`

查看指定账号（默认账号可省略）收件箱里最近的邮件摘要，默认 10 封，最多 50 封。

默认仅管理员可用。

### `/mail_read <uid> [账号标识]`

读取某一封邮件的正文与元信息，`uid` 来自 `/mail_check` 的输出。

默认仅管理员可用。

### `/mail_config_check`

检查当前插件账号配置是否完整，并输出各账号的 SMTP / IMAP 与收信模式。

默认仅管理员可用。

### `/mail_plans`

列出当前待审批的邮件操作计划（编号、发件人、主题、剩余时间）。

### `/批准 计划编号`（别名 `/mail_approve`）

校验通过后执行该计划，并回信同步结果。

### `/驳回 计划编号`（别名 `/mail_reject`）

作废该计划，不执行任何操作。

### `/mail_test recipient@example.com [账号标识]`

发送一封简单测试邮件，用于验证 SMTP 配置和发信链路。

默认仅管理员可用。

## 收信模式说明

每个账号用 `check_mode` 控制收信行为，只有两种：

- `manual`（被提醒才查）
  bot 不会主动读信。只有你在聊天里让它去看邮箱（它会调用 `list_emails` / `read_email`），
  或者你自己敲 `/mail_check`、`/mail_read` 时才会去连 IMAP。

- `idle`（服务端推送）
  维持一条常驻 IMAP 连接并挂上 IDLE，服务端一旦有新邮件就推 `EXISTS`，立刻去取：
  1. 读取正文；
  2. 如果 `auto_reply_enabled=true`，用 LLM 生成回信并通过该账号的 SMTP 发出
     （带 `In-Reply-To` / `References`，能正常串成线程）；
  3. 标记为已读；
  4. 如果 `notify_enabled=true` 且配了 `notify_session`，把原文摘要和回信内容推送给你。
  连接每 29 分钟主动重进一次 IDLE 保活，任何异常都会按 5 秒起翻倍的退避重连。

想要"新信立刻知道、但先别自作主张回"，就设 `check_mode=idle` + `auto_reply_enabled=false`。

自动回复会主动跳过这些情况，避免回环和噪音：

- 发件人就是这个账号自己
- 发件人地址命中 `receive.ignore_sender_patterns`（noreply、DMARC 报告等）
- 对方邮件带 `Auto-Submitted: auto-replied / auto-generated`
- 这封邮件的 `Message-ID` 已经回复过（状态存在 `plugin_data/astrbot_plugin_mailer/mail_state.json`）
- `auto_reply_whitelist` 非空，且发件人不在列表里（此时只通知、不回复）

## 邮件指令的审批流程

`check_mode=idle` 的账号收到**白名单发件人**（`auto_reply_whitelist`，为空则不启用）的来信时，
不会直接动手，而是走一条"先出方案、再等批准"的链路：

1. 插件用 LLM 生成一份操作方案（只输出方案，明确禁止声称已执行）
2. 把方案推到 `notify_session` 指定的聊天会话里，附带编号 `#xxxxxx`
3. 你在聊天里回复 `批准 xxxxxx` 才会真正执行；回复 `驳回 xxxxxx` 则作废

收信这一侧不会回信打扰对方：审批请求只走聊天，执行完成后才回信同步结果。

批准时会逐条校验，任何一条不满足都拒绝执行：

- 计划状态必须是 pending，且未过期（默认 30 分钟）
- 审批会话必须就是收到通知的那个会话（`notify_session`）
- 方案的 SHA-256 摘要必须与提交审批时一致，防止批准后内容被替换
- 如果配了 `security.allowed_sender_ids`，审批人必须在名单里
- 同一时间只保留一条待审批计划，新计划会把旧的置为过期，不会堆积

执行阶段用 agent 循环跑全量工具，提示词里明确"严格按方案执行、异常就停下来说明、不要自己扩大范围"，
执行完成后把总结发回聊天，并回信告知结果。

非白名单来信只做通知，不生成方案、也不执行任何动作；邮件正文里的任何指令都只当作"需求描述"，
不会作为命令被直接执行。

## 安全建议

这是一个具备实际外发能力的插件，建议上线前至少配置以下项：

- 设置 `security.allowed_sender_ids`
- 设置 `security.allowed_domains`
- 将 `security.allowed_attachment_roots` 控制在尽可能小的范围内
- 保持 `security.allow_unsafe_all_attachment_paths=false`
- 使用邮箱授权码，不要直接使用主账号密码
- 如果不希望模型自主发信，可将 `tool.require_tool_confirmation` 设为 `true`

当前版本尚未支持：

- 远程 URL 附件下载
- 远程 URL 图片下载
- 交互式人工确认发送
- 持久化发信审计日志

## 本地测试

插件已经包含针对解析、MIME 构造和安全检查的单元测试。

### 使用 uv 创建测试环境

```powershell
uv venv .venv --python 3.12
uv pip install pytest pytest-asyncio aiosmtplib "pydantic>=2.12.5" "jsonschema>=4.25.1" "mcp>=1.8.0" "deprecated>=1.2.18"
```

### 执行测试

```powershell
.venv\Scripts\python -m pytest tests -q
```

当前通过结果：

```text
17 passed
```

## 开发说明

- `mailer/models.py`：请求数据解析与校验
- `mailer/message_builder.py`：MIME 邮件构造
- `mailer/smtp_client.py`：SMTP 异步发送封装
- `main.py`：AstrBot 插件入口、策略检查、工具注册与命令处理

## 后续建议

接下来比较值得继续补的能力：

1. 增加“发送前确认”工作流
2. 在 `data/plugin_data` 下记录发信审计日志
3. 提供更友好的 HTML 模板辅助能力
4. 增加针对真实 AstrBot 运行环境的联调验证
