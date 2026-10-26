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
from ..services.intent import (
    ADAPT_THRESHOLD,
    IntentClassifier,
    SECOND_FLOOR,
)
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

# 自然闲聊里可识别的常见出发/目的地城市
CITY_HINTS = (
    "北京", "上海", "广州", "深圳", "成都", "杭州", "西安", "重庆", "南京",
    "武汉", "长沙", "厦门", "青岛", "天津", "昆明", "三亚", "香港", "南京",
)

DECISION_LABELS = {
    "buy_now": "建议立即购买",
    "watch": "建议观望",
    "no_buy": "暂不购买",
    "no_go": "数据不足",
}


class InteractionAgent:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        router: ModelRouter | None = None,
        watcher: Any = None,
    ):
        self.settings = settings
        self.registry = registry
        self.router = router
        self.watcher = watcher
        self.threshold = getattr(settings, "intent_threshold", 0.80)
        self.classifier = IntentClassifier(self.threshold, router)
        self.adapt_threshold = ADAPT_THRESHOLD
        self.second_floor = SECOND_FLOOR
        self.decay_threshold = getattr(settings, "intent_decay_threshold", 0.45)

    # ------------------------------------------------------------------
    async def handle(self, text: str, session: ChatSession) -> InteractionResult:
        raw = (text or "").strip()
        if not raw:
            return InteractionResult(session.category, session.intent, reply="我在听，请告诉我你的需求。")

        if any(word in raw for word in EXIT_WORDS):
            return InteractionResult(
                session.category, session.intent, reply="已退出本次对话，盯盘任务会保留。", action="exit"
            )

        # 取消盯盘的二次确认：先拦截，避免走进普通流程
        if session.pending_cancel_task:
            resolved = self._resolve_pending_cancel(raw, session)
            if resolved is not None:
                return resolved

        # 系统状态查询：盯盘任务数 / 取消盯盘 / skill（品类）数
        query_reply = self._detect_system_query(raw, session)
        if query_reply is not None:
            return InteractionResult(
                session.category, session.intent, reply=query_reply, action="chat"
            )

        # 盯盘/待确认/已完成阶段：先做意图重预测 + 自然闲聊，不再硬套固定话术
        if (
            session.state in ("Watching", "Ready", "Completed")
            and not re.fullmatch(r"\d", raw)
            and not self._looks_like_change(raw)
        ):
            external = await self._handle_external(raw, session)
            if external is not None:
                return external

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

        # ===== 实时意图预测（需求：先确认真实品类）=====
        conf = await self.classifier.classify(raw)
        session.intent_scores = conf

        # 尚未提交品类：命中置信度阈值才进入收集环节（需求1）
        if session.intent_category is None:
            return await self._enter_intent(raw, session, conf)

        # 已提交品类：处理切换 / 掉置信度继续确认 / 收集槽位（需求3/4）
        return await self._update_intent(raw, session, conf)

    # ------------------------------------------------------------------
    # 盯盘/待确认/已完成阶段的意图重预测与自然闲聊
    # ------------------------------------------------------------------
    async def _handle_external(
        self, raw: str, session: ChatSession
    ) -> InteractionResult | None:
        """在 Watching/Ready/Completed 下处理普通对话：先重预测意图，命中新品类则转去收集；
        否则返回自然闲聊。返回 None 表示应继续走主流程（如修改条件）。"""
        conf = await self.classifier.classify(raw)
        session.intent_scores = conf

        # 需求：启动盯盘后仍可进行意图重预测——出现新的明确品类 → 开启新一轮收集
        new_best, new_conf = IntentClassifier.best(conf, exclude=session.intent_category)
        if new_best is not None and new_conf >= self.threshold:
            session.intent_category = new_best
            session.intent_confidence = 0.99
            session.category = new_best
            session.pending_confirm = False
            session.offers = []
            session.booking_id = None
            session.order_id = None
            session.intent = PurchaseIntent(category=new_best, slots={})
            session.intent_scores[new_best] = 0.99
            return await self._collect_slots(raw, session, new_best)

        # 未检测到新意图 → 自然回应（不再重复固定菜单话术）
        if session.intent_category:
            # 已确认/盯盘中的意图置信度保持在高位，不受闲聊影响
            session.intent_scores[session.intent_category] = session.intent_confidence or 0.99
        return InteractionResult(
            session.category,
            session.intent,
            reply=self._make_chat_reply(raw, session),
            action="chat",
        )

    def _make_chat_reply(self, raw: str, session: ChatSession) -> str:
        label = self._category_label(session.intent_category or session.category) or "购买"
        if any(w in raw for w in ("好玩", "景点", "推荐", "攻略", "美食", "去哪", "吃什么")):
            city = next((c for c in CITY_HINTS if c in raw), None)
            head = f"{city}值得去的地方不少，中山陵、博物院这类都挺不错的。" if city else "那边值得逛的地方不少。"
            return f"{head}你的{label}还在盯着，有买点会第一时间提醒你；要顺带看看酒店或改条件，也直接说。"
        pool = [
            f"嗯，我在听。你的{label}还在后台盯盘，有买点会第一时间提醒你。",
            f"好的呀。想继续聊就聊，盯盘不耽误；需要改条件或再买点别的，一起说就行。",
            f"收到～正在后台盯着价格呢，有合适买点马上叫你。其它想法随时讲。",
            f"明白。盯盘照常进行，你随便聊；说说看还要不要一起看看别的。",
        ]
        return pool[sum(ord(ch) for ch in raw) % len(pool)]

    # ------------------------------------------------------------------
    # 意图预测与收集环节
    # ------------------------------------------------------------------
    async def _enter_intent(
        self, raw: str, session: ChatSession, conf: dict[str, float]
    ) -> InteractionResult:
        best, best_conf = IntentClassifier.best(conf)
        if best is not None and best_conf >= self.threshold:
            session.intent_category = best
            session.intent_confidence = 0.99
            session.category = best
            session.proposed_category = None
            session.pending_confirm = False
            session.intent = PurchaseIntent(category=best, slots={})
            session.intent_scores[best] = 0.99
            # 提交意图后立即从当前消息继续收集参数（条件齐全则直接进入 Ready）
            return await self._collect_slots(raw, session, best)

        # 用户确认上一轮引导提案的意图（如"对/嗯"），直接提升置信度
        proposed = session.proposed_category
        session.proposed_category = None
        if proposed is not None and IntentClassifier.is_continue(raw):
            session.intent_category = proposed
            session.intent_confidence = 0.99
            session.category = proposed
            session.pending_confirm = False
            session.intent = PurchaseIntent(category=proposed, slots={})
            session.intent_scores[proposed] = 0.99
            return await self._collect_slots(raw, session, proposed)

        # 无明确品类：根据意图置信度做话术引导（需求2/3）
        return await self._guided_reply(raw, session, conf)

    async def _guided_reply(
        self, raw: str, session: ChatSession, conf: dict[str, float]
    ) -> InteractionResult:
        ranked = IntentClassifier.top(conf, limit=2, floor=0.15)
        if not ranked:
            # 完全没有意图信号
            if IntentClassifier.is_small_talk(raw):
                return InteractionResult(
                    session.category,
                    session.intent,
                    reply="我在听。想订机票、酒店或茅台，直接说地点、时间就好。",
                    action="chat",
                )
            return InteractionResult(
                None,
                None,
                reply="想买什么？我可以帮你盯机票、酒店或茅台。比如：下月8号北京去上海，预算2000。",
                action="chat",
            )

        lead, lead_conf = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        # 需求3：顶部意图置信度 >40% 时，围绕该意图引导，不再罗列无关品类
        if lead_conf >= self.adapt_threshold:
            session.proposed_category = lead  # 支持"对/嗯"一键确认主意图
            if second is not None and second[1] >= self.second_floor:
                reply = f"你是想订{self._category_label(lead)}还是{self._category_label(second[0])}？"
            else:
                reply = f"是想订{self._category_label(lead)}吗？"
            return InteractionResult(
                session.category, session.intent, reply=reply, action="chat"
            )

        # 有弱信号但未到阈值：列出可能品类请用户确认
        names = "、".join(self._category_label(cat) for cat, _ in ranked)
        return InteractionResult(
            session.category,
            session.intent,
            reply=f"我猜你可能是想订{names}，方便确认一下吗？",
            action="chat",
        )

    async def _update_intent(
        self, raw: str, session: ChatSession, conf: dict[str, float]
    ) -> InteractionResult:
        committed = session.intent_category
        assert committed is not None
        committed_conf = conf.get(committed, 0.0)
        other_best, other_conf = IntentClassifier.best(conf, exclude=committed)

        # 另一品类置信度达标且更高 → 切换意图
        if other_best is not None and other_conf >= self.threshold and other_conf > committed_conf:
            session.intent_category = other_best
            session.intent_confidence = 0.99
            session.category = other_best
            session.pending_confirm = False
            session.offers = []
            session.booking_id = None
            session.order_id = None
            session.intent = PurchaseIntent(category=other_best, slots={})
            session.intent_scores[other_best] = 0.99
            return await self._collect_slots(raw, session, other_best)

        # 上一轮询问"是否继续"后的确认
        if session.pending_confirm:
            session.pending_confirm = False
            if IntentClassifier.is_continue(raw):
                session.intent_confidence = max(committed_conf, self.threshold)
                return await self._collect_slots(raw, session, committed)
            if other_best is not None and other_conf >= self.threshold:
                session.intent_category = other_best
                session.intent_confidence = 0.99
                session.category = other_best
                session.offers = []
                session.booking_id = None
                session.order_id = None
                session.intent = PurchaseIntent(category=other_best, slots={})
                session.intent_scores[other_best] = 0.99
                return await self._collect_slots(raw, session, other_best)
            return InteractionResult(
                committed,
                session.intent,
                reply="好的，那我们按之前的来。继续告诉我具体需求即可。",
                action="chat",
            )

        # 需求1：是否在给当前已确认意图补/改参数——此时绝不询问方向，且置信度锚定在高位（99%）
        collecting_input = (
            self._mentions(raw, committed)
            or committed_conf >= self.threshold
            or self._has_slot_info(raw)
        )
        if collecting_input:
            session.intent_confidence = max(session.intent_confidence, 0.99)
            session.intent_scores[committed] = session.intent_confidence
            return await self._collect_slots(raw, session, committed)

        # 纯闲聊：不打断，也不触发方向询问，置信度保持不变
        if IntentClassifier.is_small_talk(raw):
            return InteractionResult(
                committed,
                session.intent,
                reply="好嘞，我在听。继续告诉我条件，或者有其它想说的也可以。",
                action="chat",
            )

        # 其它内容：结合本条消息重新评估已确认意图置信度（历史衰退）
        session.intent_confidence = committed_conf
        session.intent_scores[committed] = committed_conf

        # 需求2：衰落实质低于 45% 且有其它品类信号时才询问是否换个方向
        if session.intent_confidence < self.decay_threshold and other_best is not None:
            session.pending_confirm = True
            return InteractionResult(
                committed,
                session.intent,
                reply=f"你似乎想换个方向？还要继续{self._category_label(committed)}吗？回复“继续”即可，或直接说新需求。",
                action="chat",
            )

        # 未跌破阈值：继续收集
        return await self._collect_slots(raw, session, committed)

    @staticmethod
    def _mentions(raw: str, category: str) -> bool:
        names = {
            "flight": ("机票", "航班", "飞机", "直飞", "飞往"),
            "hotel": ("酒店", "住宿", "宾馆", "民宿", "订房", "入住"),
            "moutai": ("茅台", "白酒", "飞天"),
        }
        return any(word in raw for word in names.get(category, ()))

    @staticmethod
    def _has_slot_info(raw: str) -> bool:
        """判断消息是否包含当前品类的槽位信息（城市/日期/预算/舱位/时间/数量）。"""
        if parse_budget(raw) is not None or parse_time_preference(raw) or parse_cabin(raw):
            return True
        if InteractionAgent._extract_dates(raw):
            return True
        if re.search(r"\d+", raw):
            return True
        if any(city in raw for city in {
            "北京", "上海", "广州", "深圳", "成都", "杭州", "西安", "重庆",
            "南京", "武汉", "长沙", "厦门", "青岛", "天津", "昆明", "三亚",
        }):
            return True
        return False

    async def _collect_slots(
        self, raw: str, session: ChatSession, category: str
    ) -> InteractionResult:
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

        # 闲聊兜底：消息未带来任何新增槽位且为聊天短语 → 保持收集（需求3）
        if not self._slots_gained(raw, slots, session) and IntentClassifier.is_small_talk(raw):
            return InteractionResult(
                category,
                session.intent,
                reply="好嘞，继续补充信息，或者告诉我其它想定的都可以。",
                action="chat",
            )

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

        pref_sources = session.intent.preferences.get("preferred_platforms", []) if session.intent else []
        intent = PurchaseIntent(
            category=category,
            slots=slots,
            budget=budget if budget is not None else slots.get("budget"),
            preferences={"preferred_platforms": list(pref_sources)},
        )
        session.intent = intent
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

        # 条件齐全 → 进入 ready（由主智能体询问是否启动盯盘）
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

    @staticmethod
    def _category_label(category: str) -> str:
        return {
            "flight": "机票",
            "hotel": "酒店",
            "moutai": "茅台",
            "custom_example": "示例品类",
        }.get(category, category)

    @staticmethod
    def _slots_gained(raw: str, slots: dict[str, Any], session: ChatSession) -> bool:
        prev = dict(session.intent.slots) if session.intent and session.intent.category else {}
        if any(slots.get(k) and slots.get(k) != prev.get(k) for k in set(slots) | set(prev)):
            return True
        if parse_time_preference(raw) or parse_cabin(raw) or parse_budget(raw) is not None:
            return True
        if InteractionAgent._extract_dates(raw) or re.search(r"住\s*\d+\s*晚|(\d+)\s*(?:人|位)", raw):
            return True
        if "往返" in raw:
            return True
        return False

    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_change(text: str) -> bool:
        return any(word in text for word in CHANGE_HINTS) or parse_budget(text) is not None

    def _detect_system_query(self, raw: str, session: ChatSession) -> str | None:
        text = (raw or "").strip()
        # 取消指定的后台盯盘任务：如「取消盯盘 2」「取消任务 w_xxx」，需二次确认
        if ("取消" in text or "停止" in text) and ("盯盘" in text or "任务" in text):
            if not self.watcher:
                return "当前没有可操作的后台盯盘任务。"
            task_id = self._resolve_task_id(text)
            if task_id is None:
                return "可以这样取消盯盘任务：\"取消任务 1\" 或 \"取消任务 w_1a2b3c\"（先回复\"有几个盯盘任务\"查看编号）。"
            task = self.watcher.describe_one(task_id)
            if task is None:
                return f"未找到正在运行的盯盘任务 {task_id}。可用“有几个盯盘任务”查看编号。"
            desc = task.get("title") or task_id
            session.pending_cancel_task = task_id
            return (
                f"确定要停止盯盘任务 {task_id}（{desc}）吗？\n"
                f"回复「确认删除」（或“取消任务 {task_id}”）停止该任务；回复「保留」则不删除。"
            )
        # 后台盯盘任务：查数量或列出当前任务，返回带编号与状态的描述
        if ("盯盘" in text or "后台" in text or "任务" in text) and any(
            word in text for word in ("几个", "多少", "哪些", "什么", "都有", "有哪些", "列出", "看看")
        ):
            tasks = self.watcher.describe_active() if self.watcher else []
            if not tasks:
                return "当前没有后台盯盘任务。"
            lines = [f"当前 {len(tasks)} 个后台盯盘任务："]
            for index, item in enumerate(tasks, start=1):
                desc = item["title"] or item["display_name"] or item["category"]
                label = item["display_name"]
                base = f"[{index}] {desc}（{label}）" if label else f"[{index}] {desc}"
                tid = f"｜任务 {item.get('task_id', '')}" if item.get("task_id") else ""
                status = item.get("status_label") or ""
                end = f"｜截止 {item['window_end'][:10]}" if item.get("window_end") else ""
                lines.append(f"{base}{tid}｜{status}{end}")
            lines.append("回复「取消任务 [编号]」可停止指定任务（会先征求确认）。")
            return "\n".join(lines)
        # skill（品类）数量与列表：有几个/哪些 skill、支持什么、能买什么
        if any(word in text for word in ("skill", "技能", "品类", "支持", "能买", "能做", "功能")) and any(
            word in text for word in ("几个", "多少", "哪些", "什么", "列出")
        ):
            cats = self.registry.enabled()
            names = "、".join(cat.display_name for cat in cats)
            return f"目前支持 {len(cats)} 个品类：{names}。"
        return None

    def _resolve_pending_cancel(self, raw: str, session: ChatSession) -> InteractionResult:
        """处理取消盯盘的二次确认。总是返回 result，未识别时保持 pending 并重新提示。"""
        task_id = session.pending_cancel_task
        task = self.watcher.describe_one(task_id) if self.watcher else None
        if task_id is None or task is None:
            session.pending_cancel_task = None
            return InteractionResult(session.category, session.intent, reply="该盯盘任务已不存在。", action="chat")
        desc = task.get("title") or task_id
        confirm = any(word in raw for word in ("确认删除", "确认", "确定", "删除", "取消任务", "停止"))
        abort = any(word in raw for word in ("保留", "放弃", "不删", "不取消", "算了"))
        if confirm and not abort:
            self.watcher.cancel(task_id)
            session.pending_cancel_task = None
            remaining = self.watcher.count_active()
            return InteractionResult(
                session.category,
                session.intent,
                reply=f"已取消盯盘任务 {task_id}（{desc}）。当前还有 {remaining} 个后台任务。",
                action="chat",
            )
        if abort:
            session.pending_cancel_task = None
            return InteractionResult(
                session.category,
                session.intent,
                reply="好的，保留该盯盘任务。",
                action="chat",
            )
        # 未识别确认/保留 → 保持待确认，重新提示
        return InteractionResult(
            session.category,
            session.intent,
            reply=f"请确认是否停止盯盘任务 {task_id}（{desc}）：回复「确认删除」停止，回复「保留」取消操作。",
            action="chat",
        )

    def _resolve_task_id(self, text: str) -> str | None:
        """把用户提到的方式解析成盯盘任务 id：显式 w_xxx，或按列表序号（1 开头）。"""
        m = re.search(r"w_[A-Za-z0-9]+", text)
        if m:
            return m.group(0)
        if self.watcher is None:
            return None
        tasks = self.watcher.describe_active()
        if not tasks:
            return None
        index = None
        m = re.search(r"(?:编号|任务|号|个)?\s*[:：]?\s*(\d+)\s*(?:号|个|项)?", text)
        if m:
            index = int(m.group(1)) - 1
        if index is not None and 0 <= index < len(tasks):
            return tasks[index]["task_id"]
        return None

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
