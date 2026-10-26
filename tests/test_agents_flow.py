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
    # 条件齐全后停留在 Ready，询问是否启动后台盯盘
    assert first["state"] == "Ready"
    assert first["booking_id"]
    assert "启动后台盯盘" in first["reply"]

    # 确认后启动盯盘
    start = _handle(container, session.id, "1")
    assert start["state"] == "Watching"
    assert start["offers"]
    paid = [offer["paid_price"] for offer in start["offers"]]
    assert paid == sorted(paid)
    assert start["decision"]["recommendation"] in ("buy_now", "watch", "no_buy", "no_go")

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
    assert full["state"] == "Ready"

    bye = _handle(container, session.id, "退出")
    assert "确认退出" in bye["reply"]  # 先反问确认
    done = _handle(container, session.id, "确认退出")
    assert done["state"] == "Exited"


def test_system_status_queries(container):
    user = container.default_user()
    session = container.main.create_session(user["id"])

    cats = _handle(container, session.id, "支持什么品类？")
    assert "品类" in cats["reply"] and cats["reply"].count("个品类")

    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    _handle(container, session.id, "1")  # 启动后台盯盘

    n = _handle(container, session.id, "现在几个盯盘任务？")
    assert "盯盘任务" in n["reply"]
    assert int(next(w for w in n["reply"].split() if w.isdigit())) >= 1


def test_watch_countdown_auto_continue(container):
    from datetime import timedelta

    from better_buyer.models import iso, now

    user = container.default_user()
    session = container.main.create_session(user["id"])
    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    watch = _handle(container, session.id, "1")  # 启动盯盘并开启倒计时
    booking_id = watch["booking_id"]

    task = container.repos.watches.get_active(booking_id)
    # 模拟用户超时未响应：把倒计时截止时间改到过去
    container.repos.watches.update(
        task["id"], await_response_until=iso(now() - timedelta(seconds=10))
    )
    res = container.watcher.process_idle_countdowns()
    assert len(res) == 1 and res[0]["booking_id"] == booking_id

    refreshed = container.repos.watches.get_active(booking_id)
    assert refreshed["status"] == "running"        # 自动继续盯盘，未停止
    assert refreshed["await_response_until"]        # 已开始下一轮倒计时，直到截止日期


def test_ready_confirm_menu(container):
    user = container.default_user()
    session = container.main.create_session(user["id"])

    ready = _handle(container, session.id, "11月8号北京飞上海，预算2000")
    assert ready["state"] == "Ready"
    assert "启动后台盯盘" in ready["reply"]

    modify = _handle(container, session.id, "2")
    assert modify["state"] == "Collecting"


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


def test_intent_commits_only_at_threshold(container):
    """需求1：只有意图置信度达到阈值才提交品类并进入收集。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    # 无明确品类：不提交，只引导
    vage = _handle(container, session.id, "我想买点东西")
    assert vage["state"] == "Idle"
    assert "想买什么" in vage["reply"]
    assert container.main.sessions[session.id].intent_category is None

    # 明确品类（机票）：命中 0.80 → 提交并进入收集
    commit = _handle(container, session.id, "11月8号北京飞上海，预算2000")
    sess = container.main.sessions[session.id]
    assert sess.intent_category == "flight"
    assert commit["state"] == "Ready"


def test_collecting_allows_small_talk(container):
    """需求3：收集参数期间插入闲聊不应被误当槽位或打断意图。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    _handle(container, session.id, "北京飞上海")  # 机票，还缺日期
    sess = container.main.sessions[session.id]
    assert sess.intent_category == "flight"

    r = _handle(container, session.id, "哈哈知道了，麻烦你了")
    assert r["state"] == "Collecting"
    assert r["action"] in ("chat", "continue")
    assert "请补充" not in r["reply"]
    assert container.main.sessions[session.id].intent_category == "flight"


