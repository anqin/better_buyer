"""命令行客户端：初始化、采集、决策、盯盘、购买、检查、导出与服务。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import timedelta
from typing import Any

from .agents.main_agent import PLATFORM_LABELS, WATCH_MENU
from .core.container import AppContainer
from .core.logging import setup_logging
from .models import Booking, PurchaseIntent, new_id, now

# 交互式 chat 中需要压到 WARNING 的后台组件，避免日志淹没对话
_BACKGROUND_LOGGERS = (
    "better_buyer.categories",
    "better_buyer.scheduler",
    "better_buyer.checker",
    "better_buyer.collector",
    "better_buyer.watcher",
    "better_buyer.purchase",
    "better_buyer.adapters",
    "better_buyer.notify",
    "better_buyer.models",
)


def _quiet_background_logs() -> None:
    # 对话界面只保留真正的错误，其余日志由回复文案承载
    for name in _BACKGROUND_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def _decode_line(raw: bytes) -> str:
    """优先 UTF-8；Windows 终端常以 GBK 传中文，回退 GB18030 避免乱码。"""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_line(prompt: str = "你> ") -> str | None:
    """读取一行；返回 None 表示 EOF。走 bytes 解码以规避代理字符。"""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        try:
            return input(prompt)
        except EOFError:
            return None
    print(prompt, end="", flush=True)
    raw = stream.readline()
    if not raw:
        print()
        return None
    return _decode_line(raw).rstrip("\r\n")


# --------------------------------------------------------------------------
# 终端样式
# --------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _style(text: str, code: str) -> str:
    """仅在交互终端下加 ANSI 颜色；重定向或管道时保持纯文本。"""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _banner() -> str:
    line = "─" * 46
    return "\n".join(
        [
            _style(line, "36"),
            _style("   Better Buyer · 智能购买助手", "1;36"),
            _style(line, "36"),
        ]
    )


def _assistant(reply: str) -> None:
    lines = (reply or "").splitlines() or [""]
    print(f"{_style('助手>', '1;32')} {lines[0]}")
    for line in lines[1:]:
        print(line)


def _status_line(payload: dict[str, Any]) -> None:
    detail = f"状态 {payload['state']}｜预约 {payload.get('booking_id') or '-'}"
    if payload.get("order_id"):
        detail += f"｜订单 {payload['order_id']}"
    print(_style(f"· {detail}", "90"))


def _watch_summary(decision: dict[str, Any]) -> str:
    labels = {
        "buy_now": "建议立即购买",
        "watch": "建议观望",
        "no_buy": "超预算，暂不购买",
        "no_go": "数据不足，暂不出手",
    }
    code = str((decision.get("features") or {}).get("cheapest_platform") or "")
    platform = PLATFORM_LABELS.get(code, code)
    window = decision.get("optimal_window") or []
    start = str(window[0])[:10] if window else ""
    parts = [
        f"当前最低 ¥{float(decision.get('current_effective_price') or 0):.2f}"
        + (f"（{platform}）" if platform else ""),
        f"预测 ¥{float(decision.get('predicted_median') or 0):.2f}",
    ]
    if start:
        parts.append(f"最优窗口 {start}")
    recommendation = str(decision.get("recommendation") or "")
    parts.append(labels.get(recommendation, recommendation))
    return _style("[盯盘] " + "｜".join(parts), "1;35")


# --------------------------------------------------------------------------
# 输出与意图
# --------------------------------------------------------------------------


def _dump(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _intent_from_args(args: argparse.Namespace, container: AppContainer) -> PurchaseIntent:
    category = args.category
    slots: dict[str, Any] = {}
    if category == "flight":
        slots.update(
            {
                "origin": args.origin,
                "destination": args.destination,
                "depart_date": args.date,
                "cabin": args.cabin,
            }
        )
    elif category == "hotel":
        slots.update(
            {
                "city": args.city or args.destination,
                "checkin_date": args.checkin or args.date,
                "checkout_date": args.checkout,
            }
        )
    elif category == "moutai":
        slots.update({"sku": args.sku, "quantity": args.quantity})
    else:
        slots.update({"item_id": args.item, "quantity": args.quantity})
    slots = {key: value for key, value in slots.items() if value not in (None, "")}
    strategy = container.registry.get(category)
    slots = strategy.normalize_slots(slots)
    issues = strategy.validate_slots(slots)
    fatal = [issue for issue in issues if issue.level == "fatal"]
    if fatal:
        raise SystemExit("参数校验失败：" + "；".join(issue.message for issue in fatal))
    missing = [key for key in strategy.required_slots if not slots.get(key)]
    if missing:
        raise SystemExit(f"缺少必填参数：{missing}")
    return PurchaseIntent(
        category=category,
        slots=slots,
        budget=args.budget,
        preferences={"preferred_platforms": list(args.prefer or [])},
    )


def _create_booking(container: AppContainer, user_id: str, intent: PurchaseIntent) -> Booking:
    strategy = container.registry.get(intent.category)
    window = strategy.optimal_window(intent.slots)
    booking = Booking(
        id=new_id("b"),
        user_id=user_id,
        category=intent.category,
        title=strategy.title(intent),
        slots=dict(intent.slots),
        attributes={"preferences": dict(intent.preferences)},
        budget=intent.budget,
        status="collecting",
        window_start=window[0],
        window_end=window[1],
    )
    container.repos.bookings.create(booking)
    return booking


def _resolve_booking(container: AppContainer, user_id: str, args: argparse.Namespace) -> Booking:
    if getattr(args, "booking_id", ""):
        booking = container.repos.bookings.get(args.booking_id)
        if booking is None:
            raise SystemExit(f"预约不存在：{args.booking_id}")
        return booking
    intent = _intent_from_args(args, container)
    return _create_booking(container, user_id, intent)


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, container: AppContainer) -> int:
    container.settings.ensure_dirs()
    container.registry.sync(container.repos.categories)
    container.memory.ensure()
    paths = container.exporter.export_all()
    _dump(
        {
            "root": str(container.settings.root),
            "config_dir": str(container.settings.config_dir),
            "db": str(container.settings.db_path),
            "memory": str(container.settings.memory_dir),
            "categories": sorted(container.registry.strategies),
            "exports": paths,
        }
    )
    return 0


def cmd_health(args: argparse.Namespace, container: AppContainer) -> int:
    _dump(
        {
            "platform_mode": container.settings.platform_mode,
            "adapters": [health.__dict__ for health in container.adapters.health_all()],
            "models": container.router.health(),
            "categories": container.registry.summary(),
            "vault_available": container.vault.available,
        }
    )
    return 0


def cmd_status(args: argparse.Namespace, container: AppContainer) -> int:
    _dump(
        {
            "bookings": len(container.repos.bookings.list(limit=500)),
            "running_watches": [dict(item) for item in container.repos.watches.list_running()],
            "pending_notifications": len(container.repos.notifications.due(now().isoformat())),
            "latest_decision": container.repos.decisions.recent(limit=1),
            "last_check": container.repos.checks.latest(),
            "scheduler": container.scheduler.status(),
        }
    )
    return 0


def cmd_collect(args: argparse.Namespace, container: AppContainer) -> int:
    intent = _intent_from_args(args, container)
    report = container.collector.collect(intent, platforms=args.platforms)
    if args.json:
        _dump(report)
    else:
        print(
            f"品类 {intent.category}：价格 {report['price_count']} 条，"
            f"券 {report['coupon_count']} 张，其中可用 {len(report['eligible_coupons'])} 张"
        )
        for item in report["eligible_coupons"]:
            print(
                f"- {item['platform']} {item['coupon_code']} {item['title']} "
                f"有效期至 {item['valid_to']}"
            )
        if report["errors"]:
            print("采集告警：")
            for error in report["errors"]:
                print(f"- {error['platform']}: {error['error']}")
    return 0


def cmd_decide(args: argparse.Namespace, container: AppContainer) -> int:
    user = container.default_user()
    booking = _resolve_booking(container, user["id"], args)
    intent = PurchaseIntent(
        category=booking.category,
        slots=dict(booking.slots),
        budget=booking.budget,
        preferences=dict(booking.attributes.get("preferences") or {}),
    )
    container.collector.collect(intent)
    decision = asyncio.run(container.predictor_agent.run(booking, intent))
    offers = container.purchase.prepare_offers(booking, intent, refresh=False)
    if args.watch:
        container.watcher.start(booking)
    _dump(
        {
            "booking_id": booking.id,
            "decision": decision.to_dict(),
            "offers": [offer.to_dict() for offer in offers[:5]],
            "watching": bool(args.watch),
        }
    )
    return 0


def cmd_watch(args: argparse.Namespace, container: AppContainer) -> int:
    user = container.default_user()
    if args.once:
        results = container.watcher.run_due_sync()
        _dump(results)
        return 0
    if args.booking_id:
        booking = container.repos.bookings.get(args.booking_id)
        if booking is None:
            raise SystemExit(f"预约不存在：{args.booking_id}")
        task_id = container.watcher.start(booking)
        print(f"已启动盯盘任务：{task_id}（预约 {booking.id}）")
    container.start_background()
    print("后台调度器运行中，Ctrl+C 结束。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在停止…")
    finally:
        container.close()
    return 0


def cmd_buy(args: argparse.Namespace, container: AppContainer) -> int:
    user = container.default_user()
    booking = _resolve_booking(container, user["id"], args)
    intent = PurchaseIntent(
        category=booking.category,
        slots=dict(booking.slots),
        budget=booking.budget,
        preferences=dict(booking.attributes.get("preferences") or {}),
    )
    offers = (
        container.repos.offers.list(booking.id, booking.category)
        if args.use_saved
        else container.purchase.prepare_offers(booking, intent, refresh=True)
    )
    if not offers:
        print("没有可用报价，请稍后再试。")
        return 1
    if args.receipt:
        if not args.order_id:
            raise SystemExit("--receipt 需要同时提供 --order-id")
        order = container.repos.orders.get(args.order_id)
        if order is None:
            raise SystemExit(f"订单不存在：{args.order_id}")
        _dump(container.purchase.register_receipt(order, args.receipt))
        return 0
    if args.offer is None:
        print(f"预约 {booking.id} 可选方案（实付升序）：")
        for index, offer in enumerate(offers, start=1):
            coupon = f"，券 {offer.coupon_code}" if offer.coupon_code else ""
            print(
                f"{index}. {offer.platform} 实付 ¥{offer.paid_price:.2f} "
                f"（标价 ¥{offer.price:.2f}，优惠 ¥{offer.discount:.2f}{coupon}）"
                f" {offer.item_name}"
            )
        print("用 --offer N 选择方案生成下单深链。")
        return 0
    index = int(args.offer) - 1
    if index < 0 or index >= len(offers):
        raise SystemExit(f"--offer 超出范围：1-{len(offers)}")
    offer = offers[index]
    order = container.purchase.create_order(booking, offer, intent)
    if args.account:
        info = container.purchase.save_credential(
            user["id"], offer.platform, args.account, args.secret or "",
            auth_type="encrypted_session" if args.secret else "alias_only",
        )
        print(f"凭据：{info['account_alias']}（加密={info['encrypted']}）")
    _dump(
        {
            "order": order.to_dict(),
            "deep_link": order.deep_link,
            "next": "在手机打开深链完成支付后执行：buy --order-id ... --receipt 订单号",
        }
    )
    return 0


def cmd_pair(args: argparse.Namespace, container: AppContainer) -> int:
    """生成 6 位配对码，供手机端 PWA 绑定同一个云端账号。"""
    user = container.default_user()
    code = container.pairing.generate(user["id"])
    base = args.base_url or container.settings.web_config.get("public_base_url") or ""
    _dump(
        {
            "code": code,
            "expires_in": container.pairing.ttl_seconds,
            "user_id": user["id"],
            "pwa_url": base or "http://<云端地址>:8000/",
            "steps": [
                "1) 先启动服务：uv run better-buyer serve",
                "2) 手机 Chrome 打开 pwa_url，右上角设置里输入配对码",
                "3) 配对成功后手机与云端共享同一个预约、订单与盯盘任务",
            ],
        }
    )
    return 0


def cmd_check(args: argparse.Namespace, container: AppContainer) -> int:
    report = container.checker.run()
    if args.json:
        _dump(report)
    else:
        print(f"检查完成：致命 {report['fatal_count']}，警告 {report['warn_count']}")
        for item in report["reports"]:
            print(f"- {item['summary']}")
    return 0


def cmd_export(args: argparse.Namespace, container: AppContainer) -> int:
    _dump(container.exporter.export_all())
    return 0


def cmd_calibrate(args: argparse.Namespace, container: AppContainer) -> int:
    _dump(container.calibration.run(horizon_days=args.horizon))
    return 0


def cmd_serve(args: argparse.Namespace, container: AppContainer) -> int:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("缺少依赖：请先执行 uv sync（需要 fastapi/uvicorn）")
    from .api.app import create_app

    app = create_app(container=container, start_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


def cmd_chat(args: argparse.Namespace, container: AppContainer) -> int:
    user = container.default_user()
    session = container.main.create_session(user["id"])
    container.start_background()
    cursor = container.event_bus.latest_cursor()
    stop = threading.Event()
    prompt = _style("你> ", "1;36")

    def listener() -> None:
        nonlocal cursor
        while not stop.is_set():
            for event in container.event_bus.since(cursor):
                cursor = event["id"]
                if event.get("event") != "decision":
                    continue
                print()
                print(_watch_summary(event.get("decision") or {}))
                if session.state == "Watching":
                    print(_style(WATCH_MENU, "33"))
                    print(prompt, end="", flush=True)
            stop.wait(2)

    thread = threading.Thread(target=listener, daemon=True)
    thread.start()
    print(_banner())
    _assistant(container.main.greeting())
    print(_style("提示：输入“退出”结束本次对话；后台盯盘会持续运行。", "90"))
    try:
        while True:
            line = _read_line(prompt)
            if line is None:
                break
            text = line.strip()
            if not text:
                continue
            payload = asyncio.run(container.main.handle(session.id, text))
            print()
            _assistant(payload["reply"])
            _status_line(payload)
            if payload["state"] == "Exited":
                break
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        stop.set()
        container.close()
    return 0


def cmd_demo(args: argparse.Namespace, container: AppContainer) -> int:
    """离线 Mock 全链路：采集 -> 预测 -> 选券 -> 生成订单深链。"""
    user = container.default_user()
    depart = (now().date() + timedelta(days=10)).isoformat()
    intent = PurchaseIntent(
        category="flight",
        slots={
            "origin": "北京",
            "destination": "上海",
            "depart_date": depart,
            "cabin": "经济舱",
        },
        budget=2000.0,
    )
    booking = _create_booking(container, user["id"], intent)
    collection = container.collector.collect(intent)
    decision = asyncio.run(container.predictor_agent.run(booking, intent))
    offers = container.purchase.prepare_offers(booking, intent, refresh=False)
    order = container.purchase.create_order(booking, offers[0], intent) if offers else None
    _dump(
        {
            "mode": container.settings.platform_mode,
            "booking": booking.to_dict(),
            "collection": {
                "prices": collection["price_count"],
                "coupons": collection["coupon_count"],
                "eligible": len(collection["eligible_coupons"]),
            },
            "decision": decision.to_dict(),
            "offers": [offer.to_dict() for offer in offers[:3]],
            "order": order.to_dict() if order else None,
            "deep_link": order.deep_link if order else "",
        }
    )
    return 0


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------


def _add_intent_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", default="flight", choices=["flight", "hotel", "moutai", "custom_example"])
    parser.add_argument("--origin", default="")
    parser.add_argument("--destination", default="")
    parser.add_argument("--date", default="", help="出发/入住日期，支持 11月8日、明天、2026-11-08")
    parser.add_argument("--checkin", default="")
    parser.add_argument("--checkout", default="")
    parser.add_argument("--city", default="")
    parser.add_argument("--cabin", default="")
    parser.add_argument("--sku", default="")
    parser.add_argument("--item", default="")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--budget", type=float)
    parser.add_argument("--prefer", nargs="*", default=[], help="偏好平台，如 ctrip fliggy")
    parser.add_argument("--platforms", nargs="*", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="better-buyer", description="优惠券采集、盯盘预测与半自动购买")
    parser.add_argument("--root", default="", help="项目根目录（默认自动查找）")
    parser.add_argument("--mock", action="store_true", help="强制 Mock 数据源，离线可运行")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="初始化目录、品类与 Markdown 导出")
    init.set_defaults(handler=cmd_init)

    chat = sub.add_parser("chat", help="自然语言主交互界面（默认启动入口）")
    chat.set_defaults(handler=cmd_chat)

    collect = sub.add_parser("collect", help="采集价格与优惠券")
    _add_intent_args(collect)
    collect.set_defaults(handler=cmd_collect)

    decide = sub.add_parser("decide", help="采集并预测，输出购买建议")
    _add_intent_args(decide)
    decide.add_argument("--booking-id", default="")
    decide.add_argument("--watch", action="store_true", help="同时启动后台盯盘")
    decide.set_defaults(handler=cmd_decide)

    watch = sub.add_parser("watch", help="启动/执行盯盘任务")
    watch.add_argument("--booking-id", default="")
    watch.add_argument("--once", action="store_true", help="只执行一次到期任务")
    watch.set_defaults(handler=cmd_watch)

    buy = sub.add_parser("buy", help="比价选券与生成下单深链/回执")
    _add_intent_args(buy)
    buy.add_argument("--booking-id", default="")
    buy.add_argument("--offer", type=int, help="1-based 方案序号")
    buy.add_argument("--use-saved", action="store_true", help="复用已保存报价，不重新采集")
    buy.add_argument("--account", default="", help="平台登录账号（Markdown 只保存脱敏别名）")
    buy.add_argument("--secret", default="", help="账号密码/会话，需 BB_MASTER_KEY 才加密落库")
    buy.add_argument("--order-id", default="")
    buy.add_argument("--receipt", default="", help="已支付订单号")
    buy.set_defaults(handler=cmd_buy)

    check = sub.add_parser("check", help="数据完整性检查")
    check.set_defaults(handler=cmd_check)

    pair = sub.add_parser("pair", help="生成手机端配对码")
    pair.add_argument("--base-url", default="", help="云端 PWA 地址，如 http://192.168.1.10:8000")
    pair.set_defaults(handler=cmd_pair)

    export = sub.add_parser("export", help="导出 Markdown 台账")
    export.set_defaults(handler=cmd_export)

    calibrate = sub.add_parser("calibrate", help="回测并更新模型可靠性")
    calibrate.add_argument("--horizon", type=int, default=3)
    calibrate.set_defaults(handler=cmd_calibrate)

    serve = sub.add_parser("serve", help="启动 FastAPI + PWA 服务")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-scheduler", action="store_true")
    serve.set_defaults(handler=cmd_serve)

    status = sub.add_parser("status", help="查看运行状态")
    status.set_defaults(handler=cmd_status)

    health = sub.add_parser("health", help="检查适配器与模型健康")
    health.set_defaults(handler=cmd_health)

    demo = sub.add_parser("demo", help="Mock 全链路演示")
    demo.set_defaults(handler=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    if args.command is None:
        args.command = "chat"
        args.handler = cmd_chat
    if args.command == "chat":
        _quiet_background_logs()
    platform_mode = "mock" if args.mock else None
    container = AppContainer.build(root=args.root or None, platform_mode=platform_mode)
    if args.command == "serve":
        if not args.host:
            args.host = container.settings.web_config.get("host", "0.0.0.0")
        if not args.port:
            args.port = int(container.settings.web_config.get("port", 8000))
    try:
        return int(args.handler(args, container) or 0)
    finally:
        if args.command != "watch" and args.command != "serve" and args.command != "chat":
            container.close()


if __name__ == "__main__":
    sys.exit(main())
