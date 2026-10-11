"""Движок «Искры» (`app/engine/iskra_engine.py`) на подменном HTTP — без сети и без ключа.

Путь по правилам API «Искры» (../CLAUDE.md, п. 5в): загрузка файла `/api/v1/chat/files` → фоновый
прогон `POST /api/v1/runs` с `{id}` и `Idempotency-Key` → опрос `GET /api/v1/runs/{id}?after=N` →
TXT-файл расшифровки из `result.files` скачивается по `download_url`. Подменяется единственная
точка выхода в сеть — `iskra_engine._urlopen` — и часы (`_now`/`_sleep`), чтобы не ждать пауз
опроса; всё остальное (multipart-поток, разбор ответа, ошибки, счёт токенов) работает по-настоящему.
Формы ответов `/runs` — из живой пробы с VPS 05.10, форма `files` — из справки «Искры».
"""
from __future__ import annotations

import http.client
import http.server
import importlib
import io
import threading
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, engine  # noqa: E402
from app.engine import iskra_engine  # noqa: E402

TXT_URL = "/api/v1/files/f-1/download"


class FakeResponse:
    def __init__(self, payload=None, status=200, raw: bytes | None = None, headers=None):
        self._raw = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._pos = 0
        self.status = status
        self.headers = headers or {}

    def read(self, size=-1):
        if size is None or size < 0:
            out, self._pos = self._raw[self._pos:], len(self._raw)
            return out
        out = self._raw[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, payload):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(payload).encode()))


def txt_file(name="Расшифровка записи.txt", url=TXT_URL):
    return [{"name": name, "download_url": url}]


