"""Markdown 导出：SQLite 为唯一真源，Markdown 为原子导出的可读副本。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..models import now_iso


class MarkdownExporter:
    def __init__(self, settings: Settings, repos: Any, registry: Any):
        self.settings = settings
        self.repos = repos
        self.registry = registry
        self.coupons_path = settings.memory_dir / "coupons.md"
        self.prices_path = settings.memory_dir / "price_history.md"
        self.decisions_path = settings.memory_dir / "decisions.md"

    # ------------------------------------------------------------------
    def export_all(self) -> dict[str, str]:
        self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
        self.export_coupons()
        self.export_prices()
        if self.settings.export_config.get("decisions_md", True):
            self.export_decisions()
        return {
            "coupons": str(self.coupons_path),
            "prices": str(self.prices_path),
            "decisions": str(self.decisions_path),
        }

    # ------------------------------------------------------------------
    def export_coupons(self) -> Path:
        lines = [
            "# 优惠券台账（导出副本）",
            "",
            f"> 生成时间：{now_iso()}；真源为 SQLite，本文件可随时重新生成。",
            "",
        ]
        for strategy in self.registry.enabled():
            coupons = self.repos.coupons.all(strategy.key)
            lines.extend([f"## {strategy.display_name}（{strategy.key}）", ""])
            if not coupons:
                lines.extend(["暂未采集到优惠券。", ""])
                continue
            lines.extend(
                [
                    "| 平台 | 券码 | 标题 | 类型 | 优惠 | 门槛 | 有效期至 | 状态 | 品类范围 |",
                    "|---|---|---|---|---|---|---|---|---|",
                ]
            )
            for coupon in coupons:
                value = (
                    f"{coupon.discount_rate * 100:.1f}%"
                    if coupon.coupon_type == "discount"
                    else f"¥{coupon.amount:.2f}"
                )
                scope = ",".join(
                    str(item)
                    for item in (coupon.scope.get("categories") or coupon.scope.get("scope") or [])
                )
                lines.append(
                    f"| {coupon.platform} | {coupon.coupon_code} | {coupon.title} | "
                    f"{coupon.coupon_type} | {value} | ¥{coupon.min_spend:.0f} | "
                    f"{coupon.valid_to or '-'} | {coupon.status} | {scope or '-'} |"
                )
            lines.append("")
        self._atomic_write(self.coupons_path, "\n".join(lines) + "\n")
        return self.coupons_path

    # ------------------------------------------------------------------
    def export_prices(self) -> Path:
        lines = [
            "# 价格变化台账（导出副本）",
            "",
            f"> 生成时间：{now_iso()}；真源为 SQLite，本文件可随时重新生成。",
            "",
        ]
        for strategy in self.registry.enabled():
            snapshots = self.repos.prices.recent(strategy.key, limit=240)
            lines.extend([f"## {strategy.display_name}（{strategy.key}）", ""])
            if not snapshots:
                lines.extend(["暂无价格快照。", ""])
                continue
            lines.extend(
                [
                    "| 采集时间 | 平台 | 标的 | 价格 | 税费 | 合计 | 来源 | 置信度 |",
                    "|---|---|---|---|---|---|---|---|",
                ]
            )
            for snapshot in snapshots:
                lines.append(
                    f"| {snapshot.captured_at} | {snapshot.platform} | {snapshot.item_name} | "
                    f"¥{snapshot.price:.2f} | ¥{snapshot.tax:.2f} | ¥{snapshot.total_price:.2f} | "
                    f"{snapshot.source} | {snapshot.confidence:.2f} |"
                )
            lines.append("")
        self._atomic_write(self.prices_path, "\n".join(lines) + "\n")
        return self.prices_path

    # ------------------------------------------------------------------
    def export_decisions(self) -> Path:
        lines = [
            "# 决策记录（导出副本）",
            "",
            f"> 生成时间：{now_iso()}；真源为 SQLite。",
            "",
        ]
        for strategy in self.registry.enabled():
            runs = self.repos.decisions.recent(strategy.key, limit=50)
            lines.extend([f"## {strategy.display_name}（{strategy.key}）", ""])
            if not runs:
                lines.extend(["暂无决策记录。", ""])
                continue
            lines.extend(
                [
                    "| 时间 | 预约 | 当前实付 | 预测中位 | 评分 | 建议 | 置信度 |",
                    "|---|---|---|---|---|---|---|",
                ]
            )
            for run in runs:
                try:
                    payload = json.loads(run.get("payload_json") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                judge_text = "是" if run.get("judge_used") else "否"
                lines.append(
                    f"| {run.get('created_at', '')} | {run.get('booking_id', '')} | "
                    f"¥{float(payload.get('current_effective_price') or 0):.2f} | "
                    f"¥{float(run.get('aggregated_pred') or 0):.2f} | "
                    f"{float(run.get('decision_score') or 0):.3f} | "
                    f"{run.get('verdict', '')} | {judge_text} |"
                )
            lines.append("")
        lines.extend(["## 最近决策明细", ""])
        for run in self.repos.decisions.recent(limit=20):
            try:
                payload = json.loads(run.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            lines.extend(
                [
                    f"### {run.get('created_at')} · {run.get('category')} · {run.get('verdict')}",
                    "",
                    f"- 当前实付：¥{float(payload.get('current_effective_price') or 0):.2f}",
                    f"- 预测区间：{payload.get('predicted_range')}",
                    f"- 决策评分：{run.get('decision_score')}；置信度：{payload.get('confidence')}",
                    f"- 理由：{'；'.join(payload.get('reasons') or [])}",
                    "",
                ]
            )
        self._atomic_write(self.decisions_path, "\n".join(lines) + "\n")
        return self.decisions_path

    # ------------------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
