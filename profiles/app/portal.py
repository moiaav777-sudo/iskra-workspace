"""Связь с порталом workspace.example.ru по контракту (`../../CLAUDE.md`): вход из заголовков,
защита от чужих сайтов, журнал действий и сверка ФИО с реестром людей.

Своего входа нет: пользователя сообщает nginx заголовками X-Portal-*; верим им, только если
X-Portal-Secret совпадает с PORTAL_SECRET. Устройство повторяет `theses/app/portal.py` и
`transcriber/app/audit.py` — контракт общий, меняется только через общий трек.
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
import urllib.request
from urllib.parse import unquote, urlparse

from . import config

SERVICE = "profiles"
LOGIN_URL = "/login?next=/profiles/"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
DETAILS_MAX = 300
CHECK_TIMEOUT = 5          # сверка ФИО: дольше не ждём — «сверка не прошла», человек видит это
log = logging.getLogger("profiles.portal")

# Для тестов: последние отправленные записи журнала (в продуктиве — просто хвост).
sent: list[dict] = []


def user_from_headers(headers) -> dict | None:
    if config.dev():
        return dict(config.DEV_USER, is_admin=False, is_chief=False)
    secret = config.env("PORTAL_SECRET")
    if not secret or not hmac.compare_digest((headers.get("x-portal-secret") or "").encode("utf-8"),
                                             secret.encode("utf-8")):
        return None
    login = (headers.get("x-portal-user") or "").strip()
    if not login:
        return None
    role = (headers.get("x-portal-role") or "employee").strip() or "employee"
    # круг показа от портала: логины подчинённых через запятую, «*» — видно всё (фильтр, а не запрет)
    team_raw = (headers.get("x-portal-team") or "").strip()
    team = ["*"] if team_raw == "*" else [t for t in (x.strip() for x in team_raw.split(",")) if t]
    return {"login": login, "role": role, "is_admin": role == "admin", "is_chief": role == "chief",
            "name": unquote(headers.get("x-portal-name") or "") or login, "team": team}


def foreign_origin(method: str, headers) -> bool:
    """Изменяющий запрос с Origin, не совпадающим с Host, — с чужого сайта (контракт п. 5)."""
    if method.upper() not in UNSAFE:
        return False
    origin = headers.get("origin") or ""
    return bool(origin) and urlparse(origin).netloc != (headers.get("host") or "")


# ── журнал ──────────────────────────────────────────────────────────────

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=10_000)
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _deliver() -> None:
    while True:
        record = _queue.get()
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        for attempt in range(3):
            try:
                req = urllib.request.Request(config.portal_url() + "/internal/audit", data=body, headers={
                    "Content-Type": "application/json", "X-Portal-Secret": config.env("PORTAL_SECRET")})
                urllib.request.urlopen(req, timeout=5).close()
                break
            except Exception as error:  # noqa: BLE001 — журнал не должен ломать работу сервиса
                if attempt == 2:
                    log.warning("журнал не отправлен: %s | %s", error, body[:300])


def audit(user: dict, action: str, obj: str = "", details: str = "", ip: str = "",
          tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Событие в журнал портала: по-русски, «было → стало», по порядку и в фоне.

    tokens_in / tokens_out — расход «Искры»; неудачный прогон тоже стоил токенов (контракт п. 4).
    """
    details = str(details or "")
    if len(details) > DETAILS_MAX:
        details = details[:DETAILS_MAX - 1].rstrip() + "…"
    record = {"service": SERVICE, "login": (user or {}).get("login", ""), "action": action,
              "object": str(obj or "")[:300], "details": details, "ip": ip,
              "tokens_in": max(0, int(tokens_in or 0)), "tokens_out": max(0, int(tokens_out or 0))}
    with _lock:
        sent.append(record)
        del sent[:-500]
    if not config.env("PORTAL_SECRET"):
        return
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_deliver, name="profiles-audit", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait(record)
    except queue.Full:
        log.warning("очередь журнала переполнена: %s", action)


# ── реестр людей (контракт п. 5а) ───────────────────────────────────────

def check_people(names: list[str]) -> dict | None:
    """Сверка ФИО с реестром людей перед выводом в документ.

    {ФИО: {status: exact | close | unknown, ...}}; None — портала нет (стенд без секрета);
    {} — портал есть, но не ответил: сверка не прошла, об этом говорим человеку.
    """
    secret = config.env("PORTAL_SECRET")
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n]
    if not secret or not names:
        return None
    body = json.dumps({"names": names}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(config.portal_url() + "/internal/people/check", data=body, headers={
            "Content-Type": "application/json", "X-Portal-Secret": secret})
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as response:
            items = json.loads(response.read() or b"{}").get("items") or []
        return {str(x.get("name") or ""): x for x in items if isinstance(x, dict)}
    except Exception:  # noqa: BLE001
        return {}
