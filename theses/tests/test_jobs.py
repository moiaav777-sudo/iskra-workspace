"""Фоновое написание тезисов (задача Оркестратора P1, 21.09).

Раньше кнопка держала запрос, пока «Искра» думала, и страница выглядела зависшей.
Проверяем: ответ сразу, одно задание на тезисы, шаги, отмена, предельный срок,
возврат на страницу, перезапуск сервиса и расход «Искры» в журнале при любом исходе.
Сети нет: подменяется либо весь прогон (`iskra._call`), либо обмен с «Искрой» (`iskra._http`).
"""
from __future__ import annotations

from tests.conftest import add_line, put_lines

import json
import threading
import time

import pytest

ANSWER = json.dumps({"bullets": [{"text": "Коллеги, добрый день."}]}, ensure_ascii=False)


def _thesis(client, emp):
    created = client.post("/api/theses", json={"topic": "О клубах", "minutes": 7},
                          headers=emp).json()
    return created["thesis"]["id"], created["rev"]


def _wait(client, emp, job_id, until=lambda j: not j["active"], limit=10):
    deadline = time.time() + limit
    while True:
        job = client.get(f"/api/jobs/{job_id}", headers=emp).json()["job"]
        if until(job) or time.time() > deadline:
            return job
        time.sleep(0.03)


@pytest.fixture()
def slow(monkeypatch):
    """Медленная «Искра»: отвечает по сигналу `release` или сдаётся на отмену."""
    from app import iskra
    box = {"calls": 0, "release": threading.Event(), "running": threading.Event()}

    def fake_call(system, user):
        box["calls"] += 1
        box["running"].set()
        ctl = iskra.control()
        while not box["release"].is_set():
            if ctl.cancelled():
                iskra.spend(700, 40)          # прогон успел потратить
                raise iskra.Cancelled("Написание отменено")
            time.sleep(0.02)
        iskra.spend(1500, 300)
        return ANSWER

    monkeypatch.setattr(iskra, "_call", fake_call)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    return box


@pytest.fixture()
def audit(monkeypatch):
    from app import portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))
    return seen


# ── нажатие ──────────────────────────────────────────────────────────────

def test_нажатие_отвечает_сразу_а_не_когда_искра_допишет(client, emp, slow):
    tid, rev = _thesis(client, emp)
    started = time.monotonic()
    r = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp)
    assert r.status_code == 202
    assert time.monotonic() - started < 0.5, "страница не должна ждать «Искру»"
    job = r.json()["job"]
    assert job["active"] and r.json()["started"] is True
    slow["release"].set()
    assert _wait(client, emp, job["id"])["state"] == "done"


def test_второе_нажатие_не_запускает_второй_прогон(client, emp, slow):
    tid, rev = _thesis(client, emp)
    first = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()
    slow["running"].wait(2)
    second = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()
    assert second["job"]["id"] == first["job"]["id"] and second["started"] is False
    slow["release"].set()
    _wait(client, emp, first["job"]["id"])
    assert slow["calls"] == 1


def test_шаг_виден_пока_искра_пишет(client, emp, slow):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    assert _wait(client, emp, job["id"], until=lambda j: j["state"] == "writing")["state"] == "writing"
    slow["release"].set()
    done = _wait(client, emp, job["id"])
    assert done["state"] == "done" and done["seconds"] > 0


def test_можно_уйти_и_вернуться(client, emp, slow):
    """Страницу закрыли и открыли снова — задание находится по тезисам."""
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    again = client.get(f"/api/theses/{tid}/job", headers=emp).json()
    assert again["job"]["id"] == job["id"] and again["job"]["active"]
    assert again["deadline"] > 0 and again["typical"]["high"] > 0
    listed = client.get("/api/theses", headers=emp).json()["theses"]
    assert [row["running"] for row in listed if row["id"] == tid] == [True]
    slow["release"].set()
    _wait(client, emp, job["id"])


