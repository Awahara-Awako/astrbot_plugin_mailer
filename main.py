from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .mailer.accounts import AccountConfig, parse_accounts, pick_account
from .mailer.imap_client import (
    MailDetail,
    MailSummary,
    list_messages,
    mark_seen,
    read_message,
    search_since,
)
from .mailer.imap_idle import IdleWatcher
from .mailer.plans import (
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingPlan,
    PlanStore,
    plan_digest,
)
from .mailer.message_builder import build_message
from .mailer.models import MailRequest
from .mailer.smtp_client import send_message
from .mailer.state import MailStateStore

MAX_REPLY_CHARS = 800

PLAN_SYSTEM_PROMPT = (
    "你是一个运维助理。你只能输出操作方案，绝对不能声称已经执行、已经完成或已经重启任何东西。"
    "方案要写成可以给人审核的形式：先写目标，再写要动哪些服务与文件，然后是分步命令或步骤，"
    "接着写影响面（会不会断服务、要不要重启），最后写回滚方式。"
    "未知的信息要明确标成待确认，不要编造路径、端口或版本号。"
    "用中文，不要使用 Markdown 语法，纯文本分条列点即可。"
)

EXECUTE_SYSTEM_PROMPT = (
    "你正在执行一份已经由用户在聊天中批准的操作方案。"
    "严格按方案执行，不要擅自扩大范围；遇到方案里没写、或者执行中出现的异常，停下来说明情况，不要自己改方案。"
    "每完成一步用一句话汇报，最后给一段简短的中文总结，说明做了什么、当前状态、以及是否还有待办。"
)

DEFAULT_AUTO_REPLY_PROMPT = (
    "你正在以邮箱主人的身份回复一封收到的邮件。"
    "回复要礼貌、简洁、具体，直接回答对方的问题；不确定的事情就说会稍后确认，不要编造。"
    "只输出邮件正文本身，不要带主题行、不要写“以下是回复”这类说明，也不要使用 Markdown 语法。"
    "正文控制在 300 字以内，用中文回复，除非对方使用其他语言。"
)


class MailerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_mailer")
        self.attachments_dir = self.data_dir / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.state = MailStateStore(self.data_dir / "mail_state.json")
        self.plans = PlanStore(self.data_dir / "mail_plans.json")
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._idle_watchers: dict[str, IdleWatcher] = {}

        if self._get_bool("tool", "enable_llm_tool", True):
            self.context.add_llm_tools(
                self._build_send_email_tool(),
                self._build_list_emails_tool(),
                self._build_read_email_tool(),
            )

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        idle_accounts = [acc for acc in self._accounts() if acc.idle]
        if not idle_accounts:
            logger.info("mailer: 没有开启 IDLE 推送的账号，跳过收信任务")
            return
        for acc in idle_accounts:
            self._start_idle_account(acc)

    def _start_idle_account(self, account: AccountConfig) -> None:
        existing = self._idle_tasks.get(account.key)
        if existing and not existing.done():
            return
        if account.imap is None:
            logger.warning("mailer: 账号 %s 是 idle 模式但没配 IMAP", account.key)
            return
        self._idle_tasks[account.key] = asyncio.create_task(
            self._idle_account_loop(account),
            name=f"astrbot_plugin_mailer_idle_{account.key}",
        )
        logger.info("mailer: 账号 %s 已启动 IMAP IDLE 推送监听", account.key)

    async def terminate(self) -> None:
        tasks: list[asyncio.Task] = list(self._idle_tasks.values())
        self._idle_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for watcher in self._idle_watchers.values():
            watcher.stop()
        self._idle_watchers.clear()

    # ------------------------------------------------------------------ 配置读取

    def _group(self, name: str) -> dict[str, Any]:
        value = self.config.get(name, {})
        return value if isinstance(value, dict) else {}

    def _get_bool(self, group_name: str, key: str, default: bool) -> bool:
        group = self._group(group_name)
        value = group.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        raise ValueError(f"{group_name}.{key} 必须是布尔值。")

    def _get_int(self, group_name: str, key: str, default: int) -> int:
        value = self._group(group_name).get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_list_of_strings(
        self, group_name: str, key: str, default: list[str] | None = None
    ) -> list[str]:
        group = self._group(group_name)
        value = group.get(key, default or [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{group_name}.{key} 必须是字符串列表。")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{group_name}.{key} 只能包含字符串。")
            text = item.strip()
            if text:
                normalized.append(text)
        return normalized

    def _get_sender_id_allowlist(self) -> list[str]:
        security_cfg = self._security_config()
        value = security_cfg.get("allowed_sender_ids", [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("security.allowed_sender_ids 必须是列表。")

        normalized: list[str] = []
        for item in value:
            if isinstance(item, (list, dict, set, tuple)):
                raise ValueError(
                    "security.allowed_sender_ids 只能包含可转换为字符串的标量值。"
                )
            text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized

    def _tool_config(self) -> dict[str, Any]:
        return self._group("tool")

    def _security_config(self) -> dict[str, Any]:
        return self._group("security")

    def _receive_config(self) -> dict[str, Any]:
        return self._group("receive")

    def _accounts(self) -> list[AccountConfig]:
        return [acc for acc in parse_accounts(dict(self.config)) if acc.enabled]

    def _account(self, name: str | None = None) -> AccountConfig:
        return pick_account(self._accounts(), name)

    def _max_body_chars(self) -> int:
        return max(200, self._get_int("receive", "max_body_chars", 4000))

    def _ignore_patterns(self) -> list[str]:
        return [
            item.lower()
            for item in self._get_list_of_strings(
                "receive",
                "ignore_sender_patterns",
                ["no-reply", "noreply", "mailer-daemon", "postmaster", "dmarcreport"],
            )
        ]

    def _notify_prefix(self) -> str:
        group = self._receive_config()
        value = group.get("notify_prefix", "")
        return str(value).strip()

    # ------------------------------------------------------------------ LLM 工具

    def _build_send_email_tool(self) -> FunctionTool:
        return FunctionTool(
            name="send_email",
            description=(
                "在用户明确要求发送邮件时，通过受控 SMTP 通道发送邮件。"
                "优先使用 html_body 生成结构化富文本邮件；仅在用户明确要求纯文本或没有富文本内容时使用 text_body。"
                "如果包含图片，优先使用 html_body 配合 inline_images。必须具备清晰的收件人、主题和正文。"
                "存在多个邮箱账号时，用 account 指定发信账号（账号标识或邮箱地址），不填则用默认账号。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "可选。发信账号标识或邮箱地址，不填则使用默认账号。",
                    },
                    "to": {
                        "type": "array",
                        "description": "主收件人邮箱地址列表。",
                        "items": {"type": "string"},
                    },
                    "cc": {
                        "type": "array",
                        "description": "可选的抄送邮箱地址列表。",
                        "items": {"type": "string"},
                    },
                    "bcc": {
                        "type": "array",
                        "description": "可选的密送邮箱地址列表。",
                        "items": {"type": "string"},
                    },
                    "subject": {
                        "type": "string",
                        "description": "邮件主题。",
                    },
                    "text_body": {
                        "type": "string",
                        "description": "纯文本正文。text_body 和 html_body 至少提供一个；如果未明确要求纯文本，优先使用 html_body。",
                    },
                    "html_body": {
                        "type": "string",
                        "description": "可选的 HTML 正文。text_body 和 html_body 至少提供一个；需要富文本排版或内嵌图片时优先使用 html_body。",
                    },
                    "reply_to": {
                        "type": "string",
                        "description": "可选的回复地址。",
                    },
                    "attachments": {
                        "type": "array",
                        "description": "可选的本地附件列表。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "绝对路径，或相对于插件附件目录的路径。",
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "可选的附件文件名覆盖值。",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "inline_images": {
                        "type": "array",
                        "description": "可选的内嵌图片列表，通过 html_body 中的 cid: 引用。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "绝对路径，或相对于插件附件目录的路径。",
                                },
                                "cid": {
                                    "type": "string",
                                    "description": "在 html_body 中以 cid:xxx 形式引用的内容 id。",
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "可选的内嵌图片文件名覆盖值。",
                                },
                            },
                            "required": ["path", "cid"],
                        },
                    },
                },
                "required": ["to", "subject"],
            },
            handler=MailerPlugin._send_email_tool_handler,
        )

    def _build_list_emails_tool(self) -> FunctionTool:
        return FunctionTool(
            name="list_emails",
            description=(
                "列出指定邮箱账号最近的收件箱邮件摘要（发件人、主题、日期、uid）。"
                "当用户让你去看邮箱、有没有新邮件时才调用；不要主动读邮件。"
                "拿到 uid 之后可以用 read_email 读取具体某一封。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "可选。账号标识或邮箱地址，不填则使用默认账号。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几封，默认 10，最大 50。",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "是否只看未读邮件，默认 false。",
                    },
                },
                "required": [],
            },
            handler=MailerPlugin._list_emails_tool_handler,
        )

    def _build_read_email_tool(self) -> FunctionTool:
        return FunctionTool(
            name="read_email",
            description=(
                "读取某一封邮件的正文与元信息。uid 来自 list_emails 的返回结果。"
                "仅在用户要求查看具体邮件内容时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "邮件 uid，来自 list_emails。"},
                    "account": {
                        "type": "string",
                        "description": "可选。账号标识或邮箱地址，不填则使用默认账号。",
                    },
                },
                "required": ["uid"],
            },
            handler=MailerPlugin._read_email_tool_handler,
        )

    async def _send_email_tool_handler(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any] | None = None,
        **payload_kwargs: Any,
    ) -> str:
        merged_payload: dict[str, Any] = {}
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("工具参数必须是对象。")
            merged_payload.update(payload)
        merged_payload.update(payload_kwargs)
        return await self.send_email_tool(event, **merged_payload)

    async def _list_emails_tool_handler(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any] | None = None,
        **payload_kwargs: Any,
    ) -> str:
        merged = self._merge_payload(payload, payload_kwargs)
        self._check_sender_allowed(event)
        account = self._account(merged.get("account"))
        limit = min(50, max(1, int(merged.get("limit") or 10)))
        unread_only = bool(merged.get("unread_only") or False)
        return await self.list_mail(account, limit=limit, unread_only=unread_only)

    async def _read_email_tool_handler(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any] | None = None,
        **payload_kwargs: Any,
    ) -> str:
        merged = self._merge_payload(payload, payload_kwargs)
        self._check_sender_allowed(event)
        uid = str(merged.get("uid") or "").strip()
        if not uid:
            raise ValueError("uid 不能为空。")
        account = self._account(merged.get("account"))
        return await self.read_mail(account, uid)

    @staticmethod
    def _merge_payload(
        payload: dict[str, Any] | None, payload_kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("工具参数必须是对象。")
            merged.update(payload)
        merged.update(payload_kwargs)
        return merged

    # ------------------------------------------------------------------ 发信

    def _require_smtp(self, account: AccountConfig):
        if account.smtp is None:
            raise ValueError(f"账号 {account.key} 未配置 SMTP，无法发信。")
        return account.smtp

    def _smtp_settings(self, account: str | None = None):
        """返回指定账号（默认账号）的 SMTP 设置。"""
        return self._require_smtp(self._account(account))

    def _require_imap(self, account: AccountConfig):
        if account.imap is None:
            raise ValueError(f"账号 {account.key} 未配置 IMAP，无法收信。")
        return account.imap

    def _check_sender_allowed(self, event: AstrMessageEvent) -> None:
        allowed_sender_ids = self._get_sender_id_allowlist()
        if allowed_sender_ids and str(event.get_sender_id()) not in allowed_sender_ids:
            raise PermissionError("当前发送者没有权限使用邮件功能。")

    def _recipient_domain(self, address: str) -> str:
        return address.rsplit("@", 1)[-1].lower()

    def _check_recipient_policy(self, request: MailRequest) -> None:
        allowed_domains = {
            item.lower()
            for item in self._get_list_of_strings("security", "allowed_domains", [])
        }
        blocked_domains = {
            item.lower()
            for item in self._get_list_of_strings("security", "blocked_domains", [])
        }

        for recipient in request.all_recipients:
            domain = self._recipient_domain(recipient)
            if domain in blocked_domains:
                raise ValueError(f"收件人域名在黑名单中: {domain}")
            if allowed_domains and domain not in allowed_domains:
                raise ValueError(f"收件人域名不在白名单中: {domain}")

    def _allowed_roots(self) -> list[Path]:
        roots = [self.attachments_dir.resolve()]
        for entry in self._get_list_of_strings(
            "security", "allowed_attachment_roots", []
        ):
            roots.append(Path(entry).expanduser().resolve())
        deduped: list[Path] = []
        for root in roots:
            if root not in deduped:
                deduped.append(root)
        return deduped

    def _resolve_safe_path(self, raw_path: Path) -> Path:
        path = raw_path
        if not path.is_absolute():
            path = self.attachments_dir / path
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"文件不存在: {resolved}")

        if self._get_bool("security", "allow_unsafe_all_attachment_paths", False):
            return resolved

        for root in self._allowed_roots():
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise ValueError(f"文件不在允许的附件目录内: {resolved}")

    def _check_file_limits(self, request: MailRequest) -> None:
        security_cfg = self._security_config()
        max_attachment_size = (
            int(security_cfg.get("max_attachment_size_mb", 10)) * 1024 * 1024
        )
        max_total_size = (
            int(security_cfg.get("max_total_attachment_size_mb", 25)) * 1024 * 1024
        )
        if max_attachment_size <= 0:
            raise ValueError("security.max_attachment_size_mb 必须大于 0。")
        if max_total_size <= 0:
            raise ValueError("security.max_total_attachment_size_mb 必须大于 0。")
        if max_total_size < max_attachment_size:
            raise ValueError(
                "security.max_total_attachment_size_mb 不能小于 max_attachment_size_mb。"
            )

        total_size = 0
        for file_ref in [*request.attachments, *request.inline_images]:
            safe_path = self._resolve_safe_path(file_ref.path)
            file_ref.path = safe_path
            file_size = safe_path.stat().st_size
            if file_size > max_attachment_size:
                raise ValueError(
                    f"文件超过单文件大小限制: {safe_path.name} ({file_size} bytes)"
                )
            total_size += file_size

        if total_size > max_total_size:
            raise ValueError(f"附件和内嵌图片总大小超过限制 ({total_size} bytes)")

    def _check_content_limits(self, request: MailRequest) -> None:
        security_cfg = self._security_config()
        if len(request.html_body) > int(security_cfg.get("max_html_length", 200000)):
            raise ValueError("html_body 超过最大长度限制。")
        if request.inline_images and not request.html_body:
            raise ValueError("使用 inline_images 时必须提供 html_body。")

    async def _send_mail(
        self,
        request: MailRequest,
        account: AccountConfig | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        acc = account or self._account()
        settings = self._require_smtp(acc)
        from_email = acc.email
        from_name = acc.from_name or "AstrBot Mailer"

        message = build_message(
            request=request,
            from_email=from_email,
            from_name=from_name,
            extra_headers=extra_headers,
        )

        status_code, status_message = await send_message(
            settings=settings,
            message=message,
            sender=from_email,
            recipients=request.all_recipients,
        )

        status_segment = f" SMTP 状态: {status_code}"
        if status_message:
            status_segment += f" {status_message}"

        return (
            "邮件发送成功。"
            f"{status_segment}。"
            f" 账号: {acc.key} <{acc.email}>。"
            f" 收件人: {', '.join(request.to)}。"
            f" 主题: {message['Subject']}"
        )

    async def send_email_tool(self, event: AstrMessageEvent, **payload: Any) -> str:
        logger.info("mailer tool invoked by %s", event.get_sender_id())
        self._check_sender_allowed(event)
        if self._get_bool("tool", "require_tool_confirmation", False):
            raise PermissionError(
                "当前已开启工具确认，不能直接发送邮件，请改为人工确认后再发送。"
            )

        account = self._account(payload.pop("account", None))
        request = MailRequest.from_payload(payload)
        self._check_recipient_policy(request)
        self._check_content_limits(request)
        self._check_file_limits(request)
        return await self._send_mail(request, account)

    # ------------------------------------------------------------------ 收信

    def _format_summary(self, account: AccountConfig, item: MailSummary) -> str:
        sender = item.from_name or item.from_addr
        if item.from_name and item.from_addr and item.from_addr not in item.from_name:
            sender = f"{item.from_name} <{item.from_addr}>"
        return (
            f"[{item.uid}] {item.date or '无日期'} | {sender} | {item.subject}"
            f"（账号 {account.key}）"
        )

    async def list_mail(
        self, account: AccountConfig, limit: int = 10, unread_only: bool = False
    ) -> str:
        settings = self._require_imap(account)
        items = await list_messages(settings, limit=limit, unread_only=unread_only)
        if not items:
            return f"账号 {account.key} <{account.email}> 的收件箱里没有符合条件的邮件。"
        lines = [self._format_summary(account, item) for item in items]
        head = (
            f"账号 {account.key} <{account.email}> 最近 {len(items)} 封"
            f"{'未读' if unread_only else ''}邮件："
        )
        return head + "\n" + "\n".join(lines)

    async def read_mail(self, account: AccountConfig, uid: str) -> str:
        settings = self._require_imap(account)
        detail: MailDetail = await read_message(
            settings, uid, max_chars=self._max_body_chars()
        )
        lines = [
            f"账号: {account.key} <{account.email}>",
            f"UID: {detail.uid}",
            f"发件人: {detail.from_name} <{detail.from_addr}>",
            f"收件人: {', '.join(detail.to) or '-'}",
        ]
        if detail.cc:
            lines.append(f"抄送: {', '.join(detail.cc)}")
        lines.extend(
            [
                f"日期: {detail.date}",
                f"主题: {detail.subject}",
            ]
        )
        if detail.attachments:
            lines.append(f"附件: {', '.join(detail.attachments)}")
        lines.append("")
        lines.append(detail.text_body or "(正文为空)")
        if detail.truncated:
            lines.append("")
            lines.append(f"（正文已截断到 {self._max_body_chars()} 字）")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 自动收信

    def _should_auto_reply(
        self, account: AccountConfig, detail: MailDetail
    ) -> tuple[bool, str]:
        from_addr = (detail.from_addr or "").lower()
        if not from_addr:
            return False, "没有发件人地址"
        if from_addr == account.email.lower():
            return False, "发件人是自己，跳过以免形成回环"
        for pattern in self._ignore_patterns():
            if pattern and pattern in from_addr:
                return False, f"发件人命中忽略规则 {pattern}"
        auto_submitted = (detail.auto_submitted or "").lower()
        if "auto-replied" in auto_submitted or "auto-generated" in auto_submitted:
            return False, "对方是自动回复，跳过"
        if self.state.has_replied(account.key, detail.message_id):
            return False, "这封邮件已经回复过"
        allowed = set(account.auto_reply_whitelist)
        if allowed:
            domain = from_addr.rsplit("@", 1)[-1]
            if from_addr not in allowed and domain not in allowed:
                return False, "发件人不在自动回复白名单里"
        return True, ""

    async def _generate_reply(
        self, account: AccountConfig, detail: MailDetail, session: str | None
    ) -> str:
        provider = self.context.get_using_provider(session or None)
        if provider is None:
            raise ValueError("没有可用的 LLM 提供方，无法生成自动回复。")
        system_prompt = DEFAULT_AUTO_REPLY_PROMPT
        prompt = (
            f"账号: {account.email}\n"
            f"发件人: {detail.from_name} <{detail.from_addr}>\n"
            f"主题: {detail.subject}\n"
            f"日期: {detail.date}\n"
            f"正文:\n{detail.text_body or '(正文为空)'}\n\n"
            "请写一封回信正文。"
        )
        response = await provider.text_chat(
            prompt=prompt, system_prompt=system_prompt
        )
        text = (getattr(response, "completion_text", "") or "").strip()
        limit = MAX_REPLY_CHARS
        if len(text) > limit:
            text = text[:limit].rstrip() + "……"
        return text

    async def _send_reply(
        self, account: AccountConfig, detail: MailDetail, reply_text: str
    ) -> None:
        recipient = detail.reply_to or detail.from_addr
        if not recipient:
            raise ValueError("无法确定回复地址")
        subject = detail.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        request = MailRequest.from_payload(
            {
                "to": [recipient],
                "subject": subject,
                "text_body": reply_text,
            }
        )
        headers: dict[str, str] = {}
        if detail.message_id:
            headers["In-Reply-To"] = detail.message_id
            references = (detail.references + " " + detail.message_id).strip()
            headers["References"] = references
        headers["Auto-Submitted"] = "auto-replied"
        await self._send_mail(request, account, extra_headers=headers)

    async def _notify(
        self, account: AccountConfig, detail: MailDetail, reply_text: str | None
    ) -> None:
        if not account.notify_enabled:
            return
        session = account.notify_session.strip()
        if not session:
            return
        lines = [
            f"{self._notify_prefix()}[{account.key}] 收到新邮件",
            f"发件人: {detail.from_name} <{detail.from_addr}>",
            f"主题: {detail.subject}",
            f"日期: {detail.date}",
        ]
        if detail.attachments:
            lines.append(f"附件: {', '.join(detail.attachments)}")
        if reply_text:
            lines.append("")
            lines.append("已自动回复：")
            lines.append(reply_text)
        else:
            lines.append("")
            lines.append("（未自动回复，等你确认后再看）")
        try:
            await self.context.send_message(
                session, MessageChain([Plain("\n".join(lines))])
            )
        except Exception:  # noqa: BLE001 - 通知失败不应打断收信流程
            logger.exception("mailer: 发送新邮件通知失败")

    async def _auto_check_account(self, account: AccountConfig) -> None:
        settings = self._require_imap(account)
        state = self.state.get(account.key)
        uids = await search_since(settings, state.last_uid)
        for uid in uids:
            detail = await read_message(
                settings, uid, max_chars=self._max_body_chars()
            )
            state.last_uid = max(state.last_uid, int(uid))

            # 白名单来信视为"操作请求"：只生成方案并请求聊天审批，绝不直接执行
            if self._is_instruction_mail(account, detail):
                await self._handle_instruction_mail(account, detail)
                with contextlib.suppress(Exception):
                    await mark_seen(settings, uid)
                continue

            reply_text: str | None = None
            should_reply = False
            reason = "该账号已关闭自动回复"
            if account.auto_reply_enabled:
                should_reply, reason = self._should_auto_reply(account, detail)
            if should_reply:
                try:
                    reply_text = await self._generate_reply(
                        account, detail, account.notify_session or None
                    )
                    if reply_text:
                        await self._send_reply(account, detail, reply_text)
                        self.state.mark_replied(account.key, detail.message_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "mailer: 账号 %s 自动回复 uid=%s 失败", account.key, uid
                    )
                    reply_text = None
            else:
                logger.info(
                    "mailer: 账号 %s uid=%s 不自动回复：%s", account.key, uid, reason
                )
            with contextlib.suppress(Exception):
                await mark_seen(settings, uid)
            await self._notify(account, detail, reply_text)
        self.state.save()

    async def _idle_account_loop(self, account: AccountConfig) -> None:
        """一个账号一条常驻 IDLE 连接；服务端推 EXISTS 时立刻去取信。"""
        settings = self._require_imap(account)
        queue: asyncio.Queue[bool] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_new_mail() -> None:
            loop.call_soon_threadsafe(queue.put_nowait, True)

        watcher = IdleWatcher(
            settings=settings,
            on_new_mail=_on_new_mail,
            logger=logger,
        )
        self._idle_watchers[account.key] = watcher
        watcher.start()
        try:
            # 启动时先补一次，避免离线期间漏掉的邮件
            with contextlib.suppress(Exception):
                await self._auto_check_account(account)
            while True:
                await queue.get()
                # 服务端推 EXISTS 时信可能还在投递中，稍微等一下再取
                await asyncio.sleep(1)
                try:
                    await self._auto_check_account(account)
                except Exception:  # noqa: BLE001
                    logger.exception("mailer: 账号 %s 收到推送后取信失败", account.key)
                if not watcher.alive:
                    logger.warning(
                        "mailer: 账号 %s 的 IDLE 监听已退出，5 秒后重启", account.key
                    )
                    await asyncio.sleep(5)
                    watcher.start()
        except asyncio.CancelledError:
            raise
        finally:
            watcher.stop()
            self._idle_watchers.pop(account.key, None)

    # ------------------------------------------------------------------ 指令邮件的审批

    def _instruction_whitelist(self, account: AccountConfig) -> set[str]:
        return set(account.auto_reply_whitelist)

    def _is_instruction_mail(self, account: AccountConfig, detail: MailDetail) -> bool:
        """只有白名单来信才当操作请求，其它来信一律不触发任何动作。"""
        allowed = self._instruction_whitelist(account)
        if not allowed:
            return False
        if not account.notify_session:
            return False
        from_addr = (detail.from_addr or "").lower()
        if not from_addr or from_addr == account.email.lower():
            return False
        for pattern in self._ignore_patterns():
            if pattern and pattern in from_addr:
                return False
        domain = from_addr.rsplit("@", 1)[-1]
        return from_addr in allowed or domain in allowed

    async def _generate_plan(
        self, account: AccountConfig, detail: MailDetail
    ) -> str:
        provider = self.context.get_using_provider(account.notify_session or None)
        if provider is None:
            raise ValueError("没有可用的 LLM 提供方，无法生成方案。")
        prompt = (
            f"这封邮件来自 {detail.from_name} <{detail.from_addr}>，"
            f"主题是《{detail.subject}》，收件账号是 {account.email}。\n"
            f"邮件正文如下：\n{detail.text_body or '(正文为空)'}\n\n"
            "请针对邮件里的要求输出一份操作方案。如果邮件里没有需要动服务器或电脑的操作，"
            "就明确写“该邮件不涉及本机操作”。"
        )
        response = await provider.text_chat(prompt=prompt, system_prompt=PLAN_SYSTEM_PROMPT)
        text = (getattr(response, "completion_text", "") or "").strip()
        if len(text) > 4000:
            text = text[:4000].rstrip() + "……"
        return text

    async def _push_plan(
        self, plan: PendingPlan, account: AccountConfig, detail: MailDetail
    ) -> None:
        minutes = max(1, plan.ttl // 60)
        lines = [
            f"【待审批 #{plan.id}】账号 {account.key} 收到一封操作请求邮件",
            f"发件人: {detail.from_name} <{detail.from_addr}>",
            f"主题: {detail.subject}",
            "",
            "方案：",
            plan.plan_text,
            "",
            f"确认无误请回复：批准 {plan.id}（{minutes} 分钟内有效，只能用一次）",
            f"不想执行请回复：驳回 {plan.id}",
        ]
        await self.context.send_message(
            plan.session, MessageChain([Plain("\n".join(lines))])
        )

    async def _handle_instruction_mail(
        self, account: AccountConfig, detail: MailDetail
    ) -> None:
        try:
            plan_text = await self._generate_plan(account, detail)
        except Exception:  # noqa: BLE001
            logger.exception("mailer: 账号 %s 生成方案失败", account.key)
            return
        if not plan_text:
            return
        plan = self.plans.create(
            account=account.key,
            mail_uid=detail.uid,
            sender=detail.from_addr,
            subject=detail.subject,
            plan_text=plan_text,
            session=account.notify_session,
        )
        await self._push_plan(plan, account, detail)

    def _check_plan_usable(self, event: AstrMessageEvent, plan: PendingPlan) -> None:
        if plan.status != STATUS_PENDING:
            raise PermissionError(f"计划 {plan.id} 当前状态是 {plan.status}，不能执行。")
        if plan.is_expired():
            self.plans.mark(plan, STATUS_EXPIRED)
            raise PermissionError(f"计划 {plan.id} 已过期，请让它重新生成方案。")
        if plan.session and event.unified_msg_origin != plan.session:
            raise PermissionError("这个计划只能在收到通知的那个会话里审批。")
        if plan_digest(plan.plan_text) != plan.digest:
            raise PermissionError("方案内容与提交审批时不一致，已拒绝执行。")
        allowed_sender_ids = self._get_sender_id_allowlist()
        if allowed_sender_ids and str(event.get_sender_id()) not in allowed_sender_ids:
            raise PermissionError("当前用户没有审批权限。")

    async def _execute_plan(self, event: AstrMessageEvent, plan: PendingPlan) -> str:
        provider_id = await self.context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        if not provider_id:
            raise ValueError("当前会话没有可用的对话模型，无法执行方案。")
        toolset = self.context.get_llm_tool_manager().get_full_tool_set()
        prompt = (
            f"以下方案已由用户在聊天中批准，来源是邮件《{plan.subject}》"
            f"（发件人 {plan.sender}）。请按方案执行：\n\n{plan.plan_text}"
        )
        response = await self.context.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=prompt,
            tools=toolset,
            system_prompt=EXECUTE_SYSTEM_PROMPT,
            max_steps=32,
        )
        return (getattr(response, "completion_text", "") or "").strip()

    # ------------------------------------------------------------------ 命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_test")
    async def mail_test(
        self, event: AstrMessageEvent, recipient: str = "", account: str = ""
    ):
        """发送一封简单测试邮件，用于验证 SMTP 配置。

        用法: /mail_test recipient@example.com [账号标识]
        """
        try:
            self._check_sender_allowed(event)
            if not recipient.strip():
                yield event.plain_result(
                    "用法: /mail_test recipient@example.com [账号标识]"
                )
                return

            acc = self._account(account or None)
            request = MailRequest.from_payload(
                {
                    "to": [recipient],
                    "subject": "AstrBot 邮件插件测试",
                    "text_body": "这是一封由 astrbot_plugin_mailer 发送的测试邮件。",
                }
            )
            self._check_recipient_policy(request)
            self._check_content_limits(request)
            self._check_file_limits(request)
            result = await self._send_mail(request, acc)
            yield event.plain_result(result)
        except Exception as exc:
            logger.exception("mail_test failed")
            yield event.plain_result(f"mail_test 失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_config_check")
    async def mail_config_check(self, event: AstrMessageEvent):
        """检查当前插件账号配置。"""
        try:
            self._check_sender_allowed(event)
            accounts = self._accounts()
            if not accounts:
                yield event.plain_result("邮件插件配置无效: 没有可用账号。")
                return
            lines = []
            for acc in accounts:
                smtp = (
                    f"{acc.smtp.host}:{acc.smtp.port}" if acc.smtp else "未配置"
                )
                imap = (
                    f"{acc.imap.host}:{acc.imap.port}/{acc.imap.mailbox}"
                    if acc.imap
                    else "未配置"
                )
                lines.append(
                    f"- {acc.key} <{acc.email}>"
                    f"{' [默认]' if acc.is_default else ''}"
                    f" SMTP={smtp} IMAP={imap}"
                    f" 收信={acc.check_mode}"
                    + (" 自动回复" if acc.auto_reply_enabled else " 只提醒不回复")
                )
            yield event.plain_result(
                f"邮件插件配置校验通过，共 {len(accounts)} 个账号：\n"
                + "\n".join(lines)
                + f"\n附件目录数量: {len(self._allowed_roots())}"
            )
        except Exception as exc:
            yield event.plain_result(f"邮件插件配置无效: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_accounts")
    async def mail_accounts(self, event: AstrMessageEvent):
        """列出所有邮箱账号及其收信模式。"""
        try:
            accounts = self._accounts()
            if not accounts:
                yield event.plain_result("还没有配置任何邮箱账号。")
                return
            lines = []
            for acc in accounts:
                mode = "IDLE 推送" if acc.check_mode == "idle" else "被提醒才查"
                stage = []
                stage.append("发信" if acc.smtp else "不能发信")
                stage.append("收信" if acc.imap else "不能收信")
                lines.append(
                    f"- {acc.key} <{acc.email}> | {mode} | {'/'.join(stage)}"
                    f"{' | 默认' if acc.is_default else ''}"
                )
            yield event.plain_result("邮箱账号：\n" + "\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"读取账号配置失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_check")
    async def mail_check(
        self, event: AstrMessageEvent, account: str = "", limit: str = "10"
    ):
        """查看收件箱最近邮件。用法: /mail_check [账号标识] [数量]"""
        try:
            acc = self._account(account or None)
            try:
                count = int(limit)
            except (TypeError, ValueError):
                count = 10
            result = await self.list_mail(acc, limit=min(50, max(1, count)))
            yield event.plain_result(result)
        except Exception as exc:
            logger.exception("mail_check failed")
            yield event.plain_result(f"查看邮件失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_read")
    async def mail_read(self, event: AstrMessageEvent, uid: str = "", account: str = ""):
        """读取一封邮件。用法: /mail_read <uid> [账号标识]"""
        try:
            if not uid.strip():
                yield event.plain_result("用法: /mail_read <uid> [账号标识]")
                return
            acc = self._account(account or None)
            result = await self.read_mail(acc, uid.strip())
            yield event.plain_result(result)
        except Exception as exc:
            logger.exception("mail_read failed")
            yield event.plain_result(f"读取邮件失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mail_plans")
    async def mail_plans(self, event: AstrMessageEvent):
        """列出当前待审批的邮件操作计划。"""
        pending = self.plans.pending()
        if not pending:
            yield event.plain_result("现在没有待审批的计划。")
            return
        lines = []
        for plan in pending:
            left = max(0, int(plan.expires_at - time.time()) // 60)
            lines.append(
                f"#{plan.id} | 来自 {plan.sender} | {plan.subject} | 剩余 {left} 分钟"
            )
        yield event.plain_result("待审批计划：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批准")
    @filter.command("mail_approve")
    async def mail_approve(self, event: AstrMessageEvent, plan_id: str = ""):
        """批准并执行一份待审批计划。用法: /批准 计划编号"""
        try:
            plan = self.plans.get(plan_id) if plan_id else self.plans.latest()
            if plan is None:
                yield event.plain_result(
                    "找不到这个计划，用 /mail_plans 看看当前待审批的编号。"
                )
                return
            self._check_plan_usable(event, plan)
        except Exception as exc:
            yield event.plain_result(f"无法批准：{exc}")
            return

        self.plans.mark(plan, STATUS_APPROVED, approved_by=str(event.get_sender_id()))
        yield event.plain_result(f"已批准 #{plan.id}，开始按方案执行喵。")
        try:
            summary = await self._execute_plan(event, plan)
        except Exception as exc:
            logger.exception("mailer: 执行计划 %s 失败", plan.id)
            self.plans.mark(plan, STATUS_APPROVED, result=f"执行异常: {exc}")
            yield event.plain_result(f"#{plan.id} 执行过程中出错：{exc}")
            return

        self.plans.mark(plan, STATUS_APPROVED, result=summary[:2000])
        yield event.plain_result(summary or f"#{plan.id} 执行完成，但没有拿到总结。")

        account = None
        for acc in self._accounts():
            if acc.key == plan.account:
                account = acc
                break
        if account and account.smtp is not None:
            with contextlib.suppress(Exception):
                await self._send_reply(
                    account,
                    MailDetail(
                        uid=plan.mail_uid,
                        subject=plan.subject,
                        from_name=plan.sender,
                        from_addr=plan.sender,
                        date="",
                        message_id="",
                    ),
                    f"方案 {plan.id} 已通过审批并执行完成。执行摘要：\n\n{summary[:1500]}",
                )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("驳回")
    @filter.command("mail_reject")
    async def mail_reject(self, event: AstrMessageEvent, plan_id: str = ""):
        """驳回一份待审批计划。用法: /驳回 计划编号"""
        try:
            plan = self.plans.get(plan_id) if plan_id else self.plans.latest()
            if plan is None:
                yield event.plain_result("没有这个计划。")
                return
            self._check_plan_usable(event, plan)
        except Exception as exc:
            yield event.plain_result(f"无法驳回：{exc}")
            return
        self.plans.mark(plan, STATUS_REJECTED, approved_by=str(event.get_sender_id()))
        yield event.plain_result(f"已驳回 #{plan.id}。")
