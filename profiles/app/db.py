"""База объективок: SQLite в `<PROFILES_HOME>/data/profiles.db`.

Объективка — одна строка: служебные поля столбцами (владелец, версия, архив, состояние прогона
«Искры») и сами сведения JSON-ом в `data` (см. `model.py`). Каждое сохранение правки идёт
с версией (`rev`): без неё вкладка, открытая раньше, затёрла бы чужую правку или ответ «Искры».

Ещё две таблицы: объединённые файлы (`merges`) и пакетная переработка (`batches`).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from . import config, model

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_name TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    rev INTEGER NOT NULL DEFAULT 1,
    fio TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    mode TEXT NOT NULL DEFAULT 'name',
    hint TEXT NOT NULL DEFAULT '',
    web INTEGER NOT NULL DEFAULT 0,
    source_name TEXT NOT NULL DEFAULT '',
    source_text TEXT NOT NULL DEFAULT '',
    photo TEXT NOT NULL DEFAULT '',
    photo_from TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    built_rev INTEGER NOT NULL DEFAULT 0,
    fit TEXT NOT NULL DEFAULT '',
    name_ok TEXT NOT NULL DEFAULT '',
    name_ok_by TEXT NOT NULL DEFAULT '',
    name_ok_at TEXT NOT NULL DEFAULT '',
    meeting TEXT NOT NULL DEFAULT '',
    create_key TEXT NOT NULL DEFAULT '',
    batch TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS profiles_owner ON profiles(owner);
CREATE TABLE IF NOT EXISTS merges (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    items TEXT NOT NULL DEFAULT '[]',
    file_name TEXT NOT NULL DEFAULT '',
    meeting TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '[]',
    web INTEGER NOT NULL DEFAULT 0,
    merge_id TEXT NOT NULL DEFAULT '',
    create_key TEXT NOT NULL DEFAULT ''
);
"""

RUNNING, QUEUED = "running", "queued"


class Conflict(Exception):
    """Версия не совпала: объективку успели изменить."""

    def __init__(self, current: dict):
        super().__init__("conflict")
        self.current = current


class Locked(Exception):
    """Пока «Искра» собирает объективку, править её нельзя."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def conn():
    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# Столбцы, добавленные после первой выкладки: старой базе дописываются при запуске (старый код
# новые столбцы просто не читает — откат безопасен).
ADDED = {"profiles": (("name_ok_by", "TEXT NOT NULL DEFAULT ''"), ("name_ok_at", "TEXT NOT NULL DEFAULT ''"))}


def init() -> None:
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        for table, cols in ADDED.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # перезапуск обрывает поток «Искры» — человек не должен ждать вечно
        c.execute("UPDATE profiles SET status='interrupted', status_text=? WHERE status IN (?, ?)",
                  ("Сервис перезапускался, прогон «Искры» прервался — запустите сбор заново", RUNNING, QUEUED))


# ── объективки ──────────────────────────────────────────────────────────

def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    out["data"] = model.clean(json.loads(out.get("data") or "{}"), sort=False)
    out["archived"] = bool(out["archived"])
    out["web"] = bool(out["web"])
    out["fit"] = json.loads(out["fit"]) if out.get("fit") else None
    out["meeting"] = json.loads(out["meeting"]) if out.get("meeting") else None
    return out


def create(user: dict, mode: str, fio: str = "", hint: str = "", web: bool = False,
           source_name: str = "", source_text: str = "", create_key: str = "", batch: str = "",
           status: str = "") -> tuple[dict, bool]:
    """Завести объективку. Повтор той же отправки (тот же ключ окна) отдаёт уже заведённую."""
    with _lock, conn() as c:
        if create_key:
            got = c.execute("SELECT * FROM profiles WHERE create_key=?", (create_key,)).fetchone()
            if got is not None:
                return _row(got), False
        pid, at = new_id(), now()
        data = model.clean({"fio": fio}, sort=False)
        c.execute("""INSERT INTO profiles (id, owner, owner_name, created_at, updated_at, updated_by,
                     updated_name, fio, data, mode, hint, web, source_name, source_text, create_key,
                     batch, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (pid, user["login"], user.get("name", ""), at, at, user["login"], user.get("name", ""),
                   data["fio"], json.dumps(data, ensure_ascii=False), mode, hint, int(bool(web)),
                   source_name, source_text, create_key, batch, status))
        return _row(c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()), True


def get(pid: str) -> dict | None:
    with conn() as c:
        return _row(c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())