# ── исходы ───────────────────────────────────────────────────────────────

def test_готово_расход_в_журнале(client, emp, slow, audit):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["release"].set()
    _wait(client, emp, job["id"])
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    assert thesis["bullets"][0]["text"] == "Коллеги, добрый день."
    done = [e for e in audit if e["action"] == "Тезисы написаны «Искрой»"]
    assert done and done[0]["tokens_in"] == 1500 and done[0]["tokens_out"] == 300
    assert done[0]["object"].startswith("О клубах"), "в журнале название, а не номер"


def test_отмена_снимает_прогон_и_пишет_расход(client, emp, slow, audit):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    client.post(f"/api/jobs/{job['id']}/cancel", headers=emp)
    done = _wait(client, emp, job["id"])
    assert done["state"] == "cancelled"
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    assert thesis["bullets"] == [], "отмена текст не трогает"
    cancelled = [e for e in audit if e["action"] == "Написание тезисов отменено"]
    assert cancelled and cancelled[0]["tokens_in"] == 700, "отменённый прогон тоже стоил токенов"


def test_ошибка_искры_понятна_и_с_расходом(client, emp, monkeypatch, audit):
    from app import iskra

    def boom(system, user):
        iskra.spend(300, 0)
        raise iskra.IskraError("«Искра» прервала прогон: run failed")

    monkeypatch.setattr(iskra, "_call", boom)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    done = _wait(client, emp, job["id"])
    assert done["state"] == "failed" and "run failed" in done["error"]
    failed = [e for e in audit if e["action"] == "«Искра» не написала тезисы"]
    assert failed and failed[0]["tokens_in"] == 300


def test_без_ключа_не_запускаем(client, emp, monkeypatch):
    from app import iskra
    monkeypatch.setattr(iskra, "key_ready", lambda: False)
    tid, rev = _thesis(client, emp)
    r = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp)
    assert r.status_code == 503 and "Ключ" in r.json()["detail"]


def test_перезапуск_не_оставляет_вечного_ожидания(client, emp):
    from app import jobs, storage
    tid, _ = _thesis(client, emp)
    conn = jobs._conn()
    conn.execute("INSERT INTO jobs (id, thesis_id, state, started_at) VALUES (?, ?, ?, ?)",
                 ("oborvan", tid, "writing", "2026-09-21T10:00:00+00:00"))
    conn.commit()
    conn.close()
    assert jobs.interrupt_stale() == 1
    job = jobs.get("oborvan")
    assert job["state"] == "failed" and "перезапускался" in job["error"]


def test_обычная_длительность_по_своим_прогонам(client, emp):
    from app import jobs
    assert jobs.typical()["own"] is False, "пока своих прогонов нет — берём «Бриф»"
    tid, _ = _thesis(client, emp)
    conn = jobs._conn()
    for n, seconds in enumerate((60, 70, 80, 90, 100)):
        conn.execute("INSERT INTO jobs (id, thesis_id, state, started_at, finished_at, seconds)"
                     " VALUES (?, ?, 'done', ?, ?, ?)",
                     (f"j{n}", tid, "2026-09-21T10:00:00+00:00", f"2026-09-21T10:0{n}:00+00:00",
                      seconds))
    conn.commit()
    conn.close()
    typical = jobs.typical()
    assert typical["own"] is True and typical["high"] == 100


# ── сам прогон у «Искры»: /api/v1/runs ───────────────────────────────────

@pytest.fixture()
def runs(monkeypatch):
    """Подмена обмена с «Искрой»: POST заводит прогон, GET отдаёт состояния по очереди."""
    from app import iskra
    box = {"states": [], "calls": []}

    def fake_http(method, path, body=None, timeout=60):
        box["calls"].append((method, path))
        if method == "POST":
            return 202, {"run_id": "r1", "status": "queued"}
        if method == "DELETE":
            return 200, {}
        state = box["states"].pop(0) if len(box["states"]) > 1 else box["states"][0]
        return 200, state

    monkeypatch.setattr(iskra, "_http", fake_http)
    monkeypatch.setattr(iskra, "POLL_MIN", 0.0)
    monkeypatch.setattr(iskra, "POLL_MAX", 0.0)
    return box