class FakeIskra:
    """Подменная «Искра»: запоминает запросы; каждый прогон отвечает по своему сценарию.

    Сценарий прогона — словарь: `answer`, `files`, `status` (completed/failed/cancelled/
    interaction_required), `error`, `usage` (in, out), `polls` (сколько опросов «running» до конца),
    `never` (прогон не кончается). `parts` — короткая запись сценариев «только ответ»;
    `file_text` — сценарий основного пути: расшифровка TXT-файлом в `result.files`.
    """

    def __init__(self, parts=None, script=None, file_text=None, upload_status=200, usage=(10, 5),
                 on_run=None, upload_payload=None, run_post_statuses=None, poll_statuses=None,
                 file_status=200, file_headers=None, file_bytes=None, delete_usage=True,
                 file_redirects=None, poll_raise=None, delete_raise=None, download_raise=None):
        if script is None:
            if file_text is not None:
                script = [{"answer": "Расшифровка сохранена в файл.", "files": txt_file()}]
            else:
                script = [{"answer": p} for p in (parts or ["[00:00] Говорящий 1: Добрый день.\n[КОНЕЦ]"])]
        self.script = script
        self.file_text = file_text
        self.file_bytes = file_bytes
        self.file_status = file_status
        self.file_headers = file_headers or {}
        self.upload_status = upload_status
        self.usage = usage
        self.on_run = on_run
        self.upload_payload = upload_payload
        self.run_post_statuses = list(run_post_statuses or [])
        self.poll_statuses = list(poll_statuses or [])
        self.delete_usage = delete_usage
        self.file_redirects = list(file_redirects or [])     # Location на каждое скачивание по очереди
        self.poll_raise = list(poll_raise or [])             # исключения на опросах по очереди
        self.delete_raise = delete_raise
        self.download_raise = download_raise
        self.uploads: list[dict] = []
        self.runs: list[dict] = []
        self.polls: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.downloads: list[dict] = []
        self._polled: dict[str, int] = {}

    @property
    def parts(self):
        return [spec.get("answer", "") for spec in self.script]

    @parts.setter
    def parts(self, answers):
        """Тесты сервиса меняют ответы на лету: «только ответ, без файла» — запасной путь."""
        self.script = [{"answer": a} for a in answers]

    # -- служебное
    def spec(self, run_id):
        n = int(run_id.split("-")[1])
        return self.script[min(n - 1, len(self.script) - 1)]

    def usage_of(self, spec):
        i, o = spec.get("usage", self.usage)
        return {"input_tokens": i, "output_tokens": o}

    def __call__(self, request, timeout):
        url = request.full_url
        method = request.get_method()
        if url.endswith("/api/v1/chat/files"):
            data = request.data
            assert hasattr(data, "read"), "файл должен уходить потоком, а не строкой байтов"
            body = b""
            while True:
                chunk = data.read(8192)
                if not chunk:
                    break
                body += chunk
            self.uploads.append({"body": body, "length": int(request.get_header("Content-length")),
                                 "type": request.get_header("Content-type"),
                                 "auth": request.get_header("Authorization")})
            if self.upload_status != 200:
                raise http_error(url, self.upload_status, {"detail": "нет"})
            return FakeResponse(self.upload_payload or {"files": [{"id": "file-1"}],
                                                        "conversation_id": "conv-1"})
        if url.endswith("/api/v1/runs") and method == "POST":
            payload = json.loads(request.data.decode("utf-8"))
            self.runs.append({"body": payload, "key": request.get_header("Idempotency-key"),
                              "auth": request.get_header("Authorization")})
            if self.on_run:
                self.on_run(len(self.runs))
            if self.run_post_statuses:
                code = self.run_post_statuses.pop(0)
                if code != 202:
                    self.runs[-1]["refused"] = code
                    raise http_error(url, code, {"detail": "занято"})
            accepted = [r for r in self.runs if "refused" not in r]
            run_id = f"run-{len(accepted)}"
            return FakeResponse({"run_id": run_id, "conversation_id": "conv-1", "status": "queued",
                                 "expires_at": "2026-10-05T10:41:58Z"}, status=202)
        if "/api/v1/runs/" in url:
            tail = url.split("/api/v1/runs/")[1]
            run_id, _, query = tail.partition("?")
            spec = self.spec(run_id)
            if method == "DELETE":
                self.deleted.append(run_id)
                if self.delete_raise is not None:
                    raise self.delete_raise
                result = {"usage": self.usage_of(spec)} if self.delete_usage else {}
                return FakeResponse({"run_id": run_id, "status": "cancelled", "result": result})
            after = int(query.split("after=")[1]) if "after=" in query else -1
            self.polls.append((run_id, after))
            if self.poll_raise:
                raise self.poll_raise.pop(0)
            if self.poll_statuses:
                code = self.poll_statuses.pop(0)
                raise http_error(url, code, {"detail": "часто"})
            n = self._polled[run_id] = self._polled.get(run_id, 0) + 1
            if spec.get("never") or n <= spec.get("polls", 1):
                running = {"run_id": run_id, "status": "running", "conversation_id": "conv-1",
                           "events": [{"type": "tool", "name": "transcribe_media"}],
                           "next_after": n * 2, "result": None, "error": None}
                if spec.get("running_usage"):
                    i, o = spec["running_usage"]
                    running["usage"] = {"input_tokens": i, "output_tokens": o}
                return FakeResponse(running)
            status = spec.get("status", "completed")
            result = {"usage": self.usage_of(spec), "conversation_id": "conv-1",
                      "answer": spec.get("answer", "") if status == "completed" else ""}
            if "files" in spec:
                result["files"] = spec["files"]
            return FakeResponse({"run_id": run_id, "status": status, "conversation_id": "conv-1",
                                 "result": result, "error": spec.get("error"),
                                 "events": [{"type": "content"}], "next_after": n * 2 + 1})
        # всё остальное — скачивание файла результата
        self.downloads.append({"url": url, "auth": request.get_header("Authorization")})
        if self.download_raise is not None:
            raise self.download_raise
        if self.file_redirects:
            location = self.file_redirects.pop(0)
            raise urllib.error.HTTPError(url, 302, "Found", {"Location": location}, io.BytesIO(b""))
        if self.file_status != 200:
            raise http_error(url, self.file_status, {"detail": "нет файла"})
        raw = self.file_bytes if self.file_bytes is not None else (self.file_text or "").encode("utf-8")
        return FakeResponse(raw=raw, headers=self.file_headers)


