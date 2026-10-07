"""Run in an isolated installation of the actual wheel or source distribution."""

from __future__ import annotations

import importlib.resources
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from chiliarch import __version__
from chiliarch.api import create_app
from chiliarch.auth import hash_password
from chiliarch.config import Settings
from chiliarch.protocol import Envelope, protocol_schema


def main() -> None:
    resources = importlib.resources.files("chiliarch")
    assert resources.joinpath("static/index.html").is_file()
    assert json.loads(resources.joinpath("protocol/schema.json").read_text()) == protocol_schema()
    assert Envelope(type="heartbeat").protocol_version == "1.0"
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings.model_validate(
            {
                "database": {"url": f"sqlite+aiosqlite:///{Path(directory) / 'smoke.db'}"},
                "logs": {"directory": directory},
                "auth": {"password_hash": hash_password("smoke-test-password")},
            }
        )
        with TestClient(create_app(config=settings)) as client:
            assert client.get("/").status_code == 200
            response = client.post("/api/auth/login", json={"password": "smoke-test-password"})
            assert response.status_code == 200
            assert client.get("/api/state").status_code == 200
    print(f"chiliarch {__version__}: installed package, UI, schema and authenticated API verified")


if __name__ == "__main__":
    main()