def test_прогон_дожидается_ответа_и_считает_расход(runs):
    from app import iskra
    runs["states"] = [{"status": "running"},
                      {"status": "completed", "answer": ANSWER,
                       "usage": {"input_tokens": 900, "output_tokens": 120}}]
    ctl = iskra.Control()
    iskra.bind(ctl)
    try:
        assert iskra._call("правила", "тема") == ANSWER
    finally:
        iskra.bind(None)
    assert ctl.spent == {"in": 900, "out": 120}
    assert ("POST", "/api/v1/runs") in runs["calls"]


def test_предельный_срок_снимает_прогон(runs):
    from app import iskra
    runs["states"] = [{"status": "running", "usage": {"input_tokens": 50}}]
    ctl = iskra.Control(deadline=0)
    iskra.bind(ctl)
    try:
        with pytest.raises(iskra.IskraError, match="не ответила"):
            iskra._call("правила", "тема")
    finally:
        iskra.bind(None)
    assert ("DELETE", "/api/v1/runs/r1") in runs["calls"], "прогон надо снять"
    assert ctl.spent["in"] == 50, "и то, что он успел потратить, посчитать"


def test_отмена_снимает_прогон_у_искры(runs):
    from app import iskra
    runs["states"] = [{"status": "running"}]
    ctl = iskra.Control(cancelled=lambda: True)
    iskra.bind(ctl)
    try:
        with pytest.raises(iskra.Cancelled):
            iskra._call("правила", "тема")
    finally:
        iskra.bind(None)
    assert ("DELETE", "/api/v1/runs/r1") in runs["calls"]


def test_сорванный_прогон_объясняется_и_считается(runs):
    from app import iskra
    runs["states"] = [{"status": "failed", "error": {"message": "run failed"},
                       "usage": {"input_tokens": 30, "output_tokens": 0}}]
    ctl = iskra.Control()
    iskra.bind(ctl)
    try:
        with pytest.raises(iskra.IskraError, match="прервала"):
            iskra._call("правила", "тема")
    finally:
        iskra.bind(None)
    assert ctl.spent["in"] == 30


# ── замечания ревьюера RC-2026-09-21-10 ──────────────────────────────────

def test_к1_правки_за_время_прогона_не_затираются(client, emp, slow, audit):
    """Пока «Искра» писала, человек добавил тезис — её вариант не применяется молча."""
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    add_line(client, emp, tid, **{"text": "Свою мысль дописал руками.", "rev": rev})
    slow["release"].set()
    done = _wait(client, emp, job["id"])
    assert done["state"] == "conflict" and done["has_draft"]
    assert "изменили" in done["error"]
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()
    assert [b["text"] for b in thesis["thesis"]["bullets"]] == ["Свою мысль дописал руками."]
    kept = [e for e in audit if e["action"] == "Тезисы «Искры» не применены"]
    assert kept and kept[0]["tokens_in"] == 1500, "прогон стоил токенов, расход — в журнал"

    # человек посмотрел вариант и решил его взять
    draft = client.get(f"/api/jobs/{job['id']}/draft", headers=emp).json()
    assert draft["bullets"][0]["text"] == "Коллеги, добрый день."
    applied = client.post(f"/api/jobs/{job['id']}/apply", headers=emp,
                          json={"rev": thesis["rev"]})
    assert applied.status_code == 200
    assert [b["text"] for b in applied.json()["thesis"]["bullets"]] == ["Коллеги, добрый день."]
    assert client.get(f"/api/jobs/{job['id']}", headers=emp).json()["job"]["has_draft"] is False


