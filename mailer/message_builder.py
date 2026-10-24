from __future__ import annotations

import html
import mimetypes
import re
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from .models import FileReference, InlineImageReference, MailRequest

HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_BREAK_RE = re.compile(r"<(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.IGNORECASE)
HTML_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(html_content: str) -> str:
    text = HTML_SCRIPT_STYLE_RE.sub(" ", html_content)
    text = HTML_BREAK_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WHITESPACE_RE.sub(" ", text).strip()


def _guess_content_type(path: Path) -> tuple[str, str]:
    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        return "application", "octet-stream"
    maintype, subtype = mime_type.split("/", 1)
    return maintype, subtype


def _set_addresses(message: EmailMessage, header: str, addresses: list[str]) -> None:
    if addresses:
        message[header] = ", ".join(addresses)


def _attach_file(
    message: EmailMessage,
    file_ref: FileReference,
) -> None:
    data = file_ref.path.read_bytes()
    maintype, subtype = _guess_content_type(file_ref.path)
    filename = file_ref.filename or file_ref.path.name
    message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def _attach_inline_image(
    message: EmailMessage, image_ref: InlineImageReference
) -> None:
    html_part = message.get_body(preferencelist=("html",))
    if html_part is None:
        raise ValueError("inline_images requires html_body.")

    data = image_ref.path.read_bytes()
    maintype, subtype = _guess_content_type(image_ref.path)
    if maintype != "image":
        raise ValueError(f"Inline image must be an image file: {image_ref.path}")
    html_part.add_related(
        data,
        maintype=maintype,
        subtype=subtype,
        cid=f"<{image_ref.cid}>",
        filename=image_ref.filename or image_ref.path.name,
    )


def build_message(
    request: MailRequest,
    from_email: str,
    from_name: str,
    subject_prefix: str = "",
    default_reply_to: str = "",
    extra_headers: dict[str, str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    if from_name:
        message["From"] = str(Address(display_name=from_name, addr_spec=from_email))
    else:
        message["From"] = from_email

    _set_addresses(message, "To", request.to)
    _set_addresses(message, "Cc", request.cc)

    subject = request.subject
    if subject_prefix:
        subject = f"{subject_prefix}{subject}"
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()

    reply_to = request.reply_to or default_reply_to
    if reply_to:
        message["Reply-To"] = reply_to

    if extra_headers:
        for header_name, header_value in extra_headers.items():
            if not header_name or not header_value:
                continue
            # 邮件头不允许出现换行（折叠过的 References 会带换行），统一压成单行
            cleaned_name = " ".join(str(header_name).split())
            cleaned_value = " ".join(str(header_value).split())
            if cleaned_name and cleaned_value:
                message[cleaned_name] = cleaned_value

    text_body = request.text_body or html_to_text(request.html_body)
    message.set_content(text_body)
    if request.html_body:
        message.add_alternative(request.html_body, subtype="html")

    for image_ref in request.inline_images:
        _attach_inline_image(message, image_ref)

    for attachment in request.attachments:
        _attach_file(message, attachment)

    return message
