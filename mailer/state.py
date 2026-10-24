"""收信状态持久化：记录每个账号已处理到的 UID 和已自动回复过的 Message-ID。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MAX_REPLIED = 500


@dataclass(slots=True)
class AccountState:
    last_uid: int = 0
    replied: list[str] = field(default_factory=list)


class MailStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, AccountState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            self._data[key] = AccountState(
                last_uid=int(value.get("last_uid", 0) or 0),
                replied=[str(item) for item in value.get("replied", []) if item],
            )

    def save(self) -> None:
        payload = {
            key: {
                "last_uid": state.last_uid,
                "replied": state.replied[-_MAX_REPLIED:],
            }
            for key, state in self._data.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def get(self, key: str) -> AccountState:
        state = self._data.get(key)
        if state is None:
            state = AccountState()
            self._data[key] = state
        return state

    def mark_replied(self, key: str, message_id: str) -> None:
        if not message_id:
            return
        state = self.get(key)
        state.replied.append(message_id)
        if len(state.replied) > _MAX_REPLIED:
            state.replied = state.replied[-_MAX_REPLIED:]

    def has_replied(self, key: str, message_id: str) -> bool:
        if not message_id:
            return False
        return message_id in self.get(key).replied

    def reset(self, key: str) -> None:
        self._data[key] = AccountState()