class Clock:
    """Подменные часы: паузы опроса не ждут, а двигают время."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def fast(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(iskra_engine, "_now", clock.now)
    monkeypatch.setattr(iskra_engine, "_sleep", clock.sleep)
    return clock


@pytest.fixture()
def recording(tmp_path, monkeypatch):
    monkeypatch.setenv("ISKRA_API_KEY", "test-key")
    monkeypatch.setenv("ISKRA_URL", "https://iskra.test")
    path = tmp_path / "совещание.mp3"
    path.write_bytes(b"ID3" + b"\x01\x02" * 40000)
    return path


def run(monkeypatch, fake, path, cancelled=lambda: False):
    fast(monkeypatch)
    monkeypatch.setattr(iskra_engine, "_urlopen", fake)
    eng = engine.get_engine("iskra")
    return eng, eng.transcribe(path, cancelled)


def failing(monkeypatch, fake, path, error=engine.TranscribeError, cancelled=lambda: False):
    fast(monkeypatch)
    monkeypatch.setattr(iskra_engine, "_urlopen", fake)
    eng = engine.get_engine("iskra")
    with pytest.raises(error) as err:
        eng.transcribe(path, cancelled)
    return eng, err


TRANSCRIPT_TXT = ("[00:00] Говорящий 1: Добрый день, коллеги.\n"
                  "[00:42] Говорящий 2: Начнём с первого вопроса.\n"
                  "[01:30] Говорящий 1: Поручаю подготовить доклад.\n")


# ── основной путь: прогон → файл расшифровки ─────────────────────────────────

def test_fayl_v_rezultate_osnovnoy_put(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[
        {"answer": "Готово, расшифровка сохранена в черновики.", "files": txt_file(), "polls": 2}])
    stages: list[str] = []
    fast(monkeypatch)
    monkeypatch.setattr(iskra_engine, "_urlopen", fake)
    eng = engine.get_engine("iskra")
    eng.progress = stages.append
    segments = eng.transcribe(recording, lambda: False)

    # загрузка: multipart с самим файлом, длина объявлена заранее, ключ в заголовке
    assert len(fake.uploads) == 1
    up = fake.uploads[0]
    assert up["length"] == len(up["body"])
    assert recording.read_bytes() in up["body"]
    assert up["type"].startswith("multipart/form-data; boundary=")
    assert up["auth"] == "Bearer test-key"

    # прогон: файл по id, тот же разговор, ключ идемпотентности
    assert len(fake.runs) == 1
    body = fake.runs[0]["body"]
    assert body["files"] == [{"id": "file-1"}]
    assert body["conversation_id"] == "conv-1"
    assert "слово в слово" in body["message"] and "таймкод" in body["message"]
    assert "TXT" in body["message"] and "черновики" in body["message"]
    assert body["instructions"]
    assert fake.runs[0]["key"]

    # опрос с ?after=N — дальше передаётся next_after
    assert [a for _r, a in fake.polls] == [0, 2, 4]

    # файл скачан с ключом с адреса «Искры» (относительная ссылка — от ISKRA_URL)
    assert fake.downloads == [{"url": "https://iskra.test" + TXT_URL, "auth": "Bearer test-key"}]

    # таймкоды и говорящие разобраны
    assert [s.start for s in segments] == [0, 42, 90]
    assert [s.end for s in segments] == [42, 90, None]
    assert [s.speaker for s in segments] == ["Говорящий 1", "Говорящий 2", "Говорящий 1"]
    assert segments[2].text == "Поручаю подготовить доклад."
    # расход — один прогон, без пометок
    assert eng.spent == {"in": 10, "out": 5, "total": 15}
    assert eng.parts == 1 and eng.note == "" and eng.source == "file"
    # строка хода дела — из событий, без текста ответа
    assert any("распознаёт речь" in s for s in stages)
    assert not any("Готово" in s for s in stages)
    assert fake.deleted == []


def test_absolyutnaya_ssylka_na_chuzhoy_adres_bez_klyucha(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[
        {"answer": "Файл сохранён.", "files": txt_file(url="https://storage.example/f.txt?sig=1")}])
    run(monkeypatch, fake, recording)
    assert fake.downloads == [{"url": "https://storage.example/f.txt?sig=1", "auth": None}]


def test_vybiraetsya_txt_rasshifrovki_sredi_faylov(monkeypatch, recording):
    files = [{"name": "Саммари.docx", "download_url": "/d/1"},
             {"name": "заметки.md", "download_url": "/d/2"},
             {"name": "Транскрипция совещания.txt", "download_url": "/d/3"},
             {"name": "без ссылки.txt"}, "мусор", {"download_url": 5}]
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[{"answer": "Файл сохранён.", "files": files}])
    _eng, segments = run(monkeypatch, fake, recording)
    assert [d["url"] for d in fake.downloads] == ["https://iskra.test/d/3"]
    assert len(segments) == 3


def test_fayl_cp1251_i_metka_konca(monkeypatch, recording):
    fake = FakeIskra(file_bytes=(TRANSCRIPT_TXT + "[КОНЕЦ]\n").encode("cp1251"),
                     script=[{"answer": "Сохранила.", "files": txt_file()}])
    eng, segments = run(monkeypatch, fake, recording)
    assert segments[0].text == "Добрый день, коллеги."
    assert all("КОНЕЦ" not in s.text for s in segments) and eng.note == ""


def test_net_fayla_beretsya_answer(monkeypatch, recording):
    fake = FakeIskra(script=[{"answer": TRANSCRIPT_TXT + "[КОНЕЦ]", "files": []}])
    eng, segments = run(monkeypatch, fake, recording)
    assert fake.downloads == []
    assert [s.start for s in segments] == [0, 42, 90]
    assert eng.source == "answer" and eng.note == ""


def test_inaya_forma_files_ne_padenie(monkeypatch, recording):
    """`files` строкой, объектом без списка, без ссылок — не падаем: берём ответ."""
    for files in ("files.txt", {"weird": True}, [{"name": "a.txt"}], None):
        fake = FakeIskra(script=[{"answer": "[00:00] Говорящий 1: Текст.\n[КОНЕЦ]", "files": files}])
        _eng, segments = run(monkeypatch, fake, recording)
        assert [s.text for s in segments] == ["Текст."]
        assert fake.downloads == []


def test_tolko_ne_tekstovye_fayly_ponyatnaya_oshibka(monkeypatch, recording):
    fake = FakeIskra(script=[{"answer": "", "files": [{"name": "a.docx", "download_url": "/d/1"}]}])
    _eng, err = failing(monkeypatch, fake, recording)
    assert "текстового" in str(err.value)


# ── срок 15 минут → повторный прогон в том же разговоре ─────────────────────

@pytest.mark.parametrize("error", [{"code": "interrupted", "message": "run interrupted"},
                                   {"code": "run_timeout"}, "expired"])
def test_srok_15_min_povtornyy_progon(monkeypatch, recording, error):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[
        {"status": "failed", "error": error, "usage": (51000, 100)},
        {"answer": "Вот файл.", "files": txt_file(), "usage": (52000, 50)}])
    eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 2
    second = fake.runs[1]["body"]
    assert second["conversation_id"] == "conv-1"
    assert "files" not in second                         # id файла одноразовый, запись не шлём
    assert "черновик" in second["message"] and "без нового распознавания" in second["message"]
    assert fake.runs[0]["key"] != fake.runs[1]["key"]
    assert len(segments) == 3
    assert eng.spent == {"in": 103000, "out": 150, "total": 103150}
    assert fake.deleted == []


def test_nash_srok_na_progon_snyat_i_povtor(monkeypatch, recording):
    """«Искра» молчит дольше ISKRA_RUN_LIMIT — снимаем прогон (DELETE, расход) и просим готовое."""
    monkeypatch.setattr(config, "ISKRA_RUN_LIMIT", 16 * 60)
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[
        {"never": True, "usage": (51000, 0)},
        {"answer": "Вот файл.", "files": txt_file(), "usage": (1000, 10)}])
    eng, segments = run(monkeypatch, fake, recording)
    assert fake.deleted == ["run-1"]
    assert len(fake.runs) == 2 and "черновик" in fake.runs[1]["body"]["message"]
    assert len(segments) == 3
    assert eng.spent["in"] == 52000


def test_srok_i_na_povtornom_progone_oshibka(monkeypatch, recording):
    fake = FakeIskra(script=[{"status": "failed", "error": {"code": "interrupted"}}])
    eng, err = failing(monkeypatch, fake, recording)
    assert len(fake.runs) == 2                            # повтор только один
    assert "15 минут" in str(err.value) and "повторного" in str(err.value)
    assert eng.spent["total"] == 30


def test_obshchiy_srok_snimaet_progon(monkeypatch, recording):
    monkeypatch.setattr(config, "ISKRA_DEADLINE", 5 * 60)
    fake = FakeIskra(script=[{"never": True}])
    eng, err = failing(monkeypatch, fake, recording)
    assert "не закончила" in str(err.value)
    assert fake.deleted == ["run-1"] and len(fake.runs) == 1
    assert eng.spent["total"] == 15                       # расход снятого прогона забран


def test_soobshchenie_o_fayle_bez_fayla_povtor(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, script=[
        {"answer": "Расшифровка сохранена в файл в черновиках разговора."},
        {"answer": "Вот он.", "files": txt_file()}])
    _eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 2 and "files" not in fake.runs[1]["body"]
    assert len(segments) == 3


def test_soobshchenie_o_fayle_i_posle_povtora_bez_fayla(monkeypatch, recording):
    fake = FakeIskra(script=[{"answer": "Сохранила расшифровку в черновики как TXT."}])
    _eng, err = failing(monkeypatch, fake, recording)
    assert str(err.value) == iskra_engine.NO_FILE and len(fake.runs) == 2


def test_povtor_gotovoy_rasshifrovki_net(monkeypatch, recording):
    """На просьбу отдать готовое «Искра» отвечает «готовой расшифровки нет» — это не текст записи."""
    fake = FakeIskra(script=[{"status": "failed", "error": {"code": "interrupted"}},
                             {"answer": "Готовой расшифровки этой записи в черновиках нет."}])
    eng, err = failing(monkeypatch, fake, recording)
    assert str(err.value) == iskra_engine.NO_READY
    assert len(fake.runs) == 2 and eng.spent["total"] == 30


# ── отказы ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    "К сожалению, я не могу прослушать аудиофайл.",
    "К сожалению, инструмент транскрибации выключен администратором.",
    "Транскрибация аудио и видео отключена администратором рабочего пространства.",
    # без «администратора» и без отказного начала (ревью RC-2026-10-05-17, К-2)
    "Транскрибация отключена.",
    "Инструмент транскрибации недоступен.",
    "Транскрибация аудио отключена в настройках рабочего пространства.",
    "Расшифровка аудио сейчас невозможна.",
    "Для этой записи нет инструмента транскрибации.",
    "Распознавание речи не поддерживается для этого файла.\n[КОНЕЦ]",
    "Transcription of audio is disabled.",
])
def test_otkaz_iskry_ne_vydaetsya_za_tekst(monkeypatch, recording, answer):
    fake = FakeIskra(parts=[answer])
    eng, err = failing(monkeypatch, fake, recording)
    # в тексте ошибки — только код ситуации, ни слова из ответа (ревью RC-17, Н-2)
    assert str(err.value) == iskra_engine.NO_TRANSCRIPT
    assert "аудио" not in str(err.value) and "администратор" not in str(err.value)
    assert len(fake.runs) == 1 and eng.spent["total"] == 15


@pytest.mark.parametrize("speech", [
    "Говорящий 1: Я не могу прийти завтра, давайте перенесём.",
    "Я не могу прийти завтра, извините. Не удалось договориться о сроках.",
    "Свет отключили, работать сегодня невозможно.",
    "[00:00] Говорящий 1: Транскрибация отключена, обсудим это позже.",
])
def test_korotkaya_rech_s_ne_mogu_rasshifrovyvaetsya(monkeypatch, recording, speech):
    """Короткая речь без таймкодов со словами «не могу»/«не удалось» — это запись, а не отказ."""
    _eng, segments = run(monkeypatch, FakeIskra(parts=[speech + "\n[КОНЕЦ]"]), recording)
    words = speech.split("Говорящий 1: ")[-1].split(",")[0]
    assert segments and words in " ".join(s.text for s in segments)


def test_interaction_required_snimaetsya(monkeypatch, recording):
    fake = FakeIskra(script=[{"status": "interaction_required", "usage": (7, 1)}])
    eng, err = failing(monkeypatch, fake, recording)
    assert "уточнение" in str(err.value)
    assert fake.deleted == ["run-1"] and eng.spent["total"] == 8


# ── отмена, 429, ошибки, расход ──────────────────────────────────────────────

def test_otmena_vo_vremya_progona_delete(monkeypatch, recording):
    flag = {"stop": False}
    fake = FakeIskra(script=[{"never": True}], on_run=lambda n: flag.update(stop=True))
    eng, _err = failing(monkeypatch, fake, recording, error=engine.Cancelled,
                        cancelled=lambda: flag["stop"])
    assert fake.deleted == ["run-1"]                      # прогон снят
    assert eng.spent["total"] == 15                       # и его расход забран


def test_otmena_rashod_iz_poslednego_sostoyaniya(monkeypatch, recording):
    """DELETE без расхода — берём из последнего увиденного состояния/повторного опроса."""
    state = {"polls": 0}

    def cancelled():
        return state["polls"] >= 2

    fake = FakeIskra(script=[{"never": True, "running_usage": (3, 1)}], delete_usage=False)
    original = fake.__call__

    def counting(request, timeout):
        if "/api/v1/runs/run-1" in request.full_url and request.get_method() == "GET":
            state["polls"] += 1
        return original(request, timeout)

    eng, _err = failing(monkeypatch, counting, recording, error=engine.Cancelled, cancelled=cancelled)
    assert fake.deleted == ["run-1"]
    assert eng.spent == {"in": 3, "out": 1, "total": 4}   # один раз, не за каждый опрос


def test_otmena_vo_vremya_zagruzki(monkeypatch, recording):
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2                         # передумали посреди загрузки файла

    fake = FakeIskra()
    _eng, _err = failing(monkeypatch, fake, recording, error=engine.Cancelled, cancelled=cancelled)
    assert fake.runs == []


def test_429_na_oprose_zhdyom(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, poll_statuses=[429, 429])
    clock = fast(monkeypatch)
    monkeypatch.setattr(iskra_engine, "_urlopen", fake)
    eng = engine.get_engine("iskra")
    t0 = clock.t
    segments = eng.transcribe(recording, lambda: False)
    assert len(segments) == 3
    assert len(fake.polls) == 4                           # два 429, running, completed
    assert clock.t - t0 >= 2 * iskra_engine.RATE_PAUSE    # после 429 ждали дольше


def test_429_i_409_na_zapuske_tot_zhe_klyuch(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, run_post_statuses=[429, 409, 202])
    _eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 3 and len({r["key"] for r in fake.runs}) == 1
    assert len(segments) == 3


def test_rashod_pri_oshibke_progona(monkeypatch, recording):
    fake = FakeIskra(script=[{"status": "failed", "error": {"code": "internal", "message": "перегрузка"}}])
    eng, err = failing(monkeypatch, fake, recording)
    assert "перегрузка" in str(err.value)
    assert eng.spent["total"] == 15 and len(fake.runs) == 1 and fake.deleted == []


def test_sboi_oprosa_terpim_potom_oshibka_i_delete(monkeypatch, recording):
    fake = FakeIskra(script=[{"never": True}], poll_statuses=[502, 503, 504])
    eng, err = failing(monkeypatch, fake, recording)
    assert "не отвечает" in str(err.value)
    assert fake.deleted == ["run-1"] and eng.spent["total"] == 15


def test_slishkom_bolshoy_fayl_rezultata(monkeypatch, recording):
    monkeypatch.setattr(config, "ISKRA_RESULT_MAX_BYTES", 1024 * 1024)
    fake = FakeIskra(file_text="[00:00] Говорящий 1: текст.\n" * 40000)
    eng, err = failing(monkeypatch, fake, recording)
    assert "больше 1 МБ" in str(err.value)
    assert "текст." not in str(err.value)
    assert eng.spent["total"] == 15


def test_obryv_skachivaniya_polovina_fayla_ne_zapis(monkeypatch, recording):
    """Ревью RC-2026-10-05-20, Н-1: сервер объявил длину и оборвал соединение — http.client отдаёт пустые байты,
    а не IncompleteRead; половина файла не должна сохраниться как полная расшифровка."""
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, file_headers={"Content-Length": str(len(TRANSCRIPT_TXT.encode("utf-8")) * 2)})
    _eng, err = failing(monkeypatch, fake, recording)
    assert "обрыв связи" in str(err.value) and "байт из" in str(err.value)
    assert "Говорящий" not in str(err.value)


def test_slishkom_bolshoy_fayl_po_zagolovku(monkeypatch, recording):
    monkeypatch.setattr(config, "ISKRA_RESULT_MAX_BYTES", 1024 * 1024)
    fake = FakeIskra(file_text="коротко", file_headers={"Content-Length": "999999999"})
    _eng, err = failing(monkeypatch, fake, recording)
    assert "больше" in str(err.value)


@pytest.mark.parametrize("code,error,word", [(404, engine.TranscribeError, "не найден"),
                                             (403, engine.TranscribeUnavailable, "files:read")])
def test_fayl_ne_skachan_ponyatnaya_oshibka(monkeypatch, recording, code, error, word):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, file_status=code)
    _eng, err = failing(monkeypatch, fake, recording, error=error)
    assert word in str(err.value)


def test_413_ponyatnaya_oshibka(monkeypatch, recording):
    _eng, err = failing(monkeypatch, FakeIskra(upload_status=413), recording)
    assert "500 МБ" in str(err.value) and "слишком большой" in str(err.value)


@pytest.mark.parametrize("code", [401, 403])
def test_klyuch_ne_prinyat(monkeypatch, recording, code):
    _eng, err = failing(monkeypatch, FakeIskra(upload_status=code), recording,
                        error=engine.TranscribeUnavailable)
    assert "ключ" in str(err.value).lower()


def test_klyuch_ne_prinyat_na_zapuske(monkeypatch, recording):
    fake = FakeIskra(run_post_statuses=[401])
    failing(monkeypatch, fake, recording, error=engine.TranscribeUnavailable)
    assert len(fake.runs) == 1


def test_taymaut_ponyatnaya_oshibka(monkeypatch, recording):
    def slow(request, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))

    _eng, err = failing(monkeypatch, slow, recording)
    assert "не ответила" in str(err.value)


def test_ne_json_otvet_ne_popadaet_v_oshibku(monkeypatch, recording):
    class Raw(FakeIskra):
        def __call__(self, request, timeout):
            if "/api/v1/runs/" in request.full_url and request.get_method() == "GET":
                return FakeResponse(raw="Говорящий 1: секретная речь без JSON".encode())
            return super().__call__(request, timeout)

    fake = Raw()
    _eng, err = failing(monkeypatch, fake, recording)
    assert "секретная" not in str(err.value)
    assert fake.deleted == ["run-1"]


# ── редиректы при скачивании: ключ только на адрес «Искры» (ревью RC-2026-10-05-17, К-1) ──

def test_redirekt_na_chuzhoy_host_bez_klyucha(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT,
                     file_redirects=["https://bucket.s3.example/f.txt?X-Amz-Signature=abc"])
    _eng, segments = run(monkeypatch, fake, recording)
    assert fake.downloads == [
        {"url": "https://iskra.test" + TXT_URL, "auth": "Bearer test-key"},
        {"url": "https://bucket.s3.example/f.txt?X-Amz-Signature=abc", "auth": None}]
    assert len(segments) == 3                             # и файл при этом скачан


def test_redirekt_na_host_iskry_s_klyuchom(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, file_redirects=["/storage/f-1.txt"])
    _eng, segments = run(monkeypatch, fake, recording)
    assert fake.downloads == [
        {"url": "https://iskra.test" + TXT_URL, "auth": "Bearer test-key"},
        {"url": "https://iskra.test/storage/f-1.txt", "auth": "Bearer test-key"}]
    assert len(segments) == 3


def test_redirekt_s_chuzhogo_obratno_na_iskru_s_klyuchom(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT,
                     file_redirects=["https://cdn.example/a", "https://iskra.test/b"])
    run(monkeypatch, fake, recording)
    assert [d["auth"] for d in fake.downloads] == ["Bearer test-key", None, "Bearer test-key"]


def test_beskonechnye_redirekty_ponyatnaya_oshibka(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, file_redirects=["https://cdn.example/x"] * 20)
    _eng, err = failing(monkeypatch, fake, recording)
    assert "перенаправлений" in str(err.value)
    assert len(fake.downloads) == iskra_engine.MAX_REDIRECTS + 1


def test_redirekt_nastoyashchiy_urllib_klyuch_ne_ukhodit(monkeypatch, recording):
    """Настоящий `_urlopen` и два локальных сервера: «Искра» отвечает 302 на чужой адрес —
    чужой сервер не видит `Authorization`, файл скачивается (так ревьюер поймал утечку)."""
    seen: dict[str, object] = {}

    class Storage(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            body = TRANSCRIPT_TXT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    storage = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Storage)

    class Iskra(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["iskra_auth"] = self.headers.get("Authorization")
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{storage.server_port}/signed?sig=1")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    iskra = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Iskra)
    for server in (storage, iskra):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "127.0.0.1,localhost")
        monkeypatch.setenv("ISKRA_URL", f"http://127.0.0.1:{iskra.server_port}")
        eng = engine.get_engine("iskra")
        text = eng.download({"name": "a.txt", "download_url": "/files/1"}, lambda: False)
    finally:
        iskra.shutdown()
        storage.shutdown()
    assert seen["iskra_auth"] == "Bearer test-key"
    assert seen["auth"] is None
    assert text == TRANSCRIPT_TXT


# ── обрыв посреди ответа (ревью RC-2026-10-05-17, Н-8) ───────────────────────

def test_incompleteread_na_oprose_ponyatnaya_oshibka_rashod_delete(monkeypatch, recording):
    fake = FakeIskra(script=[{"never": True}],
                     poll_raise=[http.client.IncompleteRead(b"x")] * iskra_engine.POLL_FAILURES)
    eng, err = failing(monkeypatch, fake, recording)
    assert "недоступна" in str(err.value) and "IncompleteRead" in str(err.value)
    assert fake.deleted == ["run-1"] and eng.spent["total"] == 15


def test_obryv_na_delete_ne_glushit_iskhodnuyu_oshibku(monkeypatch, recording):
    fake = FakeIskra(script=[{"never": True}], poll_statuses=[400],
                     delete_raise=http.client.IncompleteRead(b""))
    _eng, err = failing(monkeypatch, fake, recording)
    assert "400" in str(err.value)                        # исходная ошибка, а не обрыв DELETE
    assert fake.deleted == ["run-1"]


def test_obryv_pri_skachivanii_ponyatnaya_oshibka(monkeypatch, recording):
    fake = FakeIskra(file_text=TRANSCRIPT_TXT, download_raise=http.client.IncompleteRead(b"12"))
    eng, err = failing(monkeypatch, fake, recording)
    assert "обрыв связи" in str(err.value) and eng.spent["total"] == 15


def test_nepredvidennyy_sboy_stanovitsya_transcribeerror(monkeypatch, recording):
    fake = FakeIskra(script=[{"never": True}], poll_raise=[ValueError("сырое")])
    eng, err = failing(monkeypatch, fake, recording)
    assert "ValueError" in str(err.value) and "сырое" not in str(err.value)
    assert fake.deleted == ["run-1"] and eng.spent["total"] == 15


# ── запасной путь: текст частями по «дальше» ─────────────────────────────────

def test_zapasnoy_put_dve_chasti_skleivayutsya(monkeypatch, recording):
    fake = FakeIskra(parts=[
        "[00:00] Говорящий 1: Добрый день, коллеги.\n[00:42] Говорящий 2: Начнём с первого вопроса.\n"
        "[ПРОДОЛЖЕНИЕ]",
        "[01:30] Говорящий 1: Поручаю подготовить доклад.\n[КОНЕЦ]",
    ])
    eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 2
    assert fake.runs[1]["body"]["message"] == "дальше"
    assert fake.runs[1]["body"]["conversation_id"] == "conv-1"
    assert "files" not in fake.runs[1]["body"]
    assert [s.start for s in segments] == [0, 42, 90]
    assert all("ПРОДОЛЖЕНИЕ" not in s.text and "КОНЕЦ" not in s.text for s in segments)
    assert eng.spent == {"in": 20, "out": 10, "total": 30}
    assert eng.parts == 2 and eng.note == "" and eng.source == "answer"


def test_zapasnoy_put_srok_na_prodolzhenii_ostavlyaet_prishedshee(monkeypatch, recording):
    fake = FakeIskra(script=[{"answer": "[00:00] Говорящий 1: Первая часть.\n[ПРОДОЛЖЕНИЕ]"},
                             {"status": "failed", "error": {"code": "interrupted"}}])
    eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 2 and [s.text for s in segments] == ["Первая часть."]
    assert "не уложилась" in eng.note and eng.spent["total"] == 30


def test_otmena_mezhdu_chastyami(monkeypatch, recording):
    flag = {"stop": False}

    class Stopping(FakeIskra):
        def __call__(self, request, timeout):
            out = super().__call__(request, timeout)
            if self._polled.get("run-1", 0) >= 2:     # первая часть пришла — передумали
                flag["stop"] = True
            return out

    fake = Stopping(parts=["[00:00] Первая часть.\n[ПРОДОЛЖЕНИЕ]", "[01:00] Вторая.\n[КОНЕЦ]"])
    eng, _err = failing(monkeypatch, fake, recording, error=engine.Cancelled,
                        cancelled=lambda: flag["stop"])
    assert len(fake.runs) == 1                        # «дальше» после отмены не отправлено
    assert eng.spent["total"] == 15                   # расход первой части не потерян


def test_predel_chastey(monkeypatch, recording):
    """«Искра» не говорит [КОНЕЦ] — не крутимся вечно: не больше ISKRA_MAX_PARTS прогонов."""
    monkeypatch.setattr(config, "ISKRA_MAX_PARTS", 3)
    fake = FakeIskra(parts=[f"[0{i}:00] Часть {i}, текст {i * 7}.\n[ПРОДОЛЖЕНИЕ]" for i in range(10)])
    eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 3
    assert len(segments) == 3
    assert "не закончила" in eng.note


def test_povtor_toy_zhe_chasti_ostanavlivaet(monkeypatch, recording):
    same = "[00:00] Говорящий 1: Одно и то же.\n[ПРОДОЛЖЕНИЕ]"
    fake = FakeIskra(parts=[same, same, same])
    eng, segments = run(monkeypatch, fake, recording)
    assert len(fake.runs) == 2                        # второй ответ — повтор, дальше не просим
    assert len(segments) == 1                         # и в текст он не попал
    assert "повторила" in eng.note


def test_bez_taymkodov_segmenty_po_abzacam(monkeypatch, recording):
    fake = FakeIskra(parts=["Первый абзац речи.\n\nВторой абзац,\nпродолжение строки.\n[КОНЕЦ]"])
    _eng, segments = run(monkeypatch, fake, recording)
    assert [s.text for s in segments] == ["Первый абзац речи.", "Второй абзац, продолжение строки."]
    assert all(s.start is None for s in segments)
    assert segments[0].to_dict()["start"] is None


def test_otvet_bez_metok_polnyy_no_s_pometkoy(monkeypatch, recording):
    eng, segments = run(monkeypatch, FakeIskra(parts=["[00:00] Говорящий 1: Добрый день."]), recording)
    assert len(segments) == 1
    assert eng.note == iskra_engine.NO_END_NOTE


def test_spisok_i_vstuplenie_snimayutsya(monkeypatch, recording):
    answer = ("Вот расшифровка записи:\n\n"
              "- [00:05] Говорящий 1: Добрый день.\n"
              "- [00:40] Говорящий 2: Здравствуйте, начнём.\n"
              "Если нужно, могу также сделать саммари.\n[КОНЕЦ]")
    _eng, segments = run(monkeypatch, FakeIskra(parts=[answer]), recording)
    assert [s.start for s in segments] == [5, 40]
    assert [s.text for s in segments] == ["Добрый день.", "Здравствуйте, начнём."]


# ── прочее ───────────────────────────────────────────────────────────────────

def test_bez_klyucha_chestnaya_oshibka(monkeypatch):
    monkeypatch.delenv("ISKRA_API_KEY", raising=False)
    monkeypatch.setattr(config, "SERVICE_DIR", Path("/nonexistent-transcriber"))
    with pytest.raises(engine.TranscribeUnavailable) as err:
        engine.get_engine("iskra")
    assert "Ключ «Искры» не настроен" in str(err.value)
    status = engine.engine_status("iskra")
    assert status["engine"] == "iskra" and status["ready"] is False and not status["demo"]


def test_dvizhok_po_umolchaniyu_iskra(monkeypatch):
    monkeypatch.delenv("TRANSCRIBER_ENGINE", raising=False)
    monkeypatch.setattr(config, "_from_file", lambda path, name: "")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.ENGINE == "iskra"
        assert reloaded.MAX_FILE_BYTES == 500 * 1024 * 1024
        assert reloaded.ISKRA_DEADLINE == 40 * 60          # два прогона по 15 минут и запас
        assert reloaded.ISKRA_RESULT_MAX_BYTES == 50 * 1024 * 1024
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_fake_i_whisper_ostayutsya(monkeypatch):
    """`fake` — для тестов, `whisper` — опциональный резерв: не удалены."""
    assert engine.get_engine("fake").name == "fake"
    assert engine.engine_status("fake")["demo"] is True
    assert engine.engine_status("whisper")["engine"] == "whisper"
