"""日期、时区与中文自然语言日期解析。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..models import TZ, now

CN_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def today() -> date:
    return now().date()


def days_until(target: date | datetime | str, base: date | None = None) -> int:
    base = base or today()
    if isinstance(target, datetime):
        target = target.date()
    elif isinstance(target, str):
        parsed = datetime.fromisoformat(target)
        target = parsed.date()
    return (target - base).days


def parse_date_text(text: str, base: date | None = None) -> date | None:
    """从中文/ISO 文本中解析日期；无法解析返回 None。"""
    if not text:
        return None
    base = base or today()
    raw = text.strip()

    # ISO 或 2026/11/08 / 2026年11月8日
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    relative = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
    for word, offset in relative.items():
        if word in raw:
            return base + timedelta(days=offset)

    m = re.search(r"(\d+)\s*天后", raw)
    if m:
        return base + timedelta(days=int(m.group(1)))

    m = re.search(r"下周([一二三四五六日天])", raw)
    if m:
        target_weekday = CN_WEEKDAYS[m.group(1)]
        # 下周一为基准
        next_monday = base + timedelta(days=(7 - base.weekday()))
        return next_monday + timedelta(days=target_weekday)

    # 下个月 8 号 / 下月 8 日
    m = re.search(r"下个?月\s*(\d{1,2})\s*[日号]?", raw)
    if m:
        year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        day = int(m.group(1))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # 11月8日
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = base.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate < base:
            candidate = date(year + 1, month, day)
        return candidate

    # 8号（当月未过则当月，否则下月）
    m = re.search(r"(?<!\d)(\d{1,2})\s*[日号]", raw)
    if m:
        day = int(m.group(1))
        try:
            candidate = date(base.year, base.month, day)
            if candidate < base:
                year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
                candidate = date(year, month, day)
            return candidate
        except ValueError:
            return None
    return None


def parse_time_preference(text: str) -> str | None:
    if any(word in text for word in ("上午", "早上", "早晨")):
        return "上午"
    if any(word in text for word in ("下午", "中午")):
        return "下午"
    if any(word in text for word in ("晚上", "夜间", "晚班")):
        return "晚上"
    return None


def parse_cabin(text: str) -> str | None:
    if "头等" in text:
        return "头等舱"
    if "商务" in text:
        return "商务舱"
    if "经济" in text:
        return "经济舱"
    return None


def parse_budget(text: str) -> float | None:
    """解析“预算 2000 / 2000以内 / 不超过 3 千 / 两千”等表达。"""
    if not text:
        return None
    m = re.search(
        r"(?:预算|不超过|低于|最多|控制在|花费)\s*(?:在)?\s*(\d+(?:\.\d+)?)\s*(千|k|K|元|块)?",
        text,
    )
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(元|块|千|k|K)\s*(?:以内|以下|之内)", text)
    if m:
        value = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("千", "k"):
            value *= 1000
        return value
    cn_numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    m = re.search(r"([一二两三四五六七八九])\s*(千|万)", text)
    if m:
        value = float(cn_numbers[m.group(1)])
        return value * (10000 if m.group(2) == "万" else 1000)
    return None


def parse_quantity(text: str, default: int = 1) -> int:
    m = re.search(r"(\d+)\s*(?:瓶|件|个|台|份)", text)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"([一二两三四五六七八九十])\s*(?:瓶|件|个|台|份)", text)
    if m:
        cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        return cn.get(m.group(1), default)
    return default


def format_cn(d: date | str) -> str:
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return d
    return f"{d.month}月{d.day}日"
