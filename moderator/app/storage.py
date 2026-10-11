"""Хранилище: SQLite, одна строка = одно совещание (JSON-документ).

Выбрано намеренно: совещание по своей природе — документ. Один файл БД
переносится копированием, что важно для будущего переезда на сервер.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

_current_login = contextvars.ContextVar("moderator_current_login", default="")

from . import portal
from .models import Meeting

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "meetings.db"



def set_current_login(login: str) -> None:
    _current_login.set(login or "")


def get_current_login() -> str:
    return _current_login.get() or ""


_schema_ready: set[str] = set()
_schema_lock = threading.Lock()
_create_lock = threading.Lock()
# «найти или завести» должно быть одним действием: иначе два сервиса, нажавшие
# кнопку одновременно, заведут два совещания на одну дату и название
find_or_create_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    key = str(DB_PATH)
    if key not in _schema_ready:
        with _schema_lock:
            if key not in _schema_ready:
                conn.execute("PRAGMA journal_mode = WAL")
                _init_schema(conn)
                conn.commit()
                _schema_ready.add(key)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meetings ("
        " id TEXT PRIMARY KEY,"
        " data TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS change_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " meeting_id TEXT NOT NULL,"
        " ts TEXT NOT NULL,"
        " summary TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        " login TEXT PRIMARY KEY,"
        " password_hash TEXT NOT NULL,"
        " is_admin INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL"
        ")"
    )
    # миграция: колонка login в change_log (кто внёс изменение)
    clog_cols = [r[1] for r in conn.execute("PRAGMA table_info(change_log)").fetchall()]
    if "login" not in clog_cols:
        conn.execute("ALTER TABLE change_log ADD COLUMN login TEXT NOT NULL DEFAULT ''")
    # миграция: добавить колонку owner в meetings (если её ещё нет)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(meetings)").fetchall()]
    if "owner" not in cols:
        conn.execute("ALTER TABLE meetings ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
    if "archived" not in cols:
        conn.execute("ALTER TABLE meetings ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "keep_active" not in cols:
        conn.execute("ALTER TABLE meetings ADD COLUMN keep_active INTEGER NOT NULL DEFAULT 0")
    # кто и когда менял совещание — для предупреждения «этот же бриф правит коллега»
    conn.execute(
        "CREATE TABLE IF NOT EXISTS editors ("
        " meeting_id TEXT NOT NULL,"
        " login TEXT NOT NULL,"
        " name TEXT NOT NULL DEFAULT '',"
        " ts REAL NOT NULL,"
        " PRIMARY KEY (meeting_id, login)"
        ")"
    )
    # Своя таблица реестра людей больше не пополняется: реестр один, в портале (../CLAUDE.md п. 5а),
    # записи отсюда перенесены туда 21.09. Таблицу не удаляем — это данные на диске, а не код.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS registry ("
        " name TEXT PRIMARY KEY,"
        " display_name TEXT NOT NULL,"
        " role TEXT NOT NULL DEFAULT '',"
        " organization TEXT NOT NULL DEFAULT '',"
        " updated_at TEXT NOT NULL"
        ")"
    )
    # Предложения из «Приёмной» «Материалов» (шаг 2 перехода «Повестка → Совещание», решение
    # пользователя 29.09): документ разобран «Искрой», но ничего не применено — человек открывает
    # ссылку и подтверждает сам (по образцу `agenda/app/main.py::/api/inbox`, только читали).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS inbox_proposals ("
        " key TEXT PRIMARY KEY,"
        " meeting_id TEXT NOT NULL,"
        " fingerprint TEXT NOT NULL,"
        " data TEXT NOT NULL,"
        " created_at TEXT NOT NULL"
        ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_fingerprint ON inbox_proposals(fingerprint)")


def _diff_summary(old: dict, new: dict) -> str:
    """Краткое описание изменений между версиями (для журнала)."""
    parts = []
    for key, label in [("agenda", "повестка"), ("participants", "участники"),
                       ("orders", "поручения"), ("materials", "материалы"),
                       ("scenario", "сценарий"), ("glossary", "глоссарий"),
                       ("results", "результаты")]:
        ol, nl = old.get(key) or [], new.get(key) or []
        if len(ol) != len(nl):
            parts.append(f"{label}: {len(ol)}→{len(nl)}")
        else:
            changed = sum(1 for a, b in zip(ol, nl) if a != b)
            if changed:
                parts.append(f"{label}: изменено {changed}")

    def short(v) -> str:
        s = str(v or "").strip()
        return (s[:77] + "…") if len(s) > 80 else s

    for key, label in [("title", "название"), ("short_title_manual", "короткое название"),
                       ("subtitle", "полное наименование"), ("date", "дата"),
                       ("chair", "председатель"), ("time_start", "время начала"), ("time_end", "время окончания"),
                       ("location_primary", "место"), ("location_secondary", "место (доп.)")]:
        if old.get(key) != new.get(key):
            parts.append(f"{label}: «{short(old.get(key))}» → «{short(new.get(key))}»")
    return "; ".join(parts) if parts else "правка"


def save(meeting: Meeting) -> Meeting:
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    now_local = dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT created_at, data FROM meetings WHERE id = ?", (meeting.id,)
        ).fetchone()
        created = existing["created_at"] if existing else now
        new_data = meeting.to_dict()
        if existing:
            summary = _diff_summary(json.loads(existing["data"]), new_data)
            portal.note_change(summary)
            conn.execute(
                "INSERT INTO change_log(meeting_id, ts, summary, login) VALUES(?,?,?,?)",
                (meeting.id, now_local, summary, get_current_login()),
            )
            conn.execute(
                "DELETE FROM change_log WHERE meeting_id = ? AND id NOT IN "
                "(SELECT id FROM change_log WHERE meeting_id = ? ORDER BY id DESC LIMIT 200)",
                (meeting.id, meeting.id),
            )
        conn.execute(
            "INSERT INTO meetings(id, data, owner, created_at, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, owner=excluded.owner, updated_at=excluded.updated_at",
            (meeting.id, json.dumps(new_data, ensure_ascii=False), meeting.owner, created, now),
        )
        conn.commit()
    finally:
        conn.close()
    return meeting


def get_history(meeting_id: str, limit: int = 50) -> list[dict]:
    """Журнал изменений совещания (свежие сверху)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT ts, summary, login FROM change_log WHERE meeting_id = ? ORDER BY id DESC LIMIT ?",
            (meeting_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get(meeting_id: str) -> Meeting | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT data FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    m = Meeting.from_dict(json.loads(row["data"]))
    if m.ids_assigned and not _store_assigned_ids(meeting_id, m, row["data"]):
        return get(meeting_id)  # параллельный запрос успел записать свои id — берём их
    return m


def _store_assigned_ids(meeting_id: str, meeting: Meeting, old_data: str) -> bool:
    """Записать выданные при чтении id без журнала и без updated_at: содержимое не менялось.

    Пишем, только если строка не изменилась с момента чтения; False — опоздали.
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE meetings SET data = ? WHERE id = ? AND data = ?",
            (json.dumps(meeting.to_dict(), ensure_ascii=False), meeting_id, old_data),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def find_by_source(service: str, source_meeting_id: str) -> Meeting | None:
    """Совещание, приехавшее из другого сервиса: повторная передача обновляет его, а не плодит дубли."""
    if not (service and source_meeting_id):
        return None
    conn = _conn()
    try:
        rows = conn.execute("SELECT id, data FROM meetings ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            src = (json.loads(r["data"]).get("source") or {})
        except (json.JSONDecodeError, TypeError):
            continue
        if src.get("service") == service and str(src.get("meeting_id") or "") == source_meeting_id:
            return get(r["id"])
    return None


def _norm_key(text: str) -> str:
    """Для сравнения: без регистра и лишних пробелов."""
    return " ".join((text or "").strip().lower().split())


def find_by_date_title(date: str, title: str) -> Meeting | None:
    """Совещание по паре «дата + название» — по ней соседние сервисы находят уже заведённое.

    Архив тоже просматриваем: у прошедшей даты совещание уезжает в архив само,
    а второе такое же заводить всё равно нельзя.
    """
    from .models import normalize_date
    want_date, want_title = normalize_date(date), _norm_key(title)
    if not (want_date and want_title):
        return None
    conn = _conn()
    try:
        rows = conn.execute("SELECT id, data FROM meetings ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        if normalize_date(data.get("date") or "") == want_date and _norm_key(data.get("title") or "") == want_title:
            return get(r["id"])
    return None


def _parse_date(s: str):
    """«ДД.ММ.ГГГГ» → datetime.date (или None, если даты нет/формат иной)."""
    if not s:
        return None
    import datetime as dt
    try:
        day, month, year = str(s).strip().split(".")
        return dt.date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


def list_ids(owner: str | None = None, archived: bool = False) -> list[dict]:
    """Список совещаний. Совещания с прошедшей датой автоматически попадают в архив."""
    import datetime as dt
    today = dt.date.today()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, owner, data, created_at, updated_at, archived, keep_active FROM meetings ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        data = {}
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError, OSError):
            data = {}
        shared = data.get("shared_with", []) or []
        if owner is not None and d["owner"] != owner and owner not in shared:
            continue
        date_val = _parse_date(data.get("date", "") or "")
        is_past = date_val is not None and date_val < today
        # совещание в архиве = вручную в архиве ИЛИ дата раньше сегодняшнего дня (если не восстановлено вручную)
        effectively_archived = bool(d.get("archived")) or (is_past and not bool(d.get("keep_active")))
        if archived != effectively_archived:
            continue
        out.append({
            "id": d["id"], "owner": d["owner"],
            "created_at": d["created_at"], "updated_at": d["updated_at"],
            "shared_with": shared,
            "title": data.get("title", "") or "",
            "short_title": _short_of(data),
            "date": data.get("date", "") or "",
        })
    return out


def _short_of(data: dict) -> str:
    """Короткое название по сырой записи — то же, что `Meeting.short_title`, без сборки модели."""
    from .meeting_title import short_title
    return (data.get("short_title_manual") or "").strip() or short_title(data.get("title") or "")


def delete(meeting_id: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def archive(meeting_id: str) -> bool:
    """Переместить совещание в архив (мягкое удаление)."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE meetings SET archived = 1, keep_active = 0 WHERE id = ?", (meeting_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def restore(meeting_id: str) -> bool:
    """Вернуть совещание из архива."""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE meetings SET archived = 0, keep_active = 1 WHERE id = ?", (meeting_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── кто сейчас правит совещание ─────────────────────────────────────────

EDITOR_WINDOW_SEC = 180   # столько минут после правки человек считается работающим над совещанием


def note_editor(meeting_id: str, login: str, name: str = "") -> None:
    """Отметить, что пользователь только что менял совещание."""
    if not (meeting_id and login):
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO editors(meeting_id, login, name, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(meeting_id, login) DO UPDATE SET name=excluded.name, ts=excluded.ts",
            (meeting_id, login, name or login, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def active_editors(meeting_id: str, exclude_login: str = "") -> list[dict]:
    """Кто правил это совещание в последние минуты (кроме самого спрашивающего)."""
    edge = time.time() - EDITOR_WINDOW_SEC
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT login, name, ts FROM editors WHERE meeting_id = ? AND ts >= ? AND login <> ? ORDER BY ts DESC",
            (meeting_id, edge, exclude_login),
        ).fetchall()
        conn.execute("DELETE FROM editors WHERE ts < ?", (edge - 86400,))
        conn.commit()
    finally:
        conn.close()
    now = time.time()
    return [{"login": r["login"], "name": r["name"] or r["login"],
             "minutes_ago": int((now - r["ts"]) // 60)} for r in rows]


def last_editor_name(meeting_id: str, exclude_login: str = "") -> str:
    editors = active_editors(meeting_id, exclude_login)
    return editors[0]["name"] if editors else ""


# ── пользователи ────────────────────────────────────────────────────────

def create_user(login: str, password_hash: str, is_admin: bool = False) -> None:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO users(login, password_hash, is_admin, created_at) VALUES(?,?,?,?)",
            (login, password_hash, 1 if is_admin else 0, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(login: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT login, password_hash, is_admin, created_at FROM users WHERE login = ?",
            (login,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_users() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT login, is_admin, created_at FROM users ORDER BY login").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_user(login: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM users WHERE login = ?", (login,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_password(login: str, password_hash: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE users SET password_hash = ? WHERE login = ?", (password_hash, login))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def next_id() -> str:
    """Следующий свободный ключ meeting-N (глобально, уникален между пользователями)."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT id FROM meetings").fetchall()
    finally:
        conn.close()
    max_n = 0
    for r in rows:
        m = re.match(r"^meeting-(\d+)$", r["id"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"meeting-{max_n + 1}"


def insert_new(meeting: Meeting) -> bool:
    """Создать совещание. False, если ключ уже занят (существующее не перезаписывается)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO meetings(id, data, owner, created_at, updated_at) VALUES(?,?,?,?,?)",
            (meeting.id, json.dumps(meeting.to_dict(), ensure_ascii=False), meeting.owner, now, now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def insert_with_next_id(meeting: Meeting) -> Meeting:
    """Создать совещание со следующим свободным ключом meeting-N."""
    with _create_lock:
        for _ in range(20):
            meeting.id = next_id()
            if insert_new(meeting):
                return meeting
    raise RuntimeError("Не удалось подобрать свободный ключ совещания")


# ── предложения из «Приёмной» (шаг 2 перехода, ../CLAUDE.md → routes_iskra.inbox) ──────────

_PROPOSALS_KEEP = 500   # старые предложения никто уже не откроет — не растим таблицу без предела


def remember_proposal(key: str, meeting_id: str, fingerprint: str, data: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO inbox_proposals(key, meeting_id, fingerprint, data, created_at) VALUES(?,?,?,?,?)",
            (key, meeting_id, fingerprint, json.dumps(data, ensure_ascii=False), now),
        )
        conn.execute(
            "DELETE FROM inbox_proposals WHERE key NOT IN "
            "(SELECT key FROM inbox_proposals ORDER BY created_at DESC LIMIT ?)",
            (_PROPOSALS_KEEP,),
        )
        conn.commit()
    finally:
        conn.close()


def proposal_by_fingerprint(fingerprint: str) -> tuple[str, str, dict] | None:
    """Тот же документ к тому же совещанию уже разбирали — отдать готовое, не звать «Искру» снова."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT key, meeting_id, data FROM inbox_proposals WHERE fingerprint = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row["key"], row["meeting_id"], json.loads(row["data"])


def proposal(key: str, meeting_id: str) -> dict | None:
    """Предложение по ссылке — только для того совещания, к которому оно относится."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT data FROM inbox_proposals WHERE key = ? AND meeting_id = ?",
            (key, meeting_id),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["data"]) if row else None
