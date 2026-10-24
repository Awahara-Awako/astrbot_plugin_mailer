from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_mailer.mailer.accounts import parse_accounts, pick_account
from astrbot_plugin_mailer.mailer.state import MailStateStore


def _account_item(**overrides):
    item = {
        "__template_key": "account",
        "label": "main",
        "email": "main@example.com",
        "password": "secret",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "check_mode": "manual",
    }
    item.update(overrides)
    return item


def test_parse_accounts_supports_multiple_accounts() -> None:
    config = {
        "accounts": [
            _account_item(),
            _account_item(label="second", email="second@example.com",
                          check_mode="idle", notify_enabled=False,
                          auto_reply_enabled=True,
                          auto_reply_whitelist=["boss@example.com"]),
        ]
    }

    accounts = parse_accounts(config)

    assert [acc.key for acc in accounts] == ["main", "second"]
    assert accounts[0].is_default is True  # 没显式指定时第一个兜底为默认
    assert accounts[0].smtp is not None and accounts[0].smtp.port == 587
    assert accounts[0].smtp.use_starttls is True
    assert accounts[0].imap is not None and accounts[0].imap.port == 993
    assert accounts[0].idle is False
    assert accounts[1].check_mode == "idle"
    assert accounts[1].idle is True
    assert accounts[1].notify_enabled is False
    assert accounts[1].auto_reply_enabled is True
    assert accounts[1].auto_reply_whitelist == ["boss@example.com"]


def test_parse_accounts_uses_single_password_for_smtp_and_imap() -> None:
    accounts = parse_accounts({"accounts": [_account_item(password="pw")]})

    assert accounts[0].smtp is not None and accounts[0].smtp.password == "pw"
    assert accounts[0].imap is not None and accounts[0].imap.password == "pw"
    assert accounts[0].smtp.username == "main@example.com"
    assert accounts[0].imap.username == "main@example.com"


def test_parse_accounts_infers_tls_from_port() -> None:
    accounts = parse_accounts(
        {"accounts": [_account_item(smtp_port=465), _account_item(
            label="b", email="b@example.com", smtp_port=587)]}
    )

    assert accounts[0].smtp.use_tls is True
    assert accounts[0].smtp.use_starttls is False
    assert accounts[1].smtp.use_tls is False
    assert accounts[1].smtp.use_starttls is True


def test_parse_accounts_idle_mode_without_imap_is_not_idle() -> None:
    accounts = parse_accounts({"accounts": [_account_item(check_mode="idle", imap_host="")]})

    assert accounts[0].can_receive is False
    assert accounts[0].idle is False


def test_parse_accounts_respects_explicit_default_and_duplicate_labels() -> None:
    config = {
        "accounts": [
            _account_item(label="dup"),
            _account_item(label="dup", email="other@example.com", is_default=True),
        ]
    }

    accounts = parse_accounts(config)

    assert accounts[0].is_default is False
    assert accounts[1].is_default is True
    assert accounts[1].key == "dup#2"


def test_parse_accounts_rejects_invalid_port() -> None:
    with pytest.raises(ValueError, match="1 到 65535"):
        parse_accounts({"accounts": [_account_item(smtp_port=70000)]})


def test_parse_accounts_returns_empty_list_without_accounts() -> None:
    assert parse_accounts({}) == []
    assert parse_accounts({"accounts": []}) == []


def test_pick_account_by_label_email_and_default() -> None:
    accounts = parse_accounts(
        {
            "accounts": [
                _account_item(),
                _account_item(label="work", email="work@example.com",
                              is_default=True),
            ]
        }
    )

    assert pick_account(accounts).key == "work"
    assert pick_account(accounts, "main").email == "main@example.com"
    assert pick_account(accounts, "WORK@example.com").key == "work"

    with pytest.raises(ValueError, match="找不到账号"):
        pick_account(accounts, "missing")

    with pytest.raises(ValueError, match="没有可用邮箱账号"):
        pick_account([], "main")


def test_state_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "mail_state.json"
    store = MailStateStore(path)
    state = store.get("main")
    state.last_uid = 42
    store.mark_replied("main", "<abc@example.com>")
    store.save()

    reloaded = MailStateStore(path)
    assert reloaded.get("main").last_uid == 42
    assert reloaded.has_replied("main", "<abc@example.com>")
    assert not reloaded.has_replied("main", "<other@example.com>")

    reloaded.reset("main")
    assert reloaded.get("main").last_uid == 0
