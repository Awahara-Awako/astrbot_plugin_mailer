"""邮件指令的待审批计划。

邮件本身只当"需求输入"：收到白名单来信后，插件先出一份操作方案，
把方案推到聊天里等你批准。批准之后才真正执行，执行前会再校验一次
编号、摘要哈希、会话、有效期，防止审批被复用或方案被替换。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLAN_TTL_SECONDS = 1800
MAX_HISTORY = 50

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


def plan_digest(plan_text: str) -> str:
    return hashlib.sha256(plan_text.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class PendingPlan:
    id: str
    account: str
    mail_uid: str
    sender: str
    subject: str
    plan_text: str
    session: str
    created_at: float
    ttl: int = PLAN_TTL_SECONDS
    status: str = STATUS_PENDING
    approved_by: str = ""
    digest: str = ""
    result: str = ""

    def __post_init__(self) -> None:
        if not self.digest:
            self.digest = plan_digest(self.plan_text)

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at

    @property
    def usable(self) -> bool:
        return self.status == STATUS_PENDING and not self.is_expired()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "mail_uid": self.mail_uid,
            "sender": self.sender,
            "subject": self.subject,
            "plan_text": self.plan_text,
            "session": self.session,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "status": self.status,
            "approved_by": self.approved_by,
            "digest": self.digest,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingPlan":
        return cls(
            id=str(data.get("id", "")),
            account=str(data.get("account", "")),
            mail_uid=str(data.get("mail_uid", "")),
            sender=str(data.get("sender", "")),
            subject=str(data.get("subject", "")),
            plan_text=str(data.get("plan_text", "")),
            session=str(data.get("session", "")),
            created_at=float(data.get("created_at", 0) or 0),
            ttl=int(data.get("ttl", PLAN_TTL_SECONDS) or PLAN_TTL_SECONDS),
            status=str(data.get("status", STATUS_PENDING)),
            approved_by=str(data.get("approved_by", "")),
            digest=str(data.get("digest", "")),
            result=str(data.get("result", "")),
        )


class PlanStore:
    """计划队列：同一时间只保留一条待审批计划，历史只做记录。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._plans: list[PendingPlan] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, dict):
                try:
                    self._plans.append(PendingPlan.from_dict(item))
                except (TypeError, ValueError):
                    continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [plan.to_dict() for plan in self._plans[-MAX_HISTORY:]]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def create(
        self,
        *,
        account: str,
        mail_uid: str,
        sender: str,
        subject: str,
        plan_text: str,
        session: str,
    ) -> PendingPlan:
        # 新的待审批计划会顶掉旧的，避免队列堆积
        for plan in self._plans:
            if plan.status == STATUS_PENDING:
                plan.status = STATUS_EXPIRED
        plan = PendingPlan(
            id=secrets.token_hex(3),
            account=account,
            mail_uid=mail_uid,
            sender=sender,
            subject=subject,
            plan_text=plan_text,
            session=session,
            created_at=time.time(),
        )
        self._plans.append(plan)
        self.save()
        return plan

    def get(self, plan_id: str) -> PendingPlan | None:
        target = (plan_id or "").strip().lower().lstrip("#")
        for plan in self._plans:
            if plan.id.lower() == target:
                return plan
        return None

    def pending(self) -> list[PendingPlan]:
        return [plan for plan in self._plans if plan.usable]

    def latest(self) -> PendingPlan | None:
        pending = self.pending()
        return pending[-1] if pending else None

    def mark(self, plan: PendingPlan, status: str, *, approved_by: str = "", result: str = "") -> None:
        plan.status = status
        if approved_by:
            plan.approved_by = approved_by
        if result:
            plan.result = result
        self.save()
