"""Разбор документов фоновым прогоном «Искры» (../CLAUDE.md п. 5в, решение 05.10).

Синхронный POST /api/v1/chat у «Искры» ограничен 90 с — большой документ давал 504. Теперь
POST /api/v1/runs → опрос GET /api/v1/runs/{id}?after=N до терминального статуса. Сети в тестах
нет: подменяется `llm_extract._http` (и загрузка файла), время — `_clock`/`_sleep`.
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import llm_extract, main, portal, storage

SECRET = "portal-test-secret"
DOC = {"doc_type": "участники", "confidence": "высокая", "summary": "список",
       "data": {"participants": [{"name": "Иванов Иван Иванович", "role": "министр"}]}}


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 2))
        self.now += seconds


class FakeIskra:
    """Ответы «Искры» по порядку: POST /runs, GET /runs/{id}, DELETE. Элемент — (код, тело) или исключение."""

    def __init__(self, gets, posts=None, delete=(200, {"status": "cancelled"}), get_after_delete=None):
        self.posts = list(posts or [(202, {"run_id": "r1", "conversation_id": "c1", "status": "queued"})])
        self.gets = list(gets)
        self.delete = delete
        self.get_after_delete = get_after_delete
        self.calls: list[tuple] = []

    def __call__(self, method, path, body=None, timeout=60, headers=None):
        self.calls.append((method, path, body, dict(headers or {})))
        if method == "POST":
            item = self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]
        elif method == "DELETE":
            item = self.delete
        elif any(c[0] == "DELETE" for c in self.calls):
            item = self.get_after_delete or (200, {"status": "cancelled"})
        else:
            item = self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        if isinstance(item, Exception):
            raise item
        return item

    def methods(self):
        return [c[0] for c in self.calls]

    def gets_paths(self):
        return [c[1] for c in self.calls if c[0] == "GET"]


def running(after=2, usage=None):
    body = {"run_id": "r1", "status": "running", "result": None, "error": None, "events": [], "next_after": after}
    if usage:
        body["usage"] = usage
    return 200, body


def done(answer="", output=None, usage=None, after=4):
    return 200, {"run_id": "r1", "status": "completed", "error": None, "events": [], "next_after": after,
                 "result": {"answer": answer, "output": output, "conversation_id": "c1",
                            "usage": usage or {"input_tokens": 51176, "output_tokens": 87}}}


@pytest.fixture()
def iskra(monkeypatch):
    """Ключ есть, время ненастоящее, корзинка запроса свежая — в неё копится расход."""
    clock = Clock()
    monkeypatch.setattr(llm_extract, "_load_key", lambda: "isk_test")
    monkeypatch.setattr(llm_extract, "_clock", clock)
    monkeypatch.setattr(llm_extract, "_sleep", clock.sleep)
    bucket = portal.start_request("alice")

    def use(fake):
        monkeypatch.setattr(llm_extract, "_http", fake)
        return fake

    yield {"clock": clock, "bucket": bucket, "use": use}
    portal._request.set(None)


def spent(bucket):
    return bucket["tokens_in"], bucket["tokens_out"]


# ── прогон и опрос ──────────────────────────────────────────────────────

def test_run_polls_until_completed(iskra):
    fake = iskra["use"](FakeIskra([running(after=2), running(after=3), done("ответ")]))
    assert llm_extract._call_iskra_raw("инструкция", "текст") == "ответ"
    method, path, body, headers = fake.calls[0]
    assert (method, path) == ("POST", "/api/v1/runs")
    assert body == {"message": "текст", "instructions": "инструкция"}
    uuid.UUID(headers["Idempotency-Key"])                       # ключ повтора — uuid
    # next_after передаётся дальше
    assert fake.gets_paths() == ["/api/v1/runs/r1?after=0", "/api/v1/runs/r1?after=2", "/api/v1/runs/r1?after=3"]
    assert iskra["clock"].sleeps == [2.0, 2.5, 3.0]             # пауза растёт от 2 с
    assert "DELETE" not in fake.methods()
    assert spent(iskra["bucket"]) == (51176, 87)


def test_pause_grows_to_five_seconds(iskra):
    iskra["use"](FakeIskra([running()] * 9 + [done("x")]))
    llm_extract._call_iskra_raw("s", "u")
    assert iskra["clock"].sleeps[-4:] == [5.0, 5.0, 5.0, 5.0]
    assert max(iskra["clock"].sleeps) == 5.0


def test_schema_object_from_result_output(iskra):
    """Объект схемы `result.output` — первым; `result.answer` — когда объекта нет."""
    iskra["use"](FakeIskra([done(answer="Вот JSON: …", output=DOC)]))
    res = llm_extract.extract("список участников")
    assert res["doc_type"] == "участники"
    assert res["data"]["participants"][0]["name"] == "Иванов Иван Иванович"


def test_files_and_conversation_go_to_run(iskra):
    fake = iskra["use"](FakeIskra([done("ok")]))
    llm_extract._call_iskra_raw("s", "u", files=[{"id": "f1"}], conversation_id="c9")
    assert fake.calls[0][2]["files"] == [{"id": "f1"}] and fake.calls[0][2]["conversation_id"] == "c9"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_failed_and_cancelled_runs_count_tokens(iskra, state):
    iskra["use"](FakeIskra([running(), (200, {
        "run_id": "r1", "status": state, "next_after": 3, "events": [],
        "error": {"code": "internal", "message": "модель упала"},
        "result": {"answer": "", "usage": {"input_tokens": 51000, "output_tokens": 5}}})]))
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "u")
    assert "модель упала" in str(err.value) and state in str(err.value)
    assert spent(iskra["bucket"]) == (51000, 5)                # неудачный прогон тоже стоил токенов


def test_interaction_required_is_clear_error(iskra):
    fake = iskra["use"](FakeIskra([(200, {
        "run_id": "r1", "status": "interaction_required", "next_after": 2, "error": None,
        "result": {"answer": "Уточните, какой документ разобрать?",
                   "usage": {"input_tokens": 51200, "output_tokens": 30}}})],
        delete=(404, {"error": "not running"})))
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "u")
    text = str(err.value)
    assert "interaction_required" in text and "Уточните, какой документ разобрать?" in text
    assert "подсказку" in text
    assert "DELETE" in fake.methods()                           # прогон не оставляем висеть
    assert spent(iskra["bucket"]) == (51200, 30)               # расход — из последнего замера


# ── обрывы связи и 429 ──────────────────────────────────────────────────

def test_poll_survives_429_and_disconnect(iskra):
    fake = iskra["use"](FakeIskra([(429, {"error": "rate"}), llm_extract.IskraError("обрыв"),
                                   running(after=5), done("готово")]))
    assert llm_extract._call_iskra_raw("s", "u") == "готово"
    # после 429 и обрыва — пауза 5 с, а не чаще
    assert iskra["clock"].sleeps[:3] == [2.0, 5.0, 5.0]
    assert fake.gets_paths()[-1] == "/api/v1/runs/r1?after=5"
    assert "DELETE" not in fake.methods()


def test_post_retried_with_same_idempotency_key(iskra):
    """Запуск оборвался — повтор тем же ключом: «Искра» вернёт прежний run_id, второго исполнения нет."""
    fake = iskra["use"](FakeIskra(
        [done("ok")],
        posts=[llm_extract.IskraError("«Искра» не ответила: reset"), (429, {"error": "rate"}),
               (202, {"run_id": "r1", "status": "queued"})]))
    assert llm_extract._call_iskra_raw("s", "u") == "ok"
    keys = [c[3]["Idempotency-Key"] for c in fake.calls if c[0] == "POST"]
    assert len(keys) == 3 and len(set(keys)) == 1


def test_post_client_error_not_retried(iskra):
    fake = iskra["use"](FakeIskra([done("ok")], posts=[(404, {"error": {"code": "invalid_file"}})]))
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "u", files=[{"id": "old"}])
    assert "404" in str(err.value)
    assert fake.methods() == ["POST"]


def test_three_disconnects_give_up_and_settle(iskra):
    fake = iskra["use"](FakeIskra([llm_extract.IskraError("«Искра» не ответила: reset")],
                                  delete=(200, {"status": "cancelled",
                                                "usage": {"input_tokens": 51000, "output_tokens": 0}})))
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "u")
    assert "рвалась 3 раза" in str(err.value)
    assert fake.methods() == ["POST", "GET", "GET", "GET", "DELETE"]
    assert spent(iskra["bucket"]) == (51000, 0)


# ── срок ────────────────────────────────────────────────────────────────

def test_deadline_cancels_run_and_counts_usage(iskra):
    """14 минут без конца — прогон снимается DELETE, расход берётся из снятого прогона (один раз)."""
    fake = iskra["use"](FakeIskra(
        [running(usage={"input_tokens": 51000, "output_tokens": 1})],
        delete=(202, {"status": "cancelling"}),                 # в ответе DELETE расхода нет …
        get_after_delete=(200, {"status": "cancelled",          # … берём его повторным GET
                                "result": {"usage": {"input_tokens": 51176, "output_tokens": 300}}})))
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "u")
    assert "14 мин" in str(err.value) and "снят" in str(err.value)
    assert fake.methods()[-2:] == ["DELETE", "GET"]
    assert iskra["clock"].now - 1000.0 <= llm_extract.DEADLINE + 0.01   # дольше срока не ждали
    assert spent(iskra["bucket"]) == (51176, 300)


def test_deadline_falls_back_to_last_usage(iskra):
    """«Искра» не ответила и на снятие — остаётся последний замер расхода из опроса."""
    iskra["use"](FakeIskra([running(usage={"input_tokens": 51000, "output_tokens": 7})],
                           delete=llm_extract.IskraError("нет связи"),
                           get_after_delete=(500, {})))
    with pytest.raises(RuntimeError):
        llm_extract._call_iskra_raw("s", "u")
    assert spent(iskra["bucket"]) == (51000, 7)


def test_deadline_is_shared_by_whole_request(iskra):
    """Срок один на запрос: после 14 минут повтор не запускает новый прогон."""
    fake = iskra["use"](FakeIskra([running()]))
    with pytest.raises(RuntimeError):
        llm_extract._call_iskra_raw("s", "u")
    posts = fake.methods().count("POST")
    with pytest.raises(RuntimeError) as err:
        llm_extract._call_iskra_raw("s", "повтор")
    assert "14 мин" in str(err.value)
    assert fake.methods().count("POST") == posts


def test_unexpected_exit_still_settles(iskra, monkeypatch):
    """Любой другой выход из опроса (отмена потока) — прогон снимается, расход считается."""
    fake = iskra["use"](FakeIskra([running()], delete=(200, {
        "status": "cancelled", "usage": {"input_tokens": 9, "output_tokens": 1}})))

    def interrupted(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(llm_extract, "_sleep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        llm_extract._call_iskra_raw("s", "u")
    assert "DELETE" in fake.methods()
    assert spent(iskra["bucket"]) == (9, 1)


# ── файлы ───────────────────────────────────────────────────────────────

def test_extract_with_file_reuploads_on_retry(iskra, monkeypatch):
    """id файла одноразовый: вторая попытка грузит файл заново и шлёт новый id в новом разговоре."""
    uploads = []

    def upload(data, name):
        uploads.append((data, name))
        n = len(uploads)
        return f"f{n}", f"c{n}"
    monkeypatch.setattr(llm_extract, "_upload_file_iskra", upload)
    fake = iskra["use"](FakeIskra(
        [done("Извините, это не JSON"), done(json.dumps(DOC, ensure_ascii=False))],
        posts=[(202, {"run_id": "r1", "status": "queued"}), (202, {"run_id": "r2", "status": "queued"})]))
    res = llm_extract.extract_with_file(b"%PDF-1.4 ...", "список.pdf", "участники")
    assert res["doc_type"] == "участники"
    assert uploads == [(b"%PDF-1.4 ...", "список.pdf")] * 2
    runs = [c[2] for c in fake.calls if c[0] == "POST"]
    assert [r["files"] for r in runs] == [[{"id": "f1"}], [{"id": "f2"}]]
    assert [r["conversation_id"] for r in runs] == ["c1", "c2"]
    assert "Подсказка: документ типа «участники»" in runs[0]["message"]
    assert spent(iskra["bucket"]) == (51176 * 2, 87 * 2)       # обе попытки в журнале


def test_extract_with_file_no_second_try_after_deadline(iskra, monkeypatch):
    uploads = []
    monkeypatch.setattr(llm_extract, "_upload_file_iskra", lambda d, n: uploads.append(n) or ("f", "c"))
    iskra["use"](FakeIskra([running()]))
    with pytest.raises(RuntimeError) as err:
        llm_extract.extract_with_file(b"x", "a.pdf")
    assert "14 мин" in str(err.value)
    assert len(uploads) == 1                                   # срок вышел — второй загрузки нет


# ── через сервис ────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: events.append(e))
    monkeypatch.setenv("PORTAL_SECRET", SECRET)
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "meetings.db")
    c = TestClient(main.app)
    c.headers.update({"X-Portal-Secret": SECRET, "X-Portal-User": "alice", "X-Portal-Role": "employee"})
    c.audit = events
    return c


def test_failed_run_tokens_reach_audit(client, iskra):
    iskra["use"](FakeIskra([(200, {"run_id": "r1", "status": "failed", "error": {"message": "сбой"},
                                    "result": {"usage": {"input_tokens": 51000, "output_tokens": 2}}})]))
    r = client.post("/api/analyze", json={"text": "Повестка совещания"})
    assert r.status_code == 502 and "сбой" in r.json()["detail"]
    ev = [e for e in client.audit if e.get("tokens_in")]
    assert ev and ev[-1]["tokens_in"] == 51000 * 2              # две попытки разбора, обе посчитаны


def test_iskra_routes_run_in_threadpool():
    """Долгие вызовы «Искры» идут в пуле потоков (обычные def): цикл событий и /healthz не ждут разбора."""
    slow = ("analyze", "inbox", "opening", "closing", "command", "propose-from", "generate", "healthz")
    checked = 0
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if any(s in path for s in slow) and getattr(route, "endpoint", None):
            assert not inspect.iscoroutinefunction(route.endpoint), path
            checked += 1
    assert checked >= 9
