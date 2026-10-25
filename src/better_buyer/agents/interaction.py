"""交互智能体：中文槽位抽取、追问与意图澄清。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ..categories.flight import CITY_ALIASES
from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..core.datetime_utils import (
    parse_budget,
    parse_cabin,
    parse_date_text,
    parse_quantity,
    parse_time_preference,
    today,
)
from ..models import ChatSession, InteractionResult, PurchaseIntent, parse_iso
from ..services.model_router import ModelRouter

CATEGORY_KEYWORDS = {
    # 先匹配更具体的品类，避免“飞天茅台”被“飞”误判为机票
    "moutai": ("茅台", "白酒", "飞天", "生肖酒"),
    "hotel": ("酒店", "住宿", "入住", "房型", "民宿"),
    "flight": ("机票", "航班", "飞机", "机场", "飞往", "飞"),
    "custom_example": ("示例品类",),
}

CONFIRM_WORDS = ("确认购买", "确认下单", "确认", "就买", "买吧", "下单", "可以买", "好的买", "买了", "成交")
EXIT_WORDS = ("退出", "结束", "再见", "晚安", "不聊了")
RECEIPT_WORDS = ("回执", "已支付", "支付完成", "付款完成", "订单号")

# 已进入盯盘/待确认阶段后，只有带这些信号的话才视为修改需求
CHANGE_HINTS = (
    "改", "换", "重新", "预算", "机票", "航班", "酒店", "住宿", "茅台", "飞",
    "去", "到", "出发", "入住", "订", "买", "日期", "时候",
)

SMALL_TALK_REPLIES = {
    "Watching": "好嘞，后台还在盯盘，有买点会第一时间提醒你。也可以回复 1 现在购买、3 查看优惠券、4 停止盯盘。",
    "Ready": "记下了。条件没问题就回复 1 启动后台盯盘；想改条件直接说。",
    "Completed": "好的。想买新的，直接告诉我品类、出发地、目的地和时间即可。",
    "default": "我在听。想订机票、酒店或茅台，直接说地点、时间就好。",
}


class InteractionAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        router: ModelRouter | None = None,
    ):
        self.settings = settings
        self.registry = registry
        self.router = router

    # ------------------------------------------------------------------
    async def handle(self, text: str, session: ChatSession) -> InteractionResult:
        raw = (text or "").strip()
        if not raw:
            return InteractionResult(session.category, session.intent, reply="我在听，请告诉我你的需求。")

        if any(word in raw for word in EXIT_WORDS):
            return InteractionResult(
                session.category, session.intent, reply="已退出本次对话，盯盘任务会保留。", action="exit"
            )

        # 盯盘/待确认阶段允许闲聊：没有修改需求的信号就当作普通对话
        if (
            session.state in ("Watching", "Ready", "Completed")
            and not re.fullmatch(r"\d", raw)
            and not self._looks_like_change(raw)
        ):
            return InteractionResult(
                session.category,
                session.intent,
                reply=SMALL_TALK_REPLIES.get(session.state, SMALL_TALK_REPLIES["default"]),
                action="chat",
            )

        if session.offers:
            index = self._select_offer(raw)
            if index is not None:
                return InteractionResult(
                    session.category,
                    session.intent,
                    reply=f"已选择方案 {index + 1}，正在生成订单与深链。",
                    action="select_offer",
                    selected_offer=index,
                )

        if any(word in raw for word in RECEIPT_WORDS) and session.order_id:
            order_no = self._extract_order_no(raw)
            return InteractionResult(
                session.category,
                session.intent,
                reply="收到支付回执，正在记录订单。",
                action="receipt",
                receipt={"order_no": order_no},
            )

        if session.booking_id and any(word in raw for word in CONFIRM_WORDS):
            return InteractionResult(
                session.category,
                session.intent,
                reply="好的，正在实时刷新价格与可用券。",
                action="confirm",
            )

        category = self._detect_category(raw, session.category)
        if category is None:
            if session.category is None:
                return InteractionResult(
                    None,
                    None,
                    reply="想买什么？我可以帮你盯机票、酒店或茅台。比如：下月8号北京去上海，预算2000。",
                    action="chat",
                )
            category = session.category

        slots: dict[str, Any] = {}
        if session.intent and session.intent.category == category:
            slots.update(session.intent.slots)
        budget = session.intent.budget if session.intent and session.intent.category == category else None

        if category == "flight":
            self._parse_flight(raw, slots)
        elif category == "hotel":
            self._parse_hotel(raw, slots)
        elif category == "moutai":
            self._parse_moutai(raw, slots)
        elif category == "custom_example":
            self._parse_custom(raw, slots)
        parsed_budget = parse_budget(raw)
        if parsed_budget is not None:
            budget = parsed_budget
        if "budget" in slots:
            try:
                budget = float(slots.pop("budget"))
            except (TypeError, ValueError):
                pass

        # 可选：NLU 模型补全本地规则未识别的槽位
        if self.router is not None and category in self.registry.strategies:
            try:
                nlu = await self.router.extract_slots(
                    category, raw, self.registry.get(category).slot_schema()
                )
                if nlu:
                    for key, value in (nlu.get("slots") or {}).items():
                        if key in self.registry.get(category).slot_specs and key not in slots and value:
                            slots[key] = value
                    if nlu.get("budget") and budget is None:
                        budget = float(nlu["budget"])
            except Exception:
                pass

        strategy = self.registry.maybe_get(category)
        if strategy is None:
            return InteractionResult(
                None,
                None,
                reply=f"品类 {category} 未启用，请换一个品类。",
                action="chat",
            )

        slots = strategy.normalize_slots(slots)
        issues = strategy.validate_slots(slots)
        fatal_issues = [issue for issue in issues if issue.level == "fatal"]
        if fatal_issues:
            issue = fatal_issues[0]
            return InteractionResult(
                category,
                None,
                missing=[issue.field] if issue.field else [],
                reply=f"{issue.message}。请重新告诉我。",
                action="continue",
                slot_updates=slots,
            )

        intent = PurchaseIntent(
            category=category,
            slots=slots,
            budget=budget if budget is not None else slots.get("budget"),
            preferences={"preferred_platforms": [p for p in (session.intent.preferences.get("preferred_platforms", []) if session.intent else [])]},
        )
        missing = [key for key in strategy.required_slots if not slots.get(key)]

        if missing:
            spec = strategy.slot_specs[missing[0]]
            return InteractionResult(
                category,
                intent,
                missing=missing,
                reply=spec.ask or f"请补充：{spec.title}",
                action="continue",
                slot_updates=slots,
            )

        summary = strategy.title(intent)
        tip = f"，预算 {budget:.0f}" if budget else ""
        return InteractionResult(
            category,
            intent,
            missing=[],
            reply=f"已确认：{summary}{tip}。",
            action="ready",
            slot_updates=slots,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_change(text: str) -> bool:
        return any(word in text for word in CHANGE_HINTS) or parse_budget(text) is not None

    @staticmethod
    def _detect_category(text: str, current: str | None) -> str | None:
        for key, words in CATEGORY_KEYWORDS.items():
            if any(word in text for word in words):
                return key
        return current

    @staticmethod
    def _select_offer(text: str) -> int | None:
        if not text:
            return None
        compact = text.strip()
        if re.fullmatch(r"(?:选|选择)?\s*第?\s*([1-9])\s*(?:个|号|项)?", compact):
            match = re.match(r"(?:选|选择)?\s*第?\s*([1-9])", compact)
            return int(match.group(1)) - 1 if match else None
        match = re.search(r"第\s*([1-9])\s*(?:个|号|项|方案)", compact)
        return int(match.group(1)) - 1 if match else None

    @staticmethod
    def _extract_order_no(text: str) -> str:
        match = re.search(r"([A-Za-z0-9][A-Za-z0-9\-]{3,})", text)
        return match.group(1) if match else ""

    # ------------------------------------------------------------------
    def _parse_flight(self, text: str, slots: dict[str, Any]) -> None:
        mentions = self._city_mentions(text)
        if mentions:
            if len(mentions) >= 2:
                slots["origin"], slots["destination"] = mentions[0], mentions[1]
            else:
                city = mentions[0]
                if any(word in text for word in ("从", "出发")):
                    slots["origin"] = city
                elif any(word in text for word in ("去", "到", "飞往", "飞")):
                    slots["destination"] = city
                elif not slots.get("origin"):
                    slots["origin"] = city
                else:
                    slots["destination"] = city
        dates = self._extract_dates(text)
        if dates:
            slots["depart_date"] = dates[0].isoformat()
        time_pref = parse_time_preference(text)
        if time_pref:
            slots["depart_time_pref"] = time_pref
        cabin = parse_cabin(text)
        if cabin:
            slots["cabin"] = cabin
        if "往返" in text:
            slots["trip_type"] = "往返"

    def _parse_hotel(self, text: str, slots: dict[str, Any]) -> None:
        mentions = self._city_mentions(text)
        if mentions:
            slots["city"] = mentions[0]
        dates = self._extract_dates(text)
        if dates:
            slots["checkin_date"] = dates[0].isoformat()
            if len(dates) >= 2:
                slots["checkout_date"] = dates[1].isoformat()
        nights = re.search(r"住\s*(\d+)\s*晚", text)
        if nights and slots.get("checkin_date"):
            checkin = date.fromisoformat(slots["checkin_date"])
            slots["checkout_date"] = (checkin + timedelta(days=int(nights.group(1)))).isoformat()
        guests = re.search(r"(\d+)\s*(?:人|位)", text)
        if guests:
            slots["guests"] = int(guests.group(1))
        star = re.search(r"([三四五])\s*星", text)
        if star:
            slots["star_level"] = {"三": "三星", "四": "四星", "五": "五星"}[star.group(1)]
        if "大床" in text:
            slots["room_type"] = "大床房"
        elif "双床" in text:
            slots["room_type"] = "双床房"

    def _parse_moutai(self, text: str, slots: dict[str, Any]) -> None:
        if "飞天" in text:
            slots["sku"] = "飞天茅台53度500ml"
        elif "生肖" in text:
            slots["sku"] = "茅台生肖酒"
        elif "五星" in text:
            slots["sku"] = "五星茅台53度500ml"
        elif "茅台" in text and not slots.get("sku"):
            slots["sku"] = "茅台"
        slots["quantity"] = parse_quantity(text, int(slots.get("quantity") or 1))
        target = re.search(r"(?:心理价|低于|不超过)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?", text)
        if target:
            slots["target_unit_price"] = float(target.group(1))
        budget = parse_budget(text)
        if budget is not None:
            slots["budget"] = budget

    def _parse_custom(self, text: str, slots: dict[str, Any]) -> None:
        match = re.search(r"(?:买|要|来)\s*(.{1,20}?)(?:，|,|。|$)", text)
        if match:
            slots["item_id"] = match.group(1).strip()
        slots["quantity"] = parse_quantity(text, int(slots.get("quantity") or 1))

    # ------------------------------------------------------------------
    def _city_mentions(self, text: str) -> list[str]:
        lowered = (text or "").lower()
        aliases = sorted(CITY_ALIASES, key=len, reverse=True)
        found: list[str] = []
        index = 0
        while index < len(lowered):
            matched = None
            for alias in aliases:
                if lowered.startswith(alias, index):
                    matched = alias
                    break
            if matched:
                city = CITY_ALIASES[matched]
                if not found or found[-1] != city:
                    found.append(city)
                index += len(matched)
            else:
                index += 1
        return found

    @staticmethod
    def _extract_dates(text: str) -> list[date]:
        pattern = re.compile(
            r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}[日号]?"
            r"|\d{1,2}月\d{1,2}[日号]?"
            r"|下个?月\s*\d{1,2}\s*[日号]?"
            r"|下周[一二三四五六日天]"
            r"|今天|今天|明天|明日|后天|大后天"
            r"|\d+\s*天后"
            r"|\d{1,2}\s*[日号])"
        )
        dates: list[date] = []
        for match in pattern.finditer(text):
            parsed = parse_date_text(match.group(0), today())
            if parsed and parsed not in dates:
                dates.append(parsed)
        return dates
