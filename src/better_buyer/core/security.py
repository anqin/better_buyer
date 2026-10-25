"""登录鉴权、配对码与凭据加密。"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import now


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass
class TokenManager:
    """无第三方依赖的 HMAC 签名令牌（生产可替换为 JWT 库）。"""

    secret: str
    access_minutes: int = 15
    refresh_days: int = 30

    def issue(self, user_id: str, kind: str = "access") -> str:
        ttl = self.access_minutes * 60 if kind == "access" else self.refresh_days * 86400
        payload = {
            "sub": user_id,
            "kind": kind,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl,
            "jti": secrets.token_hex(8),
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, token: str, kind: str = "access") -> dict | None:
        try:
            body, signature = token.split(".", 1)
        except ValueError:
            return None
        expected = _b64encode(
            hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_b64decode(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("kind") != kind or payload.get("exp", 0) < int(time.time()):
            return None
        return payload


@dataclass
class PairingStore:
    """一次性 6 位配对码，短时效、限次数。

    传入 db 时落库，CLI（`better-buyer pair`）与 `serve` 进程才能共享同一个码。
    """

    ttl_seconds: int = 120
    max_attempts: int = 5
    db: Any = None
    _code: str = ""
    _expires_at: float = 0.0
    _attempts: int = 0

    def generate(self, user_id: str = "") -> str:
        self._code = f"{secrets.randbelow(1_000_000):06d}"
        self._expires_at = time.time() + self.ttl_seconds
        self._attempts = 0
        if self.db is not None:
            self.db.execute("DELETE FROM pairing_codes")
            self.db.execute(
                "INSERT INTO pairing_codes(code, expires_at, attempts, user_id, created_at)"
                " VALUES (?, ?, 0, ?, ?)",
                [self._code, int(self._expires_at), user_id, now().isoformat(timespec="seconds")],
            )
        return self._code

    @property
    def current(self) -> str:
        if self.db is not None:
            row = self.db.query_one(
                "SELECT code, expires_at FROM pairing_codes ORDER BY expires_at DESC LIMIT 1"
            )
            if row and time.time() < row["expires_at"]:
                return str(row["code"])
            return ""
        if time.time() >= self._expires_at:
            return ""
        return self._code

    def verify(self, code: str) -> bool:
        return self.consume(code) is not None

    def consume(self, code: str) -> str | None:
        """校验并消费配对码，返回绑定的 user_id（无绑定时为空串）。"""
        if self.db is not None:
            return self._consume_db(code)
        if not self._code or time.time() >= self._expires_at:
            return None
        if self._attempts >= self.max_attempts:
            return None
        self._attempts += 1
        if hmac.compare_digest(code.strip(), self._code):
            self._code = ""
            return ""
        return None

    def _consume_db(self, code: str) -> str | None:
        row = self.db.query_one("SELECT * FROM pairing_codes ORDER BY expires_at DESC LIMIT 1")
        if not row or time.time() >= row["expires_at"]:
            return None
        if int(row["attempts"]) >= self.max_attempts:
            return None
        self.db.execute(
            "UPDATE pairing_codes SET attempts=attempts+1 WHERE code=?", [row["code"]]
        )
        if hmac.compare_digest(code.strip(), str(row["code"])):
            self.db.execute("DELETE FROM pairing_codes WHERE code=?", [row["code"]])
            return str(row.get("user_id") or "")
        return None


def mask_account(value: str) -> str:
    """邮箱/手机号脱敏。"""
    value = (value or "").strip()
    if "@" in value:
        name, domain = value.split("@", 1)
        keep = min(3, max(1, len(name)))
        return f"{name[:keep]}***@{domain}"
    if len(value) >= 11:
        return f"{value[:3]}****{value[-4:]}"
    if len(value) > 4:
        return f"{value[:2]}***{value[-2:]}"
    return "***"


@dataclass
class CredentialVault:
    """AES-256-GCM 凭据保险箱；密钥仅来自环境变量。"""

    master_key: str
    key_version: int = 1
    _available: bool | None = field(default=None, init=False)

    def _key(self) -> bytes:
        return hashlib.sha256(self.master_key.encode("utf-8")).digest()

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                import cryptography  # noqa: F401

                self._available = bool(self.master_key)
            except ImportError:
                self._available = False
        return bool(self._available)

    def encrypt(self, plaintext: str) -> tuple[str, str, int]:
        if not self.available:
            raise RuntimeError("凭据加密不可用：请设置 BB_MASTER_KEY 并安装 cryptography")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key()).encrypt(nonce, plaintext.encode("utf-8"), None)
        return _b64encode(ciphertext), _b64encode(nonce), self.key_version

    def decrypt(self, ciphertext: str, nonce: str, key_version: int = 1) -> str:
        if not self.available:
            raise RuntimeError("凭据解密不可用：请设置 BB_MASTER_KEY 并安装 cryptography")
        if key_version != self.key_version:
            raise ValueError("主密钥版本不匹配，请重新授权")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = AESGCM(self._key()).decrypt(_b64decode(nonce), _b64decode(ciphertext), None)
        return raw.decode("utf-8")


def pairing_expiry_text(seconds: int) -> str:
    return f"{max(1, seconds // 60)} 分钟"


def utc_timestamp() -> float:
    return calendar.timegm(now().timetuple())
