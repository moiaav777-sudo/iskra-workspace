"""Журнал портала: каждое изменение данных уходит в POST /internal/audit.

Отправка идёт в фоне, по порядку, и не должна ломать работу сервиса: портал может
быть недоступен — сервис от этого не останавливается. Устройство — как у соседей
(`a3/service/app/audit.py`).

Приватность записи (../CLAUDE.md → задача, п. 7): в `details` не должно быть ни
слова из расшифровки или саммари — только факты (длительность, число знаков,
токены). Это проверяется тестами (`test_zhurnal_ne_soderzhit_tekста`).
"""
from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request

from . import config

_queue: "queue.Queue[dict]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()

# Для тестов: без портала записи просто копятся здесь.
sent: list[dict] = []


def _send(record: dict) -> None:
    body = json.dumps(record, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.AUDIT_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-Portal-Secret": config.PORTAL_SECRET},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except (urllib.error.URLError, OSError, ValueError):
        pass  # журнал не должен ронять сервис


def _run() -> None:
    while True:
        record = _queue.get()
        try:
            _send(record)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="transcriber-audit", daemon=True)
            _worker.start()


def write(user: dict[str, str], action: str, obj: str, details: str = "", ip: str = "",
          tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Действие по-русски, в details — «было → стало».

    tokens_in / tokens_out — расход «Искры» на это действие: портал складывает его
    по сотрудникам и сервисам на вкладке «Искра». Неудачное действие тоже стоило
    токенов — его расход тоже сообщается (контракт п. 4).
    """
    record = {
        "service": "transcriber",
        "login": user.get("login", ""),
        "action": action,
        "object": obj,
        "details": details[:300],
        "ip": ip,
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
    }
    sent.append(record)
    if len(sent) > 200:
        del sent[:-200]
    if config.DEV:
        return
    _ensure_worker()
    _queue.put(record)
