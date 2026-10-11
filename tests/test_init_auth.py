"""Initialization produces a usable, mandatory single-user login configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from chiliarch.api import create_app
from chiliarch.auth import SESSION_COOKIE
from chiliarch.cli import initialize
from chiliarch.config import Settings, atomic_write


def test_initialized_password_protects_management_and_agent_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "server" / "chiliarch.toml"
    password = "initialized-server-password"
    monkeypatch.setattr("chiliarch.cli.getpass.getpass", lambda _: password)
    initialize(config)

    with TestClient(create_app(config)) as client:
        # Initialization must result in actual authenticated access, rather than
        # only writing a password-shaped string to TOML.
        for path in (
            "/api/state",
            "/api/operations",
            "/api/chat/conversations",
            "/api/chat/conversations/not-visible/events",
            "/api/agent/events",
            "/api/agent/poll?timeout=0",
        ):
            assert client.get(path).status_code == 401
        assert (
            client.post("/api/operations/create_pairing_code", json={"arguments": {}}).status_code
            == 401
        )
        assert client.post("/api/agent/messages", json={}).status_code == 401
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/api/agent/ws"):
                pytest.fail("An unpaired agent must not establish a WebSocket")
        assert denied.value.code == 1008

        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        assert client.cookies.get(SESSION_COOKIE) is None
        login = client.post("/api/auth/login", json={"password": password})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        session = client.cookies[SESSION_COOKIE]
        assert client.get("/api/auth/session").json() == {
            "authenticated": True,
            "csrf_token": csrf,
        }
        assert client.get("/api/state").status_code == 200

        # A browser login does not replace an agent's independently issued
        # bearer credential on any of the three transport fallbacks.
        assert client.get("/api/agent/events").status_code == 401
        assert client.get("/api/agent/poll?timeout=0").status_code == 401
        assert client.post("/api/agent/messages", json={}).status_code == 401
        with pytest.raises(WebSocketDisconnect) as logged_in_denied:
            with client.websocket_connect("/api/agent/ws"):
                pytest.fail("A browser cookie must not authenticate an agent")
        assert logged_in_denied.value.code == 1008

        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.cookies.get(SESSION_COOKIE) is None
        # Replay the original cookie to distinguish server-side revocation from
        # simply clearing this browser's cookie jar.
        assert (
            client.get("/api/state", headers={"Cookie": f"{SESSION_COOKIE}={session}"}).status_code
            == 401
        )


def test_passwordless_configuration_cannot_start_application(tmp_path: Path) -> None:
    path = tmp_path / "chiliarch.toml"
    atomic_write(path, Settings())
    app = create_app(path)
    with pytest.raises(RuntimeError, match="No login password configured"):
        with TestClient(app):
            pytest.fail("An application without a login password must not start")
    assert not (tmp_path / "data" / "chiliarch.db").exists()
    assert app.state.service._maintenance_task is None
    assert app.state.supervisor.sweeper is None
