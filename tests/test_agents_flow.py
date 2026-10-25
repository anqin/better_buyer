"""智能体端到端（Mock）与存储/导出。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from better_buyer.models import Booking, now


def _handle(container, session_id, text):
    return asyncio.run(container.main.handle(session_id, text))


def test_chat_flow_end_to_end(container, settings):
    user = container.default_user()
    session = container.main.create_session(user["id"])
    assert "想订什么" in container.main.greeting()

    first = _handle(container, session.id, "11月8号北京飞上海，预算2000")
    # 条件齐全后无需确认，直接启动后台盯盘
    assert first["state"] == "Watching"
    assert first["booking_id"]
    assert "自动启动后台盯盘" in first["reply"]
    assert first["offers"]
    paid = [offer["paid_price"] for offer in first["offers"]]
    assert paid == sorted(paid)
    assert first["decision"]["recommendation"] in ("buy_now", "watch", "no_buy", "no_go")

    # 盯盘期间可以闲聊，状态保持 Watching
    chat = _handle(container, session.id, "今天天气不错啊")
    assert chat["state"] == "Watching"
    assert chat["action"] == "chat"

    confirm = _handle(container, session.id, "确认购买")
    assert confirm["state"] == "SelectingOffer"
    assert confirm["offers"]

    chosen = _handle(container, session.id, "1")
    assert chosen["state"] == "Purchasing"
    assert chosen["order_id"]
    order = container.repos.orders.get(chosen["order_id"])
    assert order is not None and order.status == "draft"
    assert order.deep_link.startswith("http")

    credential = _handle(container, session.id, "账号 buyer@example.com 密码 p@ss1234")
    assert credential["action"] == "credential_saved"
    status = container.purchase.credential_status(user["id"])
    assert status and status[0]["platform"] == order.platform
    assert status[0]["account_alias"] == "buy***@example.com"
    assert "p@ss1234" not in json.dumps(status, ensure_ascii=False)

    receipt = _handle(container, session.id, "已支付 订单号 1234567890")
    assert receipt["state"] == "Completed"
    paid_order = container.repos.orders.get(order.id)
    assert paid_order.status == "paid"
    assert paid_order.order_no == "1234567890"
    assert paid_order.paid_at

    history = (settings.memory_dir / "user_memory.md").read_text(encoding="utf-8")
    assert "1234567890" in history
    accounts = (settings.memory_dir / "platform_accounts.md").read_text(encoding="utf-8")
    assert "buy***@example.com" in accounts
    assert "p@ss1234" not in accounts


def test_clarifying_question_and_exit(container):
    user = container.default_user()
    session = container.main.create_session(user["id"])

    ask = _handle(container, session.id, "北京飞上海")
    assert ask["state"] == "Collecting"
    assert "depart_date" in ask["missing"]

    full = _handle(container, session.id, "11月8号")
    assert full["state"] == "Watching"

    bye = _handle(container, session.id, "退出")
    assert bye["state"] == "Exited"


def test_storage_and_markdown_export(container, settings):
    user = container.default_user()
    booking = Booking(
        id="b_export_test",
        user_id=user["id"],
        category="flight",
        title="北京 → 上海 2026-11-08",
        slots={"origin": "北京", "destination": "上海", "depart_date": "2026-11-08"},
        status="watching",
        window_start=now().isoformat(timespec="seconds"),
        window_end=now().isoformat(timespec="seconds"),
    )
    container.repos.bookings.create(booking)
    fetched = container.repos.bookings.get(booking.id)
    assert fetched is not None and fetched.title == booking.title
    assert any(item.id == booking.id for item in container.repos.bookings.list(user["id"]))

    paths = container.exporter.export_all()
    for name, path in paths.items():
        target = Path(path)
        assert target.exists(), f"{name} 未生成"
        assert target.read_text(encoding="utf-8").startswith("#")

    db_backup = container.db.backup(settings.backup_dir / "test.db")
    assert Path(db_backup).exists()
