"""实时意图预测：本地关键词打分 + 可选 LLM 增强。"""

from __future__ import annotations

from itertools import chain
from typing import Any

CATEGORY_LABELS = {
    "flight": "机票",
    "hotel": "酒店",
    "moutai": "茅台",
}

# 各品类的关键词权重。置信度 = 命中关键词权重之和（封顶 1.0）。
# 权重 >=0.8 的关键词本身几乎可以确定品类。
KEYWORD_WEIGHTS: dict[str, list[tuple[str, float]]] = {
    "moutai": [
        ("茅台", 0.9),
        ("白酒", 0.85),
        ("飞天", 0.8),
        ("生肖酒", 0.8),
        ("五粮液", 0.7),
        ("酱香", 0.5),
    ],
    "hotel": [
        ("酒店", 0.9),
        ("住宿", 0.85),
        ("宾馆", 0.8),
        ("民宿", 0.8),
        ("订房", 0.75),
        ("房型", 0.7),
        ("大床房", 0.8),
        ("双床房", 0.8),
        ("入住", 0.6),
        ("几晚", 0.6),
        ("房间", 0.5),
        ("出差", 0.35),
    ],
    "flight": [
        ("机票", 0.9),
        ("航班", 0.9),
        ("直飞", 0.8),
        ("飞往", 0.8),
        ("订票", 0.7),
        ("机场", 0.5),
        ("出发", 0.3),
        ("到达", 0.25),
        ("出差", 0.55),
    ],
}

# 顶部意图置信度达到该阈值时，话术改为围绕该意图引导（不再罗列无关品类）
ADAPT_THRESHOLD = 0.40
# 引导话术把第二个意图纳入「二选一」的最低置信度
SECOND_FLOOR = 0.25

# 出现即归为弱聊天信号，用于低置信度下的闲聊兜底（不打断收集）
SMALL_TALK_WORDS = (
    "哈哈", "呵呵", "嗯", "哦", "好哦", "好的吧", "行吧", "辛苦了",
    "谢谢", "感谢", "没事", "没什么", "随便", "再看看",
)

# 用户针对"是否继续上一个意图"的肯定词
CONTINUE_WORDS = ("继续", "是", "对", "嗯", "保持", "还是", "接着", "就按这个", "没错")
SWITCH_WORDS = ("不要", "算了", "不是", "换", "不买", "取消")


class IntentClassifier:
    """对用户输入预测购物品类意图，输出 {category: 0~1 置信度}。"""

    def __init__(self, threshold: float = 0.80, router: Any = None):
        self.threshold = float(threshold or 0.80)
        self.router = router

    # ------------------------------------------------------------------
    def _local(self, text: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for category, rules in KEYWORD_WEIGHTS.items():
            score = sum(weight for word, weight in rules if word in text)
            result[category] = min(1.0, score)
        # 机票强信号：出现「城市飞城市」或两个以上城市（多数为航班需求）
        cities = self._city_count(text)
        if cities >= 2 and ("飞" in text or "去" in text or "到" in text or "往返" in text):
            result["flight"] = max(result["flight"], 0.9)
        elif cities >= 2:
            result["flight"] = max(result["flight"], 0.85)
        # 机票弱信号：只有一个城市但明确说了"飞"（非"飞天"等酒类）
        elif "飞" in text and "飞天" not in text and cities >= 1:
            result["flight"] = max(result["flight"], 0.5)
        return result

    @staticmethod
    def _city_count(text: str) -> int:
        cities = {
            "北京", "上海", "广州", "深圳", "南京", "成都", "重庆", "杭州",
            "武汉", "西安", "天津", "长沙", "昆明", "乌鲁木齐", "哈尔滨",
            "三亚", "青岛", "大连", "厦门", "合肥", "郑州", "济南", "沈阳",
        }
        return sum(1 for city in cities if city in text)

    async def classify(self, text: str) -> dict[str, float]:
        """返回 {category: 0~1}，本地打分与 LLM 结果取较大值，异常时回退本地。"""
        local = self._local(text)
        if self.router is None:
            return local
        try:
            llm = await self.router.classify_intent(text, CATEGORY_LABELS)
            if llm:
                for key in list(local) or list(CATEGORY_LABELS):
                    local[key] = max(local.get(key, 0.0), llm.get(key, 0.0))
        except Exception:
            pass
        return local

    @classmethod
    def best(cls, conf: dict[str, float], exclude: str | None = None) -> tuple[str | None, float]:
        """返回置信度最高的品类及其分数（可排除某品类，用于"排除当前已确认品类"）。"""
        best_key: str | None = None
        best_val = 0.0
        for key, value in conf.items():
            if exclude and key == exclude:
                continue
            if value > best_val:
                best_val = value
                best_key = key
        if best_val <= 0:
            return None, 0.0
        return best_key, best_val

    @classmethod
    def top(
        cls, conf: dict[str, float], limit: int = 2, floor: float = 0.0
    ) -> list[tuple[str, float]]:
        """返回置信度最高的 top-N 意图（(key, conf)），过滤低于 floor 的项。"""
        ranked = sorted(
            ((key, value) for key, value in conf.items() if value > floor),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:limit]

    @staticmethod
    def is_small_talk(text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        # 仅当出现明显闲聊词才视为闲聊，避免把"北京/3晚"等短槽位信息误判
        return any(word in t for word in SMALL_TALK_WORDS) and len(t) <= 12

    @staticmethod
    def is_continue(text: str) -> bool:
        words = (s.strip() for s in chain(CONTINUE_WORDS, SMALL_TALK_WORDS))
        return any(word and word in text for word in words)

    @staticmethod
    def is_switch(text: str) -> bool:
        return any(word in text for word in SWITCH_WORDS)