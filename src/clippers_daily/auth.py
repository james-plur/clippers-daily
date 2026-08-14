from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


@dataclass
class Session:
    token: str
    csrf: str
    last_seen: float


class AuthManager:
    def __init__(self, ttl: int = 86400):
        self.ttl = ttl
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.hasher = PasswordHasher()

    @property
    def password_hash(self) -> str:
        return os.getenv("PAPERLAB_ADMIN_PASSWORD_HASH", "").strip()

    def login(self, username: str, password: str, remote_ip: str) -> Session | None:
        now = time.time()
        recent = [value for value in self.failures.get(remote_ip, []) if now - value < 300]
        self.failures[remote_ip] = recent
        if len(recent) >= 10:
            return None
        expected = os.getenv("PAPERLAB_ADMIN_USERNAME", "admin")
        try:
            valid = bool(self.password_hash) and hmac.compare_digest(username, expected) and self.hasher.verify(self.password_hash, password)
        except (VerifyMismatchError, ValueError):
            valid = False
        if not valid:
            self.failures.setdefault(remote_ip, []).append(now)
            return None
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
        with self.lock:
            self.sessions[session.token] = session
            self.failures.pop(remote_ip, None)
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self.sessions.get(token)
        now = time.time()
        if not session or now - session.last_seen > self.ttl:
            self.sessions.pop(token, None)
            return None
        session.last_seen = now
        return session

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)


def secret_status(config: dict) -> dict:
    result = {}
    for provider in config.get("llm", {}).get("providers", []):
        path = provider.get("api_key_file")
        result[f"llm:{provider['id']}"] = bool(os.getenv(provider.get("api_key_env", ""))) or bool(path and Path(path).is_file())
    for sender in config.get("email", {}).get("senders", []):
        result[f"smtp:{sender['id']}"] = Path(sender.get("password_file", "/nonexistent")).is_file()
    return result
