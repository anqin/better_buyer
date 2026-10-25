"""价格预测智能体：薄封装预测服务并持久化决策与模型票。"""

from __future__ import annotations

from typing import Any

from ..models import Booking, Decision, PurchaseIntent
from ..services.prediction import PricePredictor
from ..storage.repositories import RepositoryHub


class PredictorAgent:
    def __init__(self, repos: RepositoryHub, predictor: PricePredictor):
        self.repos = repos
        self.predictor = predictor

    @staticmethod
    def intent_from_booking(booking: Booking) -> PurchaseIntent:
        return PurchaseIntent(
            category=booking.category,
            slots=dict(booking.slots),
            budget=booking.budget,
            preferences=dict(booking.attributes.get("preferences") or {}),
        )

    async def run(
        self,
        booking: Booking,
        intent: PurchaseIntent | None = None,
    ) -> Decision:
        intent = intent or self.intent_from_booking(booking)
        snapshots = self.repos.prices.recent(booking.category, limit=240)
        coupons = self.repos.coupons.active(booking.category)
        freshness = self.repos.prices.freshness_minutes(booking.category)
        decision, predictions = await self.predictor.predict(
            booking.id,
            intent,
            snapshots,
            coupons,
            freshness_minutes=freshness,
        )
        self.repos.decisions.save(decision, predictions)
        self.repos.events.log(
            "predictor",
            "decision",
            {
                "recommendation": decision.recommendation,
                "decision_score": decision.decision_score,
                "current_effective_price": decision.current_effective_price,
                "predicted_median": decision.predicted_median,
                "model_unavailable": decision.model_unavailable,
            },
            booking_id=booking.id,
            category=booking.category,
        )
        return decision
