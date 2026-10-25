"""主智能体：状态机编排、信息齐全后启动盯盘、购买与回执闭环。"""

from __future__ import annotations

import re
from typing import Any

from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..models import (
    Booking,
    ChatSession,
    InteractionResult,
    Offer,
    Order,
    PurchaseIntent,
    new_id,
    now_iso,
)
from ..services.memory import MemoryStore
from ..services.state_machine import (
    SessionState,
    can_transition,
    transition,
)
from ..storage.md_export import MarkdownExporter
from ..storage.repositories import RepositoryHub
from .collector import CollectionAgent
from .interaction import InteractionAgent
from .predictor import PredictorAgent
from .purchase import PurchaseAgent
from .watcher import WatchAgent

PLATFORM_NAMES = {
    "携程": "ctrip",
    "飞猪": "fliggy",
    "淘宝": "taobao",
    "京东": "jd",
    "抖音": "douyin",
}
PLATFORM_LABELS = {value: key for key, value in PLATFORM_NAMES.items()}

CATEGORY_TIPS = "目前支持：机票、酒店、茅台。你可以直接说“11月8号北京飞上海，预算2000”。"

READY_MENU = (
    "=== 是否后台执行 ===\n"
    "[1] 启动后台盯盘  [2] 修改条件  [3] 取消"
)

WATCH_MENU = (
    "=== 盯盘决策 ===\n"
    "[1] 现在购买  [2] 继续观望  [3] 查看各平台优惠券  [4] 停止盯盘"
)

RECEIPT_HINTS = ("已支付", "支付完成", "付款完成", "回执", "订单号")
CANCEL_WORDS = ("取消", "算了", "返回", "不买了")


class MainAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        repos: RepositoryHub,
        interaction: InteractionAgent,
        collector: CollectionAgent,
        predictor: PredictorAgent,
        purchase: PurchaseAgent,
        watcher: WatchAgent,
        memory: MemoryStore,
        exporter: MarkdownExporter | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.repos = repos
        self.interaction = interaction
        self.collector = collector
        self.predictor = predictor
        self.purchase = purchase
        self.watcher = watcher
        self.memory = memory
        self.exporter = exporter
        self.sessions: dict[str, ChatSession] = {}

    # ------------------------------------------------------------------
    def greeting(self) -> str:
        return (
            "你好，我是你的购买助手。想订什么？直接告诉我品类、出发地、目的地和时间，预算与偏好可选。\n"
            f"{CATEGORY_TIPS}\n"
            "信息齐全后我会先和你确认，再启动后台盯盘；盯盘期间可以随时闲聊，也可以让我停止。"
        )

    def create_session(self, user_id: str) -> ChatSession:
        session_id = self.repos.sessions.create(user_id)
        session = ChatSession(id=session_id, user_id=user_id, state=SessionState.IDLE.value)
        self.sessions[session_id] = session
        self.repos.chat.add(session_id, "assistant", self.greeting())
        return session

    def get_session(self, session_id: str, user_id: str | None = None) -> ChatSession:
        if session_id in self.sessions:
            return self.sessions[session_id]
        row = self.repos.sessions.get(session_id, user_id)
        if not row:
            raise KeyError(f"会话不存在：{session_id}")
        session = ChatSession(
            id=session_id,
            user_id=row["user_id"],
            state=SessionState.IDLE.value,
        )
        self.sessions[session_id] = session
        return session

    # ------------------------------------------------------------------
    async def handle(
        self,
        session_id: str,
        text: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(session_id, user_id)
        self.repos.chat.add(session.id, "user", text or "")

        if session.state == SessionState.PURCHASING.value and not self._is_receipt(text):
            credential_reply = self._handle_credential(session, text)
            if credential_reply is not None:
                self.repos.chat.add(session.id, "assistant", credential_reply["reply"])
                result = InteractionResult(
                    session.category,
                    session.intent,
                    reply=credential_reply["reply"],
                    action=credential_reply.get("action", "continue"),
                )
                payload = self._payload(session, result)
                payload["credential"] = credential_reply.get("credential")
                payload["order"] = credential_reply.get("order")
                return payload

        if session.state == SessionState.WATCHING.value:
            choice = self._watch_choice(text)
            if choice:
                return await self._watch_action(session, choice)

        if session.state == SessionState.READY.value:
            choice = self._ready_choice(text)
            if choice:
                return await self._ready_action(session, choice)

        if session.state == SessionState.SELECTING_OFFER.value and any(word in text for word in CANCEL_WORDS):
            return self._flow_back_to_watch(session, "已取消选择平台，回到盯盘。")

        result = await self.interaction.handle(text, session)
        self._apply_result(session, result)
        payload = await self._dispatch(session, result)
        self.repos.chat.add(session.id, "assistant", payload["reply"])
        return payload

    # ------------------------------------------------------------------
    def _apply_result(self, session: ChatSession, result: InteractionResult) -> None:
        if result.category:
            session.category = result.category
        if result.intent is not None:
            session.intent = result.intent
            if result.category and result.action in ("continue", "chat", "ready"):
                if session.state in (SessionState.IDLE.value, SessionState.EXITED.value):
                    transition(session, SessionState.COLLECTING)
        if result.action == "exit":
            if can_transition(session.state, SessionState.EXITED):
                transition(session, SessionState.EXITED)
        elif result.action == "continue" and session.intent is not None:
            # 只有补充/修改条件才回到采集态；盯盘中的闲聊不应打断状态
            if can_transition(session.state, SessionState.COLLECTING):
                transition(session, SessionState.COLLECTING)

    async def _dispatch(self, session: ChatSession, result: InteractionResult) -> dict[str, Any]:
        action = result.action
        if action == "exit":
            return self._payload(session, result, reply=result.reply or "已退出本次对话。")

        if action == "ready" and result.intent is not None:
            return await self._flow_ready(session, result)

        if action in ("continue", "chat"):
            return self._payload(session, result, reply=result.reply or CATEGORY_TIPS)

        if action == "confirm":
            return self._flow_confirm(session, result)

        if action == "select_offer":
            return self._flow_select_offer(session, result)

        if action == "receipt":
            return self._flow_receipt(session, result)

        return self._payload(session, result)

    # ------------------------------------------------------------------
    async def _flow_ready(
        self, session: ChatSession, result: InteractionResult
    ) -> dict[str, Any]:
        intent = result.intent
        assert intent is not None
        if session.state in (SessionState.EXITED.value, SessionState.IDLE.value):
            transition(session, SessionState.COLLECTING)
        if not can_transition(session.state, SessionState.READY):
            if can_transition(session.state, SessionState.COLLECTING):
                transition(session, SessionState.COLLECTING)
                transition(session, SessionState.READY)
        if can_transition(session.state, SessionState.READY):
            transition(session, SessionState.READY)

        booking = self._ensure_booking(session, intent)
        if self.watcher.status(booking.id) is not None:
            self.watcher.stop(booking.id)
            self.repos.bookings.update(booking.id, status="collecting")
        session.offers = []
        session.last_decision = None
        session.order_id = None

        reply = "\n".join(
            [
                result.reply or f"已确认：{booking.title}。",
                "",
                "确认无误就启动后台盯盘；需要改条件也可以直接说。",
                "",
                READY_MENU,
            ]
        )
        payload = self._payload(session, result, reply=reply)
        payload["booking_id"] = booking.id
        return payload

    async def _flow_start_watch(
        self, session: ChatSession, result: InteractionResult | None = None
    ) -> dict[str, Any]:
        booking = self._current_booking(session)
        intent = session.intent or (self.predictor.intent_from_booking(booking) if booking else None)
        if booking is None or intent is None:
            return self._payload(session, result, reply="还没有完整的购买条件，请先告诉我品类、地点与时间。")

        if can_transition(session.state, SessionState.WATCHING):
            transition(session, SessionState.WATCHING)
        collection = self.collector.collect(intent)
        decision = await self.predictor.run(booking, intent)
        session.last_decision = decision
        offers = self.purchase.prepare_offers(booking, intent, refresh=False)
        session.offers = offers
        self.watcher.start(booking)
        self.repos.bookings.update(booking.id, status="watching")

        parts = [
            self._decision_line(decision, offers),
        ]
        if offers:
            parts.extend(["", self._format_offers(offers[:3], "前 3 个可选方案")])
        if collection.get("errors"):
            parts.extend(["", f"注意：{len(collection['errors'])} 个平台采集失败，已使用可用数据。"])
        parts.extend(["", WATCH_MENU])
        payload = self._payload(session, result, reply="\n".join(parts))
        payload["collection"] = collection
        payload["booking_id"] = booking.id
        return payload

    def _flow_confirm(self, session: ChatSession, result: InteractionResult) -> dict[str, Any]:
        booking = self._current_booking(session)
        if booking is None:
            return self._payload(session, result, reply="还没有完整的购买条件，请先告诉我品类、地点与时间。")
        intent = session.intent or self.predictor.intent_from_booking(booking)
        offers = self._platform_offers(self.purchase.prepare_offers(booking, intent, refresh=True))
        session.offers = offers
        if not can_transition(session.state, SessionState.SELECTING_OFFER):
            if can_transition(session.state, SessionState.DECIDING):
                transition(session, SessionState.DECIDING)
            elif can_transition(session.state, SessionState.WATCHING):
                transition(session, SessionState.WATCHING)
        if can_transition(session.state, SessionState.SELECTING_OFFER):
            transition(session, SessionState.SELECTING_OFFER)
        if not offers:
            if can_transition(session.state, SessionState.WATCHING):
                transition(session, SessionState.WATCHING)
            return self._payload(
                session,
                result,
                reply="实时刷新后没有可用报价，可能已售罄或优惠已失效。已回到盯盘模式。",
            )
        reply = "\n".join(
            [
                "=== 选择平台（实付价从低到高）===",
                self._format_platforms(offers),
                "",
                "回复序号选择平台；回复“取消”回到盯盘。",
            ]
        )
        return self._payload(session, result, reply=reply)

    def _flow_select_offer(self, session: ChatSession, result: InteractionResult) -> dict[str, Any]:
        booking = self._current_booking(session)
        if booking is None or not session.offers:
            return self._payload(session, result, reply="当前没有可选方案，请先确认购买条件。")
        index = result.selected_offer or 0
        if index < 0 or index >= len(session.offers):
            return self._payload(
                session,
                result,
                reply=f"序号超出范围，请回复 1-{len(session.offers)}。",
            )
        offer = session.offers[index]
        order = self.purchase.create_order(booking, offer, session.intent)
        session.order_id = order.id
        session.credential_platform = order.platform
        if not can_transition(session.state, SessionState.PURCHASING):
            if can_transition(session.state, SessionState.SELECTING_OFFER):
                transition(session, SessionState.SELECTING_OFFER)
            elif can_transition(session.state, SessionState.WATCHING):
                transition(session, SessionState.WATCHING)
                if can_transition(session.state, SessionState.SELECTING_OFFER):
                    transition(session, SessionState.SELECTING_OFFER)
        if can_transition(session.state, SessionState.PURCHASING):
            transition(session, SessionState.PURCHASING)

        label = PLATFORM_LABELS.get(order.platform, order.platform)
        summary = [
            f"=== 下单 · {label} ===",
            f"{order.item_name}",
            f"标价 ¥{order.list_price:.2f} · 优惠 ¥{order.discount:.2f} · 实付 ¥{order.paid_price:.2f}",
        ]
        credential = self.purchase.credential_status(session.user_id)
        saved = next((item for item in credential if item["platform"] == order.platform), None)
        if saved is not None:
            session.credential_step = ""
            reply = "\n".join(
                summary
                + [
                    f"已保存账号：{saved['account_alias']}",
                    f"下单深链：{order.deep_link}",
                    "完成支付后回复“已支付 订单号 XXX”，我会记录购买并设置出发提醒。",
                ]
            )
        else:
            session.credential_step = "account"
            reply = "\n".join(
                summary
                + [
                    f"首次使用 {label}，请回复登录账号（回复“取消”可退出）：",
                ]
            )
        payload = self._payload(session, result, reply=reply)
        payload["order"] = order.to_dict()
        payload["deep_link"] = order.deep_link if saved is not None else ""
        return payload

    def _flow_receipt(self, session: ChatSession, result: InteractionResult) -> dict[str, Any]:
        if not session.order_id:
            return self._payload(session, result, reply="还没有待支付的订单。")
        order = self.repos.orders.get(session.order_id)
        if order is None:
            return self._payload(session, result, reply="订单不存在，请重新选择方案。")
        order_no = str((result.receipt or {}).get("order_no") or "")
        receipt = self.purchase.register_receipt(order, order_no)
        if not can_transition(session.state, SessionState.COMPLETED):
            if can_transition(session.state, SessionState.PURCHASING):
                transition(session, SessionState.PURCHASING)
            elif can_transition(session.state, SessionState.WATCHING):
                transition(session, SessionState.WATCHING)
        if can_transition(session.state, SessionState.COMPLETED):
            transition(session, SessionState.COMPLETED)
        reply = "\n".join(
            [
                "购买记录已写入本地记忆。",
                f"{order.platform} · 实付 ¥{receipt['paid_price']:.2f} · 优惠 ¥{receipt['discount']:.2f}",
                f"订单号：{receipt['order_no'] or '-'}",
                f"已创建 {len(receipt['notifications'])} 条出发/入住提醒，可在通知目录或手机推送中查看。",
                "需要继续买别的，直接告诉我新需求。",
            ]
        )
        payload = self._payload(session, result, reply=reply)
        payload["receipt"] = receipt
        return payload

    # ------------------------------------------------------------------
    def _handle_credential(self, session: ChatSession, text: str) -> dict[str, Any] | None:
        if not session.order_id:
            return None
        order = self.repos.orders.get(session.order_id)
        if order is None:
            return None
        platform = order.platform
        label = PLATFORM_LABELS.get(platform, platform)

        if session.credential_step in ("account", "secret") and any(
            word in text for word in CANCEL_WORDS
        ):
            session.credential_step = ""
            session.credential_account = ""
            if can_transition(session.state, SessionState.SELECTING_OFFER):
                transition(session, SessionState.SELECTING_OFFER)
            return {
                "reply": "已取消登录授权。回复序号可重新选择平台，或回复“取消”回到盯盘。",
                "action": "credential_cancelled",
                "state": session.state,
            }

        if session.credential_step == "account":
            combined_account = re.search(r"账号[:：\s]*([^\s，,。]+)", text)
            combined_secret = re.search(r"(?:密码|口令)[:：\s]*([^\s，。]+)", text)
            if combined_account and combined_secret:
                return self._save_credential(
                    session, order, combined_account.group(1), combined_secret.group(1), text
                )
            account = re.sub(r"^(?:账号|账户)[:：\s]*", "", text.strip())
            if not account:
                return {
                    "reply": f"请回复 {label} 的登录账号（回复“取消”可退出）：",
                    "action": "credential_pending",
                    "state": session.state,
                }
            session.credential_account = account
            session.credential_step = "secret"
            return {
                "reply": f"请输入 {label} 的登录密码/令牌（回复“跳过”只保存账号别名）：",
                "action": "credential_pending",
                "state": session.state,
            }

        if session.credential_step == "secret":
            secret = "" if "跳过" in text else text.strip()
            info = self.purchase.save_credential(
                session.user_id,
                platform,
                session.credential_account,
                secret,
                auth_type="encrypted_session" if secret else "alias_only",
            )
            session.credential_step = ""
            session.credential_account = ""
            return {
                "reply": self._credential_saved_reply(order, info),
                "action": "credential_saved",
                "state": session.state,
                "credential": info,
                "order": order.to_dict(),
            }

        # 兼容一次说全：账号 xxx 密码 yyy
        account_match = re.search(r"账号[:：\s]*([^\s，,。]+)", text)
        secret_match = re.search(r"(?:密码|口令)[:：\s]*([^\s，。]+)", text)
        if account_match:
            return self._save_credential(
                session,
                order,
                account_match.group(1),
                secret_match.group(1) if secret_match else "",
                text,
            )
        return None

    def _save_credential(
        self,
        session: ChatSession,
        order: Order,
        account: str,
        secret: str,
        text: str,
    ) -> dict[str, Any]:
        platform = order.platform
        for name, key in PLATFORM_NAMES.items():
            if name in text:
                platform = key
                break
        info = self.purchase.save_credential(
            session.user_id,
            platform,
            account,
            secret,
            auth_type="encrypted_session" if secret else "alias_only",
        )
        session.credential_step = ""
        session.credential_account = ""
        return {
            "reply": self._credential_saved_reply(order, info),
            "action": "credential_saved",
            "state": session.state,
            "credential": info,
            "order": order.to_dict(),
        }

    @staticmethod
    def _credential_saved_reply(order: Order, info: dict[str, Any]) -> str:
        platform = info["platform"]
        label = PLATFORM_LABELS.get(platform, platform)
        return "\n".join(
            [
                f"=== 凭据已保存 · {label} ===",
                f"账号：{info['account_alias']}"
                + ("（敏感信息已加密）" if info["encrypted"] else "（仅保存脱敏别名）"),
                f"下单深链：{order.deep_link}",
                "完成支付后回复“已支付 订单号 XXX”，我会记录购买并设置出发提醒。",
            ]
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _is_receipt(text: str) -> bool:
        return any(word in text for word in RECEIPT_HINTS)

    @staticmethod
    def _watch_choice(text: str) -> str | None:
        raw = text.strip()
        if raw in ("1", "１") or any(word in raw for word in ("现在购买", "立即购买", "现在买")):
            return "buy"
        if raw in ("2", "２") or any(word in raw for word in ("继续观望", "观望", "继续盯")):
            return "wait"
        if raw in ("3", "３") or any(word in raw for word in ("优惠券", "查看券")):
            return "coupons"
        if raw in ("4", "４") or any(word in raw for word in ("停止盯盘", "停止", "不盯了")):
            return "stop"
        return None

    @staticmethod
    def _ready_choice(text: str) -> str | None:
        raw = text.strip()
        if raw in ("1", "１") or any(word in raw for word in ("启动", "开始", "盯盘")):
            return "start"
        if raw in ("2", "２") or any(word in raw for word in ("修改", "改条件", "重来")):
            return "modify"
        if raw in ("3", "３") or any(word in raw for word in CANCEL_WORDS):
            return "cancel"
        return None

    async def _watch_action(self, session: ChatSession, choice: str) -> dict[str, Any]:
        if choice == "buy":
            return self._flow_confirm(
                session,
                InteractionResult(session.category, session.intent, action="confirm"),
            )
        if choice == "wait":
            return self._payload(
                session,
                InteractionResult(session.category, session.intent, action="watch"),
                reply="继续盯盘中，命中合适买点我会提醒你。\n\n" + WATCH_MENU,
            )
        if choice == "coupons":
            return self._payload(
                session,
                InteractionResult(session.category, session.intent, action="coupons"),
                reply=self._format_coupons(session.category) + "\n\n" + WATCH_MENU,
            )
        return self._flow_back_to_watch(
            session, "已停止盯盘。可以告诉我新的条件，或重新启动盯盘。", restart=True
        )

    async def _ready_action(self, session: ChatSession, choice: str) -> dict[str, Any]:
        if choice == "start":
            return await self._flow_start_watch(session)
        if choice == "modify":
            if can_transition(session.state, SessionState.COLLECTING):
                transition(session, SessionState.COLLECTING)
            return self._payload(
                session,
                InteractionResult(session.category, session.intent, action="modify"),
                reply="请告诉我要修改的条件，例如“改成11月9号”“预算1500”。",
            )
        booking = self._current_booking(session)
        if booking is not None:
            if self.watcher.status(booking.id) is not None:
                self.watcher.stop(booking.id)
            self.repos.bookings.update(booking.id, status="cancelled")
        session.booking_id = None
        session.intent = None
        session.category = None
        session.offers = []
        session.last_decision = None
        if can_transition(session.state, SessionState.COLLECTING):
            transition(session, SessionState.COLLECTING)
        return self._payload(
            session,
            InteractionResult(None, None, action="cancel"),
            reply="已取消本次需求。随时告诉我新的品类、地点和时间。",
        )

    def _flow_back_to_watch(
        self,
        session: ChatSession,
        reply: str,
        restart: bool = False,
    ) -> dict[str, Any]:
        if can_transition(session.state, SessionState.WATCHING):
            transition(session, SessionState.WATCHING)
        elif can_transition(session.state, SessionState.READY):
            transition(session, SessionState.READY)
        booking = self._current_booking(session)
        if restart and booking is not None:
            self.repos.bookings.update(booking.id, status="ready")
        text = reply + ("\n\n" + READY_MENU if session.state == SessionState.READY.value else "\n\n" + WATCH_MENU)
        return self._payload(
            session,
            InteractionResult(session.category, session.intent, action="watch"),
            reply=text,
        )

    def _format_coupons(self, category: str | None) -> str:
        coupons = self.repos.coupons.active(category) if category else []
        if not coupons:
            return "当前没有可用优惠券，我会继续留意。"
        lines = ["=== 各平台优惠券 ==="]
        for index, coupon in enumerate(coupons[:10], start=1):
            if coupon.coupon_type == "amount":
                detail = f"满{coupon.min_spend:.0f}减{coupon.amount:.0f}"
            else:
                detail = f"{coupon.discount_rate:.0%} 折扣"
            expiry = f" · 有效期至 {coupon.valid_to[:10]}" if coupon.valid_to else ""
            title = coupon.title or detail
            lines.append(f"[{index}] {PLATFORM_LABELS.get(coupon.platform, coupon.platform)} · {title}{expiry}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _ensure_booking(self, session: ChatSession, intent: PurchaseIntent) -> Booking:
        existing = self._current_booking(session)
        strategy = self.registry.get(intent.category)
        window = strategy.optimal_window(intent.slots)
        if existing is not None:
            self.repos.bookings.update(
                existing.id,
                title=strategy.title(intent),
                slots=intent.slots,
                budget=intent.budget,
                status="collecting",
                window_start=window[0],
                window_end=window[1],
                attributes={
                    **existing.attributes,
                    "preferences": intent.preferences,
                },
            )
            updated = self.repos.bookings.get(existing.id)
            return updated or existing
        booking = Booking(
            id=new_id("b"),
            user_id=session.user_id,
            category=intent.category,
            title=strategy.title(intent),
            slots=dict(intent.slots),
            attributes={"preferences": dict(intent.preferences)},
            budget=intent.budget,
            status="collecting",
            window_start=window[0],
            window_end=window[1],
        )
        self.repos.bookings.create(booking)
        session.booking_id = booking.id
        return booking

    def _current_booking(self, session: ChatSession) -> Booking | None:
        if session.booking_id:
            booking = self.repos.bookings.get(session.booking_id)
            if booking is not None:
                return booking
        bookings = self.repos.bookings.list(session.user_id, limit=1)
        if bookings:
            session.booking_id = bookings[0].id
            return bookings[0]
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _decision_line(decision: Any, offers: list[Offer]) -> str:
        labels = {
            "buy_now": "建议立即购买",
            "watch": "建议观望",
            "no_buy": "超预算，暂不购买",
            "no_go": "数据不足，暂不出手",
        }
        platform = PLATFORM_LABELS.get(offers[0].platform, offers[0].platform) if offers else "-"
        window = ""
        if decision.optimal_window:
            start = str(decision.optimal_window[0])[:10]
            if start:
                window = f"｜最优窗口 {start}"
        return (
            f"[盯盘] 当前最低 ¥{decision.current_effective_price:.2f}（{platform}）"
            f"｜预测 ¥{decision.predicted_median:.2f}"
            f"{window}"
            f"｜{labels.get(decision.recommendation, decision.recommendation)}"
        )

    @staticmethod
    def _format_platforms(offers: list[Offer]) -> str:
        lines = []
        for index, offer in enumerate(offers, start=1):
            name = PLATFORM_LABELS.get(offer.platform, offer.platform)
            coupon = f" - 券 ¥{offer.discount:.0f}" if offer.discount else ""
            lines.append(f"[{index}] {name}  ¥{offer.price:.0f}{coupon} = ¥{offer.paid_price:.2f}")
        return "\n".join(lines)

    @staticmethod
    def _platform_offers(offers: list[Offer]) -> list[Offer]:
        seen: dict[str, Offer] = {}
        for offer in offers:
            seen.setdefault(offer.platform, offer)
        return sorted(seen.values(), key=lambda item: item.paid_price)

    @staticmethod
    def _format_decision(decision: Any) -> str:
        labels = {
            "buy_now": "建议立即购买",
            "watch": "建议继续观察",
            "no_buy": "超出预算，暂不购买",
            "no_go": "数据不足/异常，暂不出手",
        }
        lines = [
            f"【决策】{labels.get(decision.recommendation, decision.recommendation)}",
            f"当前最低实付 ¥{decision.current_effective_price:.2f}；"
            f"预测中位 ¥{decision.predicted_median:.2f}；"
            f"评分 {decision.decision_score:.2f}；置信度 {decision.confidence:.2f}",
            f"理由：{'；'.join(decision.reasons[:4])}",
        ]
        if decision.guardrail_reasons:
            lines.append(f"硬约束：{'；'.join(decision.guardrail_reasons)}")
        return "\n".join(lines)

    @staticmethod
    def _format_offers(offers: list[Offer], title: str = "可选方案（按实付价升序）") -> str:
        if not offers:
            return "暂无可比报价。"
        lines = [f"{title}："]
        for index, offer in enumerate(offers, start=1):
            coupon = f"，券：{offer.coupon_title}" if offer.coupon_title else "，无可用券"
            unit = ""
            total_paid = offer.attributes.get("total_paid")
            if offer.category == "moutai" and total_paid:
                unit = f"，合计 ¥{float(total_paid):.2f}"
            stock = "" if offer.in_stock else "（缺货）"
            name = PLATFORM_LABELS.get(offer.platform, offer.platform)
            lines.append(
                f"{index}. {name}{stock} · {offer.item_name} · 标价 ¥{offer.price:.2f}"
                f" · 优惠 ¥{offer.discount:.2f} · 实付 ¥{offer.paid_price:.2f}{unit}{coupon}"
            )
        return "\n".join(lines)

    def _payload(
        self,
        session: ChatSession,
        result: InteractionResult | None = None,
        reply: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session.id,
            "state": session.state,
            "category": session.category,
            "action": result.action if result else "continue",
            "reply": reply if reply is not None else (result.reply if result else ""),
            "missing": list(result.missing) if result else [],
            "intent": session.intent.to_dict() if session.intent else None,
            "booking_id": session.booking_id,
            "order_id": session.order_id,
            "decision": session.last_decision.to_dict() if session.last_decision else None,
            "offers": [offer.to_dict() for offer in session.offers],
            "created_at": now_iso(),
        }
        return payload
