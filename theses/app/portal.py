"""Связь с порталом workspace.example.ru: пользователь из заголовков nginx и журнал действий.

Вход, пароли и права живут в портале. nginx перед каждым запросом спрашивает портал
и передаёт сюда X-Portal-User / X-Portal-Role вместе с общим секретом X-Portal-Secret —
без верного секрета заголовкам не доверяем (порт 8960 на ПК доступен локальным процессам).

Файл повторяет устройство `agenda/app/portal.py` и `moderator/app/portal.py`:
контракт с порталом общий, меняется он только через общий трек.
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import queue
import re
import threading
import urllib.request
from pathlib import Path
from urllib.parse import unquote

SERVICE = "theses"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOGIN_URL = "/login?next=/theses/"
log = logging.getLogger("theses.portal")

# Что накопилось за текущий запрос: сводки изменений, объект и расход «Искры».
_request: contextvars.ContextVar[dict | None] = contextvars.ContextVar("portal_request", default=None)


def env(name: str, default: str = "") -> str:
    """Настройка из окружения или из `.env` рядом с сервисом.

    Оттуда же берётся `ISKRA_API_KEY`: ключ пишет портал при выкладке,
    сервис его не хранит и не спрашивает.
    """
    val = os.environ.get(name, "")
    if val:
        return val
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def user_from_headers(headers) -> dict | None:
    secret = env("PORTAL_SECRET")
    if not secret or not hmac.compare_digest(headers.get("x-portal-secret", ""), secret):
        return None
    login = headers.get("x-portal-user", "").strip()
    if not login:
        return None
    role = headers.get("x-portal-role", "employee")
    # Круг показа от портала: логины подчинённых через запятую, «*» — видно всё.
    team_raw = (headers.get("x-portal-team") or "").strip()
    team = ["*"] if team_raw == "*" else [t for t in (x.strip() for x in team_raw.split(",")) if t]
    return {"login": login, "role": role, "is_admin": role == "admin",
            "is_chief": role == "chief",
            "name": unquote(headers.get("x-portal-name", "")) or login, "team": team}


def start_request() -> dict:
    bucket = {"changes": [], "object": "", "tokens_in": 0, "tokens_out": 0}
    _request.set(bucket)
    return bucket


def note_tokens(tokens_in: int, tokens_out: int) -> None:
    """Расход «Искры» за этот запрос — уходит в журнал портала вместе с самим действием,
    чтобы на вкладке «Искра» было видно, кто и на что потратил. Неудачный запрос
    тоже стоил токенов, его расход сообщается так же."""
    bucket = _request.get()
    if bucket is not None:
        bucket["tokens_in"] = bucket.get("tokens_in", 0) + max(0, int(tokens_in or 0))
        bucket["tokens_out"] = bucket.get("tokens_out", 0) + max(0, int(tokens_out or 0))


def note_change(summary: str) -> None:
    """Описание изменения для журнала, по-русски и с «было → стало»."""
    bucket = _request.get()
    if bucket is not None and summary:
        bucket["changes"].append(summary)


def skip_audit() -> None:
    """Запрос ничего не изменил (повтор уже выполненного) — в журнал не пишем."""
    bucket = _request.get()
    if bucket is not None:
        bucket["skip"] = True


def note_object(obj: str) -> None:
    bucket = _request.get()
    if bucket is not None:
        bucket["object"] = obj


# ── описание действий для журнала ───────────────────────────────────────

_ACTIONS = [
    ("POST", r"/api/theses", "Созданы тезисы"),
    ("PUT", r"/api/theses/[^/]+", "Изменена карточка тезисов"),
    ("DELETE", r"/api/theses/[^/]+", "Тезисы отправлены в архив"),
    ("POST", r"/api/theses/[^/]+/restore", "Тезисы восстановлены из архива"),
    ("DELETE", r"/api/theses/[^/]+/permanent", "Тезисы удалены навсегда"),
    ("POST", r"/api/theses/[^/]+/sources.*", "Приложен документ к тезисам"),
    ("DELETE", r"/api/theses/[^/]+/sources/[^/]+", "Документ откреплён от тезисов"),
    ("PUT", r"/api/theses/[^/]+/text", "Изменён текст тезисов"),
    ("GET", r"/api/theses/[^/]+/export/theses\.docx", "Тезисы выгружены в Word"),
    # служебная строка: по ней аналитик ведёт ряд, поэтому переименование сервиса
    # (п. 10) её не касается — человеку текст «Совещание» показывает страница (RC-18)
    ("GET", r"/api/theses/[^/]+/moderator-payload", "Тезисы переданы в «Модератор»"),
    ("*", r"/api/profiles.*", "Изменён список лиц, для которых пишем"),
    ("POST", r"/api/rules/reload", "Перечитаны правила написания"),
    ("POST", r"/api/jobs/[^/]+/apply", "Применён вариант тезисов от «Искры»"),
    ("POST", r"/api/jobs/[^/]+/discard", "Вариант тезисов от «Искры» отложен"),
]
_TID = re.compile(r"^/api/theses/([^/]+)")


def describe(method: str, path: str) -> str | None:
    for m, pattern, action in _ACTIONS:
        if (m == "*" and method != "GET" or m == method) and re.fullmatch(pattern, path):
            return action
    return None


def thesis_id(path: str) -> str:
    m = _TID.match(path)
    return unquote(m.group(1)) if m else ""


_queue: queue.Queue = queue.Queue(maxsize=10_000)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _deliver() -> None:
    while True:
        event = _queue.get()
        secret = env("PORTAL_SECRET")
        url = env("PORTAL_URL", "http://127.0.0.1:8700") + "/internal/audit"
        body = json.dumps({"service": SERVICE, **event}, ensure_ascii=False).encode("utf-8")
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json", "X-Portal-Secret": secret})
                urllib.request.urlopen(req, timeout=5).close()
                break
            except Exception as e:  # журнал не должен ломать работу сервиса
                if attempt == 2:
                    log.warning("audit не отправлен: %s | %s", e, body[:500])


DETAILS_MAX = 300


def send_audit(**event) -> None:
    """Событие в журнал портала: по одному, в порядке действий, не задерживая ответ."""
    global _worker
    if not env("PORTAL_SECRET"):
        return
    # памятка, п. 8: «было → стало» в журнале — коротко, без простыней текста
    details = str(event.get("details") or "")
    if len(details) > DETAILS_MAX:
        event["details"] = details[:DETAILS_MAX - 1].rstrip() + "…"
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_deliver, name="portal-audit", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait(event)
    except queue.Full:
        log.warning("очередь журнала переполнена, событие потеряно: %s", event.get("action"))
