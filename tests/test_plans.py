from __future__ import annotations

import time
from pathlib import Path

from astrbot_plugin_mailer.mailer.plans import (
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    PendingPlan,
    PlanStore,
    plan_digest,
)


def _create(store: PlanStore, text: str = "方案：重启 frps", uid: str = "7"):
    return store.create(
        account="awako-mail",
        mail_uid=uid,
        sender="mloluyu@mloluyu.uno",
        subject="处理方案批复",
        plan_text=text,
        session="default:FriendMessage:1582018374",
    )


def test_create_and_lookup(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / "plans.json")
    plan = _create(store)

    assert plan.status == STATUS_PENDING
    assert plan.digest == plan_digest("方案：重启 frps")
    assert store.get(plan.id) is plan
    assert store.get("#" + plan.id.upper()) is plan
    assert store.latest() is plan
    assert len(store.pending()) == 1


def test_new_plan_expires_previous_pending(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / "plans.json")
    first = _create(store)
    second = _create(store, text="方案：卸载 docker")

    assert first.status == STATUS_EXPIRED
    assert second.status == STATUS_PENDING
    assert [item.id for item in store.pending()] == [second.id]


def test_expiry_and_mark_persist(tmp_path: Path) -> None:
    path = tmp_path / "plans.json"
    store = PlanStore(path)
    plan = _create(store)
    plan.ttl = 0
    plan.created_at = time.time() - 10
    assert plan.is_expired() is True
    assert plan.usable is False

    store.mark(plan, STATUS_REJECTED, approved_by="1582018374")
    store.save()

    reloaded = PlanStore(path)
    stored = reloaded.get(plan.id)
    assert stored is not None
    assert stored.status == STATUS_REJECTED
    assert stored.approved_by == "1582018374"
    assert stored.digest == plan.digest


def test_digest_changes_when_plan_text_tampered() -> None:
    plan = PendingPlan(
        id="abc123",
        account="a",
        mail_uid="1",
        sender="x@example.com",
        subject="s",
        plan_text="原始方案",
        session="s",
        created_at=time.time(),
    )
    assert plan.digest == plan_digest("原始方案")
    plan.plan_text = "被替换的方案"
    assert plan_digest(plan.plan_text) != plan.digest


def test_unknown_id_returns_none(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / "plans.json")
    _create(store)
    assert store.get("nope") is None
