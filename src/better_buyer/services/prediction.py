"""价格预测智能体：本地统计模型 + 多模型协同 + guardrail。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..categories.registry import CategoryRegistry
from ..core.config import Settings
from ..models import Decision, ModelPrediction, PriceSnapshot, PurchaseIntent, Coupon, now
from .model_router import ModelRouter
from .ranking import OfferRanker
from .scoring import EnsembleResult, EnsembleScorer, Guardrail, GuardrailResult

DISCLAIMER = "基于历史与模型估计，不构成价格承诺；以平台结算页为准"


class PricePredictor:
    def __init__(
        self,
        settings: Settings,
        registry: CategoryRegistry,
        repos: Any,
        router: ModelRouter,
        scorer: EnsembleScorer,
        ranker: OfferRanker,
    ):
        self.settings = settings
        self.registry = registry
        self.repos = repos
        self.router = router
        self.scorer = scorer
        self.ranker = ranker
        self.guardrail = Guardrail(settings.price_stale_hours)

    # ------------------------------------------------------------------
    def local_predict(self, features: dict[str, Any], current_price: float) -> tuple[float, float]:
        stats = features.get("stats") or {}
        ewma = float(stats.get("ewma") or 0)
        trend = float(stats.get("trend") or 0)
        seasonal = float(features.get("seasonal_factor") or 1.0)
        sample_count = int(stats.get("sample_count") or 0)
        if sample_count == 0 or ewma <= 0:
            return round(current_price, 2), 0.25

        local_pred = (
            0.55 * ewma
            + 0.25 * ewma * seasonal
            + 0.20 * (ewma + trend * 6)
        )
        if current_price > 0:
            local_pred = max(current_price * 0.7, min(current_price * 1.3, local_pred))

        volatility = float(stats.get("volatility") or 0)
        sample_quality = min(1.0, sample_count / 12.0)
        stability = 1.0 - min(1.0, volatility / 0.30)
        freshness = self._freshness_quality(features)
        quality = 0.5 * sample_quality + 0.25 * stability + 0.25 * freshness
        quality = max(0.05, min(1.0, quality))
        return round(local_pred, 2), round(quality, 4)

    def _freshness_quality(self, features: dict[str, Any]) -> float:
        freshness = features.get("data_freshness_minutes")
        if freshness is None:
            return 0.8
        max_age = max(1, self.guardrail.max_age_minutes)
        return max(0.0, 1.0 - float(freshness) / max_age)

    # ------------------------------------------------------------------
    async def predict(
        self,
        booking_id: str,
        intent: PurchaseIntent,
        snapshots: list[PriceSnapshot],
        coupons: list[Coupon],
        freshness_minutes: int | None = None,
        at: datetime | None = None,
    ) -> tuple[Decision, list[ModelPrediction]]:
        at = at or now()
        strategy = self.registry.get(intent.category)
        usable = [s for s in snapshots if not s.quarantined]
        features = strategy.build_prediction_features(intent, usable, coupons, at)
        offers = self.ranker.rank(intent, usable, coupons, booking_id, at)
        current = self.ranker.best_effective(offers)
        freshness = (
            int(freshness_minutes)
            if freshness_minutes is not None
            else self.repos.prices.freshness_minutes(intent.category)
        )
        features["data_freshness_minutes"] = freshness
        features["offer_count"] = len(offers)
        features["cheapest_platform"] = offers[0].platform if offers else ""

        local_pred, local_quality = self.local_predict(features, current)
        features["local_prediction"] = local_pred
        features["local_quality"] = local_quality

        prompt = strategy.build_prediction_prompt(features)
        model_predictions = await self.router.predict(
            intent.category, features, prompt, query_hash=strategy.build_query(intent).query_hash
        )
        ensemble = self.scorer.score(
            local_pred,
            local_quality,
            model_predictions,
            min_participants=self.router.min_participants(),
        )

        judge_used = False
        buy_threshold = self._buy_threshold(intent.category, strategy.buy_score_threshold)
        if ensemble.successful_predictions and self.scorer.needs_judge(
            ensemble.disagreement, ensemble.decision_score, buy_threshold
        ):
            judge = await self.router.judge(
                intent.category,
                features,
                [vote.to_dict() for vote in ensemble.votes],
                ensemble.disagreement,
            )
            if judge is not None:
                ensemble = self.scorer.apply_judge(ensemble, judge)
                judge_used = True
                features["judge"] = judge.to_dict()

        predicted = ensemble.predicted_final
        if current > 0:
            predicted = max(current * 0.7, min(current * 1.3, predicted))
        else:
            predicted = local_pred
        ensemble.predicted_final = round(predicted, 2)

        model_unavailable = ensemble.successful_predictions == 0
        confidence = ensemble.confidence
        if model_unavailable:
            confidence = min(0.60, confidence)
        if ensemble.low_diversity:
            confidence = round(max(0.0, confidence - 0.03), 4)
        decision_score = ensemble.decision_score
        if model_unavailable:
            decision_score = round(min(decision_score, 0.60), 4)

        # guardrail 不包含预算，预算单独判定为 no_buy
        guard = self.guardrail.check(
            freshness,
            offers,
            coupons,
            budget=None,
            current_price=current,
            predicted_price=predicted,
            category=intent.category,
            strategy=strategy,
        )
        if current <= 0:
            guard = GuardrailResult(False, guard.reasons + ["没有可用的报价"])

        margin = (predicted - current) / predicted if predicted > 0 and current > 0 else 0.0
        urgent = strategy.is_urgent(intent, coupons, at)
        trend_up = float((features.get("stats") or {}).get("trend") or 0) > 0

        if not guard.ok:
            recommendation = "no_go"
        elif intent.budget is not None and offers and min(
            self.ranker.budget_amount(intent.category, offer) for offer in offers
        ) > float(intent.budget):
            recommendation = "no_buy"
        elif decision_score >= buy_threshold and margin >= strategy.min_saving_margin:
            recommendation = "buy_now"
        elif urgent or trend_up:
            recommendation = "buy_now"
        else:
            recommendation = "watch"

        reasons = self._build_reasons(
            features, offers, current, predicted, margin, freshness, model_unavailable,
            ensemble, urgent, recommendation,
        )
        window = strategy.optimal_window(intent.slots, at)
        spread = max(
            0.05,
            min(
                0.20,
                float((features.get("stats") or {}).get("volatility") or 0.08),
            ),
        )
        predicted_range = (
            round(predicted * (1 - spread), 2),
            round(predicted * (1 + spread), 2),
        )
        risk_note = (
            ["风险提示：已进入临近窗口或价格上行区间，买入后仍可能波动"]
            if recommendation == "buy_now" and decision_score < buy_threshold
            else []
        )
        reasons = risk_note + reasons

        decision = Decision(
            category=intent.category,
            booking_id=booking_id,
            current_effective_price=round(current, 2),
            predicted_median=round(predicted, 2),
            predicted_range=predicted_range,
            recommendation=recommendation,
            decision_score=round(decision_score, 4),
            confidence=round(confidence, 4),
            optimal_window=window,
            reasons=reasons,
            model_votes=ensemble.votes,
            data_freshness_minutes=freshness,
            disclaimer=DISCLAIMER,
            judge_used=judge_used,
            low_diversity=ensemble.low_diversity,
            local_prediction=local_pred,
            features=features,
            guardrail_ok=guard.ok,
            guardrail_reasons=guard.reasons,
            model_unavailable=model_unavailable,
            created_at=at.isoformat(timespec="seconds"),
        )
        return decision, model_predictions

    # ------------------------------------------------------------------
    def _buy_threshold(self, category: str, fallback: float) -> float:
        override = (self.settings.llm_config.get("category_overrides") or {}).get(category) or {}
        thresholds = override.get("thresholds") or {}
        return float(thresholds.get("buy_now", fallback))

    @staticmethod
    def _build_reasons(
        features: dict[str, Any],
        offers: list[Any],
        current: float,
        predicted: float,
        margin: float,
        freshness: int,
        model_unavailable: bool,
        ensemble: EnsembleResult,
        urgent: bool,
        recommendation: str,
    ) -> list[str]:
        reasons: list[str] = []
        stats = features.get("stats") or {}
        if offers:
            cheapest = offers[0]
            reasons.append(
                f"当前可获取最低实付 ¥{current:.2f}（{cheapest.platform}，"
                f"{'已用券' if cheapest.discount else '未用券'}）"
            )
        if predicted > 0 and current > 0:
            if margin >= 0:
                reasons.append(f"预测中位数 ¥{predicted:.2f}，当前低于预测 {margin * 100:.1f}%")
            else:
                reasons.append(f"预测中位数 ¥{predicted:.2f}，当前高于预测 {abs(margin) * 100:.1f}%")
        if stats.get("sample_count"):
            reasons.append(
                f"近 {stats.get('sample_count')} 次采集波动率 "
                f"{float(stats.get('volatility') or 0) * 100:.1f}%，样本 "
                f"{stats.get('sample_count')} 条"
            )
        days = features.get("days_to_depart", features.get("days_to_checkin"))
        if days is not None:
            reasons.append(f"距目标时间还有 {days} 天")
        if urgent:
            reasons.append("已进入临近窗口，目标券可能即将过期")
        if freshness > 60:
            reasons.append(f"数据已 {freshness} 分钟未更新")
        if model_unavailable:
            reasons.append("外部模型不可用，已回退本地模型，置信度受限")
        elif ensemble.low_diversity:
            reasons.append("参与决策的模型数量不足，已标记低多样性")
        if not reasons:
            reasons.append("暂无可比价格数据，建议继续观察")
        if recommendation == "watch":
            reasons.append("未达到买入阈值，继续盯盘")
        return reasons[:6]