def test_к1_применить_вариант_поверх_новых_правок_нельзя(client, emp, slow):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    add_line(client, emp, tid, **{"text": "Раз.", "rev": rev})
    slow["release"].set()
    _wait(client, emp, job["id"])
    stale = client.get(f"/api/theses/{tid}", headers=emp).json()["rev"]
    add_line(client, emp, tid, **{"text": "Два.", "rev": stale})
    r = client.post(f"/api/jobs/{job['id']}/apply", headers=emp, json={"rev": stale})
    assert r.status_code == 409


def test_к1_оставить_свою_редакцию(client, emp, slow):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    add_line(client, emp, tid, **{"text": "Моё.", "rev": rev})
    slow["release"].set()
    _wait(client, emp, job["id"])
    after = client.post(f"/api/jobs/{job['id']}/discard", headers=emp).json()["job"]
    assert after["has_draft"] is False


def test_к1_тезисы_в_архив_во_время_прогона(client, emp, slow):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    client.delete(f"/api/theses/{tid}", headers=emp)
    slow["release"].set()
    done = _wait(client, emp, job["id"])
    assert done["state"] == "conflict" and "архив" in done["error"]
    from app import storage
    assert storage.get(tid).thesis.bullets == [], "в архивные тезисы молча не пишем"


def test_удалили_во_время_прогона_понятная_причина(client, emp, admin, slow):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    client.delete(f"/api/theses/{tid}/permanent", headers=admin)
    slow["release"].set()
    from app import jobs
    deadline = time.time() + 5
    while jobs.get(job["id"])["active"] and time.time() < deadline:
        time.sleep(0.03)
    done = jobs.get(job["id"])
    assert done["state"] == "failed" and "удалили" in done["error"]
    assert "Внутренняя" not in done["error"]


