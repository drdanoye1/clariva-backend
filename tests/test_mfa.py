"""
MFA (TOTP) — full enroll -> verify -> two-step login -> disable flow,
exercised through the real HTTP endpoints. Uses pyotp directly to compute
codes the same way an authenticator app would, so this is a real
end-to-end test of the flow rather than a mocked one.
"""
from __future__ import annotations

import uuid

import pyotp


def _register_and_login(client, label: str = "mfa") -> dict:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    password = "TestPassword123!"
    resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": password,
        "full_name": "MFA User", "organization": "Test Org",
    })
    assert resp.status_code == 201, resp.text
    login = client.post("/api/v1/auth/login", data={"username": email, "password": password})
    assert login.status_code == 200
    assert login.json()["mfa_required"] is False
    token = login.json()["access_token"]
    return {"email": email, "password": password, "headers": {"Authorization": f"Bearer {token}"}}


def test_setup_requires_auth(client):
    resp = client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 401


def test_setup_returns_secret_and_qr_code(client):
    user = _register_and_login(client)
    resp = client.post("/api/v1/auth/mfa/setup", headers=user["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["secret"]) >= 16
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert len(body["qr_code_png_base64"]) > 100  # a real base64 PNG, not empty


def test_verify_with_wrong_code_rejected(client):
    user = _register_and_login(client)
    client.post("/api/v1/auth/mfa/setup", headers=user["headers"])
    resp = client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=user["headers"])
    assert resp.status_code == 400


def test_full_enroll_and_two_step_login_flow(client):
    user = _register_and_login(client)

    setup = client.post("/api/v1/auth/mfa/setup", headers=user["headers"]).json()
    secret = setup["secret"]
    code = pyotp.TOTP(secret).now()

    verify = client.post("/api/v1/auth/mfa/verify", json={"code": code}, headers=user["headers"])
    assert verify.status_code == 200, verify.text
    assert verify.json()["mfa_enabled"] is True

    # Password-only login must now stop short of issuing real tokens.
    login = client.post("/api/v1/auth/login", data={"username": user["email"], "password": user["password"]})
    assert login.status_code == 200
    body = login.json()
    assert body["mfa_required"] is True
    assert body["access_token"] is None
    mfa_token = body["mfa_token"]
    assert mfa_token

    # Wrong code at the second step is rejected.
    bad = client.post("/api/v1/auth/mfa/login", json={"mfa_token": mfa_token, "code": "000000"})
    assert bad.status_code == 401

    # Correct code completes login.
    good_code = pyotp.TOTP(secret).now()
    completed = client.post("/api/v1/auth/mfa/login", json={"mfa_token": mfa_token, "code": good_code})
    assert completed.status_code == 200, completed.text
    assert completed.json()["access_token"]
    assert completed.json()["mfa_required"] is False


def test_mfa_login_rejects_garbage_challenge_token(client):
    resp = client.post("/api/v1/auth/mfa/login", json={"mfa_token": "not-a-real-token", "code": "123456"})
    assert resp.status_code == 401


def test_disable_requires_correct_password(client):
    user = _register_and_login(client)
    setup = client.post("/api/v1/auth/mfa/setup", headers=user["headers"]).json()
    code = pyotp.TOTP(setup["secret"]).now()
    client.post("/api/v1/auth/mfa/verify", json={"code": code}, headers=user["headers"])

    resp = client.post("/api/v1/auth/mfa/disable",
                        json={"password": "wrong-password", "code": code}, headers=user["headers"])
    assert resp.status_code == 400


def test_disable_turns_mfa_off_and_login_no_longer_requires_it(client):
    user = _register_and_login(client)
    setup = client.post("/api/v1/auth/mfa/setup", headers=user["headers"]).json()
    secret = setup["secret"]
    code = pyotp.TOTP(secret).now()
    client.post("/api/v1/auth/mfa/verify", json={"code": code}, headers=user["headers"])

    disable_code = pyotp.TOTP(secret).now()
    resp = client.post("/api/v1/auth/mfa/disable",
                        json={"password": user["password"], "code": disable_code}, headers=user["headers"])
    assert resp.status_code == 204

    login = client.post("/api/v1/auth/login", data={"username": user["email"], "password": user["password"]})
    assert login.status_code == 200
    assert login.json()["mfa_required"] is False
    assert login.json()["access_token"]
