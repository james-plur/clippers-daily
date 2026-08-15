from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
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
    def __init__(self, ttl: int = 86400, database: Path | None = None):
        self.ttl = ttl
        self.database = database
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.hasher = PasswordHasher()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _database(self) -> sqlite3.Connection | None:
        if not self.database:
            return None
        db = sqlite3.connect(self.database, timeout=10)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
          token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL)""")
        return db

    @property
    def password_hash(self) -> str:
        return os.getenv("PAPERLAB_ADMIN_PASSWORD_HASH", "").strip()

    def login(self, username: str, password: str, remote_ip: str) -> Session | None:
        now = time.time()
        with self.lock:
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
            with self.lock:
                self.failures.setdefault(remote_ip, []).append(now)
            return None
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
        with self.lock:
            self.sessions[session.token] = session
            self.failures.pop(remote_ip, None)
        db = self._database()
        if db:
            with db:
                db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
                db.execute("INSERT OR REPLACE INTO web_sessions VALUES (?,?,?,?)",
                           (self._token_hash(session.token), session.csrf, now, now + self.ttl))
            db.close()
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self.lock:
            session = self.sessions.get(token)
            now = time.time()
            if session and now - session.last_seen <= self.ttl:
                session.last_seen = now
                persistent = self._database()
                if persistent:
                    with persistent:
                        persistent.execute("UPDATE web_sessions SET last_seen=?,expires_at=? WHERE token_hash=?",
                                           (now, now + self.ttl, self._token_hash(token)))
                    persistent.close()
                return session
            if session:
                self.sessions.pop(token, None)
        db = self._database()
        if not db:
            return None
        row = db.execute("SELECT csrf,last_seen,expires_at FROM web_sessions WHERE token_hash=?",
                         (self._token_hash(token),)).fetchone()
        if not row or row[2] < now:
            with db:
                db.execute("DELETE FROM web_sessions WHERE token_hash=?", (self._token_hash(token),))
            db.close()
            return None
        with db:
            db.execute("UPDATE web_sessions SET last_seen=?,expires_at=? WHERE token_hash=?",
                       (now, now + self.ttl, self._token_hash(token)))
        db.close()
        session = Session(token, row[0], now)
        with self.lock:
            self.sessions[token] = session
        return session

    def logout(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.sessions.pop(token, None)
            db = self._database()
            if db:
                with db:
                    db.execute("DELETE FROM web_sessions WHERE token_hash=?", (self._token_hash(token),))
                db.close()


def secret_status(config: dict) -> dict:
    result = {}
    for provider in config.get("llm", {}).get("providers", []):
        path = provider.get("api_key_file")
        result[f"llm:{provider['id']}"] = bool(os.getenv(provider.get("api_key_env", ""))) or bool(path and Path(path).is_file())
    for sender in config.get("email", {}).get("senders", []):
        result[f"smtp:{sender['id']}"] = Path(sender.get("password_file", "/nonexistent")).is_file()
    return result
