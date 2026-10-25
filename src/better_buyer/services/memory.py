"""本地记忆文件：偏好读取与购买记录追加。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..models import Booking, Order, now


class MemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.memory_dir / "user_memory.md"
        self.accounts_path = settings.memory_dir / "platform_accounts.md"
        self.legacy_accounts_path = settings.memory_dir / "platform_credentials.md"

    # ------------------------------------------------------------------
    def ensure(self) -> None:
        self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                "# 用户记忆\n\n## 偏好\n- preferred_platforms: \n- cabin: \n- budget: \n\n## 购买记录\n",
                encoding="utf-8",
            )

    def load_preferences(self, user_id: str | None = None) -> dict[str, Any]:
        self.ensure()
        preferences: dict[str, Any] = {}
        in_section = False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                in_section = stripped == "## 偏好"
                continue
            if not in_section or not stripped.startswith("- "):
                continue
            body = stripped[2:]
            if ":" not in body:
                continue
            key, value = body.split(":", 1)
            key, value = key.strip(), value.strip()
            if not value:
                continue
            if key == "preferred_platforms":
                preferences[key] = [item.strip() for item in value.split(",") if item.strip()]
            elif key in ("budget",):
                try:
                    preferences[key] = float(value)
                except ValueError:
                    preferences[key] = value
            else:
                preferences[key] = value
        return preferences

    def save_preferences(self, preferences: dict[str, Any]) -> None:
        self.ensure()
        lines = ["# 用户记忆", "", "## 偏好"]
        for key, value in preferences.items():
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## 购买记录"])
        existing = self.path.read_text(encoding="utf-8")
        if "## 购买记录" in existing:
            purchase_part = existing.split("## 购买记录", 1)[1].strip()
            if purchase_part:
                lines.extend(["", purchase_part])
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    def append_purchase(self, order: Order, booking: Booking) -> None:
        self.ensure()
        content = self.path.read_text(encoding="utf-8")
        row = (
            f"| {now().strftime('%Y-%m-%d %H:%M')} | {order.category} | {booking.title} | "
            f"{order.platform} | {order.paid_price:.2f} | {order.discount:.2f} | "
            f"{order.order_no or order.id} |\n"
        )
        header = "| 时间 | 品类 | 标的 | 平台 | 实付 | 优惠 | 订单号 |\n|---|---|---|---|---|---|---|\n"
        if "| 时间 | 品类 |" not in content:
            content = content.rstrip() + "\n\n" + header
        self.path.write_text(content + row, encoding="utf-8")

    # ------------------------------------------------------------------
    def write_platform_accounts(self, rows: list[dict[str, Any]]) -> None:
        """只写脱敏别名与授权状态，绝不写密码/cookie/token 原文。"""
        self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 平台账号（脱敏）",
            "",
            "> 本文件只记录平台、脱敏账号别名、授权方式与状态。",
            "> 密码、支付密码、短信验证码、cookie 与 token 原文均不写入 Markdown。",
            "",
        ]
        for row in rows:
            lines.extend(
                [
                    f"- platform: {row.get('platform', '')}",
                    f"  account_alias: {row.get('account_alias', '***')}",
                    f"  auth_type: {row.get('auth_type', 'qr_session')}",
                    f"  status: {'authorized' if row.get('ciphertext') or row.get('auth_type') == 'alias_only' else 'unauthorized'}",
                    f"  expires_at: {row.get('expires_at') or '-'}",
                    "",
                ]
            )
        text = "\n".join(lines)
        self.accounts_path.write_text(text, encoding="utf-8")
        # 兼容旧文件名：内容始终为脱敏别名，不含凭据
        self.legacy_accounts_path.write_text(text, encoding="utf-8")

    def read_purchase_history(self, limit: int = 20) -> list[str]:
        self.ensure()
        content = self.path.read_text(encoding="utf-8")
        rows = [line for line in content.splitlines() if line.startswith("| 20")]
        return rows[-limit:]
