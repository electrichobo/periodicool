"""Login, sessions, CSRF tokens, the app secret and the API key.

One admin user. Passwords are hashed with scrypt; session cookies hold a random
token and the database keeps only its sha256. CSRF tokens are an HMAC of the
session token (or of an anonymous cookie before login) with the app secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from periodicool.library import get_setting
from periodicool.models import LoginSession, User

SECRET_ENV = "PERIODICOOL_SECRET"
SECRET_FILE = "secret.key"
API_KEY_FILE = "api.key"
SESSION_COOKIE = "periodicool_session"
ANON_COOKIE = "periodicool_csrf"
SESSION_DAYS = 30
MODES = ("required", "local", "off")
MIN_PASSWORD = 8

# scrypt cost: about 16 MB and well under a second per hash.
_N, _R, _P = 2**14, 8, 1


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str, confirm: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password != confirm:
        return "The passwords do not match."
    return None


# Secret files


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text + "\n")
    path.chmod(0o600)


def load_secret(config_dir: Path, env: Mapping[str, str] | None = None) -> bytes:
    """The app secret: from the environment, else /config/secret.key (made on first use)."""
    env = os.environ if env is None else env
    if value := env.get(SECRET_ENV, "").strip():
        return value.encode()
    path = config_dir / SECRET_FILE
    if not path.exists():
        _write_private(path, secrets.token_hex(32))
    return path.read_text(encoding="ascii").strip().encode()


def api_key(config_dir: Path) -> str:
    path = config_dir / API_KEY_FILE
    if not path.exists():
        return regenerate_api_key(config_dir)
    return path.read_text(encoding="ascii").strip()


def regenerate_api_key(config_dir: Path) -> str:
    key = secrets.token_hex(16)
    _write_private(config_dir / API_KEY_FILE, key)
    return key


def api_key_matches(config_dir: Path, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), api_key(config_dir).encode())


# Users and sessions


def _now() -> datetime:
    return datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(moment: datetime) -> datetime:
    # SQLite returns naive datetimes; they were stored as UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def has_user(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(User)))


def create_user(session: Session, username: str, password: str) -> User:
    user = User(username=username.strip(), password_hash=hash_password(password))
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None:
        # Spend the same time as a real check so usernames cannot be probed by timing.
        verify_password(password, hash_password("x"))
        return None
    return user if verify_password(password, user.password_hash) else None


def start_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    user.last_login_at = now
    session.add(
        LoginSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=SESSION_DAYS),
        )
    )
    session.flush()
    return token


def session_user(session: Session, token: str | None) -> User | None:
    """The user for a cookie token. Sessions slide: each visit pushes the expiry out."""
    if not token:
        return None
    row = session.scalar(select(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
    if row is None:
        return None
    now = _now()
    if _aware(row.expires_at) <= now:
        session.delete(row)
        return None
    if now - _aware(row.last_seen_at) > timedelta(minutes=5):
        row.last_seen_at = now
        row.expires_at = now + timedelta(days=SESSION_DAYS)
    return row.user


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))


def end_all_sessions(session: Session, user: User) -> None:
    session.execute(delete(LoginSession).where(LoginSession.user_id == user.id))


def reset_password(session: Session, username: str | None, password: str) -> User | None:
    query = select(User).order_by(User.id)
    if username:
        query = query.where(User.username == username)
    user = session.scalars(query).first()
    if user is None:
        return None
    user.password_hash = hash_password(password)
    end_all_sessions(session, user)
    return user


# CSRF


def csrf_token(secret: bytes, basis: str) -> str:
    return hmac.new(secret, b"csrf:" + basis.encode(), hashlib.sha256).hexdigest()


def csrf_ok(secret: bytes, basis: str | None, given: str | None) -> bool:
    if not basis or not given:
        return False
    return hmac.compare_digest(csrf_token(secret, basis), given)


# Auth mode and client address


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "required"
    trust_proxy: bool = False


def auth_config(session: Session) -> AuthConfig:
    mode = get_setting(session, "auth.mode", "required")
    if mode not in MODES:
        mode = "required"
    return AuthConfig(mode=mode, trust_proxy=bool(get_setting(session, "auth.trust_proxy", False)))


def client_address(peer: str | None, forwarded_for: str | None, trust_proxy: bool) -> str:
    if trust_proxy and forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return peer or ""


def is_local(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


# Failed-login backoff (in memory: a restart clears it, which is fine for one admin)

FREE_ATTEMPTS = 5
FIRST_WAIT = 30.0
MAX_WAIT = 15 * 60.0


@dataclass
class LoginThrottle:
    clock: object = time.monotonic
    _state: dict[str, tuple[int, float]] = field(default_factory=dict)

    def wait_left(self, address: str) -> float:
        _, until = self._state.get(address, (0, 0.0))
        return max(0.0, until - self.clock())

    def failed(self, address: str) -> None:
        count, _ = self._state.get(address, (0, 0.0))
        count += 1
        wait = 0.0
        if count >= FREE_ATTEMPTS:
            wait = min(FIRST_WAIT * 2 ** (count - FREE_ATTEMPTS), MAX_WAIT)
        self._state[address] = (count, self.clock() + wait)

    def succeeded(self, address: str) -> None:
        self._state.pop(address, None)
