"""База «Транскрибатора»: SQLite, одна запись — один загруженный файл.

Устройство повторяет `a3/service/app/db.py`: версия черновика растёт при каждом
сохранении правки, по ней ловится одновременная правка двумя людьми (DEV-CHECKLIST,
п. 2). Само аудио/видео здесь не хранится — только путь к временному файлу на время
обработки (`app/media.py`) и результат — три документа (решение пользователя 02.10):
дословная транскрипция (`draft`), саммари (`summary`) и перечень поручений (`orders`, JSON).
У каждого своя версия: правка человека и запись «Искры» не затирают друг друга.

Единый формат блоков (решение пользователя 13.10): правка человека с оформлением хранится ещё и
HTML — `draft_html`, `summary_html`, у поручения в JSON `orders` ключ `html` (очищенный
`richtext_common.clean`). Текст рядом остаётся всегда — по нему работают «Искра», TXT и перечень
в Word. Когда текст перезаписывает «Искра», HTML сбрасывается в '' — показывается свежий текст.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config, doc_html, orders as orders_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    -- '' — только что создана, transcribing — «Искра» расшифровывает запись, summarizing —
    -- пишет саммари, ordering — составляет перечень поручений, done — готово,
    -- error — не удалось, cancelled — отменено человеком
    state         TEXT    NOT NULL DEFAULT '',
    state_note    TEXT    NOT NULL DEFAULT '',
    duration      REAL    NOT NULL DEFAULT 0,
    draft         TEXT    NOT NULL DEFAULT '',      -- JSON: список кусков {start, end, text}
    draft_version INTEGER NOT NULL DEFAULT 1,
    summary       TEXT    NOT NULL DEFAULT '',
    summary_version INTEGER NOT NULL DEFAULT 1,
    draft_html    TEXT    NOT NULL DEFAULT '',      -- правка транскрипции с оформлением; '' — из текста
    summary_html  TEXT    NOT NULL DEFAULT '',      -- правка саммари с оформлением; '' — из текста
    orders        TEXT    NOT NULL DEFAULT '',      -- JSON: [{text, assignee, deadline, html?}]; '' — ещё нет
    orders_version INTEGER NOT NULL DEFAULT 1,
    chair         TEXT    NOT NULL DEFAULT '',      -- председатель: подпись перечня «И.О. Фамилия»
    chair_role    TEXT    NOT NULL DEFAULT '',      -- его должность: шапка перечня
    meeting_date  TEXT    NOT NULL DEFAULT '',      -- ДД.ММ.ГГГГ: дата в документах; пусто — дата загрузки
    meeting       TEXT    NOT NULL DEFAULT '',      -- JSON: к какому совещанию приложено
    tokens        INTEGER NOT NULL DEFAULT 0,
    engine        TEXT    NOT NULL DEFAULT '',
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    created_by    TEXT    NOT NULL,
    created_name  TEXT    NOT NULL DEFAULT '',
    updated_at    TEXT    NOT NULL,
    updated_by    TEXT    NOT NULL,
    updated_name  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_updated ON records (updated_at DESC);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


STATES = {"", "transcribing", "summarizing", "ordering", "done", "error", "cancelled"}


WORKING = {"transcribing", "summarizing", "ordering"}

# база первой версии (27.09): колонок под три документа и шапку перечня ещё нет.
# Откат на старый код такую базу переживает — лишние колонки он просто не читает.
MIGRATIONS = (
    ("summary_version", "INTEGER NOT NULL DEFAULT 1"),
    ("orders", "TEXT NOT NULL DEFAULT ''"),
    ("orders_version", "INTEGER NOT NULL DEFAULT 1"),
    ("chair", "TEXT NOT NULL DEFAULT ''"),
    ("chair_role", "TEXT NOT NULL DEFAULT ''"),
    ("meeting_date", "TEXT NOT NULL DEFAULT ''"),
    ("meeting", "TEXT NOT NULL DEFAULT ''"),
    # единый формат блоков (решение пользователя 13.10): правка с оформлением
    ("draft_html", "TEXT NOT NULL DEFAULT ''"),
    ("summary_html", "TEXT NOT NULL DEFAULT ''"),
)


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        for column, kind in MIGRATIONS:
            if column not in have:
                conn.execute(f"ALTER TABLE records ADD COLUMN {column} {kind}")


def interrupt_stale() -> int:
    """При старте: задания, оборванные перезапуском, — прерваны, а не «висят вечно».

    Задания сервиса живут только в памяти (`app/jobs.py`) — перезапуск их всегда теряет,
    поэтому по-настоящему «висящих» записей после старта быть не должно: то, что ещё
    числится «идёт обработка», однозначно осталось от прошлого процесса.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE records SET state = 'error',"
            " state_note = 'Сервис перезапускался, обработка прервалась. Загрузите запись заново.'"
            f" WHERE state IN ({', '.join('?' for _ in WORKING)})", sorted(WORKING),
        )
        return cur.rowcount


