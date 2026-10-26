"""跨模块共享的数据模型（全部使用可 JSON 序列化的基础类型）。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai，避免依赖系统 tzdata


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# 品类与槽位
# --------------------------------------------------------------------------


@dataclass
class SlotSpec:
    key: str
    title: str
    required: bool = False
    type: str = "str"          # str | date | int | float
    ask: str = ""              # 缺失时的追问文案
    choices: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class CategoryQuery:
    """品类无关的采集参数，由 CategoryStrategy.build_query() 生成。"""

    category: str
    params: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)

    @property
    def query_hash(self) -> str:
        return stable_hash({"category": self.category, "params": self.params})

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class PurchaseIntent:
    category: str
    slots: dict[str, Any] = field(default_factory=dict)
    budget: float | None = None
    preferences: dict[str, Any] = field(default_factory=dict)
    watch_window: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PurchaseIntent":
        return cls(
            category=data.get("category", ""),
            slots=dict(data.get("slots") or {}),
            budget=data.get("budget"),
            preferences=dict(data.get("preferences") or {}),
            watch_window=data.get("watch_window"),
        )


@dataclass
class Issue:
    level: str                 # fatal | warn | info
    code: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


# --------------------------------------------------------------------------
# 采集数据
# --------------------------------------------------------------------------


@dataclass
class PriceSnapshot:
    category: str
    platform: str
    item_id: str
    item_name: str
    price: float                      # 品类可比价（机票含税 / 酒店每晚 / 茅台单价）
    tax: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = "mock"              # official_api | aggregator | playwright | mock
    confidence: float = 0.8
    captured_at: str = field(default_factory=now_iso)
    stale: bool = False
    quarantined: bool = False
    hour_bucket: str = ""

    @property
    def total_price(self) -> float:
        return round(self.price + self.tax, 2)

    def finalize(self) -> "PriceSnapshot":
        if not self.hour_bucket:
            dt = parse_iso(self.captured_at) or now()
            self.hour_bucket = dt.strftime("%Y-%m-%dT%H")
        return self

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PriceSnapshot":
        return cls(
            category=row["category"],
            platform=row["platform"],
            item_id=row["item_id"],
            item_name=row.get("item_name", ""),
            price=float(row["price"]),
            tax=float(row.get("tax") or 0),
            attributes=json.loads(row.get("attributes_json") or "{}"),
            source=row.get("source", "mock"),
            confidence=float(row.get("confidence") or 0),
            captured_at=row.get("captured_at", ""),
            stale=bool(row.get("stale")),
            quarantined=bool(row.get("quarantined")),
            hour_bucket=row.get("hour_bucket", ""),
        )


@dataclass
class Coupon:
    category: str
    platform: str
    coupon_code: str
    title: str
    coupon_type: str = "amount"       # amount（满减/直减）| discount（折扣）
    amount: float = 0.0
    discount_rate: float = 0.0        # 折扣券：0.1 表示立减 10%
    min_spend: float = 0.0
    valid_from: str = ""
    valid_to: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    source: str = "mock"
    confidence: float = 0.8
    status: str = "active"
    collected_at: str = field(default_factory=now_iso)
    quarantined: bool = False
    id: int | None = None

    def is_valid(self, at: datetime | str | None = None) -> bool:
        if self.quarantined or self.status != "active":
            return False
        moment = parse_iso(at) if not isinstance(at, datetime) else at
        moment = moment or now()
        start = parse_iso(self.valid_from)
        end = parse_iso(self.valid_to)
        if start and moment < start:
            return False
        if end and moment > end:
            return False
        return True

    def discount_for(self, price: float) -> float:
        if price < self.min_spend:
            return 0.0
        if self.coupon_type == "discount":
            return round(price * max(0.0, min(0.95, self.discount_rate)), 2)
        return round(min(max(0.0, self.amount), price), 2)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Coupon":
        return cls(
            category=row["category"],
            platform=row["platform"],
            coupon_code=row["coupon_code"],
            title=row.get("title", ""),
            coupon_type=row.get("coupon_type", "amount"),
            amount=float(row.get("amount") or 0),
            discount_rate=float(row.get("discount_rate") or 0),
            min_spend=float(row.get("min_spend") or 0),
            valid_from=row.get("valid_from", ""),
            valid_to=row.get("valid_to", ""),
            scope=json.loads(row.get("scope_json") or "{}"),
            source=row.get("source", "mock"),
            confidence=float(row.get("confidence") or 0),
            status=row.get("status", "active"),
            collected_at=row.get("collected_at", ""),
            quarantined=bool(row.get("quarantined")),
            id=int(row["id"]) if row.get("id") is not None else None,
        )


# --------------------------------------------------------------------------
# 会话 / 任务 / 方案 / 订单
# --------------------------------------------------------------------------


@dataclass
class Booking:
    id: str
    user_id: str
    category: str
    title: str
    slots: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    budget: float | None = None
    status: str = "collecting"
    window_start: str = ""
    window_end: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Booking":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            category=row["category"],
            title=row.get("title", ""),
            slots=json.loads(row.get("slots_json") or "{}"),
            attributes=json.loads(row.get("attributes_json") or "{}"),
            budget=row.get("budget"),
            status=row.get("status", "collecting"),
            window_start=row.get("window_start", ""),
            window_end=row.get("window_end", ""),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


@dataclass
class Offer:
    id: str
    booking_id: str
    category: str
    platform: str
    item_id: str
    item_name: str
    price: float
    coupon_id: str | None
    coupon_code: str | None
    discount: float
    paid_price: float
    attributes: dict[str, Any] = field(default_factory=dict)
    quoted_at: str = field(default_factory=now_iso)
    coupon_title: str = ""
    in_stock: bool = True
    confidence: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class Order:
    id: str
    booking_id: str
    category: str
    platform: str
    list_price: float
    coupon_id: str | None
    discount: float
    paid_price: float
    order_no: str = ""
    status: str = "draft"
    deep_link: str = ""
    idempotency_key: str = ""
    paid_at: str = ""
    created_at: str = field(default_factory=now_iso)
    item_id: str = ""
    item_name: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Order":
        return cls(
            id=row["id"],
            booking_id=row["booking_id"],
            category=row["category"],
            platform=row["platform"],
            list_price=float(row.get("list_price") or 0),
            coupon_id=row.get("coupon_id"),
            discount=float(row.get("discount") or 0),
            paid_price=float(row.get("paid_price") or 0),
            order_no=row.get("order_no", ""),
            status=row.get("status", "draft"),
            deep_link=row.get("deep_link", ""),
            idempotency_key=row.get("idempotency_key", ""),
            paid_at=row.get("paid_at", ""),
            created_at=row.get("created_at", ""),
            item_id=row.get("item_id", ""),
            item_name=row.get("item_name", ""),
            attributes=json.loads(row.get("attributes_json") or "{}"),
        )


# --------------------------------------------------------------------------
# 预测 / 决策
# --------------------------------------------------------------------------


@dataclass
class ModelPrediction:
    provider: str
    model: str
    role: str = "predictor"
    prediction: float | None = None
    direction: str = "flat"           # down | flat | up
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    failed: bool = False
    error: str = ""
    cost_usd: float = 0.0
    is_cold_start: bool = False
    reliability: float = 0.5
    score: float = 0.0
    key_alias: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class ModelVote:
    model: str
    provider: str
    direction: str
    confidence: float
    score: float
    prediction: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class Decision:
    category: str
    booking_id: str
    current_effective_price: float
    predicted_median: float
    predicted_range: tuple[float, float]
    recommendation: str              # buy_now | watch | no_buy | no_go
    decision_score: float
    confidence: float
    optimal_window: tuple[str, str]
    reasons: list[str]
    model_votes: list[ModelVote] = field(default_factory=list)
    data_freshness_minutes: int = 0
    disclaimer: str = "基于历史与模型估计，不构成价格承诺"
    judge_used: bool = False
    low_diversity: bool = False
    local_prediction: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)
    guardrail_ok: bool = True
    guardrail_reasons: list[str] = field(default_factory=list)
    model_unavailable: bool = False
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = to_dict(self)
        data["predicted_range"] = list(self.predicted_range)
        data["optimal_window"] = list(self.optimal_window)
        return data


# --------------------------------------------------------------------------
# 会话交互 / 通知
# --------------------------------------------------------------------------


@dataclass
class InteractionResult:
    category: str | None
    intent: PurchaseIntent | None
    missing: list[str] = field(default_factory=list)
    reply: str = ""
    action: str = "continue"          # continue | ready | confirm | select_offer | modify | chat | exit
    confidence: float = 0.8
    slot_updates: dict[str, Any] = field(default_factory=dict)
    selected_offer: int | None = None
    receipt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class NotificationSpec:
    type: str
    title: str
    body: str
    at: str = ""
    lead_hours: int | None = None
    channel: str = "web_push"


@dataclass
class Notification:
    id: str
    user_id: str
    booking_id: str
    category: str
    type: str
    channel: str
    payload: dict[str, Any]
    status: str = "pending"
    scheduled_at: str = field(default_factory=now_iso)
    sent_at: str = ""
    window_key: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class ChatMessage:
    id: int | None
    session_id: str
    role: str
    content: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class ChatSession:
    """单次对话的运行时状态（进程内）。"""

    id: str
    user_id: str
    category: str | None = None
    intent: PurchaseIntent | None = None
    booking_id: str | None = None
    state: str = "Collecting"
    last_decision: Decision | None = None
    offers: list[Offer] = field(default_factory=list)
    order_id: str | None = None
    # 实时意图预测（本地规则 + 可选 LLM 增强）
    intent_category: str | None = None   # 已确认的品类（提交进收集环节后不再为空）
    intent_confidence: float = 0.0       # 当前已确认品类的最新置信度 0~1
    intent_scores: dict[str, float] = field(default_factory=dict)  # 各品类最新置信度
    proposed_category: str | None = None # 引导话术向用户提案的品类（待确认）
    pending_confirm: bool = False        # 是否在等待用户确认"继续上一个意图"
    pending_watch_start: bool = False    # 数据不足时是否在等待用户确认是否仍要盯盘
    pending_cancel_task: str | None = None  # 待用户二次确认删除的盯盘任务 id
    pending_exit: bool = False         # 是否在等待用户确认退出本次对话
    credential_step: str = ""            # "" | account | secret
    credential_account: str = ""
    credential_platform: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category,
            "intent": self.intent.to_dict() if self.intent else None,
            "booking_id": self.booking_id,
            "state": self.state,
            "last_decision": self.last_decision.to_dict() if self.last_decision else None,
            "offers": [o.to_dict() for o in self.offers],
            "order_id": self.order_id,
            "intent_category": self.intent_category,
            "intent_confidence": self.intent_confidence,
            "intent_scores": dict(self.intent_scores),
            "proposed_category": self.proposed_category,
            "pending_confirm": self.pending_confirm,
            "pending_watch_start": self.pending_watch_start,
            "pending_cancel_task": self.pending_cancel_task,
            "pending_exit": self.pending_exit,
            "credential_step": self.credential_step,
            "credential_platform": self.credential_platform,
            "history": list(self.history),
        }
