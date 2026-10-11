"""Пароли (PBKDF2, формат совместим с «Модератором»), сессии, защита от перебора, коды 2FA (TOTP)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from urllib.parse import quote

from . import config, db

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 14 * 24 * 3600      # две недели
SESSION_COOKIE = "portal_session"
MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hash_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def password_problem(password: str, login: str) -> str:
    """Почему пароль не годится (пустая строка — годится)."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LEN} символов."
    low = password.lower()
    if login and login.lower() in low:
        return "Пароль не должен содержать логин."
    if len(set(password)) < 5:
        return "Пароль слишком простой."
    if low in {"password123", "qwerty12345", "1234567890", "admin12345", "0987654321"}:
        return "Пароль слишком распространённый."
    return ""


# ── сессии ──────────────────────────────────────────────────────────────

def _sign(payload: str, user: dict) -> str:
    # В подпись входят хеш пароля и поколение сессий: смена пароля или «выйти везде» отзывает старые cookie.
    msg = f"{payload}|{user['password_hash']}|{user['session_gen']}".encode("utf-8")
    return hmac.new(config.ensure_env("SECRET_KEY").encode("utf-8"), msg, hashlib.sha256).hexdigest()


def make_session(user: dict) -> str:
    payload = f"{user['login']}.{int(time.time()) + SESSION_TTL}"
    return f"{payload}.{_sign(payload, user)}"


def verify_session(cookie: str | None) -> dict | None:
    if not cookie:
        return None
    try:
        payload, sig = cookie.rsplit(".", 1)
        login, expiry = payload.rsplit(".", 1)
        if int(expiry) < time.time():
            return None
        user = db.get_user(login)
        if not user or not user["active"]:
            return None
        if not hmac.compare_digest(sig, _sign(payload, user)):
            return None
        return user
    except Exception:
        return None


# ── защита от перебора ──────────────────────────────────────────────────

LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_IP = 60      # один внешний адрес офиса — это все сотрудники сразу
LOGIN_MAX_PER_LOGIN = 8

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _forget_old(now: float) -> None:
    for key in [k for k, ts in _failures.items() if not ts or now - max(ts) > LOGIN_WINDOW]:
        _failures.pop(key, None)


def _recent(key: str, now: float) -> list[float]:
    ts = [t for t in _failures.get(key, []) if now - t < LOGIN_WINDOW]
    if ts:
        _failures[key] = ts
    else:
        _failures.pop(key, None)
    return ts


def login_blocked(ip: str, login: str) -> bool:
    now = time.time()
    with _failures_lock:
        return (len(_recent("ip:" + ip, now)) >= LOGIN_MAX_PER_IP
                or len(_recent("login:" + login.lower(), now)) >= LOGIN_MAX_PER_LOGIN)


def register_failure(ip: str, login: str) -> None:
    now = time.time()
    with _failures_lock:
        _forget_old(now)
        for key in ("ip:" + ip, "login:" + login.lower()):
            _failures.setdefault(key, []).append(now)


def clear_failures(login: str) -> None:
    with _failures_lock:
        _failures.pop("login:" + login.lower(), None)


def reset_limits_for_tests() -> None:
    with _failures_lock:
        _failures.clear()


def check_credentials(login: str, password: str) -> dict | None:
    """Одинаковое время проверки для существующих и несуществующих логинов."""
    user = db.get_user(login)
    ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
    return user if (user and ok and user["active"]) else None


# ── TOTP (RFC 6238) ─────────────────────────────────────────────────────

TOTP_STEP = 30
_used_codes: dict[str, int] = {}     # логин → последний принятый шаг (код нельзя использовать дважды)
_used_codes_lock = threading.Lock()


def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _totp_at(secret, int((at if at is not None else time.time()) // TOTP_STEP))


def verify_totp(secret: str, code: str, login: str = "") -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    now = int(time.time() // TOTP_STEP)
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(_totp_at(secret, step), code):
            if not login:
                return True
            # Один код — один вход: проверка и отметка под общим замком, иначе два
            # одновременных запроса примут его дважды.
            with _used_codes_lock:
                if _used_codes.get(login, -1) >= step:
                    return False
                _used_codes[login] = step
            return True
    return False


def totp_uri(secret: str, login: str) -> str:
    label = quote(f"Рабочее пространство:{login}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote('workspace.example.ru')}&digits=6&period={TOTP_STEP}"
