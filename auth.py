"""Password hashing and in-memory sessions for the camera dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 5
MIN_SCRYPT_N = 2**12
MAX_SCRYPT_N = 2**17
MIN_SCRYPT_STRENGTH = (2**14) * 5
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
SESSION_COOKIE = "__Host-sentinel_session"
LOOPBACK_SESSION_COOKIE = "sentinel_session"
LOGIN_WINDOW_SECONDS = 60
LOGIN_ATTEMPTS_PER_WINDOW = 5
MAX_SESSIONS = 16


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """Return a versioned, salted scrypt password verifier."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be between {MIN_PASSWORD_LENGTH} and "
            f"{MAX_PASSWORD_LENGTH} characters"
        )
    if n < MIN_SCRYPT_N or n > MAX_SCRYPT_N or n & (n - 1):
        raise ValueError("scrypt work factor must be a supported power of two")
    salt = secrets.token_bytes(16)
    verifier = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64encode(salt)}${_b64encode(verifier)}"


def _parse_password_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_verifier = encoded.split("$")
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = _b64decode(raw_salt)
        verifier = _b64decode(raw_verifier)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("configured password hash is invalid") from exc
    if (
        algorithm != "scrypt"
        or n < MIN_SCRYPT_N
        or n > MAX_SCRYPT_N
        or n & (n - 1)
        or not 1 <= r <= SCRYPT_R
        or not 1 <= p <= 10
        or not 16 <= len(salt) <= 32
        or len(verifier) != 32
    ):
        raise ValueError("configured password hash is invalid")
    return n, r, p, salt, verifier


def verify_password(password: str, encoded: str) -> bool:
    """Validate a password without allowing a malformed verifier to consume excess RAM."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        n, r, p, salt, expected = _parse_password_hash(encoded)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, UnicodeEncodeError):
        return False


def load_password_hash(path: str) -> str:
    password_file = Path(path)
    try:
        metadata = password_file.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"CAMERA_PASSWORD_FILE is not a regular file: {path}")
        if metadata.st_mode & stat.S_IROTH:
            raise ValueError(f"CAMERA_PASSWORD_FILE must not be world-readable: {path}")
        if metadata.st_size > 4096:
            raise ValueError("CAMERA_PASSWORD_FILE is unexpectedly large")
        encoded = password_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"could not read CAMERA_PASSWORD_FILE: {path}") from exc
    if not encoded:
        raise ValueError("CAMERA_PASSWORD_FILE is empty")
    return encoded


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str
    session_seconds: int
    secure_cookie: bool

    @classmethod
    def from_environment(cls, *, secure_transport: bool) -> "AuthConfig":
        username = os.getenv("CAMERA_USERNAME", "admin")
        if not username or len(username) > 64 or any(ord(char) < 32 for char in username):
            raise ValueError("CAMERA_USERNAME must be 1-64 printable characters")

        encoded = os.getenv("CAMERA_PASSWORD_HASH")
        if not encoded:
            password_file = os.getenv(
                "CAMERA_PASSWORD_FILE", "/etc/raspi-security-camera/password-hash"
            )
            encoded = load_password_hash(password_file)
        n, _r, p, _salt, _verifier = _parse_password_hash(encoded)
        if n * p < MIN_SCRYPT_STRENGTH:
            raise ValueError("configured password hash uses an insufficient work factor")

        raw_session_seconds = os.getenv("CAMERA_SESSION_SECONDS", "43200")
        try:
            session_seconds = int(raw_session_seconds)
        except ValueError as exc:
            raise ValueError("CAMERA_SESSION_SECONDS must be an integer") from exc
        if not 300 <= session_seconds <= 604800:
            raise ValueError("CAMERA_SESSION_SECONDS must be between 300 and 604800")
        return cls(username, encoded, session_seconds, secure_transport)


@dataclass(frozen=True)
class AuthSession:
    token: str
    csrf_token: str
    username: str
    created_at: float
    expires_at: float


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after = retry_after


class Authenticator:
    """Verifies one device account and owns short-lived opaque sessions."""

    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.cookie_name = SESSION_COOKIE if config.secure_cookie else LOOPBACK_SESSION_COOKIE
        self._lock = threading.Lock()
        self._sessions: dict[str, AuthSession] = {}
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)

    def login(self, username: str, password: str, client_ip: str) -> AuthSession | None:
        now = time.monotonic()
        self._claim_login_attempt(client_ip, now)

        valid_password = verify_password(password, self.config.password_hash)
        valid_username = hmac.compare_digest(username, self.config.username)
        if not (valid_password and valid_username):
            return None

        issued_at = time.monotonic()
        token = secrets.token_urlsafe(32)
        session = AuthSession(
            token=token,
            csrf_token=secrets.token_urlsafe(32),
            username=self.config.username,
            created_at=issued_at,
            expires_at=issued_at + self.config.session_seconds,
        )
        with self._lock:
            self._attempts.pop(client_ip, None)
            self._prune_sessions_locked(issued_at)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda item: item.created_at)
                self._sessions.pop(oldest.token, None)
            self._sessions[token] = session
        return session

    def _claim_login_attempt(self, client_ip: str, now: float) -> None:
        with self._lock:
            attempts = self._attempts[client_ip]
            cutoff = now - LOGIN_WINDOW_SECONDS
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= LOGIN_ATTEMPTS_PER_WINDOW:
                retry_after = max(1, int(LOGIN_WINDOW_SECONDS - (now - attempts[0]) + 0.999))
                raise LoginRateLimited(retry_after)
            attempts.append(now)

    def session_from_cookie(self, cookie_header: str | None) -> AuthSession | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except CookieError:
            return None
        morsel = cookie.get(self.cookie_name)
        if morsel is None:
            return None
        return self.session_for_token(morsel.value)

    def session_for_token(self, token: str) -> AuthSession | None:
        now = time.monotonic()
        with self._lock:
            self._prune_sessions_locked(now)
            return self._sessions.get(token)

    def is_active(self, session: AuthSession) -> bool:
        active = self.session_for_token(session.token)
        return active is not None and hmac.compare_digest(active.token, session.token)

    def logout(self, session: AuthSession) -> None:
        with self._lock:
            self._sessions.pop(session.token, None)

    def csrf_matches(self, session: AuthSession, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(session.csrf_token, supplied)

    def set_cookie_header(self, session: AuthSession) -> str:
        attributes = [
            f"{self.cookie_name}={session.token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={self.config.session_seconds}",
        ]
        if self.config.secure_cookie:
            attributes.append("Secure")
        return "; ".join(attributes)

    def clear_cookie_header(self) -> str:
        attributes = [
            f"{self.cookie_name}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
        ]
        if self.config.secure_cookie:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _prune_sessions_locked(self, now: float) -> None:
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
