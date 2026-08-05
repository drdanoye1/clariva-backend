"""Auth router — register / login / me / update / password change."""
from __future__ import annotations


def test_register_creates_user(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "SecurePass123!",
            "full_name": "New User",
            "organization": "Acme Research",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "newuser@example.com"
    assert "hashed_password" not in body  # never leak the hash


def test_register_rejects_duplicate_email(client):
    payload = {
        "email": "dupe@example.com",
        "password": "SecurePass123!",
        "full_name": "Dupe User",
        "organization": "Acme Research",
    }
    first = client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 400


def test_register_rejects_short_password(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "shortpw@example.com",
            "password": "short",
            "full_name": "Short PW",
            "organization": "Acme Research",
        },
    )
    assert resp.status_code == 422


def test_login_with_wrong_password_rejected(client, registered_user):
    resp = client.post(
        "/api/v1/auth/login",
        data={"username": registered_user["email"], "password": "wrong-password"},
    )
    assert resp.status_code == 401


def test_login_returns_usable_bearer_token(client, registered_user):
    me = client.get("/api/v1/auth/me", headers=registered_user["headers"])
    assert me.status_code == 200
    assert me.json()["email"] == registered_user["email"]


def test_me_rejects_garbage_token(client):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_update_me_changes_full_name(client, registered_user):
    resp = client.patch(
        "/api/v1/auth/me",
        json={"full_name": "Updated Name"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Updated Name"


def test_change_password_then_login_with_new_password(client, registered_user):
    resp = client.patch(
        "/api/v1/auth/me/password",
        json={"current_password": registered_user["password"], "new_password": "BrandNewPass456!"},
        headers=registered_user["headers"],
    )
    assert resp.status_code == 204

    old_login = client.post(
        "/api/v1/auth/login",
        data={"username": registered_user["email"], "password": registered_user["password"]},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        data={"username": registered_user["email"], "password": "BrandNewPass456!"},
    )
    assert new_login.status_code == 200