def test_intent_drop_below_threshold_prompts_continue(container):
    """需求4：已确认意图置信度掉到阈值以下且出现其它品类信号时，询问是否继续。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    _handle(container, session.id, "北京飞上海")  # 机票，缺日期
    hesitated = _handle(container, session.id, "要不订个房间呢")
    sess = container.main.sessions[session.id]
    assert sess.pending_confirm is True
    assert "继续" in hesitated["reply"]

    # 确认继续 → 保持机票收集
    again = _handle(container, session.id, "继续机票")
    assert container.main.sessions[session.id].intent_category == "flight"
    assert container.main.sessions[session.id].pending_confirm is False
    assert again["action"] in ("continue", "chat", "ready")


def test_cancel_watch_by_number(container):
    """需求2：盯盘任务有编号，可按编号取消，且取消前需二次确认。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    watch = _handle(container, session.id, "1")  # 启动后台盯盘
    active = container.watcher.describe_active()
    assert len(active) == 1 and active[0]["task_id"]

    # 请求取消 → 先进入确认，任务仍在后台
    pending = _handle(container, session.id, "取消盯盘 1")
    assert "确认" in pending["reply"]
    assert container.watcher.count_active() == 1

    # 回复「保留」→ 不删除
    keep = _handle(container, session.id, "保留")
    assert "保留" in keep["reply"]
    assert container.watcher.count_active() == 1

    # 再次发起并确认删除 → 任务被停止
    _handle(container, session.id, "取消盯盘 1")
    cancelled = _handle(container, session.id, "确认删除")
    assert "已取消" in cancelled["reply"]
    assert container.watcher.count_active() == 0

    # 按任务 id 取消
    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    _handle(container, session.id, "1")
    tid = container.watcher.describe_active()[0]["task_id"]
    _handle(container, session.id, f"取消任务 {tid}")
    cancelled2 = _handle(container, session.id, f"取消任务 {tid}")
    assert "已取消" in cancelled2["reply"]
    assert container.watcher.count_active() == 0


def test_guided_intent_boost_and_status(container):
    """需求1/2/3：状态显示意图置信度；对话引导提升意图；话术不再罗列无关品类。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    # "最近要出差" → 机票 55%、酒店 35%；话术围绕机票/酒店二选一，不再提茅台
    r = _handle(container, session.id, "最近要出差")
    assert "茅台" not in r["reply"]
    assert "机票" in r["reply"] and "酒店" in r["reply"]
    assert r["action"] == "chat"
    scores = r.get("intent_scores") or {}
    assert int(scores.get("flight", 0) * 100) >= 55
    assert int(scores.get("hotel", 0) * 100) >= 35

    # 用户确认"想要订机票" → 意图置信度提升 → 提交机票
    confirm = _handle(container, session.id, "想要订机票")
    sess = container.main.sessions[session.id]
    assert sess.intent_category == "flight"
    assert confirm["state"] == "Collecting"

    # 用"对"直接确认引导提案（proposed_category）也能提交
    session2 = container.main.create_session(user["id"])
    _handle(container, session2.id, "最近要出差")
    ack = _handle(container, session2.id, "对")
    assert container.main.sessions[session2.id].intent_category == "flight"
    assert ack["state"] == "Collecting"


def test_collecting_slot_does_not_prompt_direction_change(container):
    """回归：已确认意图后，补参数（如出发城市）绝不能触发“换个方向”的询问。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])

    _handle(container, session.id, "机票")  # 提交机票意图，收集参数
    r = _handle(container, session.id, "北京")  # 补出发城市
    sess = container.main.sessions[session.id]
    assert "换个方向" not in r["reply"]
    assert sess.intent_category == "flight"
    assert sess.pending_confirm is False
    assert int(sess.intent_confidence * 100) >= 90  # 置信度仍锚定在高位


def _start_watch(container, session_id: str) -> None:
    _handle(container, session_id, "11月8号北京飞上海，预算2000")
    _handle(container, session_id, "1")  # 启动后台盯盘


def test_watching_chat_is_natural_not_canned(container):
    """需求：盯盘后闲聊应自然回应并对意图重预测，不再重复固定菜单话术。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])
    _start_watch(container, session.id)
    assert container.main.sessions[session.id].state == "Watching"

    chat = _handle(container, session.id, "南京有什么好玩的地方")
    sess = container.main.sessions[session.id]
    assert "值得去" in chat["reply"]  # 自然回应包含目的地建议
    assert "后台还在盯盘" not in chat["reply"]  # 固定话术已移除
    assert sess.intent_category == "flight"  # 未被误判为新意图
    assert chat["state"] == "Watching"


def test_watching_repredict_switches_to_new_intent(container):
    """需求：盯盘后表达新品类意图 → 重预测并转入新一轮收集，旧盯盘任务保留。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])
    _start_watch(container, session.id)
    before = container.watcher.count_active()
    assert before >= 1

    pivot = _handle(container, session.id, "我要再订个酒店")
    sess = container.main.sessions[session.id]
    assert sess.intent_category == "hotel"
    assert pivot["state"] == "Collecting"
    assert sess.booking_id is None  # 已脱离旧看盘，开始新收集
    assert container.watcher.count_active() == before  # 旧盯盘任务仍在后台


