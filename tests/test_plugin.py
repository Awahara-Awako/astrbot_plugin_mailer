from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _install_astrbot_stubs() -> dict[str, ModuleType | None]:
    originals = {
        name: sys.modules.get(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
        )
    }

    astrbot_module = ModuleType("astrbot")
    api_module = ModuleType("astrbot.api")
    event_module = ModuleType("astrbot.api.event")
    star_module = ModuleType("astrbot.api.star")
    components_module = ModuleType("astrbot.api.message_components")

    class _MessageChain:
        def __init__(self, components=None) -> None:
            self.components = list(components or [])

    class _Plain:
        def __init__(self, text: str = "") -> None:
            self.text = text

    components_module.Plain = _Plain
    event_module.MessageChain = _MessageChain

    class _Logger:
        def info(self, *_args, **_kwargs) -> None:
            return None

        def exception(self, *_args, **_kwargs) -> None:
            return None

    class _FunctionTool:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    class _Filter:
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def command(_name):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def permission_type(_permission_type):
            def decorator(func):
                return func

            return decorator

    class _Star:
        def __init__(self, context) -> None:
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(_name: str) -> Path:
            raise RuntimeError(
                "StarTools.get_data_dir should not be used in unit tests"
            )

    api_module.AstrBotConfig = dict
    api_module.FunctionTool = _FunctionTool
    api_module.logger = _Logger()
    event_module.AstrMessageEvent = object
    event_module.filter = _Filter()
    star_module.Context = object
    star_module.Star = _Star
    star_module.StarTools = _StarTools

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.api.message_components"] = components_module
    return originals


def _restore_astrbot_stubs(originals: dict[str, ModuleType | None]) -> None:
    for name, module in originals.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def plugin_symbols():
    originals = _install_astrbot_stubs()
    try:
        main_module = importlib.import_module("astrbot_plugin_mailer.main")
        models_module = importlib.import_module("astrbot_plugin_mailer.mailer.models")
        yield {
            "MailerPlugin": main_module.MailerPlugin,
            "MailRequest": models_module.MailRequest,
        }
    finally:
        for module_name in (
            "astrbot_plugin_mailer.main",
            "astrbot_plugin_mailer.mailer.models",
        ):
            sys.modules.pop(module_name, None)
        _restore_astrbot_stubs(originals)


@pytest.fixture
def MailerPlugin(plugin_symbols):
    return plugin_symbols["MailerPlugin"]


@pytest.fixture
def MailRequest(plugin_symbols):
    return plugin_symbols["MailRequest"]


class DummyContext:
    def __init__(self) -> None:
        self.tools = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)


def run_async(awaitable):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


def build_plugin(mailer_plugin_cls, tmp_path: Path, **overrides):
    config = {
        "accounts": [
            {
                "__template_key": "account",
                "label": "bot",
                "email": "bot@example.com",
                "password": "secret",
                "is_default": True,
                "check_mode": "manual",
                "from_name": "AstrBot Mailer",
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "imap_host": "imap.example.com",
                "imap_port": 993,
            }
        ],
        "tool": {
            "enable_llm_tool": True,
            "require_tool_confirmation": False,
        },
        "security": {
            "allowed_sender_ids": [],
            "allowed_domains": [],
            "blocked_domains": [],
            "allowed_attachment_roots": [str(tmp_path.resolve())],
            "allow_unsafe_all_attachment_paths": False,
            "max_attachment_size_mb": 2,
            "max_total_attachment_size_mb": 4,
            "max_html_length": 10000,
        },
    }
    for key, value in overrides.items():
        if key in config and isinstance(config[key], dict) and isinstance(value, dict):
            config[key].update(value)
        else:
            config[key] = value

    plugin = mailer_plugin_cls.__new__(mailer_plugin_cls)
    plugin.context = DummyContext()
    plugin.config = config
    plugin.data_dir = tmp_path
    plugin.attachments_dir = tmp_path
    return plugin


def test_recipient_policy_blocks_disallowed_domains(
    tmp_path: Path, MailerPlugin, MailRequest
) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        security={"allowed_domains": ["example.com"]},
    )
    request = MailRequest.from_payload(
        {
            "to": ["alice@other.com"],
            "subject": "Blocked",
            "text_body": "Hello",
        }
    )

    with pytest.raises(ValueError, match="不在白名单"):
        plugin._check_recipient_policy(request)


def test_file_limits_resolve_paths_inside_allowed_roots(
    tmp_path: Path, MailerPlugin, MailRequest
) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    attachment = tmp_path / "safe.txt"
    attachment.write_text("payload", encoding="utf-8")
    request = MailRequest.from_payload(
        {
            "to": ["alice@example.com"],
            "subject": "Safe",
            "text_body": "Hello",
            "attachments": [{"path": str(attachment)}],
        }
    )

    plugin._check_file_limits(request)
    assert request.attachments[0].path == attachment.resolve()


def test_file_limits_reject_paths_outside_allowed_roots(
    tmp_path: Path, MailerPlugin, MailRequest
) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("payload", encoding="utf-8")
    request = MailRequest.from_payload(
        {
            "to": ["alice@example.com"],
            "subject": "Unsafe",
            "text_body": "Hello",
            "attachments": [{"path": str(outside)}],
        }
    )

    with pytest.raises(ValueError, match="不在允许的附件目录内"):
        plugin._check_file_limits(request)


