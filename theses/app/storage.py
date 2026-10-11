"""Хранение тезисов и профилей: SQLite в data/theses.db.

Правила пространства (`../CLAUDE.md`):
- у тезисов есть номер версии `rev`; правка со старым `rev` получает 409,
  а не тихо затирает чужую работу;
- удаление для сотрудника — архив, удалить навсегда может только `admin`
  (роль проверяется в `main.py`, здесь — только действие);
- кто и когда менял, видно в журнале изменений.

Профиль — карточка «для кого пишем»: имя, должность и какие файлы правил из `docs/`
за ним закреплены. Сами правила в базе не лежат: их правит пользователь файлом.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import rules
from .models import Thesis, new_id, sync_gaps_from_text, sync_mode

DATA_DIR = Path(os.environ.get("THESES_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "theses.db"

_current_login: contextvars.ContextVar[str] = contextvars.ContextVar("login", default="")

EDITOR_WINDOW_MINUTES = 5      # сколько считаем, что коллега «правит прямо сейчас»


class Conflict(Exception):
    """Тезисы успели изменить: у клиента на руках устаревшая версия."""

    def __init__(self, stored: "Stored"):
        super().__init__("Тезисы изменил кто-то другой")
        self.stored = stored


@dataclass
class Stored:
    thesis: Thesis
    rev: int
    archived: bool
    updated_at: str
    updated_by: str

    def to_dict(self) -> dict:
        return {"thesis": self.thesis.to_dict(), "rev": self.rev, "archived": self.archived,
                "updated_at": self.updated_at, "updated_by": self.updated_by}


@dataclass
class Profile:
    """Для кого пишем. Правила подставляются по нему при создании тезисов."""
    id: str = field(default_factory=new_id)
    name: str = ""         # как называем человека: «Д.Н. Чернышенко»
    role: str = ""         # должность: «Заместитель Председателя Правительства»
    base_file: str = ""    # общие правила на всех, если заведены
    own_file: str = ""     # правила этого человека — сильнее общих
    note: str = ""

    def to_dict(self) -> dict:
        out = {"id": self.id, "name": self.name, "role": self.role,
               "base_file": self.base_file, "own_file": self.own_file, "note": self.note}
        out["has_rules"] = bool((self.own_file and rules.exists(self.own_file))
                                or (self.base_file and rules.exists(self.base_file)))
        out["missing_files"] = [f for f in (self.base_file, self.own_file)
                                if f and not rules.exists(f)]
        return out

    def rules_text(self) -> str:
        return rules.compose(self.base_file, self.own_file, self.name)


def set_current_login(login: str) -> None:
    _current_login.set(login or "")


def current_login() -> str:
    return _current_login.get()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


_prepared: set[str] = set()
_prepare_lock = threading.Lock()
# поколение базы: растёт, когда её готовят заново. По нему свои таблицы в этой же базе
# (задания) узнают, что их пора завести снова (ревьюер RC-18, Н4)
_generation: dict[str, int] = {}


def generation() -> int:
    return _generation.get(str(DB_PATH), 0)


def _conn() -> sqlite3.Connection:
    """Подключение к базе. Журнал WAL и таблицы готовятся один раз на базу, под замком.

    Раньше это делал каждый запрос: на свежей базе несколько одновременных запросов
    упирались друг в друга ещё до переключения на WAL, и SQLite сразу отвечал
    «database is locked», не дожидаясь (нашлось тестом на четыре одновременных создания).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = str(DB_PATH)
    if key in _prepared and not DB_PATH.exists():
        # файл базы убрали на ходу (перенос, восстановление из копии) — готовим заново,
        # иначе до перезапуска все запросы получали бы 500 (ревьюер RC-16, Н4)
        with _prepare_lock:
            if not DB_PATH.exists():
                _prepared.discard(key)
    if key not in _prepared:
        with _prepare_lock:
            if key not in _prepared:
                first = sqlite3.connect(DB_PATH, timeout=15)
                try:
                    first.row_factory = sqlite3.Row
                    first.execute("PRAGMA journal_mode=WAL")
                    _init_schema(first)
                finally:
                    first.close()
                _generation[key] = _generation.get(key, 0) + 1
                _prepared.add(key)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS theses ("
        " id TEXT PRIMARY KEY,"
        " data TEXT NOT NULL,"
        " rev INTEGER NOT NULL DEFAULT 1,"
        " archived INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL,"
        " created_by TEXT NOT NULL DEFAULT '',"
        " updated_at TEXT NOT NULL,"
        " updated_by TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS change_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " thesis_id TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " login TEXT NOT NULL DEFAULT '',"
        " summary TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS editors ("
        " thesis_id TEXT NOT NULL,"
        " login TEXT NOT NULL,"
        " name TEXT NOT NULL DEFAULT '',"
        " ts TEXT NOT NULL,"
        " PRIMARY KEY (thesis_id, login))"
    )
    # одноразовые ключи окна «Новые тезисы»: повтор той же отправки (двойной щелчок,
    # повтор после обрыва) возвращает уже созданные тезисы, а не заводит вторые
    conn.execute(
        "CREATE TABLE IF NOT EXISTS create_keys ("
        " key TEXT PRIMARY KEY,"
        " thesis_id TEXT NOT NULL DEFAULT '',"
        " ts TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS profiles ("
        " id TEXT PRIMARY KEY,"
        " name TEXT NOT NULL DEFAULT '',"
        " role TEXT NOT NULL DEFAULT '',"
        " base_file TEXT NOT NULL DEFAULT '',"
        " own_file TEXT NOT NULL DEFAULT '',"
        " note TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT '',"
        " updated_at TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()


def _row_to_stored(row: sqlite3.Row) -> Stored:
    return Stored(
        thesis=Thesis.from_dict(json.loads(row["data"])),
        rev=row["rev"],
        archived=bool(row["archived"]),
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


# ── чтение ───────────────────────────────────────────────────────────────

def get(thesis_id: str) -> Stored | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM theses WHERE id = ?", (thesis_id,)).fetchone()
        return _row_to_stored(row) if row else None
    finally:
        conn.close()


def listing(archived: bool = False) -> list[dict]:
    """Список тезисов для боковой панели: без текста, свежие сверху."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, data, rev, archived, updated_at, updated_by, created_by FROM theses"
            " WHERE archived = ? ORDER BY updated_at DESC",
            (1 if archived else 0,),
        ).fetchall()
        out = []
        for row in rows:
            thesis = Thesis.from_dict(json.loads(row["data"]))
            out.append({
                "id": row["id"],
                "topic": thesis.topic or "Без темы",
                "profile": thesis.profile,
                "minutes": thesis.minutes,
                "event": thesis.event,
                "mode": thesis.mode,
                "bullets": len(thesis.bullets),
                "open_gaps": thesis.open_gaps,
                "created_by": row["created_by"],
                "rev": row["rev"],
                "archived": bool(row["archived"]),
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            })
        return out
    finally:
        conn.close()


def heal_mixed(corpus_of) -> int:
    """Исправить в записанных тезисах слова вперемешку, которые исправляются однозначно
    (задача 22.09, п. 4): такой текст остался от прогонов «Искры» до исправлений, и новый код
    только показывал предупреждение. Запускается при старте; каждая правка — в историю
    и в журнал портала от имени сервиса. Возвращает число исправленных тезисов."""
    from . import latin, portal, richtext
    fixed = 0
    conn = _conn()
    try:
        rows = conn.execute("SELECT id, data, rev FROM theses").fetchall()
    finally:
        conn.close()
    for row in rows:
        raw = json.loads(row["data"])
        thesis = Thesis.from_dict(raw)
        # записано до редактора с оформлением — переводим и записываем: так Markdown
        # становится оформлением, а места под цифру попадают в список (22.09, п. 3)
        migrate = bool(thesis.bullets) and not raw.get("html")
        words = corpus_of(thesis)
        replaced: list[str] = []
        items = richtext.blocks(thesis.html)
        for block in items:
            for run in block.runs:
                text, _ = latin.fix_mixed(run.text, words)
                if text != run.text:
                    for was, now_ in zip(run.text.split(), text.split()):
                        if was != now_ and f"{was} → {now_}" not in replaced:
                            replaced.append(f"{was} → {now_}")
                    run.text = text
        if not replaced and not migrate:
            continue
        if replaced:
            thesis.set_html(richtext.render(items))
        summary = ("исправлены слова наполовину латиницей: " + ", ".join(replaced[:8])
                   if replaced else "текст переведён в редактор с оформлением")
        set_current_login("система")
        try:
            save(thesis, expected_rev=row["rev"], summary=summary)
        except Conflict:
            continue
        # каждое изменение данных — событие в журнале (контракт, п. 4); токенов не стоило
        portal.send_audit(login="система",
                          action=("Исправлены слова наполовину латиницей" if replaced
                                  else "Тезисы переведены в редактор с оформлением"),
                          object=audit_name(thesis.id), details=summary, ip="",
                          tokens_in=0, tokens_out=0)
        fixed += 1
    return fixed


def history(thesis_id: str, limit: int = 50) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT ts, login, summary FROM change_log WHERE thesis_id = ?"
            " ORDER BY id DESC LIMIT ?", (thesis_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── запись ───────────────────────────────────────────────────────────────

def create(thesis: Thesis) -> Stored:
    thesis.ensure_html()
    sync_gaps_from_text(thesis)
    sync_mode(thesis)
    now, login = _now(), _current_login.get()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO theses (id, data, rev, archived, created_at, created_by, updated_at, updated_by)"
            " VALUES (?, ?, 1, 0, ?, ?, ?, ?)",
            (thesis.id, json.dumps(thesis.to_dict(with_text=True), ensure_ascii=False), now, login, now, login),
        )
        conn.execute("INSERT INTO change_log (thesis_id, ts, login, summary) VALUES (?, ?, ?, ?)",
                     (thesis.id, now, login, "тезисы созданы"))
        conn.commit()
    finally:
        conn.close()
    return Stored(thesis=thesis, rev=1, archived=False, updated_at=now, updated_by=login)


def save(thesis: Thesis, expected_rev: int | None = None, summary: str = "") -> Stored:
    """Сохранить тезисы. `expected_rev` — версия, которую правил пользователь.

    Если за это время текст изменил кто-то другой, поднимается Conflict:
    набранное не теряется, разрешение показывает интерфейс.
    """
    thesis.ensure_html()
    sync_gaps_from_text(thesis)
    sync_mode(thesis)
    now, login = _now(), _current_login.get()
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM theses WHERE id = ?", (thesis.id,)).fetchone()
        if row is None:
            raise KeyError(thesis.id)
        if expected_rev is not None and expected_rev != row["rev"]:
            raise Conflict(_row_to_stored(row))
        rev = row["rev"] + 1
        conn.execute(
            "UPDATE theses SET data = ?, rev = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (json.dumps(thesis.to_dict(with_text=True), ensure_ascii=False), rev, now, login, thesis.id),
        )
        if summary:
            conn.execute("INSERT INTO change_log (thesis_id, ts, login, summary) VALUES (?, ?, ?, ?)",
                         (thesis.id, now, login, summary))
        conn.commit()
    finally:
        conn.close()
    return Stored(thesis=thesis, rev=rev, archived=bool(row["archived"]),
                  updated_at=now, updated_by=login)


def set_archived(thesis_id: str, archived: bool) -> bool:
    now, login = _now(), _current_login.get()
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE theses SET archived = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (1 if archived else 0, now, login, thesis_id))
        if cur.rowcount:
            conn.execute("INSERT INTO change_log (thesis_id, ts, login, summary) VALUES (?, ?, ?, ?)",
                         (thesis_id, now, login,
                          "отправлено в архив" if archived else "восстановлено из архива"))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def delete_permanent(thesis_id: str) -> bool:
    """Удалить навсегда. Вызывать только для роли admin — проверка в main.py."""
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM theses WHERE id = ?", (thesis_id,))
        conn.execute("DELETE FROM change_log WHERE thesis_id = ?", (thesis_id,))
        conn.execute("DELETE FROM editors WHERE thesis_id = ?", (thesis_id,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


# ── кто правит прямо сейчас ──────────────────────────────────────────────

def touch_editor(thesis_id: str, login: str, name: str) -> None:
    if not (thesis_id and login):
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO editors (thesis_id, login, name, ts) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(thesis_id, login) DO UPDATE SET ts = excluded.ts, name = excluded.name",
            (thesis_id, login, name, _now()))
        conn.commit()
    finally:
        conn.close()


def active_editors(thesis_id: str, except_login: str) -> list[dict]:
    edge = (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=EDITOR_WINDOW_MINUTES)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT login, name, ts FROM editors WHERE thesis_id = ? AND login != ? AND ts > ?",
            (thesis_id, except_login, edge)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── профили «для кого пишем» ─────────────────────────────────────────────

def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(id=row["id"], name=row["name"], role=row["role"],
                   base_file=row["base_file"], own_file=row["own_file"], note=row["note"])


def profiles() -> list[Profile]:
    adopt_new_rules_files()
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return [_row_to_profile(r) for r in rows]
    finally:
        conn.close()


def profile(profile_id: str) -> Profile | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def save_profile(prof: Profile) -> Profile:
    now, login = _now(), _current_login.get()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO profiles (id, name, role, base_file, own_file, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET name = excluded.name, role = excluded.role,"
            " base_file = excluded.base_file, own_file = excluded.own_file,"
            " note = excluded.note, updated_at = excluded.updated_at",
            (prof.id, prof.name, prof.role, prof.base_file, prof.own_file, prof.note, now, now))
        conn.commit()
    finally:
        conn.close()
    return prof


def delete_profile(profile_id: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def profile_in_use(profile_id: str) -> int:
    """Сколько тезисов написано для этого человека: карточку с работой не удаляем молча."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT data FROM theses").fetchall()
        return sum(1 for r in rows if json.loads(r["data"]).get("profile") == profile_id)
    finally:
        conn.close()


def adopt_new_rules_files() -> list[Profile]:
    """Положили в `docs/` новый файл правил — заводим по нему карточку человека.

    Имя берётся из самого файла («Подготовка тезисов для Д.Н. Чернышенко»), остальное
    человек дополняет руками. Файлы, уже закреплённые за кем-то, не трогаем.
    """
    conn = _conn()
    try:
        taken = {row["own_file"] for row in conn.execute("SELECT own_file FROM profiles").fetchall()}
    finally:
        conn.close()
    added = []
    for found in rules.files():
        if found.name in taken:
            continue
        prof = Profile(name=found.person or found.title or found.name, own_file=found.name)
        save_profile(prof)
        added.append(prof)
    return added


def audit_name(obj: str) -> str:
    """Объект для журнала: «название · номер» вместо голого номера, если тезисы на месте.

    Нужен и запросам (`main.portal_gate`), и фоновым заданиям — они пишут в журнал сами,
    без запроса, и должны называть тезисы так же.
    """
    if not obj:
        return ""
    stored = get(obj)
    if stored is None:
        return obj
    title = (stored.thesis.topic or "").strip()
    return f"{title} · {obj}"[:300] if title else obj


CREATE_KEY_STALE = 30   # секунд: столько создание не длится; ключ без тезисов дольше — брошен


def claim_create_key(key: str) -> tuple[bool, str]:
    """Занять ключ создания. (True, "") — ключ наш, создаём; (False, id) — такие тезисы
    уже создал запрос с тем же ключом (id пустой, пока тот ещё создаёт).

    Ключ не застревает (ревьюер RC-16, Н3): если тезисы по нему удалили навсегда или
    создававший запрос упал, не дописав id, дольше `CREATE_KEY_STALE` секунд, — ключ
    перезанимается. Проверка и захват — одной записью под замком базы.
    """
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT thesis_id, ts FROM create_keys WHERE key = ?",
                           (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO create_keys (key, ts) VALUES (?, ?)", (key, _now()))
            conn.commit()
            return True, ""
        thesis_id = row["thesis_id"]
        edge = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=CREATE_KEY_STALE)).isoformat()
        abandoned = not thesis_id and row["ts"] < edge
        gone = bool(thesis_id) and conn.execute(
            "SELECT 1 FROM theses WHERE id = ?", (thesis_id,)).fetchone() is None
        if abandoned or gone:
            conn.execute("UPDATE create_keys SET thesis_id = '', ts = ? WHERE key = ?",
                         (_now(), key))
            conn.commit()
            return True, ""
        conn.commit()
        return False, thesis_id
    finally:
        conn.close()


def bind_create_key(key: str, thesis_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE create_keys SET thesis_id = ? WHERE key = ?", (thesis_id, key))
        # старые ключи не копим: повтор бывает в пределах секунд, а не суток
        edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        conn.execute("DELETE FROM create_keys WHERE ts < ?", (edge,))
        conn.commit()
    finally:
        conn.close()


def release_create_key(key: str) -> None:
    """Создать не вышло — ключ освобождаем, чтобы повтор мог попробовать снова."""
    conn = _conn()
    try:
        conn.execute("DELETE FROM create_keys WHERE key = ? AND thesis_id = ''", (key,))
        conn.commit()
    finally:
        conn.close()
