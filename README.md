<p align="center">
  <img src="logo.png" alt="AstrBot Mailer" width="128" />
</p>

<h1 align="center">AstrBot Mailer Plugin</h1>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-0.1.1-blue" />
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green" />
  <img alt="Tests" src="https://img.shields.io/badge/tests-23%20passing-brightgreen" />
  <br />
  为 AstrBot 提供 SMTP 邮件发送能力 — 支持 LLM Agent 自动发信与管理命令
</p>

---

## ✨ 特性

| 特性 | 说明 |
|------|------|
| 🤖 **LLM 工具** | 注册 `send_email` FunctionTool，AI 可在对话中直接发送邮件 |
| 🎨 **HTML 邮件** | 富文本正文 + 自动纯文本 fallback |
| 🖼️ **内嵌图片** | HTML 中 `cid:` 引用，所见即所得 |
| 📎 **附件支持** | 路径沙箱隔离，拒绝越权访问 |
| 👥 **CC / BCC / Reply-To** | 完整的邮件头部支持 |
| 🏷️ **主题前缀** | 可预设全局前缀，如 `[AstrBot]` |
| 🛡️ **多层安全** | 发送者白名单 · 域名黑白名单 · 路径沙箱 · 大小限制 |
| ⚙️ **管理命令** | `/mail_test` 发测试邮件 · `/mail_config_check` 查配置 |

---

## 📦 安装

将插件目录放入 AstrBot 的 `data/plugins/` 下，重启 AstrBot 或在 WebUI 中启用即可。

```bash
pip install -r requirements.txt
```

---

## 🔧 配置

所有配置项可在 AstrBot WebUI 中修改。

### SMTP 服务器

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `smtp.host` | `string` | `""` | SMTP 地址（如 `smtp.qq.com`） |
| `smtp.port` | `int` | `465` | 端口（465 隐式 TLS / 587 STARTTLS） |
| `smtp.use_tls` | `bool` | `true` | 启用隐式 TLS |
| `smtp.use_starttls` | `bool` | `false` | 启用 STARTTLS（与 TLS 互斥） |
| `smtp.username` | `string` | `""` | 登录用户名 |
| `smtp.password` | `string` | `""` | 密码或授权码 |
| `smtp.from_name` | `string` | `"AstrBot Mailer"` | 发件人显示名 |
| `smtp.from_email` | `string` | `""` | 发件人地址 |
| `smtp.default_reply_to` | `string` | `""` | 默认回复地址 |
| `smtp.subject_prefix` | `string` | `""` | 主题前缀 |
| `smtp.timeout_seconds` | `float` | `30` | 超时秒数 |

### LLM 工具

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `tool.enable_llm_tool` | `bool` | `true` | 注册 LLM 工具 |
| `tool.require_tool_confirmation` | `bool` | `false` | 发信前要求人工确认（WIP） |

### 安全策略

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `security.allowed_sender_ids` | `string[]` | `[]` | 用户 ID 白名单（空=不限制） |
| `security.allowed_domains` | `string[]` | `[]` | 收件人域名白名单（空=不限） |
| `security.blocked_domains` | `string[]` | `[]` | 收件人域名黑名单（优先匹配） |
| `security.allowed_attachment_roots` | `string[]` | `[]` | 允许读取的根目录路径 |
| `security.allow_unsafe_all_attachment_paths` | `bool` | `false` | ⚠️ 允许任意路径附件（高风险） |
| `security.max_attachment_size_mb` | `int` | `10` | 单附件上限（MB） |
| `security.max_total_attachment_size_mb` | `int` | `25` | 附件总上限（MB） |
| `security.max_html_length` | `int` | `200000` | HTML 正文最大字符数 |

---

## 🚀 使用

### LLM 调用

启用 `tool.enable_llm_tool` 后，AI 可在对话中调用 `send_email` 工具。

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `to` | ✅ | 收件人地址数组 |
| `subject` | ✅ | 邮件主题 |
| `text_body` / `html_body` | ✅ 至少其一 | 正文（纯文本 / HTML） |
| `cc` | ❌ | 抄送地址数组 |
| `bcc` | ❌ | 密送地址数组 |
| `reply_to` | ❌ | 回复地址 |
| `attachments` | ❌ | 附件 `[{path, filename?}]` |
| `inline_images` | ❌ | 内嵌图片 `[{path, cid, filename?}]` |

### 管理员命令

| 命令 | 说明 |
|------|------|
| `/mail_test <recipient@example.com>` | 发送测试邮件验证 SMTP 配置 |
| `/mail_config_check` | 查看当前插件配置状态 |

---

## 🧪 开发

```bash
pip install -r requirements.txt
pytest tests/ -v
```

当前测试覆盖：**23 tests passing**

---

## 📄 许可

[MIT](LICENSE)
