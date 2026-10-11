"""База библиотеки листов: SQLite, два стола — листы и их версии.

Версия листа растёт на единицу при каждом сохранении. Номер версии клиент
присылает обратно, и по нему ловится одновременная правка двумя людьми.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    data          TEXT    NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    created_by    TEXT    NOT NULL,
    created_name  TEXT    NOT NULL DEFAULT '',
    updated_at    TEXT    NOT NULL,
    updated_by    TEXT    NOT NULL,
    updated_name  TEXT    NOT NULL DEFAULT '',
    -- как лист прошёл проверку вёрстки: '' пока неизвестно, checking — идёт
    -- проверка, fixing — «Искра» дорабатывает, warn — остались замечания, ok — чисто
    state         TEXT    NOT NULL DEFAULT '',
    state_note    TEXT    NOT NULL DEFAULT '',
    state_at      TEXT    NOT NULL DEFAULT '',
    -- сколько кругов доработки «Искрой» лист уже прошёл: счётчик у листа, а не
    -- у вкладки, иначе каждое открытие начинало бы цикл заново
    fix_rounds    INTEGER NOT NULL DEFAULT 0,
    -- сколько раз лист вообще уходил в «Искру» на доработку, включая неудачные
    -- заходы: предел должен считать потраченные деньги, а не сохранённые версии
    fix_calls     INTEGER NOT NULL DEFAULT 0,
    -- во сколько токенов «Искры» обошёлся лист: разбор справки плюс все круги
    tokens        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sheet_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id    INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    data        TEXT    NOT NULL,
    author      TEXT    NOT NULL,
    author_name TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    UNIQUE (sheet_id, version)
);
CREATE INDEX IF NOT EXISTS idx_sheets_updated ON sheets (updated_at DESC);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _older_than(stamp: str, seconds: int) -> bool:
    if not stamp:
        return True
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.now() - moment).total_seconds() > seconds


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


STATES = {"", "checking", "fixing", "warn", "ok"}
WORKING = {"checking", "fixing"}


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # база с прошлых версий: доводим до текущей схемы
        have = {r["name"] for r in conn.execute("PRAGMA table_info(sheets)")}
        for column, kind in (("state", "TEXT NOT NULL DEFAULT ''"),
                             ("state_note", "TEXT NOT NULL DEFAULT ''"),
                             ("state_at", "TEXT NOT NULL DEFAULT ''"),
                             ("fix_rounds", "INTEGER NOT NULL DEFAULT 0"),
                             ("fix_calls", "INTEGER NOT NULL DEFAULT 0"),
                             ("tokens", "INTEGER NOT NULL DEFAULT 0")):
            if column not in have:
                conn.execute(f"ALTER TABLE sheets ADD COLUMN {column} {kind}")


def title_of(data: dict[str, Any], fallback: str = "Лист без названия") -> str:
    """Название берём из шапки листа: человеку не нужно вводить его дважды."""
    header = data.get("header") or {}
    title = str(header.get("title") or "").replace("\n", " ").strip()
    return " ".join(title.split())[:200] or fallback


STALE_FIX = 6 * 60  # брошенный круг: вкладку закрыли, ответа «Искры» уже не будет


def row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    """Строка списка: без самих данных листа, чтобы список был лёгким."""
    keys = dict(row)
    state = keys.get("state", "") or ""
    note = keys.get("state_note", "") or ""
    if state in WORKING and _older_than(keys.get("state_at", ""), STALE_FIX):
        state, note = "warn", "доработка прервана — откройте лист и попробуйте ещё раз"
    return {
        "id": row["id"],
        "title": row["title"],
        "version": row["version"],
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "created_name": row["created_name"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "updated_name": row["updated_name"],
        "state": state,
        "state_note": note,
        "state_at": keys.get("state_at", ""),
        "fix_rounds": keys.get("fix_rounds", 0) or 0,
        "fix_calls": keys.get("fix_calls", 0) or 0,
        "tokens": keys.get("tokens", 0) or 0,
    }


def list_sheets(q: str = "", archived: bool = False, sort: str = "updated") -> list[dict[str, Any]]:
    order = {
        "updated": "updated_at DESC",
        "created": "created_at DESC",
        "title": "title COLLATE NOCASE ASC",
    }.get(sort, "updated_at DESC")
    sql = f"SELECT * FROM sheets WHERE archived = ? {{where}} ORDER BY {order}"
    args: list[Any] = [1 if archived else 0]
    where = ""
    if q.strip():
        # поиск идёт и по названию, и по тексту листа: так находится лист по числу из него
        where = "AND (title LIKE ? OR data LIKE ?)"
        like = f"%{q.strip()}%"
        args += [like, like]
    with connect() as conn:
        rows = conn.execute(sql.format(where=where), args).fetchall()
    return [row_to_card(r) for r in rows]


def get_sheet(sheet_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    if not row:
        return None
    card = row_to_card(row)
    card["data"] = json.loads(row["data"])
    return card


def create_sheet(data: dict[str, Any], user: dict[str, str], title: str = "") -> dict[str, Any]:
    stamp, text = now(), json.dumps(data, ensure_ascii=False)
    name = (title or "").strip() or title_of(data)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sheets (title, data, version, archived, created_at, created_by, created_name,"
            " updated_at, updated_by, updated_name) VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
            (name, text, stamp, user["login"], user["name"], stamp, user["login"], user["name"]),
        )
        sheet_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO sheet_versions (sheet_id, version, title, data, author, author_name, created_at, note)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            (sheet_id, name, text, user["login"], user["name"], stamp, "создан"),
        )
    return get_sheet(sheet_id)  # type: ignore[return-value]


class Conflict(Exception):
    """Лист успел измениться у другого человека."""

    def __init__(self, row: sqlite3.Row) -> None:
        super().__init__("лист изменён")
        self.version = row["version"]
        self.by = row["updated_name"] or row["updated_by"]
        self.at = row["updated_at"]


def save_sheet(sheet_id: int, data: dict[str, Any], user: dict[str, str],
               base_version: int, title: str = "", note: str = "") -> dict[str, Any] | None:
    stamp, text = now(), json.dumps(data, ensure_ascii=False)
    name = (title or "").strip() or title_of(data)
    with connect() as conn:
        # BEGIN IMMEDIATE: читаем и пишем версию в одной транзакции, иначе два одновременных
        # сохранения прочитают одну версию и второй упадёт на уникальном ключе вместо 409.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
        if not row:
            return None
        if int(base_version) != int(row["version"]):
            raise Conflict(row)
        version = int(row["version"]) + 1
        conn.execute(
            "UPDATE sheets SET title = ?, data = ?, version = ?, updated_at = ?, updated_by = ?,"
            " updated_name = ? WHERE id = ?",
            (name, text, version, stamp, user["login"], user["name"], sheet_id),
        )
        conn.execute(
            "INSERT INTO sheet_versions (sheet_id, version, title, data, author, author_name, created_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sheet_id, version, name, text, user["login"], user["name"], stamp, note),
        )
    return get_sheet(sheet_id)


def bump_fix_round(sheet_id: int) -> int:
    """Счёт кругов доработки ведёт лист, а не вкладка: заново цикл не начинается."""
    with connect() as conn:
        conn.execute("UPDATE sheets SET fix_rounds = fix_rounds + 1 WHERE id = ?", (sheet_id,))
        row = conn.execute("SELECT fix_rounds FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    return int(row["fix_rounds"]) if row else 0


def reset_fix_rounds(sheet_id: int) -> None:
    """Счётчик кругов обнуляется, но общий счёт обращений к «Искре» продолжает расти."""
    with connect() as conn:
        conn.execute("UPDATE sheets SET fix_rounds = 0 WHERE id = ?", (sheet_id,))


def fix_calls(sheet_id: int) -> int:
    """Сколько раз лист уходил в «Искру» на доработку — считая неудачные заходы.

    Раньше предел считался по сохранённым версиям, и сорвавшийся круг в него не
    попадал: лист мог сжечь сотни тысяч токенов, ни разу не приблизившись к
    пределу. Платим мы за обращения, по ним и считаем.
    """
    with connect() as conn:
        row = conn.execute("SELECT fix_calls FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    return int(row["fix_calls"]) if row else 0


def bump_fix_call(sheet_id: int) -> int:
    with connect() as conn:
        conn.execute("UPDATE sheets SET fix_calls = fix_calls + 1 WHERE id = ?", (sheet_id,))
        row = conn.execute("SELECT fix_calls FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    return int(row["fix_calls"]) if row else 0


def add_tokens(sheet_id: int, spent: int) -> int:
    """Расход «Искры» копится у листа: и разбор справки, и каждый круг доработки."""
    if spent <= 0:
        return 0
    with connect() as conn:
        conn.execute("UPDATE sheets SET tokens = tokens + ? WHERE id = ?", (int(spent), sheet_id))
        row = conn.execute("SELECT tokens FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    return int(row["tokens"]) if row else 0


def touch_state(sheet_id: int) -> None:
    """Работа ещё идёт: без этой отметки список через шесть минут сочтёт круг брошенным."""
    with connect() as conn:
        conn.execute("UPDATE sheets SET state_at = ? WHERE id = ? AND state IN ('checking', 'fixing')",
                     (now(), sheet_id))


def set_state(sheet_id: int, state: str, note: str = "") -> dict[str, Any] | None:
    """Как лист прошёл проверку: это видно в списке, пока идёт доработка."""
    if state not in STATES:
        state = ""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sheets SET state = ?, state_note = ?, state_at = ? WHERE id = ?",
            (state, note[:120], now(), sheet_id),
        )
        if not cur.rowcount:
            return None
    return get_sheet(sheet_id)


def set_archived(sheet_id: int, archived: bool) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute("UPDATE sheets SET archived = ? WHERE id = ?", (1 if archived else 0, sheet_id))
        if not cur.rowcount:
            return None
    return get_sheet(sheet_id)


def delete_sheet(sheet_id: int) -> bool:
    with connect() as conn:
        conn.execute("DELETE FROM sheet_versions WHERE sheet_id = ?", (sheet_id,))
        return bool(conn.execute("DELETE FROM sheets WHERE id = ?", (sheet_id,)).rowcount)


def list_versions(sheet_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT version, title, author, author_name, created_at, note FROM sheet_versions"
            " WHERE sheet_id = ? ORDER BY version DESC",
            (sheet_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_version(sheet_id: int, version: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sheet_versions WHERE sheet_id = ? AND version = ?", (sheet_id, version)
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["data"] = json.loads(row["data"])
    return out
