"""Проверки контракта с порталом и полного цикла обработки записи.

Без сети, без «Искры» и без установки настоящего движка распознавания — как у соседей
(a3, «Тезисы»): движок и саммари подменяются управляемыми подделками.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import pytest
from unittest.mock import ANY
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import audit, config, db, engine, jobs, main, summarizer  # noqa: E402

SECRET = "stend-secret-1"
ADMIN = {"X-Portal-Secret": SECRET, "X-Portal-User": "admin", "X-Portal-Role": "admin",
         "X-Portal-Name": "%D0%90%D0%B4%D0%BC%D0%B8%D0%BD"}
EMP = {"X-Portal-Secret": SECRET, "X-Portal-User": "emp", "X-Portal-Role": "employee",
       "X-Portal-Name": "emp"}


class ScriptedEngine:
    """Управляемый движок распознавания для тестов: можно задержать, отменить, задать текст."""

    name = "scripted"

    def __init__(self, segments=None, hang=False, boom=None):
        self.segments = segments if segments is not None else [
            engine.Segment(0, 5, "тестовый текст записи про редкое слово кварц")]
        self.hang = hang       # крутиться, пока не отменят
        self.boom = boom       # бросить это исключение сразу

    def transcribe(self, path, cancelled):
        if self.boom is not None:
            raise self.boom
        if self.hang:
            while not cancelled():
                time.sleep(0.01)
            raise engine.Cancelled("Распознавание отменено")
        if cancelled():
            raise engine.Cancelled("Распознавание отменено")
        return self.segments


def fake_summary(text, cancelled=None, tokens=100):
    return text, {"in": tokens, "out": 0, "total": tokens}


ORDER = {"text": "Минпросвещения России (С.С. Кравцову) подготовить доклад о ходе работ",
         "assignee": "С.С. Кравцов", "deadline": "25.06.2026"}


def fake_orders(text, cancelled=None, tokens=25):
    return [dict(ORDER)], {"in": 20, "out": 5, "total": tokens}


def hanging_summary(text, cancelled=None):
    while not (cancelled and cancelled()):
        time.sleep(0.01)
    raise summarizer.Cancelled("Саммари отменено", {"in": 50, "out": 0, "total": 50})


def failing_summary(text, cancelled=None):
    raise summarizer.SummaryError("«Искра» прервала саммари: перегрузка",
                                  {"in": 300, "out": 10, "total": 310})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "transcriber.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PORTAL_SECRET", SECRET)
    monkeypatch.setattr(config, "DEV", False)
    monkeypatch.setattr(main.engine_mod, "get_engine", lambda name="": ScriptedEngine())
    monkeypatch.setattr(main.summarizer, "summarize", fake_summary)
    monkeypatch.setattr(main.orders_mod, "extract", fake_orders)
    audit.sent.clear()
    jobs.reset()
    db.init()
    with TestClient(main.app) as c:
        yield c
    jobs.reset()


def upload(client, headers=EMP, filename="record.mp3", content=b"0" * 4096):
    return client.post("/api/records", headers=headers,
                       files={"file": (filename, io.BytesIO(content), "audio/mpeg")})


def done(client, response, headers=EMP, timeout=10.0):
    assert response.status_code == 202, response.text
    job = response.json()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("фоновая обработка не закончилась")


# ── контракт с порталом ─────────────────────────────────────────────────────

def test_healthz_bez_vhoda(client):
    assert client.get("/healthz").status_code == 200


def test_bez_sekreta_otkaz(client):
    assert client.get("/api/records").status_code == 403
    assert client.get("/api/records", headers={**ADMIN, "X-Portal-Secret": "wrong"}).status_code == 403
    assert upload(client, headers={**EMP, "X-Portal-Secret": "wrong"}).status_code == 403


def test_vnutrennih_adresov_net(client):
    """У сервиса нет собственных /internal-адресов — работа только через /api и портал."""
    assert client.get("/internal/records", headers=EMP).status_code == 404


def test_chuzhoy_origin_otklonyaetsya(client):
    response = upload(client, headers={**EMP, "Origin": "https://evil.example"})
    assert response.status_code == 403
    ok = upload(client, headers={**EMP, "Origin": "http://testserver", "Host": "testserver"})
    assert ok.status_code == 202


def test_imya_iz_zagolovka_dekodiruetsya(client):
    job = done(client, upload(client, headers=ADMIN))
    assert job["record"]["created_name"] == "Админ"


def test_imya_polzovatelya_ekraniruetsya(client):
    headers = dict(ADMIN)
    headers["X-Portal-Name"] = '%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E'
    page = client.get("/", headers=headers).text
    assert "<img src=x" not in page


# ── загрузка и полный цикл: распознавание → саммари ─────────────────────────

def test_povtor_odnoy_otpravki_ne_zavodit_dubl(client):
    """Двойной клик или повтор браузера с тем же `client_key` — та же запись, не вторая."""
    form = {"client_key": "abcdefgh12345678"}
    files = {"file": ("record.mp3", io.BytesIO(b"0" * 4096), "audio/mpeg")}
    first = client.post("/api/records", headers=EMP, data=form, files=files)
    assert first.status_code == 202
    files = {"file": ("record.mp3", io.BytesIO(b"0" * 4096), "audio/mpeg")}
    second = client.post("/api/records", headers=EMP, data=form, files=files)
    assert second.status_code == 202
    assert first.json()["record"]["id"] == second.json()["record"]["id"]
    done(client, first)
    assert len(client.get("/api/records", headers=EMP).json()["records"]) == 1


def test_zagruzka_neizvestnogo_formata_otklonyaetsya(client):
    response = client.post("/api/records", headers=EMP,
                           files={"file": ("заметка.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})
    assert response.status_code == 400


def test_pustoy_fayl_otklonyaetsya(client):
    response = client.post("/api/records", headers=EMP,
                           files={"file": ("record.mp3", io.BytesIO(b""), "audio/mpeg")})
    assert response.status_code == 400


def test_polny_cikl_raspoznavanie_i_summari(client):
    """Долгая обработка не держит соединение: ответ — сразу номер задания."""
    started = upload(client, filename="совещание.mp3")
    assert started.status_code == 202
    job = started.json()
    assert job["status"] in ("queued", "running") and job["id"]
    finished = done(client, started)
    assert finished["status"] == "done"
    record = client.get(f"/api/records/{finished['record']['id']}", headers=EMP).json()
    assert record["state"] == "done"
    assert "кварц" in record["draft_text"]
    assert record["summary"]              # саммари подмены — тот же текст
    # третий документ — перечень поручений; html для блока карточки выводится из текста (13.10)
    # у поручения постоянный id (ревью RC-2026-10-13-2, К-1)
    assert record["orders"] == [{**ORDER, "html": f"<p>{ORDER['text']}</p>", "id": ANY}]
    assert record["tokens"] == 125        # саммари 100 + поручения 25
    assert record["engine"] == "scripted"


def test_dolgaya_obrabotka_ne_derzhit_soedinenie(client, monkeypatch):
    go = threading.Event()

    class SlowEngine(ScriptedEngine):
        def transcribe(self, path, cancelled):
            go.wait(5)
            return super().transcribe(path, cancelled)

    monkeypatch.setattr(main.engine_mod, "get_engine", lambda name="": SlowEngine())
    started = upload(client)
    assert started.status_code == 202
    job = started.json()
    assert job["status"] in ("queued", "running")
    # список записей отвечает, пока «Искра» и распознавание ещё работают
    assert client.get("/api/records", headers=EMP).status_code == 200
    go.set()
    assert done(client, started)["status"] == "done"


def test_pustaya_rech_bez_summari(client, monkeypatch):
    """Ничего не распознано — саммари не запрашиваем: нечего суммировать, не тратим токены."""
    calls = []
    monkeypatch.setattr(main.engine_mod, "get_engine",
                        lambda name="": ScriptedEngine(segments=[]))
    monkeypatch.setattr(main.summarizer, "summarize",
                        lambda *a, **k: calls.append(1) or fake_summary(*a, **k))
    finished = done(client, upload(client))
    assert finished["status"] == "done"
    record = finished["record"]
    assert record["tokens"] == 0
    assert record["summary"] == ""
    assert calls == []


# ── отмена ───────────────────────────────────────────────────────────────────

def test_otmena_vo_vremya_raspoznavaniya(client, monkeypatch):
    monkeypatch.setattr(main.engine_mod, "get_engine", lambda name="": ScriptedEngine(hang=True))
    started = upload(client)
    job_id = started.json()["id"]
    # дождаться, пока задание реально стартует
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/jobs/{job_id}", headers=EMP).json()["status"] == "running":
            break
        time.sleep(0.02)
    cancelled = client.post(f"/api/jobs/{job_id}/cancel", headers=EMP)
    assert cancelled.status_code == 200
    finished = done(client, started)
    assert finished["status"] == "cancelled"


def test_otmena_vo_vremya_summari_uchityvaet_tokeny(client, monkeypatch):
    monkeypatch.setattr(main.summarizer, "summarize", hanging_summary)
    started = upload(client)
    job_id = started.json()["id"]

    def stage_reached() -> bool:
        record_id = client.get(f"/api/jobs/{job_id}", headers=EMP).json().get("record_id")
        if not record_id:
            return False
        return client.get(f"/api/records/{record_id}", headers=EMP).json()["state"] == "summarizing"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not stage_reached():
        time.sleep(0.02)
    client.post(f"/api/jobs/{job_id}/cancel", headers=EMP)
    finished = done(client, started)
    assert finished["status"] == "cancelled"
    record = client.get(f"/api/records/{finished['record_id']}", headers=EMP).json()
    assert record["tokens"] == 50          # отменённое саммари тоже стоило токенов
    assert record["state"] == "cancelled"


# ── неудачи и расход токенов «Искры» ─────────────────────────────────────────

def test_neudachnyy_summari_tozhe_schitaet_tokeny(client, monkeypatch):
    monkeypatch.setattr(main.summarizer, "summarize", failing_summary)
    finished = done(client, upload(client))
    assert finished["status"] == "error"
    audit.sent  # noqa: B018 — для наглядности в отладке
    record = client.get(f"/api/records/{finished['record_id']}", headers=EMP).json()
    assert record["tokens"] == 310
    assert record["state"] == "error"
    last = [r for r in audit.sent if r["service"] == "transcriber"][-1]
    assert (last["tokens_in"], last["tokens_out"]) == (300, 10)


def test_dvizhok_ne_nastroen_ponyatnaya_oshibka(client, monkeypatch):
    monkeypatch.setattr(main.engine_mod, "get_engine",
                        lambda name="": (_ for _ in ()).throw(
                            engine.TranscribeUnavailable("нужно установить faster-whisper")))
    finished = done(client, upload(client))
    assert finished["status"] == "error"
    assert "faster-whisper" in finished["message"]


def test_dvizhok_padaet_s_oshibkoy(client, monkeypatch):
    monkeypatch.setattr(main.engine_mod, "get_engine",
                        lambda name="": ScriptedEngine(boom=engine.TranscribeError("битый файл")))
    finished = done(client, upload(client))
    assert finished["status"] == "error"
    assert "битый файл" in finished["message"]


# ── очередь ──────────────────────────────────────────────────────────────────

def test_ochered_zapolnena_daet_429(client, monkeypatch):
    monkeypatch.setattr(main.engine_mod, "get_engine", lambda name="": ScriptedEngine(hang=True))
    first = upload(client)
    assert first.status_code == 202
    for _ in range(jobs.MAX_WAITING):
        assert upload(client).status_code == 202
    over = upload(client)
    assert over.status_code == 429


def test_perezapusk_ne_ostavlyaet_zavisshuyu_zapis(client):
    """Задания живут только в памяти — после перезапуска ни одна запись не должна
    вечно висеть «идёт обработка» (DEV-CHECKLIST, п. 3)."""
    record = db.create_record({"login": "emp", "name": "emp"}, "запись")
    db.set_state(record["id"], "transcribing", "идёт распознавание речи")
    assert db.interrupt_stale() == 1
    stuck = client.get(f"/api/records/{record['id']}", headers=EMP).json()
    assert stuck["state"] == "error"
    assert "перезапуск" in stuck["state_note"].lower()


def test_perezapusk_chistit_zabroshennye_fayly(tmp_path, monkeypatch):
    from app import media
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    config.UPLOAD_DIR.mkdir(parents=True)
    leftover = config.UPLOAD_DIR / "orphan.mp3"
    leftover.write_bytes(b"0")
    media.clear_upload_dir()
    assert not leftover.exists()


def test_neizvestnoe_zadanie_ponyatnaya_oshibka(client):
    response = client.get("/api/jobs/net-takogo", headers=EMP)
    assert response.status_code == 404
    assert "перезапуск" in response.json()["message"]


# ── приватность журнала ──────────────────────────────────────────────────────

def test_zhurnal_ne_soderzhit_teksta_zapisi(client):
    """В журнал уходят факты (длительность, число знаков, токены), но не сама речь."""
    finished = done(client, upload(client))
    assert finished["status"] == "done"
    for record_ in audit.sent:
        if record_["service"] != "transcriber":
            continue
        assert "кварц" not in record_["details"]
        assert "тестовый текст записи" not in record_["details"]
    actions = [r["action"] for r in audit.sent if r["service"] == "transcriber"]
    assert "загружена запись" in actions
    assert "запись расшифрована" in actions
    assert "саммари записи готово" in actions
    assert "перечень поручений записи готов" in actions


def test_zhurnal_pishet_dlinu_i_tokeny(client):
    """Каждый прогон «Искры» — своё событие со своим расходом (DEV-CHECKLIST, п. 4)."""
    finished = done(client, upload(client))
    sent = [r for r in audit.sent if r["service"] == "transcriber"]
    summary = [r for r in sent if r["action"] == "саммари записи готово"]
    orders = [r for r in sent if r["action"] == "перечень поручений записи готов"]
    assert len(summary) == 1 and len(orders) == 1
    assert "знаков транскрипции" in summary[0]["details"]
    assert (summary[0]["tokens_in"], summary[0]["tokens_out"]) == (100, 0)
    assert (orders[0]["tokens_in"], orders[0]["tokens_out"]) == (20, 5)


# ── правка черновика ──────────────────────────────────────────────────────────

def test_pravka_chernovika_trebuet_versiyu(client):
    record_id = done(client, upload(client))["record"]["id"]
    response = client.put(f"/api/records/{record_id}/text", json={"text": "правка"}, headers=EMP)
    assert response.status_code == 400


def test_pravka_chernovika_lovit_odnovremennuyu_pravku(client):
    record_id = done(client, upload(client))["record"]["id"]
    record = client.get(f"/api/records/{record_id}", headers=EMP).json()
    ok = client.put(f"/api/records/{record_id}/text",
                    json={"text": "первая правка", "version": record["draft_version"]}, headers=EMP)
    assert ok.status_code == 200
    late = client.put(f"/api/records/{record_id}/text",
                      json={"text": "вторая правка", "version": record["draft_version"]}, headers=EMP)
    assert late.status_code == 409


def test_pravka_chernovika_sohranyaetsya(client):
    record_id = done(client, upload(client))["record"]["id"]
    record = client.get(f"/api/records/{record_id}", headers=EMP).json()
    saved = client.put(f"/api/records/{record_id}/text",
                       json={"text": "исправленный текст записи", "version": record["draft_version"]},
                       headers=EMP).json()
    assert saved["draft_text"] == "исправленный текст записи"
    assert saved["draft_version"] == record["draft_version"] + 1


# ── архив и удаление ──────────────────────────────────────────────────────────

def test_arhiv_i_udalenie_tolko_adminu(client):
    record_id = done(client, upload(client))["record"]["id"]
    archived = client.post(f"/api/records/{record_id}/archive", headers=EMP).json()
    assert archived["archived"] is True
    assert client.get("/api/records", headers=EMP).json()["records"] == []
    assert client.get("/api/records?archived=true", headers=EMP).json()["records"][0]["id"] == record_id
    assert client.delete(f"/api/records/{record_id}", headers=EMP).status_code == 403
    assert client.delete(f"/api/records/{record_id}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/records/{record_id}", headers=EMP).status_code == 404


# ── поиск и скачивание ──────────────────────────────────────────────────────

def test_poisk_po_nazvaniyu(client):
    done(client, upload(client, filename="Совещание по кварцу.mp3"))
    found = client.get("/api/search?q=кварцу", headers=EMP).json()["items"]
    assert len(found) == 1
    assert found[0]["url"].startswith("/transcriber/#record=")


def test_poisk_ne_ishchet_po_soderzhaniyu(client):
    """Приватность: искать по тексту расшифровки/саммари нельзя, только по названию."""
    done(client, upload(client, filename="запись.mp3"))
    assert client.get("/api/search?q=кварц", headers=EMP).json()["items"] == []


def test_skachivanie_docx_i_txt(client):
    record_id = done(client, upload(client))["record"]["id"]
    docx = client.get(f"/api/records/{record_id}/export/transcript.docx", headers=EMP)
    assert docx.status_code == 200
    assert "attachment" in docx.headers["content-disposition"]
    txt = client.get(f"/api/records/{record_id}/export/transcript.txt", headers=EMP)
    assert txt.status_code == 200
    assert "кварц" in txt.text


# ── статика и типографика ────────────────────────────────────────────────────

def test_stili_i_skripty_sveryayutsya_posle_vykladki(client):
    for name in ("style.css", "app.js"):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_tipografika_obshchaya(client):
    css = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")
    body = css.split("body {", 1)[1].split("}", 1)[0]
    assert "font-family" not in body and "font-size" not in body
    assert "var(--text-small)" in css
    html = client.get("/", headers=EMP).text
    assert '<link rel="stylesheet" href="/static/base.css">' in html


def test_shapka_servisa_est(client):
    html = client.get("/", headers=EMP).text
    assert 'class="topbar"' in html and "Транскрибатор" in html


# ── движок ───────────────────────────────────────────────────────────────────

def test_api_engine_status(client):
    info = client.get("/api/engine", headers=EMP).json()
    assert "engine" in info and "ready" in info


def test_fake_engine_deterministic_i_otmenyaemyy():
    """Настоящая подменная реализация (не ScriptedEngine теста): даёт текст и уважает отмену."""
    from app.engine import fake

    tmp = Path(__file__).resolve().parent / "_fixture.tmp"
    tmp.write_bytes(b"0" * (32 * 1024))
    try:
        segments = fake.engine.transcribe(tmp, lambda: False)
        assert segments and all(isinstance(s, engine.Segment) for s in segments)
        with pytest.raises(engine.Cancelled):
            fake.engine.transcribe(tmp, lambda: True)
    finally:
        tmp.unlink(missing_ok=True)


def test_whisper_model_iz_env_ne_zahardkozhen(monkeypatch):
    """`TRANSCRIBER_WHISPER_MODEL` меняет модель — раньше `small` был захардкожен."""
    import importlib

    monkeypatch.setenv("TRANSCRIBER_WHISPER_MODEL", "medium")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.WHISPER_MODEL == "medium"
    finally:
        monkeypatch.delenv("TRANSCRIBER_WHISPER_MODEL", raising=False)
        importlib.reload(config)


def test_whisper_model_po_umolchaniyu_small(monkeypatch):
    monkeypatch.delenv("TRANSCRIBER_WHISPER_MODEL", raising=False)
    import importlib
    reloaded = importlib.reload(config)
    assert reloaded.WHISPER_MODEL == "small"


def test_dvizhok_whisper_bez_biblioteki_ponyatnaya_oshibka(monkeypatch):
    """Реальный (не подменённый в тесте) диспетчер: без `faster-whisper` — честная ошибка."""
    from app import speech_common

    monkeypatch.setattr(speech_common, "AVAILABLE", False)
    with pytest.raises(engine.TranscribeUnavailable):
        engine.get_engine("whisper")


def test_get_engine_keshiruet_model_po_razmeru(monkeypatch):
    """Одна и та же модель не поднимается заново на каждую запись (загрузка медленная)."""
    from app import speech_common

    monkeypatch.setattr(speech_common, "AVAILABLE", True)
    monkeypatch.setattr(speech_common, "_whisper_instances", {})
    monkeypatch.setattr(config, "WHISPER_MODEL", "small")

    first = engine.get_engine("whisper")
    second = engine.get_engine("whisper")
    assert first is second

    monkeypatch.setattr(config, "WHISPER_MODEL", "medium")
    third = engine.get_engine("whisper")
    assert third is not first
    assert third._model_size == "medium"
