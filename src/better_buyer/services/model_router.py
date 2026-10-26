"""多 provider / 多 API Key 模型路由：并行、限流、熔断、预算与缓存。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.config import Settings
from ..core.logging import get_logger
from ..models import ModelPrediction, stable_hash, now

logger = get_logger("better_buyer.models")


class ModelCallError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class Provider:
    id: str
    family: str = ""
    enabled: bool = True
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    weight: float = 1.0
    max_rpm: int = 60
    cost_tier: int = 1
    temperature: float = 0.2
    roles: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provider":
        return cls(
            id=str(data.get("id", "")),
            family=str(data.get("family", "")),
            enabled=bool(data.get("enabled", True)),
            base_url=str(data.get("base_url", "")),
            model=str(data.get("model", "")),
            api_key_env=str(data.get("api_key_env", "")),
            weight=float(data.get("weight", 1.0)),
            max_rpm=int(data.get("max_rpm", 60)),
            cost_tier=int(data.get("cost_tier", 1)),
            temperature=float(data.get("temperature", 0.2)),
            roles=list(data.get("roles") or []),
            categories=list(data.get("categories") or []),
        )

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class ProviderState:
    failures: int = 0
    cooldown_until: float = 0.0
    call_times: list[float] = field(default_factory=list)
    calls_today: int = 0
    day: str = ""
    used_tokens: int = 0
    last_error: str = ""


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class ModelRouter:
    """按角色与品类调度全部健康的 provider；单 Key 故障自动切换/熔断。"""

    def __init__(
        self,
        settings: Settings,
        repos: Any = None,
        transport: Callable[[Provider, list[dict[str, str]]], str] | None = None,
    ):
        self.settings = settings
        self.repos = repos
        self.config = settings.llm_config
        self.enabled = bool(self.config.get("enabled", True))
        self.timeout_seconds = float(self.config.get("timeout_seconds", 20))
        self.max_parallel = int(self.config.get("max_parallel", 4))
        self.cache_ttl = int(self.config.get("cache_ttl_minutes", 10)) * 60
        self.roles = dict(self.config.get("roles") or {})
        self.providers = [
            Provider.from_dict(item) for item in self.config.get("providers") or [] if item.get("id")
        ]
        self.by_id = {provider.id: provider for provider in self.providers}
        self.states: dict[str, ProviderState] = {p.id: ProviderState() for p in self.providers}
        self.budget = dict(self.config.get("budget") or {})
        self.overrides = dict(self.config.get("category_overrides") or {})
        self.transport = transport or self._http_transport
        self._cache: dict[str, tuple[float, list[ModelPrediction]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 候选与健康
    # ------------------------------------------------------------------
    def candidates(self, role: str, category: str) -> list[Provider]:
        if not self.enabled:
            return []
        role_cfg = self.roles.get(role) or {}
        pool = list(role_cfg.get("providers") or [])
        excluded = set((self.overrides.get(category) or {}).get("exclude") or [])
        result: list[Provider] = []
        for provider_id in pool:
            provider = self.by_id.get(provider_id)
            if not provider or provider_id in excluded or not provider.enabled:
                continue
            if role not in provider.roles:
                continue
            if provider.categories and category not in provider.categories:
                continue
            if not provider.key_configured:
                continue
            state = self.states[provider_id]
            self._roll_day(state)
            if state.cooldown_until > time.time():
                continue
            if provider.max_rpm > 0:
                window = [t for t in state.call_times if time.time() - t < 60]
                state.call_times = window
                if len(window) >= provider.max_rpm:
                    continue
            if self.daily_budget_exhausted(state):
                continue
            result.append(provider)
        return result

    def _roll_day(self, state: ProviderState) -> None:
        today = now().date().isoformat()
        if state.day != today:
            state.day = today
            state.calls_today = 0
            state.used_tokens = 0

    def daily_budget_exhausted(self, state: ProviderState) -> bool:
        limit = int(self.budget.get("daily_token_limit", 0) or 0)
        return bool(limit and state.used_tokens >= limit)

    def _trim(self, role: str, category: str) -> list[Provider]:
        providers = self.candidates(role, category)
        limit = int(self.budget.get("max_calls_per_decision", 0) or 0)
        if limit and len(providers) > limit:
            # 超预算时按 cost_tier 从高到低裁剪，优先保留低成本模型
            providers = sorted(providers, key=lambda p: (p.cost_tier, -p.weight))[:limit]
        return providers[: max(1, self.max_parallel)]

    def min_participants(self) -> int:
        return int((self.roles.get("predictor") or {}).get("min_participants", 2) or 0)

    # ------------------------------------------------------------------
    # 预测
    # ------------------------------------------------------------------
    async def predict(
        self,
        category: str,
        features: dict[str, Any],
        prompt: str,
        query_hash: str = "",
    ) -> list[ModelPrediction]:
        providers = self._trim("predictor", category)
        if not providers:
            return []
        cache_key = stable_hash(
            {"category": category, "query": query_hash, "hour": now().strftime("%Y-%m-%dT%H")}
        )
        cached = self._cache.get(cache_key)
        if cached and time.time() < cached[0]:
            return cached[1]

        messages = [
            {
                "role": "system",
                "content": "你是机票/酒店/商品价格预测器。只输出 JSON，不输出解释性散文。",
            },
            {"role": "user", "content": prompt},
        ]
        tasks = [self._predict_one(provider, messages, category, features) for provider in providers]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        self._cache[cache_key] = (time.time() + self.cache_ttl, results)
        return results

    async def _predict_one(
        self,
        provider: Provider,
        messages: list[dict[str, str]],
        category: str,
        features: dict[str, Any],
    ) -> ModelPrediction:
        state = self.states[provider.id]
        started = time.perf_counter()
        last_error = ""
        for attempt in range(2):
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self.transport, provider, messages),
                    timeout=self.timeout_seconds,
                )
                data = _extract_json(raw)
                if data is None:
                    last_error = "模型输出不是合法 JSON"
                    continue
                prediction = float(data.get("predicted_price"))
                if prediction <= 0:
                    last_error = "predicted_price 非法"
                    continue
                direction = str(data.get("direction", "flat")).lower()
                if direction not in ("down", "flat", "up"):
                    direction = "flat"
                confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
                reasons = [str(x) for x in (data.get("reasons") or [])][:5]
                evidence = [str(x) for x in (data.get("evidence") or [])][:5]
                stats = self._model_stats(category, provider)
                self._record_success(provider, state)
                return ModelPrediction(
                    provider=provider.id,
                    model=provider.model,
                    role="predictor",
                    prediction=prediction,
                    direction=direction,
                    confidence=confidence,
                    reasons=reasons,
                    evidence=evidence,
                    cost_usd=round(0.0002 * provider.cost_tier, 6),
                    is_cold_start=stats.get("samples", 0) < 20,
                    reliability=float(stats.get("reliability_score", 0.5)),
                    key_alias=provider.id,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except asyncio.TimeoutError:
                last_error = "模型调用超时"
            except ModelCallError as exc:
                last_error = str(exc)
                if exc.status in (401, 403, 429):
                    state.cooldown_until = time.time() + 60
                    break
            except Exception as exc:
                last_error = str(exc)
        self._record_failure(provider, state, last_error)
        return ModelPrediction(
            provider=provider.id,
            model=provider.model,
            role="predictor",
            failed=True,
            error=last_error,
            key_alias=provider.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------
    # judge / nlu
    # ------------------------------------------------------------------
    async def judge(
        self,
        category: str,
        features: dict[str, Any],
        votes: list[dict[str, Any]],
        disagreement: float,
    ) -> ModelPrediction | None:
        providers = self._trim("judge", category)
        if not providers:
            return None
        provider = providers[0]
        prompt = (
            "请复核以下价格预测分歧，输出 JSON："
            '{"predicted_price": 数值, "direction": "down|flat|up", "confidence": 0-1, '
            '"reasons": ["..."], "evidence": ["..."]}\n'
            f"特征：{json.dumps(features, ensure_ascii=False, default=str)}\n"
            f"模型票：{json.dumps(votes, ensure_ascii=False)}\n分歧度：{disagreement:.2f}"
        )
        messages = [
            {"role": "system", "content": "你是审慎的决策复核员，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.transport, provider, messages), timeout=self.timeout_seconds
            )
            data = _extract_json(raw) or {}
            prediction = float(data.get("predicted_price") or 0)
            if prediction <= 0:
                return None
            self._record_success(provider, self.states[provider.id])
            return ModelPrediction(
                provider=provider.id,
                model=provider.model,
                role="judge",
                prediction=prediction,
                direction=str(data.get("direction", "flat")).lower(),
                confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
                reasons=[str(x) for x in (data.get("reasons") or [])][:5],
                evidence=[str(x) for x in (data.get("evidence") or [])][:5],
                reliability=float(self._model_stats(category, provider).get("reliability_score", 0.5)),
                key_alias=provider.id,
            )
        except Exception as exc:
            self._record_failure(provider, self.states[provider.id], str(exc))
            return None

    async def extract_slots(
        self, category: str, text: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        providers = self._trim("nlu", category)
        if not providers:
            return None
        provider = providers[0]
        prompt = (
            "从用户话语中抽取购物槽位，只输出 JSON："
            '{"slots": {...}, "budget": 数值或null}\n'
            f"槽位 schema：{json.dumps(schema, ensure_ascii=False)}\n"
            f"用户输入：{text}"
        )
        messages = [
            {"role": "system", "content": "你是严谨的槽位抽取器，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.transport, provider, messages), timeout=self.timeout_seconds
            )
            data = _extract_json(raw)
            if data and isinstance(data.get("slots"), dict):
                self._record_success(provider, self.states[provider.id])
                return data
        except Exception as exc:
            self._record_failure(provider, self.states[provider.id], str(exc))
        return None

    async def classify_intent(self, text: str, labels: dict[str, str]) -> dict[str, float] | None:
        """实时意图预测：返回各候选品类 {key: 0~1 置信度}，无 NLU 可用时返回 None。"""
        if not self.enabled:
            return None
        provider = None
        for candidate in self.providers:
            if "nlu" in candidate.roles and candidate.enabled and candidate.key_configured:
                provider = candidate
                break
        if provider is None:
            return None
        candidates = "、".join(f"{key}({label})" for key, label in labels.items())
        prompt = (
            "判断用户话语最可能落在哪个购物品类上，只输出 JSON："
            f'{{"{labels} 各键": 0~1 置信度}}\n'
            f"候选品类：{candidates}\n"
            f"用户输入：{text}"
        )
        messages = [
            {"role": "system", "content": "你是严谨的意图分类器，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.transport, provider, messages), timeout=self.timeout_seconds
            )
            data = _extract_json(raw)
            if isinstance(data, dict):
                return {
                    key: max(0.0, min(1.0, float(value)))
                    for key, value in data.items()
                    if key in labels
                }
        except Exception as exc:
            self._record_failure(provider, self.states[provider.id], str(exc))
        return None

    # ------------------------------------------------------------------
    # 健康
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        items = []
        now_ts = time.time()
        for provider in self.providers:
            state = self.states[provider.id]
            items.append(
                {
                    "id": provider.id,
                    "family": provider.family,
                    "model": provider.model,
                    "enabled": provider.enabled,
                    "key_configured": provider.key_configured,
                    "roles": provider.roles,
                    "categories": provider.categories,
                    "failures": state.failures,
                    "cooldown_remaining_s": max(0, int(state.cooldown_until - now_ts)),
                    "calls_today": state.calls_today,
                    "used_tokens": state.used_tokens,
                    "last_error": state.last_error,
                }
            )
        return {
            "enabled": self.enabled,
            "providers": items,
            "min_participants": self.min_participants(),
            "budget": self.budget,
            "transport": getattr(self.transport, "__name__", "custom"),
        }

    # ------------------------------------------------------------------
    def _model_stats(self, category: str, provider: Provider) -> dict[str, Any]:
        if self.repos and hasattr(self.repos, "model_stats"):
            row = self.repos.model_stats.get(category, provider.id, provider.model)
            if row:
                return row
        return {"samples": 0, "reliability_score": 0.5}

    def _record_success(self, provider: Provider, state: ProviderState) -> None:
        self._roll_day(state)
        state.call_times.append(time.time())
        state.calls_today += 1
        state.used_tokens += 800  # 估算值，用于预算保护
        state.failures = 0
        state.last_error = ""

    def _record_failure(self, provider: Provider, state: ProviderState, error: str) -> None:
        self._roll_day(state)
        state.failures += 1
        state.last_error = error
        if state.failures >= 3:
            state.cooldown_until = time.time() + 60

    def _http_transport(self, provider: Provider, messages: list[dict[str, str]]) -> str:
        if not provider.base_url or not provider.api_key:
            raise ModelCallError("provider 未配置 base_url 或 API Key", 401)
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": provider.temperature,
        }
        request = urllib.request.Request(
            provider.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {provider.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ModelCallError(f"HTTP {exc.code}: {provider.id}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise ModelCallError(f"网络错误：{exc.reason}") from exc
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelCallError("模型响应结构异常") from exc

    def clear_cache(self) -> None:
        self._cache.clear()
