from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from chiliarch.api import create_app
from chiliarch.auth import hash_password
from chiliarch.config import Settings


def test_session_csrf_password_change_and_route_isolation(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{tmp_path}/api.db"},
            "logs": {"directory": str(tmp_path / "logs")},
            "auth": {"password_hash": hash_password("test-password")},
        }
    )
    with TestClient(create_app(config=settings)) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/auth/session").json() == {
            "authenticated": False,
            "csrf_token": None,
        }
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        logged_in = client.post("/api/auth/login", json={"password": "test-password"})
        assert logged_in.status_code == 200
        csrf = logged_in.json()["csrf_token"]
        assert client.get("/api/state").status_code == 200
        assert (
            client.post("/api/operations/create_pairing_code", json={"arguments": {}}).status_code
            == 403
        )
        assert (
            client.post(
                "/api/operations/create_pairing_code",
                json={"arguments": {}},
                headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
        pairing = client.post(
            "/api/operations/create_pairing_code",
            json={"arguments": {}},
            headers={"X-CSRF-Token": csrf},
        ).json()
        agent = client.post(
            "/api/agent/pair", json={"code": pairing["code"], "name": "demo", "machine": "machine"}
        ).json()
        assert (
            client.get(
                "/api/state",
                headers={"Authorization": f"Bearer {agent['credential']}"},
                cookies={"chiliarch_session": "invalid"},
            ).status_code
            == 401
        )
        public = client.get("/api/state").json()
        assert public["config"]["auth"]["password_hash"] == "********"
        assert "credential_hash" not in public["agents"][0]
        changed = client.post(
            "/api/operations/change_password",
            json={"arguments": {"password": "new-password"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/auth/login", json={"password": "new-password"}).status_code == 200
        assert client.get("/api/missing").status_code == 404
