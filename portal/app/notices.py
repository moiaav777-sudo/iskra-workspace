"""Объявления сотрудникам (решение пользователя 02.10: А, Б, В, Г и предупреждение за минуту до выкладки).

Два источника, один ответ для общей шапки (`/api/notice`):
- каталог уведомлений, который пишет выкладка (`/opt/work/notices` на VPS, `PORTAL_NOTICES_DIR`):
  `current.json` — объявления выкладки («через минуту обновление», «Обновлено: …»),
  `versions.json` — какой коммит какого сервиса выложен (по нему открытая страница узнаёт,
  что её код устарел);
- объявления администратора — таблица `notices` в базе портала.

Портал файлы только читает: битый или отсутствующий файл — это «объявлений нет», а не ошибка.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from . import config

KINDS = ("info", "warning", "maintenance", "update")
KIND_NAMES = {"info": "сообщение", "warning": "предупреждение", "maintenance": "плановые работы",
              "update": "обновление"}
# Порядок в полосе: сначала то, что требует действия.
_ORDER = {"maintenance": 0, "warning": 1, "info": 2, "update": 3}
MAX_TEXT = 500
BOOT = dt.datetime.now().astimezone().isoformat(timespec="seconds")   # когда запущен этот процесс портала


def notices_dir() -> Path:
    """Каталог уведомлений: `PORTAL_NOTICES_DIR`; без неё — `notices` рядом с папкой портала, если он есть
    (на VPS портал в /opt/<work|meet>/portal, выкладка пишет в /opt/<work|meet>/notices — ревью
    RC-2026-10-02-22, К-1); иначе `data/notices` (стенд, тесты). Проверяется на каждый запрос: каталог
    появляется при первой записи выкладки, перезапуск портала для этого не нужен."""
    explicit = config.read_env("PORTAL_NOTICES_DIR")
    if explicit:
        return Path(explicit)
    beside = config.BASE_DIR.parent / "notices"
    if beside.is_dir():
        return beside
    return config.DATA_DIR / "notices"


def _read(name: str, default):
    try:
        data = json.loads((notices_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_time(value) -> dt.datetime | None:
    """ISO-время с поясом или без (без пояса — местное время сервера). Пусто/мусор — None."""
    if not value or not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone() if t.tzinfo is None else t


def is_active(item: dict, at: dt.datetime) -> bool:
    start, end = parse_time(item.get("from")), parse_time(item.get("until"))
    if item.get("from") and start is None:
        return False
    if item.get("until") and end is None:
        return False
    return (start is None or start <= at) and (end is None or at < end)


def service_name(service: str) -> str:
    if not service:
        return "все сервисы"
    if service == "portal":
        return "Портал"
    return next((s["name"] for s in config.SERVICES if s["id"] == service), service)


def _public(item: dict, source: str) -> dict:
    kind = item.get("kind") if item.get("kind") in KINDS else "info"
    service = str(item.get("service") or "")
    if service == "all":
        service = ""
    out = {"id": str(item.get("id") or ""), "kind": kind, "text": str(item.get("text") or "")[:MAX_TEXT],
           "service": service, "service_name": service_name(service),
           "from": str(item.get("from") or ""), "until": str(item.get("until") or ""), "source": source}
    if item.get("starts"):
        out["starts"] = str(item["starts"])
    return out


def file_notices(at: dt.datetime | None = None) -> list[dict]:
    at = at or now()
    items = _read("current.json", [])
    return [_public(i, "deploy") for i in items
            if isinstance(i, dict) and i.get("id") and i.get("text") and is_active(i, at)]


# ── объявления администратора ──────────────────────────────────────────

def setup(c: sqlite3.Connection) -> None:
    """Таблица появилась 02.10 — добавлением: старый код портала её просто не замечает."""
    c.execute(
        """CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'info',
            service TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            until TEXT NOT NULL DEFAULT '',
            login TEXT NOT NULL DEFAULT '',
            removed_at TEXT NOT NULL DEFAULT '',
            removed_by TEXT NOT NULL DEFAULT ''
        )""")


def _conn():
    from . import db
    return db.conn()


def _admin_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    item = _public({"id": f"admin-{d['id']}", "kind": d["kind"], "text": d["text"], "service": d["service"],
                    "from": d["created_at"], "until": d["until"]}, "admin")
    item.update({"login": d["login"], "removed_at": d["removed_at"], "removed_by": d["removed_by"],
                 "number": d["id"]})
    return item


def admin_notices(*, include_ended: int = 0, at: dt.datetime | None = None) -> list[dict]:
    """Действующие объявления администратора; include_ended — сколько последних снятых/истёкших добавить."""
    at = at or now()
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        c.close()
    active, ended = [], []
    for r in rows:
        item = _admin_row(r)
        live = not r["removed_at"] and is_active({"from": r["created_at"], "until": r["until"]}, at)
        item["active"] = live
        (active if live else ended).append(item)
    return active + ended[:max(0, include_ended)]


def create(text: str, kind: str, service: str, until: str, login: str) -> tuple[dict, bool]:
    """Новое объявление. Повтор того же (двойное нажатие) возвращает уже созданное: (объявление, создано ли)."""
    c = _conn()
    try:
        same = c.execute(
            "SELECT * FROM notices WHERE removed_at = '' AND text = ? AND kind = ? AND service = ? AND until = ? "
            "ORDER BY id DESC LIMIT 1", (text, kind, service, until)).fetchone()
        if same and is_active({"from": same["created_at"], "until": same["until"]}, now()):
            return _admin_row(same), False
        from . import db
        cur = c.execute("INSERT INTO notices(text, kind, service, created_at, until, login) VALUES(?,?,?,?,?,?)",
                        (text, kind, service, db.now_iso(), until, login))
        c.commit()
        row = c.execute("SELECT * FROM notices WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _admin_row(row), True
    finally:
        c.close()


def remove(number: int, login: str) -> tuple[dict | None, bool]:
    """Снять объявление: (объявление или None, если такого нет; снято ли сейчас). Повтор — не ошибка."""
    from . import db
    c = _conn()
    try:
        row = c.execute("SELECT * FROM notices WHERE id = ?", (number,)).fetchone()
        if not row:
            return None, False
        if row["removed_at"]:
            return _admin_row(row), False
        c.execute("UPDATE notices SET removed_at = ?, removed_by = ? WHERE id = ? AND removed_at = ''",
                  (db.now_iso(), login, number))
        c.commit()
        return _admin_row(c.execute("SELECT * FROM notices WHERE id = ?", (number,)).fetchone()), True
    finally:
        c.close()


# ── ответы для шапки ───────────────────────────────────────────────────

def active(at: dt.datetime | None = None) -> list[dict]:
    at = at or now()
    keep = ("id", "kind", "text", "service", "service_name", "from", "until", "source", "starts")
    items = file_notices(at) + [{k: v for k, v in n.items() if k in keep}
                                for n in admin_notices(at=at)]
    # по важности вида, внутри вида — свежие сверху (сортировка устойчивая: сначала по времени, потом по виду)
    items.sort(key=lambda n: parse_time(n["from"]) or at, reverse=True)
    items.sort(key=lambda n: _ORDER.get(n["kind"], 9))
    return items


def versions() -> dict:
    data = _read("versions.json", {})
    clean = {}
    for svc, v in data.items():
        if isinstance(v, dict) and (svc in config.SERVICE_IDS or svc == "portal"):
            clean[svc] = {"commit": str(v.get("commit") or "")[:40], "at": str(v.get("at") or "")}
    return clean
