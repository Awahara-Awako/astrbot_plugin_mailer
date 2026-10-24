"""多邮箱账号配置解析。

配置里 accounts 是一个 template_list，每项就是一个邮箱账号：
- 一个邮箱地址，一个密码（SMTP 与 IMAP 共用），自动推断 TLS
- check_mode 决定收信行为：
    manual —— 只在被提醒（工具调用 / 命令）时才去查
    idle   —— 常驻 IMAP IDLE，服务端推送到就处理
- auto_reply_enabled 决定要不要自动回信；auto_reply_whitelist 不为空时只回白名单
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .imap_client import IMAPSettings
from .smtp_client import SMTPSettings

CHECK_MODES = ("manual", "idle")
DEFAULT_SMTP_TIMEOUT = 30.0
DEFAULT_IMAP_TIMEOUT = 30.0


def _as_bool(value: Any, default: bool = False) -> bool:
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
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(",", " ").split()
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return []
    result: list[str] = []
    for item in items:
        text = item.strip().lower()
        if text and text not in result:
            result.append(text)
    return result


def _build_smtp(
    host: str, port: Any, username: str, password: str
) -> SMTPSettings | None:
    """按端口推断加密方式：465 用隐式 TLS，其它端口用 STARTTLS。"""
    if not host:
        return None
    smtp_port = _as_int(port, 587)
    if not 1 <= smtp_port <= 65535:
        raise ValueError("SMTP 端口必须在 1 到 65535 之间")
    implicit_tls = smtp_port == 465
    return SMTPSettings(
        host=host,
        port=smtp_port,
        username=username,
        password=password,
        use_tls=implicit_tls,
        use_starttls=not implicit_tls,
        timeout_seconds=DEFAULT_SMTP_TIMEOUT,
    )


def _build_imap(host: str, port: Any, username: str, password: str) -> IMAPSettings | None:
    if not host:
        return None
    imap_port = _as_int(port, 993)
    if not 1 <= imap_port <= 65535:
        raise ValueError("IMAP 端口必须在 1 到 65535 之间")
    return IMAPSettings(
        host=host,
        port=imap_port,
        username=username,
        password=password,
        use_ssl=imap_port == 993,
        mailbox="INBOX",
        timeout_seconds=DEFAULT_IMAP_TIMEOUT,
    )


@dataclass(slots=True)
class AccountConfig:
    key: str
    email: str
    password: str = ""
    is_default: bool = False
    enabled: bool = True
    from_name: str = "AstrBot Mailer"
    smtp: SMTPSettings | None = None
    imap: IMAPSettings | None = None
    check_mode: str = "manual"
    auto_reply_enabled: bool = False
    auto_reply_whitelist: list[str] = field(default_factory=list)
    notify_enabled: bool = True
    notify_session: str = ""

    @property
    def can_receive(self) -> bool:
        return self.imap is not None

    @property
    def idle(self) -> bool:
        return self.check_mode == "idle" and self.can_receive


def _parse_account_item(item: dict[str, Any], index: int) -> AccountConfig:
    email_addr = _as_str(item.get("email"))
    key = _as_str(item.get("label")) or email_addr or f"account{index + 1}"
    if not email_addr:
        raise ValueError(f"账号 {key} 缺少邮箱地址")

    password = _as_str(item.get("password"))
    check_mode = _as_str(item.get("check_mode"), "manual").lower()
    if check_mode not in CHECK_MODES:
        check_mode = "manual"

    return AccountConfig(
        key=key,
        email=email_addr,
        password=password,
        is_default=_as_bool(item.get("is_default"), False),
        enabled=_as_bool(item.get("enabled"), True),
        from_name=_as_str(item.get("from_name"), "") or "AstrBot Mailer",
        smtp=_build_smtp(
            host=_as_str(item.get("smtp_host")),
            port=item.get("smtp_port", 587),
            username=email_addr,
            password=password,
        ),
        imap=_build_imap(
            host=_as_str(item.get("imap_host")),
            port=item.get("imap_port", 993),
            username=email_addr,
            password=password,
        ),
        check_mode=check_mode,
        auto_reply_enabled=_as_bool(item.get("auto_reply_enabled"), False),
        auto_reply_whitelist=_as_str_list(item.get("auto_reply_whitelist")),
        notify_enabled=_as_bool(item.get("notify_enabled"), True),
        notify_session=_as_str(item.get("notify_session")),
    )


def parse_accounts(config: dict[str, Any]) -> list[AccountConfig]:
    """从插件配置里解析出账号列表。"""
    raw = config.get("accounts")
    accounts: list[AccountConfig] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            accounts.append(_parse_account_item(item, index))

    if accounts and not any(acc.is_default for acc in accounts):
        accounts[0].is_default = True

    seen: dict[str, int] = {}
    for acc in accounts:
        if acc.key in seen:
            seen[acc.key] += 1
            acc.key = f"{acc.key}#{seen[acc.key]}"
        else:
            seen[acc.key] = 1
    return accounts


def pick_account(
    accounts: list[AccountConfig], name: str | None = None
) -> AccountConfig:
    """按标识 / 邮箱地址选账号，name 为空则取默认账号。"""
    if not accounts:
        raise ValueError("没有可用邮箱账号，请先在插件配置里添加账号")
    target = (name or "").strip()
    if target:
        lowered = target.lower()
        for acc in accounts:
            if acc.key == target or acc.key.lower() == lowered:
                return acc
        for acc in accounts:
            if acc.email.lower() == lowered:
                return acc
        raise ValueError(f"找不到账号: {target}")
    for acc in accounts:
        if acc.is_default:
            return acc
    return accounts[0]
