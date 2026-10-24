"""IMAP 收信支持（只读为主，必要时可标记已读）。

设计上尽量少依赖：使用标准库 imaplib + email，通过 asyncio.to_thread 包成异步，
避免额外引入 aioimaplib 之类的第三方包。
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import re
from dataclasses import dataclass, field
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class IMAPSettings:
    host: str
    port: int
    username: str
    password: str
    use_ssl: bool = True
    mailbox: str = "INBOX"
    timeout_seconds: float = 30.0


@dataclass(slots=True)
class MailSummary:
    uid: str
    subject: str
    from_name: str
    from_addr: str
    date: str
    message_id: str


@dataclass(slots=True)
class MailDetail(MailSummary):
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    reply_to: str = ""
    text_body: str = ""
    attachments: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""
    auto_submitted: str = ""
    truncated: bool = False


def _decode_raw(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
    except Exception:  # noqa: BLE001 - 头部编码格式千奇百怪，解不开就原样返回
        return str(value)
    chunks: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            for enc in (charset, "utf-8", "gb18030", "latin-1"):
                if not enc:
                    continue
                try:
                    chunks.append(text.decode(enc, errors="strict"))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                chunks.append(text.decode("utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks).strip()


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        try:
            return payload.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message, max_chars: int) -> tuple[str, list[str], bool]:
    """返回 (正文文本, 附件文件名列表, 是否被截断)。"""
    attachments: list[str] = []
    text_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename or "attachment" in disposition:
            attachments.append(_decode_raw(filename) or "未命名附件")
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            text_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    body = "\n".join(text_parts).strip()
    if not body and html_parts:
        body = _html_to_text("\n".join(html_parts))
    body = _normalize_text(body)

    truncated = False
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars]
        truncated = True
    return body, attachments, truncated


def _connect(settings: IMAPSettings) -> imaplib.IMAP4:
    if settings.use_ssl:
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            settings.host, settings.port, timeout=settings.timeout_seconds
        )
    else:
        client = imaplib.IMAP4(
            settings.host, settings.port, timeout=settings.timeout_seconds
        )
    client.login(settings.username, settings.password)
    return client


def _close(client: imaplib.IMAP4) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - 未 select 时 close 会报错，忽略即可
        pass
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        pass


def _parse_summary(uid: str, raw: bytes) -> MailSummary:
    msg = email.message_from_bytes(raw)
    from_name, from_addr = parseaddr(_decode_raw(msg.get("From")))
    return MailSummary(
        uid=str(uid),
        subject=_decode_raw(msg.get("Subject")) or "(无主题)",
        from_name=_decode_raw(from_name) or from_addr,
        from_addr=from_addr or "",
        date=_decode_raw(msg.get("Date")),
        message_id=(msg.get("Message-ID") or "").strip(),
    )


def _list_sync(
    settings: IMAPSettings, limit: int, unread_only: bool
) -> list[MailSummary]:
    client = _connect(settings)
    try:
        typ, _ = client.select(settings.mailbox, readonly=True)
        if typ != "OK":
            raise ValueError(f"无法打开信箱 {settings.mailbox}")
        criterion = "UNSEEN" if unread_only else "ALL"
        typ, data = client.uid("search", None, criterion)
        if typ != "OK":
            raise ValueError("IMAP 搜索失败")
        raw_uids = (data[0] or b"").split()
        uids = [u.decode() for u in raw_uids][-limit:] if limit > 0 else []
        results: list[MailSummary] = []
        for uid in reversed(uids):
            typ, fetched = client.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])",
            )
            if typ != "OK" or not fetched:
                continue
            chunk = fetched[0]
            if not isinstance(chunk, tuple) or len(chunk) < 2:
                continue
            results.append(_parse_summary(uid, chunk[1]))
        return results
    finally:
        _close(client)


def _read_sync(settings: IMAPSettings, uid: str, max_chars: int) -> MailDetail:
    client = _connect(settings)
    try:
        typ, _ = client.select(settings.mailbox, readonly=True)
        if typ != "OK":
            raise ValueError(f"无法打开信箱 {settings.mailbox}")
        typ, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not fetched:
            raise ValueError(f"读取邮件失败: uid={uid}")
        chunk = fetched[0]
        if not isinstance(chunk, tuple) or len(chunk) < 2:
            raise ValueError(f"邮件内容为空: uid={uid}")
        msg = email.message_from_bytes(chunk[1])
        body, attachments, truncated = _extract_body(msg, max_chars)
        from_name, from_addr = parseaddr(_decode_raw(msg.get("From")))
        to_list = [
            addr
            for _, addr in email.utils.getaddresses([msg.get("To") or ""])
            if addr
        ]
        cc_list = [
            addr
            for _, addr in email.utils.getaddresses([msg.get("Cc") or ""])
            if addr
        ]
        return MailDetail(
            uid=str(uid),
            subject=_decode_raw(msg.get("Subject")) or "(无主题)",
            from_name=_decode_raw(from_name) or from_addr,
            from_addr=from_addr or "",
            date=_decode_raw(msg.get("Date")),
            message_id=(msg.get("Message-ID") or "").strip(),
            to=to_list,
            cc=cc_list,
            reply_to=parseaddr(_decode_raw(msg.get("Reply-To")))[1],
            text_body=body,
            attachments=attachments,
            in_reply_to=(msg.get("In-Reply-To") or "").strip(),
            references=(msg.get("References") or "").strip(),
            auto_submitted=(msg.get("Auto-Submitted") or "").strip(),
            truncated=truncated,
        )
    finally:
        _close(client)


def _search_since_sync(settings: IMAPSettings, last_uid: int) -> list[str]:
    client = _connect(settings)
    try:
        typ, _ = client.select(settings.mailbox, readonly=False)
        if typ != "OK":
            raise ValueError(f"无法打开信箱 {settings.mailbox}")
        typ, data = client.uid("search", None, "ALL")
        if typ != "OK":
            raise ValueError("IMAP 搜索失败")
        uids = [int(u.decode()) for u in (data[0] or b"").split() if u.decode().isdigit()]
        return [str(u) for u in sorted(uids) if u > last_uid]
    finally:
        _close(client)


def _mark_seen_sync(settings: IMAPSettings, uid: str) -> None:
    client = _connect(settings)
    try:
        typ, _ = client.select(settings.mailbox, readonly=False)
        if typ != "OK":
            return
        client.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        _close(client)


async def list_messages(
    settings: IMAPSettings, limit: int = 10, unread_only: bool = False
) -> list[MailSummary]:
    return await asyncio.to_thread(_list_sync, settings, limit, unread_only)


async def read_message(
    settings: IMAPSettings, uid: str, max_chars: int = 4000
) -> MailDetail:
    return await asyncio.to_thread(_read_sync, settings, uid, max_chars)


async def search_since(settings: IMAPSettings, last_uid: int) -> list[str]:
    return await asyncio.to_thread(_search_since_sync, settings, last_uid)


async def mark_seen(settings: IMAPSettings, uid: str) -> None:
    await asyncio.to_thread(_mark_seen_sync, settings, uid)