def test_switch_category_keeps_booking_and_task_separate(container):
    """需求：订完机票盯盘后转订酒店，酒店得到独立 booking 与独立盯盘任务，不再串到机票上。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])
    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    _handle(container, session.id, "1")  # 机票盯盘
    flight_tasks = container.watcher.describe_active()
    flight_tid = flight_tasks[0]["task_id"]
    assert flight_tasks[0]["category"] == "flight"

    _handle(container, session.id, "我要再订个酒店")
    _handle(container, session.id, "11月8号北京酒店住1晚")
    r = _handle(container, session.id, "1")  # 启动酒店盯盘；若数据不足会先询问
    if "数据不足" in r["reply"]:
        r = _handle(container, session.id, "1")  # 确认继续盯盘

    tasks = container.watcher.describe_active()
    assert len(tasks) == 2
    by_cat = {t["category"]: t for t in tasks}
    assert set(by_cat) == {"flight", "hotel"}
    assert by_cat["flight"]["task_id"] == flight_tid  # 机票任务未受影响、品类不变
    # booking 已分开，题目不再串乱
    books = {b.category for b in container.repos.bookings.list(user["id"])}
    assert books == {"flight", "hotel"}


def test_no_go_start_asks_confirmation(container, monkeypatch):
    """需求：数据不足时不自动启动盯盘，先询问并说明是外部数据原因，确认后才启动。"""
    from better_buyer.models import Decision

    user = container.default_user()
    session = container.main.create_session(user["id"])
    _handle(container, session.id, "11月8号北京飞上海，预算2000")  # 进入 Ready

    async def fake_run(booking, intent=None):
        return Decision(
            category=booking.category,
            booking_id=booking.id,
            current_effective_price=0.0,
            predicted_median=0.0,
            predicted_range=(0.0, 0.0),
            recommendation="no_go",
            decision_score=0.0,
            confidence=0.0,
            optimal_window=("", ""),
            reasons=["没有可用的报价"],
            guardrail_reasons=["没有可用的报价"],
        )

    monkeypatch.setattr(container.main.predictor, "run", fake_run)

    r = _handle(container, session.id, "1")  # 请求启动盯盘
    assert "数据不足" in r["reply"]
    assert "程序故障" in r["reply"]  # 明确告知是外部原因而非程序故障
    assert container.watcher.count_active() == 0  # 未自动启动
    assert container.main.sessions[session.id].pending_watch_start is True

    r2 = _handle(container, session.id, "1")  # 确认继续盯盘
    assert container.watcher.count_active() == 1
    assert container.watcher.describe_active()[0]["status_label"] == "异常（数据不足）"


def test_exit_requires_confirmation_and_accepts_english(container):
    """需求：支持「退出」/「exit」退出对话，且先反问确认；误触可继续对话。"""
    user = container.default_user()
    session = container.main.create_session(user["id"])
    _handle(container, session.id, "11月8号北京飞上海，预算2000")
    _handle(container, session.id, "1")  # 启动盯盘

    # 首次输入「退出」→ 先询问确认，未真正退出
    ask = _handle(container, session.id, "退出")
    assert "确认退出" in ask["reply"]
    assert ask["state"] != "Exited"

    # 反悔继续对话 → 不清除会话，仍可聊天
    keep = _handle(container, session.id, "今天天气不错")
    assert keep["state"] != "Exited"

    # 再次输入「exit」→ 反问；确认后真正退出
    ask2 = _handle(container, session.id, "exit")
    assert "确认退出" in ask2["reply"]
    done = _handle(container, session.id, "确认退出")
    assert done["action"] == "exit"
    assert done["state"] == "Exited"
    assert "盯盘" in done["reply"]  # 提示后台盯盘会保留
