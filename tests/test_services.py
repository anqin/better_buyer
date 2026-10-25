"""打分、护栏、状态机、事件总线、安全与记忆。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from better_buyer.core.events import EventBus
from better_buyer.core.security import CredentialVault, TokenManager, mask_account
from better_buyer.models import ModelPrediction
from better_buyer.services.scoring import EnsembleScorer, Guardrail


def test_ensemble_scorer_combines_models():
    scorer = EnsembleScorer(
        {
            "scoring": {
                "model": {
                    "direction": 0.30,
                    "plausibility": 0.20,
                    "confidence": 0.20,
                    "reliability": 0.20,
                    "evidence": 0.05,
                    "cost": 0.05,
                },
                "final": {"local": 0.50, "models": 0.40, "judge": 0.10},
                "thresholds": {"judge_escalation": 0.25},
            }
        }
    )
    predictions = [
        ModelPrediction(provider="a", model="m1", prediction=700, direction="down", confidence=0.8, reasons=["r"], evidence=["e"]),
        ModelPrediction(provider="b", model="m2", prediction=760, direction="down", confidence=0.6, reasons=["r"]),
        ModelPrediction(provider="c", model="m3", failed=True, error="timeout"),
    ]
    result = scorer.score(local_prediction=720, local_quality=0.6, predictions=predictions)

    assert 700 <= result.predicted_final <= 760
    assert 0.0 <= result.decision_score <= 1.0
    assert result.direction == "down"
    assert result.successful_predictions == 2
    assert result.low_diversity is False
    assert scorer.needs_judge(result.disagreement, result.decision_score, 0.60) in (True, False)


def test_guardrail_blocks_stale_budget_and_outlier():
    guardrail = Guardrail(stale_hours=6)
    offer = SimpleNamespace(paid_price=900.0, attributes={})

    stale = guardrail.check(9999, [offer], [], 5000, 900, 910)
    assert stale.ok is False
    assert any("未更新" in reason for reason in stale.reasons)

    over_budget = guardrail.check(10, [offer], [], 500, 900, 910)
    assert over_budget.ok is False
    assert any("预算" in reason for reason in over_budget.reasons)

    outlier = guardrail.check(10, [offer], [], 5000, 900, 2000)
    assert outlier.ok is False
    assert any("限幅" in reason for reason in outlier.reasons)

    assert guardrail.check(10, [offer], [], 5000, 900, 910).ok is True


def test_state_machine_transitions():
    from better_buyer.services.state_machine import InvalidTransition, can_transition, transition

    session = SimpleNamespace(state="Idle")
    assert can_transition("Idle", "Collecting") is True
    transition(session, "Collecting")
    assert session.state == "Collecting"
    transition(session, "Ready")
    transition(session, "Watching")
    transition(session, "SelectingOffer")
    transition(session, "Purchasing")
    transition(session, "Completed")
    assert session.state == "Completed"

    with pytest.raises(InvalidTransition):
        transition(session, "Purchasing")


def test_event_bus_cursor_and_recent():
    bus = EventBus()
    first = bus.publish({"event": "decision", "value": 1})
    second = bus.publish({"event": "notification", "value": 2})
    assert first["id"] == 1 and second["id"] == 2
    assert [event["id"] for event in bus.since(1)] == [2]
    assert bus.latest_cursor() == 2
    assert len(bus.recent(1)) == 1


def test_tokens_and_pairing_and_mask(container):
    tokens = TokenManager("test-secret", access_minutes=15)
    token = tokens.issue("user-1", "access")
    payload = tokens.verify(token, "access")
    assert payload and payload["sub"] == "user-1"
    assert tokens.verify(token + "x", "access") is None
    assert tokens.verify(token, "refresh") is None

    code = container.pairing.generate("user-1")
    wrong = "000001" if code != "000001" else "000002"
    assert container.pairing.consume(wrong) is None
    assert container.pairing.consume(code) == "user-1"
    assert container.pairing.consume(code) is None

    assert mask_account("someone@example.com") == "som***@example.com"
    assert mask_account("13812345678") == "138****5678"


def test_credential_vault_roundtrip():
    vault = CredentialVault("unit-test-master-key")
    if not vault.available:
        pytest.skip("cryptography 未安装")
    ciphertext, nonce, version = vault.encrypt("secret-password")
    assert "secret-password" not in ciphertext
    assert vault.decrypt(ciphertext, nonce, version) == "secret-password"

    with pytest.raises(Exception):
        CredentialVault("another-key").decrypt(ciphertext, nonce, version)


def test_memory_markdown_never_contains_secret(settings):
    from better_buyer.services.memory import MemoryStore

    memory = MemoryStore(settings)
    memory.ensure()
    memory.write_platform_accounts(
        [
            {
                "platform": "ctrip",
                "account_alias": "som***@example.com",
                "auth_type": "encrypted_session",
                "ciphertext": "encrypted-blob",
                "expires_at": "2026-12-01",
            }
        ]
    )
    accounts = (settings.memory_dir / "platform_accounts.md").read_text(encoding="utf-8")
    legacy = (settings.memory_dir / "platform_credentials.md").read_text(encoding="utf-8")
    assert "som***@example.com" in accounts
    assert "encrypted-blob" not in accounts
    assert accounts == legacy