def test_к2_параллельные_запуски_дают_одно_задание(client, emp, slow):
    """12 одновременных запусков по одним тезисам — одно задание и один вызов «Искры»."""
    from app import jobs
    hold = threading.Event()             # задания остаются активными, пока идёт проверка

    def prepare():
        hold.wait(10)
        raise jobs.iskra.Cancelled("конец проверки")

    try:
        for _ in range(5):
            tid, rev = _thesis(client, emp)
            barrier = threading.Barrier(12)
            got = []

            def go():
                barrier.wait()
                job, _ = jobs.start(tid, rev, "emp", "", prepare)
                got.append(job["id"])

            threads = [threading.Thread(target=go) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            assert len(got) == 12 and len(set(got)) == 1, f"заданий завелось {len(set(got))}"
    finally:
        hold.set()
        slow["release"].set()


def test_к2_база_не_пускает_второе_активное(client, emp):
    """Защита держится и между процессами: второе активное задание не вставить."""
    import sqlite3
    from app import jobs
    tid, _ = _thesis(client, emp)
    conn = jobs._conn()
    try:
        conn.execute("INSERT INTO jobs (id, thesis_id, state, started_at) VALUES ('a', ?, 'writing', 'x')",
                     (tid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO jobs (id, thesis_id, state, started_at)"
                         " VALUES ('b', ?, 'reading', 'x')", (tid,))
        # законченных — сколько угодно
        conn.execute("INSERT INTO jobs (id, thesis_id, state, started_at) VALUES ('c', ?, 'done', 'x')",
                     (tid,))
    finally:
        conn.close()


def test_отмена_на_подготовке_до_искры(client, emp, monkeypatch):
    from app import iskra, jobs
    called = []
    monkeypatch.setattr(iskra, "_call", lambda s, u: called.append(1) or ANSWER)
    tid, rev = _thesis(client, emp)
    gate = threading.Event()

    def prepare():
        gate.wait(5)
        return (None, "", "", "")

    job, _ = jobs.start(tid, rev, "emp", "", prepare)
    jobs.cancel(job["id"])
    gate.set()
    done = _wait(client, emp, job["id"])
    assert done["state"] == "cancelled" and not called


def test_срок_берётся_при_создании_управления(monkeypatch):
    from app import iskra
    monkeypatch.setattr(iskra, "DEADLINE", 5.0)
    assert iskra.Control().deadline == 5.0


# ── опрос «Искры»: 429, сбои связи, расход после снятия ──────────────────

@pytest.fixture()
def script(monkeypatch):
    """Опрос по сценарию: состояние, код 429 или сбой связи — по очереди."""
    from app import iskra
    box = {"get": [], "calls": [], "delete": (200, {})}

    def fake_http(method, path, body=None, timeout=60):
        box["calls"].append(method)
        if method == "POST":
            return 202, {"run_id": "r1", "status": "queued"}
        if method == "DELETE":
            return box["delete"]
        step = box["get"].pop(0) if len(box["get"]) > 1 else box["get"][0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return step, {"error": "too many requests"}
        return 200, step

    monkeypatch.setattr(iskra, "_http", fake_http)
    monkeypatch.setattr(iskra, "POLL_MIN", 0.0)
    monkeypatch.setattr(iskra, "POLL_MAX", 0.0)
    return box


def _in_thread(fn, limit=5):
    """Запустить и убедиться, что не висит: вечный цикл — это и есть К3."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as error:  # noqa: BLE001
            box["error"] = error

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    assert not t.is_alive(), "прогон завис и не сдался по сроку"
    return box


def test_к3_429_не_уводит_мимо_срока(script):
    from app import iskra
    script["get"] = [429]
    ctl = iskra.Control(deadline=0.6)

    def run():
        iskra.bind(ctl)
        try:
            return iskra._call("правила", "тема")
        finally:
            iskra.bind(None)

    box = _in_thread(run)
    assert isinstance(box.get("error"), iskra.IskraError) and "не ответила" in str(box["error"])
    assert "DELETE" in script["calls"], "прогон по сроку снимается"


def test_временный_сбой_опроса_не_валит_прогон(script):
    from app import iskra
    script["get"] = [iskra.IskraError("обрыв"), iskra.IskraError("обрыв"),
                     {"status": "completed", "answer": ANSWER}]
    ctl = iskra.Control()
    iskra.bind(ctl)
    try:
        assert iskra._call("правила", "тема") == ANSWER
    finally:
        iskra.bind(None)


def test_связь_рвётся_подряд_прогон_снимается_и_расход_забирается(script):
    from app import iskra
    script["get"] = [iskra.IskraError("обрыв")]
    script["delete"] = (200, {"status": "cancelled", "usage": {"input_tokens": 90}})
    ctl = iskra.Control()
    box = _in_thread(lambda: (iskra.bind(ctl), iskra._call("правила", "тема")))
    assert isinstance(box.get("error"), iskra.IskraError) and "рвалась" in str(box["error"])
    assert "DELETE" in script["calls"] and ctl.spent["in"] == 90


def test_расход_после_отмены_берётся_из_ответа_снятия(script):
    """Расход в прогоне накопительный: берём последний замер, а не сумму."""
    from app import iskra
    script["get"] = [{"status": "running", "usage": {"input_tokens": 50}}]
    script["delete"] = (200, {"status": "cancelled", "usage": {"input_tokens": 70}})
    flag = {"n": 0}

    def cancelled():
        flag["n"] += 1
        return flag["n"] > 3              # отменили после первого опроса

    ctl = iskra.Control(cancelled=cancelled)
    iskra.bind(ctl)
    try:
        with pytest.raises(iskra.Cancelled):
            iskra._call("правила", "тема")
    finally:
        iskra.bind(None)
    assert ctl.spent["in"] == 70


def test_таблица_заводится_без_гонки(client, emp):
    """Несколько потоков разом открывают свежую базу — ни один не падает на «duplicate column»."""
    from app import jobs, storage
    jobs._ready.discard(str(storage.DB_PATH))
    conn = storage._conn()
    conn.execute("DROP TABLE IF EXISTS jobs")
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, thesis_id TEXT NOT NULL, login TEXT NOT NULL DEFAULT '',"
                 " ip TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',"
                 " notes TEXT NOT NULL DEFAULT '', tokens_in INTEGER NOT NULL DEFAULT 0,"
                 " tokens_out INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,"
                 " finished_at TEXT NOT NULL DEFAULT '', seconds REAL NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    errors = []
    barrier = threading.Barrier(8)

    def open_it():
        barrier.wait()
        try:
            jobs._conn().close()
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors
    cols = {r["name"] for r in jobs._conn().execute("PRAGMA table_info(jobs)").fetchall()}
    assert {"base_rev", "draft"} <= cols


# ── RC-2026-09-21-13 ─────────────────────────────────────────────────────

def _conflict(client, emp, slow):
    """Прогон, во время которого поправили тезисы: вариант «Искры» ждёт решения."""
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    add_line(client, emp, tid, **{"text": "Моё.", "rev": rev})
    slow["release"].set()
    _wait(client, emp, job["id"])
    return tid, job["id"]


def test_п1_применить_без_версии_нельзя(client, emp, slow):
    tid, job_id = _conflict(client, emp, slow)
    r = client.post(f"/api/jobs/{job_id}/apply", headers=emp, json={})
    assert r.status_code == 400 and "версия" in r.json()["detail"]
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    assert [b["text"] for b in thesis["bullets"]] == ["Моё."], "правка цела"


def test_п3_оставить_моё_не_стирает_вариант(client, emp, slow):
    tid, job_id = _conflict(client, emp, slow)
    job = client.post(f"/api/jobs/{job_id}/discard", headers=emp).json()["job"]
    assert job["has_draft"] is False and job["kept_draft"] is True
    # вариант на месте, к нему можно вернуться и взять его позже
    assert client.get(f"/api/jobs/{job_id}/draft", headers=emp).status_code == 200
    listed = client.get(f"/api/theses/{tid}/drafts", headers=emp).json()["drafts"]
    assert [d["id"] for d in listed] == [job_id]
    rev = client.get(f"/api/theses/{tid}", headers=emp).json()["rev"]
    applied = client.post(f"/api/jobs/{job_id}/apply", headers=emp, json={"rev": rev})
    assert applied.status_code == 200
    assert client.get(f"/api/theses/{tid}/drafts", headers=emp).json()["drafts"] == []


def test_п3_отклонять_нечего_и_пока_идёт_нельзя(client, emp, slow, audit):
    tid, rev = _thesis(client, emp)
    job = client.post(f"/api/theses/{tid}/write", json={"rev": rev}, headers=emp).json()["job"]
    slow["running"].wait(2)
    assert client.post(f"/api/jobs/{job['id']}/discard", headers=emp).status_code == 404
    slow["release"].set()
    done = _wait(client, emp, job["id"])
    assert done["state"] == "done" and done["error"] == "", "у готового — без хвостов"
    assert not [e for e in audit if "отложен" in e["action"]], "без варианта событие не пишется"


def test_п4_новый_прогон_не_теряет_прежний_вариант(client, emp, slow):
    tid, job_id = _conflict(client, emp, slow)
    slow["release"].clear()
    rev = client.get(f"/api/theses/{tid}", headers=emp).json()["rev"]
    again = client.post(f"/api/theses/{tid}/write", headers=emp,
                        json={"rev": rev, "replace": True}).json()["job"]
    assert again["id"] != job_id
    listed = client.get(f"/api/theses/{tid}/drafts", headers=emp).json()["drafts"]
    assert job_id in [d["id"] for d in listed], "прежний вариант по-прежнему доступен"
    slow["release"].set()
    _wait(client, emp, again["id"])