def listing(archived: bool = False) -> list[dict]:
    with conn() as c:
        rows = c.execute("""SELECT id, owner, owner_name, created_at, updated_at, updated_by, updated_name,
                            archived, rev, fio, data, mode, photo, status, status_text, fit, built_rev, batch,
                            name_ok, name_ok_by, name_ok_at
                            FROM profiles WHERE archived=? ORDER BY updated_at DESC""",
                         (int(archived),)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        data = json.loads(item.pop("data") or "{}")
        item["position"] = str(data.get("position") or "")
        item["archived"] = bool(item["archived"])
        item["fit"] = json.loads(item["fit"]) if item.get("fit") else None
        item["has_photo"] = bool(item.pop("photo"))
        item["name_confirmed"] = bool(item["name_ok"]) and item["name_ok"] == item["fio"]
        out.append(item)
    return out


def save_data(pid: str, data: dict, user: dict, rev: int | None, *, force_unlocked: bool = False) -> dict:
    """Записать сведения с проверкой версии. Идёт прогон «Искры» — Locked."""
    clean = model.clean(data, sort=False)
    with _lock, conn() as c:
        cur = _row(c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())
        if cur is None:
            raise KeyError(pid)
        if cur["status"] in (RUNNING, QUEUED) and not force_unlocked:
            raise Locked()
        if rev is not None and int(rev) != cur["rev"]:
            raise Conflict(cur)
        c.execute("""UPDATE profiles SET data=?, fio=?, rev=rev+1, updated_at=?, updated_by=?, updated_name=?
                     WHERE id=?""", (json.dumps(clean, ensure_ascii=False), clean["fio"], now(),
                                     user["login"], user.get("name", ""), pid))
        if clean["fio"] != cur["data"].get("fio", ""):
            # подтверждение «это другой человек» — только для того ФИО, при котором его дали:
            # смена ФИО снимает его, возврат прежнего не возвращает (ревью RC-2026-10-05-4, Н-10)
            c.execute("UPDATE profiles SET name_ok='', name_ok_by='', name_ok_at='' WHERE id=?", (pid,))
        return _row(c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())


def set_fields(pid: str, **fields: Any) -> dict | None:
    """Служебные поля (состояние прогона, фото, сборка) — без смены версии сведений."""
    if not fields:
        return get(pid)
    for key in ("fit", "meeting"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False) if fields[key] is not None else ""
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, conn() as c:
        c.execute(f"UPDATE profiles SET {cols} WHERE id=?", (*fields.values(), pid))
        return _row(c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone())


def touch(pid: str, user: dict) -> None:
    with _lock, conn() as c:
        c.execute("UPDATE profiles SET updated_at=?, updated_by=?, updated_name=? WHERE id=?",
                  (now(), user["login"], user.get("name", ""), pid))


def set_archived(pid: str, archived: bool) -> bool:
    with _lock, conn() as c:
        return c.execute("UPDATE profiles SET archived=?, updated_at=? WHERE id=?",
                         (int(archived), now(), pid)).rowcount > 0


def delete(pid: str) -> bool:
    with _lock, conn() as c:
        return c.execute("DELETE FROM profiles WHERE id=?", (pid,)).rowcount > 0


# ── объединённые файлы ──────────────────────────────────────────────────

def add_merge(owner: str, title: str, items: list, file_name: str) -> dict:
    mid = new_id()
    with _lock, conn() as c:
        c.execute("INSERT INTO merges (id, owner, created_at, title, items, file_name) VALUES (?,?,?,?,?,?)",
                  (mid, owner, now(), title, json.dumps(items, ensure_ascii=False), file_name))
    return get_merge(mid)


def get_merge(mid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM merges WHERE id=?", (mid,)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["items"] = json.loads(out["items"] or "[]")
    out["meeting"] = json.loads(out["meeting"]) if out.get("meeting") else None
    return out


def set_merge_meeting(mid: str, meeting: dict) -> None:
    with _lock, conn() as c:
        c.execute("UPDATE merges SET meeting=? WHERE id=?", (json.dumps(meeting, ensure_ascii=False), mid))


# ── пакетная переработка ────────────────────────────────────────────────

def add_batch(owner: str, items: list, web: bool, create_key: str = "", lookup_only: bool = False):
    """Завести пакет сразу со строками. lookup_only — только найти уже заведённый по ключу окна."""
    with _lock, conn() as c:
        if create_key:
            got = c.execute("SELECT id FROM batches WHERE create_key=?", (create_key,)).fetchone()
            if got is not None:
                return get_batch(got["id"]) if lookup_only else (get_batch(got["id"]), False)
        if lookup_only:
            return None
        bid, at = new_id(), now()
        c.execute("INSERT INTO batches (id, owner, created_at, updated_at, items, web, create_key) "
                  "VALUES (?,?,?,?,?,?,?)", (bid, owner, at, at, json.dumps(items), int(web), create_key))
    return get_batch(bid), True


def get_batch(bid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["items"] = json.loads(out["items"] or "[]")
    out["web"] = bool(out["web"])
    return out


def set_batch(bid: str, **fields: Any) -> dict | None:
    if "items" in fields:
        fields["items"] = json.dumps(fields["items"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, conn() as c:
        c.execute(f"UPDATE batches SET {cols} WHERE id=?", (*fields.values(), bid))
    return get_batch(bid)


def batches_of(owner: str, limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id FROM batches WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                         (owner, limit)).fetchall()
    return [b for b in (get_batch(r["id"]) for r in rows) if b]