def row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    """Строка списка: без самого черновика и саммари — список должен быть лёгким."""
    return {
        "id": row["id"],
        "title": row["title"],
        "state": row["state"],
        "state_note": row["state_note"],
        "duration": row["duration"],
        "draft_version": row["draft_version"],
        "tokens": row["tokens"],
        "engine": row["engine"],
        "has_draft": bool(row["draft"]),
        "has_summary": bool(row["summary"]),
        "has_orders": bool(row["orders"]),
        "summary_version": row["summary_version"],
        "orders_version": row["orders_version"],
        "chair": row["chair"],
        "chair_role": row["chair_role"],
        "meeting_date": row["meeting_date"],
        "meeting": _json_or(row["meeting"], None),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "created_name": row["created_name"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "updated_name": row["updated_name"],
    }


def _json_or(raw: str, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _segments_to_text(segments: list[dict[str, Any]]) -> str:
    """Обычный текст с таймкодами и говорящими по абзацам — то, что видит и правит человек:
    «[05:12] Говорящий 1: текст».

    Без таймкода (`start` пуст) абзац идёт без него — «Искра» не всегда ставит таймкоды.
    Ручная правка первой версии хранилась одним куском с `start == end == 0` — после правки
    текст уже не совпадает с распознаванием построчно, показывать выдуманный «00:00» было бы
    враньём.
    """
    lines = []
    for seg in segments:
        start, end = seg.get("start"), seg.get("end")
        text = str(seg.get("text", "")).strip()
        speaker = str(seg.get("speaker") or "").strip()
        if speaker:
            text = f"{speaker}: {text}"
        if start is None or (start == 0 and end == 0):
            lines.append(text)
        else:
            lines.append(f"[{_stamp(start)}] {text}")
    return "\n\n".join(lines)


def _stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def create_record(user: dict[str, str], title: str) -> dict[str, Any]:
    stamp = now()
    name = (title or "").strip()[:200] or "Запись без названия"
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO records (title, state, created_at, created_by, created_name,"
            " updated_at, updated_by, updated_name) VALUES (?, '', ?, ?, ?, ?, ?, ?)",
            (name, stamp, user["login"], user["name"], stamp, user["login"], user["name"]),
        )
        record_id = int(cur.lastrowid)
    return get_record(record_id)  # type: ignore[return-value]


def get_record(record_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        return None
    card = row_to_card(row)
    card["draft"] = json.loads(row["draft"]) if row["draft"] else []
    card["draft_text"] = _segments_to_text(card["draft"]) if card["draft"] else ""
    card["summary"] = row["summary"]
    card["orders"] = [o for o in _json_or(row["orders"], []) if isinstance(o, dict)]
    orders_mod.ensure_ids(card["orders"], positional=True)    # старые поручения без id — по месту
    # HTML для блоков карточки (решение пользователя 13.10): сохранённый человеком или из текста
    # (`*_styled` — правку с оформлением сохранил человек: Word и «Совещание» берут её HTML)
    # HTML действителен, только если согласован с текстом (ревью RC-2026-10-13-2, Н-1)
    card["draft_styled"] = doc_html.consistent(row["draft_html"], card["draft_text"])
    card["summary_styled"] = doc_html.consistent(row["summary_html"], card["summary"])
    card["draft_html"] = doc_html.draft_html(card["draft_text"], row["draft_html"])
    card["summary_html"] = doc_html.summary_html(card["summary"], row["summary_html"])
    for order in card["orders"]:
        order["html"] = doc_html.order_html(order)
    return card


def list_records(q: str = "", archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM records WHERE archived = ? {where} ORDER BY updated_at DESC"
    args: list[Any] = [1 if archived else 0]
    where = ""
    if q.strip():
        where = "AND title LIKE ?"
        args.append(f"%{q.strip()}%")
    with connect() as conn:
        rows = conn.execute(sql.format(where=where), args).fetchall()
    return [row_to_card(r) for r in rows]


def set_state(record_id: int, state: str, note: str = "") -> dict[str, Any] | None:
    if state not in STATES:
        state = ""
    with connect() as conn:
        cur = conn.execute("UPDATE records SET state = ?, state_note = ? WHERE id = ?",
                           (state, note[:200], record_id))
        if not cur.rowcount:
            return None
    return get_record(record_id)


def set_result(record_id: int, segments: list[dict[str, Any]], duration: float, engine: str,
              state: str = "summarizing", note: str = "") -> dict[str, Any] | None:
    """Распознавание закончилось: черновик записан, дальше идёт саммари (или сразу готово)."""
    text = json.dumps(segments, ensure_ascii=False)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE records SET draft = ?, draft_html = '', duration = ?, engine = ?, state = ?,"
            " state_note = ? WHERE id = ?",
            (text, duration, engine, state if state in STATES else "", note[:200], record_id),
        )
        if not cur.rowcount:
            return None
    return get_record(record_id)


def set_summary(record_id: int, summary: str, tokens: int, state: str = "done",
               note: str = "", base_version: int | None = None) -> bool:
    """Саммари от «Искры». `base_version` — версия саммари на старте прогона: если человек
    успел его поправить, его правку не затираем (DEV-CHECKLIST, п. 2) — вернём False."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT summary_version FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return False
        fresh = base_version is None or int(base_version) == int(row["summary_version"])
        conn.execute(
            "UPDATE records SET tokens = tokens + ?, state = ?, state_note = ? WHERE id = ?",
            (max(0, int(tokens)), state if state in STATES else "", note[:200], record_id))
        if fresh:
            # текст пишет «Искра» — оформление прежней правки к нему не относится
            conn.execute("UPDATE records SET summary = ?, summary_html = '',"
                         " summary_version = summary_version + 1 WHERE id = ?", (summary, record_id))
    return fresh


def set_orders(record_id: int, orders: list[dict[str, str]], tokens: int, state: str = "done",
               note: str = "", base_version: int | None = None) -> bool:
    """Перечень поручений от «Искры» — с той же защитой правки человека, что у саммари."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT orders_version FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return False
        fresh = base_version is None or int(base_version) == int(row["orders_version"])
        conn.execute(
            "UPDATE records SET tokens = tokens + ?, state = ?, state_note = ? WHERE id = ?",
            (max(0, int(tokens)), state if state in STATES else "", note[:200], record_id))
        if fresh:
            orders = orders_mod.ensure_ids([dict(o) for o in orders])
            conn.execute("UPDATE records SET orders = ?, orders_version = orders_version + 1"
                         " WHERE id = ?", (json.dumps(orders, ensure_ascii=False), record_id))
    return fresh


def add_tokens(record_id: int, spent: int) -> int:
    if spent <= 0:
        return 0
    with connect() as conn:
        conn.execute("UPDATE records SET tokens = tokens + ? WHERE id = ?", (int(spent), record_id))
        row = conn.execute("SELECT tokens FROM records WHERE id = ?", (record_id,)).fetchone()
    return int(row["tokens"]) if row else 0


class Conflict(Exception):
    """Документ успел измениться у другого человека, в другой вкладке или у «Искры»."""

    def __init__(self, row: sqlite3.Row, field: str = "draft_version") -> None:
        super().__init__("документ изменён")
        self.version = row[field]
        self.by = row["updated_name"] or row["updated_by"]
        self.at = row["updated_at"]


def save_draft_text(record_id: int, text: str, user: dict[str, str],
                    base_version: int, html: str = "") -> dict[str, Any] | None:
    """Правка черновика руками: текст целиком, без таймкодов (человек может их стереть).

    Версия обязательна (DEV-CHECKLIST, п. 2): без неё правка могла бы затереть то,
    что дописала «Искра» или другой человек в другой вкладке. `html` — та же правка с
    оформлением (уже очищенная, решение пользователя 13.10); '' — правка простым текстом.
    """
    stamp = now()
    # ручная правка хранится одним куском без таймкода — таймкоды нужны только
    # для исходной расшифровки, после правки текст уже не совпадает с ней построчно
    # start == end == 0 — признак ручной правки ещё с первой версии: откат на старый код его понимает
    segments = [{"start": 0, "end": 0, "text": text}] if text.strip() else []
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return None
        if int(base_version) != int(row["draft_version"]):
            raise Conflict(row)
        version = int(row["draft_version"]) + 1
        conn.execute(
            "UPDATE records SET draft = ?, draft_html = ?, draft_version = ?, updated_at = ?,"
            " updated_by = ?, updated_name = ? WHERE id = ?",
            (json.dumps(segments, ensure_ascii=False), html if text.strip() else "", version, stamp,
             user["login"], user["name"], record_id),
        )
    return get_record(record_id)


def _save_versioned(record_id: int, user: dict[str, str], base_version: int, field: str,
                    version_field: str, value: str,
                    extra: dict[str, str] | None = None) -> dict[str, Any] | None:
    stamp = now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return None
        if int(base_version) != int(row[version_field]):
            raise Conflict(row, version_field)
        sets = "".join(f", {name} = ?" for name in (extra or {}))
        conn.execute(
            f"UPDATE records SET {field} = ?{sets}, {version_field} = {version_field} + 1,"
            " updated_at = ?, updated_by = ?, updated_name = ? WHERE id = ?",
            (value, *(extra or {}).values(), stamp, user["login"], user["name"], record_id))
    return get_record(record_id)


def save_summary_text(record_id: int, text: str, user: dict[str, str],
                      base_version: int, html: str = "") -> dict[str, Any] | None:
    """Правка саммари руками — с версией, как у черновика; `html` — с оформлением (13.10)."""
    text = text.strip()
    return _save_versioned(record_id, user, base_version, "summary", "summary_version",
                           text, {"summary_html": html if text else ""})


def save_orders(record_id: int, orders: list[dict[str, str]], user: dict[str, str],
                base_version: int) -> dict[str, Any] | None:
    """Правка перечня поручений руками — с версией, как у черновика. id поручений сохраняются,
    у новых — выдаются (ревью RC-2026-10-13-2, К-1)."""
    orders = orders_mod.ensure_ids([dict(o) for o in orders])
    return _save_versioned(record_id, user, base_version, "orders", "orders_version",
                           json.dumps(orders, ensure_ascii=False))


def set_meta(record_id: int, user: dict[str, str], chair: str, chair_role: str,
             meeting_date: str) -> dict[str, Any] | None:
    """Председатель, его должность и дата совещания — для шапки, подписи и имён документов."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE records SET chair = ?, chair_role = ?, meeting_date = ?, updated_at = ?,"
            " updated_by = ?, updated_name = ? WHERE id = ?",
            (chair[:200], chair_role[:300], meeting_date[:10], now(), user["login"], user["name"],
             record_id))
        if not cur.rowcount:
            return None
    return get_record(record_id)


def set_meeting(record_id: int, meeting: dict[str, Any]) -> dict[str, Any] | None:
    """Запомнить, к какому совещанию приложены документы записи."""
    with connect() as conn:
        cur = conn.execute("UPDATE records SET meeting = ? WHERE id = ?",
                           (json.dumps(meeting, ensure_ascii=False), record_id))
        if not cur.rowcount:
            return None
    return get_record(record_id)


def set_archived(record_id: int, archived: bool) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute("UPDATE records SET archived = ? WHERE id = ?",
                           (1 if archived else 0, record_id))
        if not cur.rowcount:
            return None
    return get_record(record_id)


def delete_record(record_id: int) -> bool:
    with connect() as conn:
        return bool(conn.execute("DELETE FROM records WHERE id = ?", (record_id,)).rowcount)
