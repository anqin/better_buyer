"""HTTP API 与配对流程（TestClient，不启动调度器）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from better_buyer.api.app import create_app


def test_api_health_auth_chat_and_pairing(container):
    client = TestClient(create_app(container=container, start_scheduler=False))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    register = client.post(
        "/api/auth/register",
        json={"device_id": "api-device", "display_name": "API 测试"},
    )
    assert register.status_code == 200
    token = register.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/auth/me", headers=headers).json()["display_name"] == "API 测试"

    session = client.post("/api/chat/sessions", headers=headers)
    assert session.status_code == 200
    session_id = session.json()["session_id"]
    assert session.json()["greeting"]

    reply = client.post(
        f"/api/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"text": "11月8号北京飞上海，预算2000"},
    )
    assert reply.status_code == 200
    payload = reply.json()
    assert payload["state"] == "Ready"
    assert payload["booking_id"]

    started = client.post(
        f"/api/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"text": "1"},
    )
    assert started.status_code == 200
    payload = started.json()
    assert payload["state"] == "Watching"
    assert payload["offers"]

    bookings = client.get("/api/bookings", headers=headers)
    assert bookings.status_code == 200
    assert any(item["id"] == payload["booking_id"] for item in bookings.json()["bookings"])

    detail = client.get(f"/api/bookings/{payload['booking_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["booking"]["category"] == "flight"

    code_response = client.post("/api/auth/pairing/code", headers=headers)
    assert code_response.status_code == 200
    code = code_response.json()["code"]
    assert len(code) == 6

    paired = client.post(
        "/api/auth/pairing/verify",
        json={"code": code, "device_id": "phone-device", "display_name": "手机"},
    )
    assert paired.status_code == 200
    phone_token = paired.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {phone_token}"})
    assert me.status_code == 200
    assert me.json()["id"] == register.json()["user"]["id"]

    assert client.post(
        "/api/auth/pairing/verify",
        json={"code": "000000", "device_id": "phone-device"},
    ).status_code == 400
