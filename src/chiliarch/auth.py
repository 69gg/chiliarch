"""Single-user password sessions, CSRF checks and agent credential hashing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import delete, select

from chiliarch.database import Database, UserSession

if TYPE_CHECKING:
    from chiliarch.config import SettingsManager

SESSION_COOKIE = "chiliarch_session"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must have at least eight characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=67108864)
    return (
        "scrypt$32768$8$1$"
        + base64.urlsafe_b64encode(salt).decode()
        + "$"
        + base64.urlsafe_b64encode(derived).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or int(n) != 32768 or int(r) != 8 or int(p) != 1:
            return False
        derived = hashlib.scrypt(
            password.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=67108864,
        )
        return hmac.compare_digest(derived, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


class Authentication:
    def __init__(self, db: Database, settings: SettingsManager) -> None:
        self.db = db
        self.settings = settings
        self.attempts: defaultdict[str, deque[float]] = defaultdict(deque)

    def check_origin(self, request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        expected = self.settings.current.server.public_url or str(request.base_url).rstrip("/")
        parsed_expected = urlsplit(expected)
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme, parsed_origin.netloc) != (
            parsed_expected.scheme,
            parsed_expected.netloc,
        ):
            raise HTTPException(403, "Origin does not match this Chiliarch server")

    async def login(self, password: str, remote: str) -> tuple[str, str]:
        now = time.time()
        options = self.settings.current.auth
        attempts = self.attempts[remote]
        while attempts and now - attempts[0] > options.login_window_seconds:
            attempts.popleft()
        if len(attempts) >= options.max_login_attempts:
            raise HTTPException(429, "Too many login attempts; retry after the configured window")
        attempts.append(now)
        if not options.password_hash or not verify_password(password, options.password_hash):
            raise HTTPException(401, "Invalid password")
        attempts.clear()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with self.db.sessions.begin() as session:
            await session.execute(delete(UserSession).where(UserSession.expires_at < now))
            session.add(
                UserSession(
                    token_hash=hash_token(token),
                    csrf_token=csrf,
                    expires_at=now + options.session_ttl_seconds,
                )
            )
        return token, csrf

    async def session(self, token: str | None) -> UserSession | None:
        if not token:
            return None
        async with self.db.sessions() as session:
            return (
                await session.execute(
                    select(UserSession).where(
                        UserSession.token_hash == hash_token(token),
                        UserSession.expires_at > time.time(),
                    )
                )
            ).scalar_one_or_none()

    async def require_user(self, request: Request) -> UserSession:
        session = await self.session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(401, "Authentication required")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.check_origin(request)
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied, session.csrf_token):
                raise HTTPException(403, "A valid CSRF token is required")
        return session

    async def logout(self, token: str | None) -> None:
        if token:
            async with self.db.sessions.begin() as session:
                await session.execute(
                    delete(UserSession).where(UserSession.token_hash == hash_token(token))
                )