def test_file_limits_allow_any_path_when_unsafe_switch_enabled(
    tmp_path: Path, MailerPlugin, MailRequest
) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        security={"allow_unsafe_all_attachment_paths": True},
    )
    outside = tmp_path.parent / "outside-unsafe.txt"
    outside.write_text("payload", encoding="utf-8")
    request = MailRequest.from_payload(
        {
            "to": ["alice@example.com"],
            "subject": "Unsafe allowed",
            "text_body": "Hello",
            "attachments": [{"path": str(outside)}],
        }
    )

    plugin._check_file_limits(request)
    assert request.attachments[0].path == outside.resolve()


def test_sender_allowlist_blocks_unknown_sender(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        security={"allowed_sender_ids": ["user-1"]},
    )
    event = SimpleNamespace(get_sender_id=lambda: "user-2")

    with pytest.raises(PermissionError, match="没有权限"):
        plugin._check_sender_allowed(event)


def test_sender_allowlist_accepts_numeric_ids(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        security={"allowed_sender_ids": [12345678]},
    )
    event = SimpleNamespace(get_sender_id=lambda: "12345678")

    plugin._check_sender_allowed(event)


def test_smtp_settings_infer_tls_from_port(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    settings = plugin._smtp_settings()
    assert settings.host == "smtp.example.com"
    assert settings.port == 465
    assert settings.use_tls is True
    assert settings.use_starttls is False

    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        accounts=[
            {
                "__template_key": "account",
                "label": "bot",
                "email": "bot@example.com",
                "password": "secret",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
            }
        ],
    )
    settings = plugin._smtp_settings()
    assert settings.use_tls is False
    assert settings.use_starttls is True


def test_smtp_settings_reject_invalid_port(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        accounts=[
            {
                "__template_key": "account",
                "label": "bot",
                "email": "bot@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": 70000,
            }
        ],
    )

    with pytest.raises(ValueError, match="1 到 65535"):
        plugin._smtp_settings()


def test_bool_config_parses_string_values(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        tool={"enable_llm_tool": "true", "require_tool_confirmation": "false"},
        security={"allow_unsafe_all_attachment_paths": "false"},
    )

    assert plugin._get_bool("tool", "enable_llm_tool", True) is True
    assert plugin._get_bool("tool", "require_tool_confirmation", True) is False
    assert (
        plugin._get_bool("security", "allow_unsafe_all_attachment_paths", True) is False
    )


def test_security_lists_reject_string_values(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(
        MailerPlugin, tmp_path, security={"allowed_domains": "example.com"}
    )

    with pytest.raises(ValueError, match="必须是字符串列表"):
        plugin._check_recipient_policy(
            SimpleNamespace(all_recipients=["alice@example.com"])
        )


def test_file_limits_reject_non_positive_size_limits(
    tmp_path: Path, MailerPlugin, MailRequest
) -> None:
    plugin = build_plugin(
        MailerPlugin,
        tmp_path,
        security={"max_attachment_size_mb": 0},
    )
    attachment = tmp_path / "safe.txt"
    attachment.write_text("payload", encoding="utf-8")
    request = MailRequest.from_payload(
        {
            "to": ["alice@example.com"],
            "subject": "Safe",
            "text_body": "Hello",
            "attachments": [{"path": str(attachment)}],
        }
    )

    with pytest.raises(ValueError, match="必须大于 0"):
        plugin._check_file_limits(request)


def test_tool_handler_accepts_runtime_payload_dict(
    tmp_path: Path, MailerPlugin
) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    captured = {}

    async def fake_send_email_tool(event, **payload):
        captured["event"] = event
        captured["payload"] = payload
        return "ok"

    plugin.send_email_tool = fake_send_email_tool
    event = SimpleNamespace(get_sender_id=lambda: "user-1")
    payload = {
        "to": ["alice@example.com"],
        "subject": "Test",
        "text_body": "Hello",
    }

    result = run_async(plugin._send_email_tool_handler(event, payload))

    assert result == "ok"
    assert captured["event"] is event
    assert captured["payload"] == payload


def test_tool_handler_works_after_star_manager_partial_binding(
    tmp_path: Path, MailerPlugin
) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    captured = {}

    async def fake_send_email_tool(event, **payload):
        captured["event"] = event
        captured["payload"] = payload
        return "ok"

    plugin.send_email_tool = fake_send_email_tool
    event = SimpleNamespace(get_sender_id=lambda: "user-1")
    payload = {
        "to": ["alice@example.com"],
        "subject": "Test",
        "text_body": "Hello",
    }

    bound_handler = MailerPlugin._send_email_tool_handler.__get__(plugin, MailerPlugin)
    result = run_async(bound_handler(event, payload))

    assert result == "ok"
    assert captured["event"] is event
    assert captured["payload"] == payload


def test_tool_description_encourages_html_email(tmp_path: Path, MailerPlugin) -> None:
    plugin = build_plugin(MailerPlugin, tmp_path)
    tool = plugin._build_send_email_tool()

    assert "优先使用 html_body" in tool.description
    assert "inline_images" in tool.description
    assert "至少提供一个" in tool.parameters["properties"]["text_body"]["description"]
    assert (
        "优先使用 html_body"
        in tool.parameters["properties"]["html_body"]["description"]
    )
