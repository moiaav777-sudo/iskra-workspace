"""Фоновое написание тезисов: задание в базе и поток, который его выполняет.

Раньше «Написать тезисы» держал запрос, пока «Искра» думала, — до пяти минут, — и
страница выглядела зависшей (задача Оркестратора P1 от 21.09). Теперь нажатие
заводит задание и сразу отвечает; страница опрашивает его и показывает шаг и время.

- Задание лежит в базе: можно уйти со страницы и вернуться — индикатор восстановится.
- На тезисы одно действующее задание, и это держит сама база: частичный уникальный
  индекс по активным заданиям. Второе нажатие — хоть из другой вкладки, хоть от
  коллеги, хоть в ту же миллисекунду — получает то же задание; прогона не два.
- Результат ложится только поверх той версии тезисов, с которой прогон начался.
  Если за время прогона тезисы поправили (сам человек, коллега, другая вкладка),
  результат не применяется: он хранится при задании как вариант «Искры», и человек
  сам решает — взять его или оставить своё. Молча затирать правки нельзя (ревьюер
  RC-2026-09-21-10, К1).
- «Отменить» снимает прогон у «Искры». Отмена и запись результата идут под одним
  замком задания, поэтому отменённое не записывается даже в последний миг.
- Перезапуск сервиса обрывает поток — такие задания при старте помечаются прерванными.
- Журнал портала пишет само задание: исход (готово, ошибка, отмена, не применено)
  становится известен без запроса. Расход «Искры» уходит при любом исходе.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import statistics
import threading
import traceback

from . import iskra, portal, richtext, storage
from .models import Bullet, Gap, Ref, new_id

log = logging.getLogger("theses.jobs")

# шаги, которые видит человек
READING, WRITING, CHECKING, POLISHING = "reading", "writing", "checking", "polishing"
# исходы
DONE, FAILED, CANCELLED = "done", "failed", "cancelled"
# «Искра» дописала, но тезисы за это время изменили — вариант ждёт решения человека
CONFLICT = "conflict"
# «Искра» написала новую редакцию для заполненных тезисов — человек решает, брать ли (22.09, п. 1)
PROPOSED = "proposed"
ACTIVE = (READING, WRITING, CHECKING, POLISHING)
_ACTIVE_SQL = "(" + ", ".join("?" * len(ACTIVE)) + ")"
_ACTIVE_LITERAL = "(" + ", ".join(f"'{s}'" for s in ACTIVE) + ")"

# «Обычно N минут», пока своих прогонов мало: так идут прогоны «Брифа»
# (brief/app/ui.html, «Обычно 3–5 минут»). Как наберётся пять своих — считаем по ним.
FALLBACK_TYPICAL = (180, 300)
OWN_ENOUGH = 5
KEEP_DAYS = 90          # законченные задания старше — удаляются при старте сервиса

_cancel_flags: dict[str, threading.Event] = {}
_job_locks: dict[str, threading.Lock] = {}
_flags_lock = threading.Lock()
# процесс один, но замок не лишний: без него между проверкой и вставкой успевает
# проскочить второй запрос; уникальный индекс ниже держит то же самое и между процессами
_start_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _init(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        " id TEXT PRIMARY KEY,"
        " thesis_id TEXT NOT NULL,"
        " login TEXT NOT NULL DEFAULT '',"
        " ip TEXT NOT NULL DEFAULT '',"
        " state TEXT NOT NULL,"
        " error TEXT NOT NULL DEFAULT '',"
        " notes TEXT NOT NULL DEFAULT '',"
        " tokens_in INTEGER NOT NULL DEFAULT 0,"
        " tokens_out INTEGER NOT NULL DEFAULT 0,"
        " started_at TEXT NOT NULL,"
        " finished_at TEXT NOT NULL DEFAULT '',"
        " seconds REAL NOT NULL DEFAULT 0)"
    )
    # столбцы, появившиеся позже, — в уже заведённую базу. Два подключения могут
    # одновременно увидеть, что столбца нет, — второе получит «duplicate column», это не беда
    have = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, ddl in (("base_rev", "INTEGER NOT NULL DEFAULT 0"),
                      ("draft", "TEXT NOT NULL DEFAULT ''"),
                      # '' — ждёт решения; kept — человек оставил своё; applied — взят
                      ("decided", "TEXT NOT NULL DEFAULT ''"),
                      # 1 — результат не применять, а предложить как вариант
                      ("propose", "INTEGER NOT NULL DEFAULT 0")):
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError as error:
                if "duplicate column" not in str(error):
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_by_thesis ON jobs(thesis_id, started_at)")
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(thesis_id)"
                     " WHERE state IN " + _ACTIVE_LITERAL)
    except sqlite3.IntegrityError:
        # в старой базе уже два активных — их закроет interrupt_stale при старте
        log.warning("jobs: есть задвоенные активные задания, индекс отложен до перезапуска")
    conn.commit()


_ready: set[tuple[str, int]] = set()
_ready_lock = threading.Lock()


def _conn():
    """Подключение к базе заданий. Таблицы и индексы заводятся один раз на базу —
    не на каждом опросе и не наперегонки из нескольких потоков."""
    conn = storage._conn()
    # с поколением базы: убрали файл на ходу — таблицу заданий заводим снова (RC-18, Н4)
    key = (str(storage.DB_PATH), storage.generation())
    if key not in _ready:
        with _ready_lock:
            if key not in _ready:
                _init(conn)
                _ready.add(key)
    return conn


def _between(start: str, end: str) -> float:
    try:
        return (dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _seconds_since(started: str) -> float:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return 0.0


def _row(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["notes"] = [n for n in (out.get("notes") or "").split("\n") if n]
    out["active"] = out["state"] in ACTIVE
    # сколько прошло — считает сервер: часы браузера и сервера могут расходиться
    out["elapsed"] = (_seconds_since(out["started_at"]) if out["active"]
                      else out.get("seconds") or _between(out["started_at"], out.get("finished_at")))
    draft = out.pop("draft", "")
    # вариант ждёт решения человека — только тогда показываем окно выбора;
    # отложенный («оставил своё») не стирается: к нему можно вернуться (ревьюер, П3)
    out["has_draft"] = bool(draft) and not out.get("decided")
    out["kept_draft"] = bool(draft) and out.get("decided") == "kept"
    out.pop("ip", None)
    return out


def get(job_id: str) -> dict | None:
    conn = _conn()
    try:
        return _row(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def draft(job_id: str) -> dict | None:
    """Вариант «Искры», не применённый из-за правок за время прогона."""
    conn = _conn()
    try:
        row = conn.execute("SELECT draft FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not row["draft"]:
        return None
    return json.loads(row["draft"])


def decide_draft(job_id: str, decision: str, why: str) -> None:
    """Решение по варианту принято. Взятый вариант лежит в тексте, а отложенный
    остаётся при задании: «Оставить мой» не должен терять работу «Искры» (П3)."""
    if decision == "applied":
        _set(job_id, draft="", decided="applied", error=why)
    else:
        _set(job_id, decided=decision, error=why)


def drafts(thesis_id: str) -> list[dict]:
    """Все варианты «Искры» по тезисам, не вошедшие в текст: и ждущие решения, и отложенные.
    Новый прогон прячет прежний вариант из окна, но отсюда он по-прежнему доступен (П4)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE thesis_id = ? AND draft != '' ORDER BY started_at DESC",
            (thesis_id,)).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def latest(thesis_id: str) -> dict | None:
    """Последнее задание по тезисам — по нему страница восстанавливает индикатор."""
    conn = _conn()
    try:
        return _row(conn.execute(
            "SELECT * FROM jobs WHERE thesis_id = ? ORDER BY started_at DESC LIMIT 1",
            (thesis_id,)).fetchone())
    finally:
        conn.close()


