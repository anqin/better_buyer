"""综合打分 EnsembleScorer 与 guardrail。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import ModelPrediction, ModelVote


@dataclass
class EnsembleResult:
    predicted_final: float
    decision_score: float
    confidence: float
    direction: str
    disagreement: float
    votes: list[ModelVote]
    low_diversity: bool
    local_quality: float
    model_score: float
    judge_score: float = 0.0
    successful_predictions: int = 0
    total_predictions: int = 0


@dataclass
class GuardrailResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


class EnsembleScorer:
    def __init__(self, llm_config: dict[str, Any]):
        scoring = dict(llm_config.get("scoring") or {})
        self.model_weights = dict(scoring.get("model") or {})
        self.final_weights = dict(scoring.get("final") or {})
        self.thresholds = dict(scoring.get("thresholds") or {})
        self._validate()

    def _validate(self) -> None:
        defaults_model = {
            "direction": 0.30,
            "plausibility": 0.20,
            "confidence": 0.20,
            "reliability": 0.20,
            "evidence": 0.05,
            "cost": 0.05,
        }
        defaults_final = {"local": 0.50, "models": 0.40, "judge": 0.10}
        for key, value in defaults_model.items():
            self.model_weights.setdefault(key, value)
        for key, value in defaults_final.items():
            self.final_weights.setdefault(key, value)
        if abs(sum(self.model_weights.values()) - 1.0) > 0.02:
            raise ValueError("config/llm.yaml 的 scoring.model 权重之和必须为 1")
        if abs(sum(self.final_weights.values()) - 1.0) > 0.02:
            raise ValueError("config/llm.yaml 的 scoring.final 权重之和必须为 1")

    # ------------------------------------------------------------------
    @staticmethod
    def _weighted_median(values: list[tuple[float, float]]) -> float:
        valid = [(value, max(weight, 0.01)) for value, weight in values if value > 0]
        if not valid:
            return 0.0
        valid.sort(key=lambda item: item[0])
        total = sum(weight for _, weight in valid)
        cumulative = 0.0
        for value, weight in valid:
            cumulative += weight
            if cumulative >= total / 2:
                return value
        return valid[-1][0]

    def score(
        self,
        local_prediction: float,
        local_quality: float,
        predictions: list[ModelPrediction],
        min_participants: int = 2,
        weights_by_provider: dict[str, float] | None = None,
    ) -> EnsembleResult:
        weights_by_provider = weights_by_provider or {}
        successful = [p for p in predictions if not p.failed and p.prediction]
        total = len(predictions)

        if not successful:
            confidence = min(0.60, max(0.0, local_quality * 0.60))
            final_weights = self.final_weights
            denom = final_weights["local"] + final_weights["models"] or 1.0
            decision_score = final_weights["local"] / denom * local_quality
            return EnsembleResult(
                predicted_final=round(local_prediction, 2),
                decision_score=round(decision_score, 4),
                confidence=round(confidence, 4),
                direction="flat",
                disagreement=0.0,
                votes=[],
                low_diversity=total < min_participants,
                local_quality=round(local_quality, 4),
                model_score=0.0,
                successful_predictions=0,
                total_predictions=total,
            )

        median = self._weighted_median(
            [(float(p.prediction or 0), max(p.confidence, 0.1)) for p in successful]
        )
        direction_counts: dict[str, int] = {"down": 0, "flat": 0, "up": 0}
        for prediction in successful:
            direction_counts[prediction.direction] = direction_counts.get(prediction.direction, 0) + 1
        max_share = max(direction_counts.values()) / len(successful)
        disagreement = round(1.0 - max_share, 4)
        consensus = max(direction_counts, key=direction_counts.get)

        scores: list[float] = []
        votes: list[ModelVote] = []
        for prediction in successful:
            plausibility = 1.0 - min(1.0, abs(float(prediction.prediction or 0) - median) / max(median, 1))
            evidence = 1.0 if prediction.evidence and prediction.reasons else (
                0.5 if prediction.evidence or prediction.reasons else 0.0
            )
            cost_penalty = min(1.0, float(prediction.cost_usd) * 100)
            score = (
                self.model_weights["direction"] * (direction_counts[prediction.direction] / len(successful))
                + self.model_weights["plausibility"] * plausibility
                + self.model_weights["confidence"] * float(prediction.confidence)
                + self.model_weights["reliability"] * float(prediction.reliability)
                + self.model_weights["evidence"] * evidence
                - self.model_weights["cost"] * cost_penalty
            )
            score = max(0.0, min(1.0, score))
            prediction.score = round(score, 4)
            scores.append(score)
            votes.append(
                ModelVote(
                    model=f"Model {chr(65 + len(votes))}",
                    provider=prediction.provider,
                    direction=prediction.direction,
                    confidence=round(float(prediction.confidence), 4),
                    score=round(score, 4),
                    prediction=round(float(prediction.prediction or 0), 2),
                )
            )

        model_score = sum(scores) / len(scores)
        weighted_median = self._weighted_median(
            [
                (float(p.prediction or 0), p.score or 0.01)
                for p in successful
            ]
        )
        local_confidence = sum(float(p.confidence) for p in successful) / len(successful)
        confidence = 0.55 * local_confidence + 0.45 * local_quality
        confidence *= 1.0 - 0.5 * disagreement
        confidence = max(0.0, min(0.95, confidence))

        decision_score = (
            self.final_weights["local"] * local_quality
            + self.final_weights["models"] * model_score
        )
        # 此处尚未引入 judge，按 local/models 权重重新归一；judge 评分在 apply_judge 中再合成
        denom = self.final_weights["local"] + self.final_weights["models"]
        if denom > 0:
            decision_score = decision_score / denom

        return EnsembleResult(
            predicted_final=round(weighted_median, 2),
            decision_score=round(max(0.0, min(1.0, decision_score)), 4),
            confidence=round(confidence, 4),
            direction=consensus,
            disagreement=disagreement,
            votes=votes,
            low_diversity=len(successful) < min_participants,
            local_quality=round(local_quality, 4),
            model_score=round(model_score, 4),
            successful_predictions=len(successful),
            total_predictions=total,
        )

    # ------------------------------------------------------------------
    def apply_judge(self, result: EnsembleResult, judge: ModelPrediction | None) -> EnsembleResult:
        if judge is None or judge.failed or not judge.prediction:
            result.judge_score = 0.0
            return result
        plausibility = 1.0 - min(
            1.0, abs(float(judge.prediction) - result.predicted_final) / max(result.predicted_final, 1)
        )
        result.judge_score = round(max(0.0, min(1.0, float(judge.confidence) * 0.7 + plausibility * 0.3)), 4)
        result.decision_score = round(
            max(
                0.0,
                min(
                    1.0,
                    self.final_weights["local"] * result.local_quality
                    + self.final_weights["models"] * result.model_score
                    + self.final_weights["judge"] * result.judge_score,
                ),
            ),
            4,
        )
        result.confidence = round(min(0.97, result.confidence + 0.03), 4)
        return result

    def needs_judge(
        self, disagreement: float, decision_score: float, buy_threshold: float
    ) -> bool:
        escalate_threshold = float(self.thresholds.get("judge_escalation", 0.25))
        near_threshold = abs(decision_score - buy_threshold) <= 0.08
        return disagreement > escalate_threshold or near_threshold


class Guardrail:
    """硬约束优先于任何模型意见。"""

    def __init__(self, stale_hours: float = 6.0):
        self.max_age_minutes = int(stale_hours * 60)

    def check(
        self,
        freshness_minutes: int,
        offers: list[Any],
        coupons: list[Any],
        budget: float | None,
        current_price: float,
        predicted_price: float,
        category: str = "flight",
        strategy: Any = None,
    ) -> GuardrailResult:
        reasons: list[str] = []
        if freshness_minutes > self.max_age_minutes:
            reasons.append(f"数据已 {freshness_minutes} 分钟未更新，可能过期")
        if not offers:
            reasons.append("没有可用的报价")
        if budget is not None:
            cheapest = min(
                (self._budget_amount(offer, category) for offer in offers), default=0.0
            )
            if cheapest > float(budget):
                reasons.append(f"最低实付 {cheapest:.2f} 超出预算 {float(budget):.2f}")
        if current_price <= 0:
            reasons.append("当前实付价非法")
        if predicted_price <= 0:
            reasons.append("预测价格非法")
        if current_price > 0 and predicted_price > 0:
            drift = abs(predicted_price - current_price) / current_price
            if drift > 0.30:
                reasons.append("预测价格超出 ±30% 限幅")
        if strategy is not None:
            for coupon in coupons:
                if not coupon.is_valid():
                    continue
        return GuardrailResult(not reasons, reasons)

    @staticmethod
    def _budget_amount(offer: Any, category: str) -> float:
        if category == "moutai":
            return float(offer.attributes.get("total_paid") or offer.paid_price)
        return float(offer.paid_price)
