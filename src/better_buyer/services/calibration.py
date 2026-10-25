"""回测校准：用实际价格更新模型可靠性。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..models import now, parse_iso
from ..storage.repositories import RepositoryHub


class CalibrationService:
    def __init__(self, repos: RepositoryHub, alpha: float = 0.2):
        self.repos = repos
        self.alpha = alpha

    # ------------------------------------------------------------------
    def run(self, horizon_days: int = 3, limit: int = 500) -> dict[str, Any]:
        cutoff = (now() - timedelta(days=horizon_days)).isoformat(timespec="seconds")
        runs = self.repos.db.query(
            "SELECT * FROM decision_runs WHERE created_at<=? ORDER BY created_at DESC LIMIT ?",
            [cutoff, limit],
        )
        updated = 0
        skipped = 0
        for run in runs:
            actual = self._actual_price(run, horizon_days)
            if actual is None or actual <= 0:
                skipped += 1
                continue
            predictions = self.repos.db.query(
                "SELECT * FROM model_predictions WHERE decision_run_id=? AND prediction IS NOT NULL",
                [run["id"]],
            )
            current = self._current_from_payload(run)
            actual_direction = self._direction(current, actual)
            for prediction in predictions:
                self._update_one(run["category"], prediction, actual, actual_direction, current)
                updated += 1
        report = {"runs": len(runs), "updated": updated, "skipped": skipped}
        self.repos.events.log("calibration", "backtest", report)
        return report

    # ------------------------------------------------------------------
    def _actual_price(self, run: dict[str, Any], horizon_days: int) -> float | None:
        start = parse_iso(run["created_at"])
        if not start:
            return None
        end = start + timedelta(days=horizon_days)
        row = self.repos.db.query_one(
            """SELECT MIN(price) AS actual FROM price_snapshots
               WHERE category=? AND quarantined=0 AND captured_at>? AND captured_at<=?""",
            [run["category"], start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")],
        )
        if row and row.get("actual") is not None:
            return float(row["actual"])
        return None

    @staticmethod
    def _current_from_payload(run: dict[str, Any]) -> float:
        import json

        try:
            payload = json.loads(run.get("payload_json") or "{}")
        except json.JSONDecodeError:
            return 0.0
        return float(payload.get("current_effective_price") or 0)

    @staticmethod
    def _direction(current: float, actual: float) -> str:
        if current <= 0:
            return "flat"
        if actual < current * 0.98:
            return "down"
        if actual > current * 1.02:
            return "up"
        return "flat"

    def _update_one(
        self,
        category: str,
        prediction: dict[str, Any],
        actual: float,
        actual_direction: str,
        current: float,
    ) -> None:
        provider = prediction["provider"]
        model = prediction["model"]
        predicted = float(prediction["prediction"])
        previous = self.repos.model_stats.get(category, provider, model) or {}
        samples = int(previous.get("samples") or 0)
        old_mae = float(previous.get("mae") or 0)
        old_acc = float(previous.get("direction_accuracy") or 0)
        old_brier = float(previous.get("brier") or 0)
        old_reliability = float(previous.get("reliability_score") or 0.5)

        mae = abs(predicted - actual)
        mae_smooth = mae if samples == 0 else (1 - self.alpha) * old_mae + self.alpha * mae
        correct = 1.0 if prediction.get("direction") == actual_direction else 0.0
        acc_smooth = correct if samples == 0 else (1 - self.alpha) * old_acc + self.alpha * correct
        confidence = float(prediction.get("confidence") or 0.5)
        brier_item = (confidence - correct) ** 2
        brier_smooth = brier_item if samples == 0 else (1 - self.alpha) * old_brier + self.alpha * brier_item
        base = current or actual or 1.0
        error_ratio = min(1.0, mae / max(base, 1.0))
        target = 0.6 * (1.0 - error_ratio) + 0.4 * acc_smooth
        reliability = (1 - self.alpha) * old_reliability + self.alpha * target
        self.repos.model_stats.upsert(
            category=category,
            provider=provider,
            model=model,
            samples=samples + 1,
            mae=round(mae_smooth, 2),
            direction_accuracy=round(acc_smooth, 4),
            brier=round(brier_smooth, 4),
            reliability=round(min(1.0, max(0.0, reliability)), 4),
        )