def active_theses() -> set[str]:
    conn = _conn()
    try:
        rows = conn.execute("SELECT thesis_id FROM jobs WHERE state IN " + _ACTIVE_SQL, ACTIVE)
        return {r["thesis_id"] for r in rows.fetchall()}
    finally:
        conn.close()


def typical() -> dict:
    """Сколько обычно идёт написание: по своим прогонам, если их хватает."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT seconds FROM jobs WHERE state IN (?, ?) AND seconds > 0"
            " ORDER BY finished_at DESC LIMIT 20", (DONE, PROPOSED)).fetchall()
    finally:
        conn.close()
    own = [r["seconds"] for r in rows]
    if len(own) >= OWN_ENOUGH:
        median = statistics.median(own)
        return {"low": int(median * 0.7), "high": int(max(own)), "own": True}
    low, high = FALLBACK_TYPICAL
    return {"low": low, "high": high, "own": False}


def _set(job_id: str, **fields) -> None:
    if not fields:
        return
    names = ", ".join(f"{k} = ?" for k in fields)
    conn = _conn()
    try:
        conn.execute(f"UPDATE jobs SET {names} WHERE id = ?", (*fields.values(), job_id))
        conn.commit()
    finally:
        conn.close()


def interrupt_stale() -> int:
    """При старте: задания, оборванные перезапуском, — прерваны, а не «идут вечно».
    Заодно старые законченные задания убираются: таблица читается на каждом опросе."""
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ? WHERE state IN " + _ACTIVE_SQL,
            (FAILED, "Сервис перезапускался, прогон оборвался. Запустите написание ещё раз",
             _now(), *ACTIVE))
        edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).isoformat()
        conn.execute("DELETE FROM jobs WHERE state NOT IN " + _ACTIVE_SQL
                     + " AND started_at < ?", (*ACTIVE, edge))
        conn.commit()
        count = cur.rowcount
    finally:
        conn.close()
    # индекс мог не завестись, пока в базе были задвоенные активные, — заводим заново
    with _ready_lock:
        _ready.discard((str(storage.DB_PATH), storage.generation()))
    _conn().close()
    return count


# ── запуск и отмена ──────────────────────────────────────────────────────

def _running(conn, thesis_id: str):
    return conn.execute("SELECT * FROM jobs WHERE thesis_id = ? AND state IN " + _ACTIVE_SQL,
                        (thesis_id, *ACTIVE)).fetchone()


def start(thesis_id: str, base_rev: int, login: str, ip: str, prepare,
          propose: bool = False) -> tuple[dict, bool]:
    """Завести задание и запустить поток. Уже идёт — вернуть его же.

    `base_rev` — версия тезисов на момент нажатия: результат ляжет только поверх неё.
    `prepare()` вызывается в потоке и отдаёт (thesis, rules_text, person, sources_text).
    Возвращает (задание, заведено ли новое).
    """
    with _start_lock:
        conn = _conn()
        try:
            running = _running(conn, thesis_id)
            if running is not None:
                return _row(running), False
            job_id = new_id()
            try:
                conn.execute(
                    "INSERT INTO jobs (id, thesis_id, login, ip, state, started_at, base_rev,"
                    " propose) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, thesis_id, login, ip, READING, _now(), int(base_rev),
                     1 if propose else 0))
                conn.commit()
            except sqlite3.IntegrityError:
                # другой процесс успел раньше — индекс не пустил второе задание
                conn.rollback()
                return _row(_running(conn, thesis_id)), False
        finally:
            conn.close()
    with _flags_lock:
        _cancel_flags[job_id] = threading.Event()
        _job_locks[job_id] = threading.Lock()
    threading.Thread(target=_work, args=(job_id, thesis_id, login, ip, prepare),
                     name=f"theses-job-{job_id}", daemon=True).start()
    return get(job_id), True


def cancel(job_id: str) -> dict | None:
    """Попросить остановиться. Поток снимет прогон у «Искры» и запишет исход сам.

    Флаг ставится под замком задания — тем же, под которым поток записывает результат:
    либо отмена успела и результат не пишется, либо результат уже записан и отменять нечего.
    """
    job = get(job_id)
    if job is None or not job["active"]:
        return job
    with _flags_lock:
        flag = _cancel_flags.get(job_id)
        lock = _job_locks.get(job_id)
    if flag is None or lock is None:
        # поток уже не наш (сервис перезапускался) — закрываем задание сами
        _set(job_id, state=CANCELLED, finished_at=_now(), error="Отменено")
    else:
        with lock:
            if (get(job_id) or {}).get("active"):
                flag.set()
    return get(job_id)


# ── сама работа ──────────────────────────────────────────────────────────

def _audit(action: str, thesis_id: str, login: str, ip: str, details: str, spent: dict) -> None:
    portal.send_audit(login=login, action=action, object=storage.audit_name(thesis_id),
                      details=details, ip=ip,
                      tokens_in=spent.get("in", 0), tokens_out=spent.get("out", 0))


def _pack(result: dict) -> str:
    return json.dumps({"bullets": [b.to_dict() for b in result["bullets"]],
                       "html": result.get("html", ""),
                       "refs": [r.to_dict() for r in result.get("refs", [])],
                       "gaps": [g.to_dict() for g in result["gaps"]],
                       "notes": result["notes"]}, ensure_ascii=False)


def unpack(data: dict) -> dict:
    return {"bullets": [Bullet.from_dict(b) for b in data.get("bullets", [])],
            "html": data.get("html", ""),
            "refs": [Ref.from_dict(r) for r in data.get("refs", [])],
            "gaps": [Gap.from_dict(g) for g in data.get("gaps", [])],
            "notes": data.get("notes", [])}


def apply_result(stored: storage.Stored, result: dict, expected_rev: int | None,
                 summary: str) -> storage.Stored:
    html = result.get("html") or richtext.html_from_bullets(result["bullets"])
    stored.thesis.bullets = result["bullets"]
    stored.thesis.set_html(html)
    stored.thesis.refs = result.get("refs", [])
    stored.thesis.gaps = result["gaps"]
    return storage.save(stored.thesis, expected_rev=expected_rev, summary=summary)


def _work(job_id: str, thesis_id: str, login: str, ip: str, prepare) -> None:
    with _flags_lock:
        flag = _cancel_flags.get(job_id) or threading.Event()
        lock = _job_locks.get(job_id) or threading.Lock()
    ctl = iskra.Control(on_stage=lambda name: _set(job_id, state=name),
                        cancelled=flag.is_set)
    iskra.bind(ctl)
    storage.set_current_login(login)
    job = get(job_id) or {}
    started = job.get("started_at") or _now()
    conn = _conn()
    try:
        row = conn.execute("SELECT base_rev, propose FROM jobs WHERE id = ?",
                           (job_id,)).fetchone()
        base_rev, propose = row["base_rev"], bool(row["propose"])
    finally:
        conn.close()

    def finish(state: str, action: str, details: str, **extra) -> None:
        # сначала журнал, потом исход: кто увидел исход, тот увидит и запись о нём
        _audit(action, thesis_id, login, ip, details, ctl.spent)
        _set(job_id, state=state, finished_at=_now(), seconds=_seconds_since(started),
             tokens_in=ctl.spent["in"], tokens_out=ctl.spent["out"], **extra)

    try:
        thesis, rules_text, person, sources_text = prepare()
        if flag.is_set():                           # отменили ещё на подготовке
            raise iskra.Cancelled("Написание отменено")
        result = iskra.write(thesis, rules_text, person, sources_text)
        summary = f"«Искра» написала тезисы: пунктов {len(result['bullets'])}"

        # отмена и запись — под одним замком: отменённое не записывается даже в последний миг
        with lock:
            if flag.is_set():
                raise iskra.Cancelled("Написание отменено")
            stored = storage.get(thesis_id)
            if stored is None:
                finish(FAILED, "«Искра» не написала тезисы",
                       "тезисы удалили во время прогона; результат не сохранён",
                       error="Тезисы удалили, пока «Искра» писала, — результат некуда сохранить")
                return
            if propose:
                # заполненные тезисы: новая редакция не ложится сама — её принимает человек
                finish(PROPOSED, "Тезисы написаны «Искрой»: вариант к принятию",
                       f"{summary}; предложен как вариант, решает человек",
                       draft=_pack(result), notes="\n".join(result["notes"]), error="")
                return
            if stored.archived:
                finish(CONFLICT, "Тезисы «Искры» не применены",
                       f"{summary}; тезисы отправили в архив во время прогона",
                       error="Тезисы отправили в архив, пока «Искра» писала. Её вариант "
                             "сохранён: восстановите тезисы и примените его, если он нужен",
                       draft=_pack(result), notes="\n".join(result["notes"]))
                return
            try:
                apply_result(stored, result, base_rev, summary)
            except storage.Conflict:
                # за время прогона тезисы изменили — молча не затираем (ревьюер, К1)
                finish(CONFLICT, "Тезисы «Искры» не применены",
                       f"{summary}; за время прогона тезисы изменили — версия "
                       f"{base_rev} → {stored.rev}, решает человек",
                       error="Пока «Искра» писала, тезисы изменили. Её вариант не применён, "
                             "чтобы не затереть правки: посмотрите его и решите сами",
                       draft=_pack(result), notes="\n".join(result["notes"]))
                return
            except KeyError:
                finish(FAILED, "«Искра» не написала тезисы",
                       "тезисы удалили во время прогона; результат не сохранён",
                       error="Тезисы удалили, пока «Искра» писала, — результат некуда сохранить")
                return
            seconds = _seconds_since(started)
            finish(DONE, "Тезисы написаны «Искрой»", f"{summary}; за {int(seconds)} с",
                   notes="\n".join(result["notes"]), error="")
    except iskra.Cancelled:
        finish(CANCELLED, "Написание тезисов отменено", "отменено человеком; текст не менялся",
               error="Отменено")
    except iskra.IskraError as error:
        finish(FAILED, "«Искра» не написала тезисы", f"{error}; текст не менялся",
               error=str(error))
    except Exception:  # noqa: BLE001 — поток не должен умирать молча
        log.error("задание %s упало:\n%s", job_id, traceback.format_exc())
        finish(FAILED, "«Искра» не написала тезисы", "внутренняя ошибка сервиса; текст не менялся",
               error="Внутренняя ошибка сервиса — текст не менялся. Попробуйте ещё раз")
    finally:
        iskra.bind(None)
        with _flags_lock:
            _cancel_flags.pop(job_id, None)
            _job_locks.pop(job_id, None)
