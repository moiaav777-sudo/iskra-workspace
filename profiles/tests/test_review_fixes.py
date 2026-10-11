"""Замечания ревью RC-2026-10-05-2 и типовые файлы на входе (решение пользователя 05.10)."""
from __future__ import annotations

import io
import json
import threading
import time
import zipfile

import pytest

from app import docs_common, jobs
from tests.conftest import answer_json, docx, png, wait_done
from tests.test_docs_common import DOC, broken_deflate, needs_soffice, soffice_convert, xlsx


# ── Н-1: битый поток deflate ────────────────────────────────────────────

def test_справка_с_битым_deflate_понятная_400(client, emp):
    bad = broken_deflate(docx(["Кравцов Сергей Сергеевич", "министр"], image=png(10, 12)))
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("справка.docx", bad, "application/octet-stream")})
    assert r.status_code == 400 and "повреждён" in r.json()["detail"]
    assert client.get("/api/profiles", headers=emp, params={"scope": "all"}).json()["items"] == []


def test_пакет_с_битым_файлом_без_сирот_и_пустого_пакета(client, emp, fake_iskra):
    bad = broken_deflate(docx(["Кравцов Сергей Сергеевич", "министр"]))
    files = [("files", ("good.txt", "Иванова Мария Петровна, директор департамента".encode(), "text/plain")),
             ("files", ("bad.docx", bad, "application/octet-stream")),
             ("files", ("good2.txt", "Петров Олег Игоревич, заместитель министра".encode(), "text/plain"))]
    r = client.post("/api/batches", headers=emp, files=files, data={"key": "brokenkey01"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert [i["file"] for i in b["items"]] == ["good.txt", "bad.docx", "good2.txt"]
    assert "повреждён" in b["items"][1]["error"] and b["items"][1]["profile"] is None
    jobs.drain(10)
    profiles = client.get("/api/profiles", headers=emp, params={"scope": "all"}).json()["items"]
    assert len(profiles) == 2 and all(p["batch"] == b["id"] for p in profiles)       # сирот нет
    again = client.post("/api/batches", headers=emp, files=files, data={"key": "brokenkey01"}).json()
    assert again["id"] == b["id"] and len(again["items"]) == 3                         # не пустой старый пакет
    assert len(client.get("/api/profiles", headers=emp, params={"scope": "all"}).json()["items"]) == 2


def test_пакет_неожиданный_сбой_чтения_одного_файла(client, emp, fake_iskra, monkeypatch):
    from app import extract
    real = extract.read_source

    def flaky(name, raw):
        if name == "boom.txt":
            raise MemoryError("нет памяти")
        return real(name, raw)
    monkeypatch.setattr(extract, "read_source", flaky)
    files = [("files", ("ok.txt", "Иванова Мария Петровна, директор".encode(), "text/plain")),
             ("files", ("boom.txt", "Петров Олег Игоревич".encode(), "text/plain"))]
    b = client.post("/api/batches", headers=emp, files=files).json()
    assert b["items"][1]["error"].startswith("Файл «boom.txt» не прочитан") and b["items"][0]["profile"]
    jobs.drain(10)


# ── Н-2: блокирующая работа вне событийного цикла ───────────────────────

def _healthz_while(client, action) -> tuple[float, object]:
    """Запустить действие в потоке и померить, как быстро отвечает /healthz, пока оно идёт."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", action()))
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    assert client.get("/healthz").status_code == 200
    took = time.monotonic() - started
    t.join(30)
    return took, out.get("r")


def test_медленная_сверка_фио_не_вешает_сервис(client, emp, monkeypatch):
    from app import portal
    pids = []
    for fio in ("Кравцов Сергей Сергеевич", "Иванова Мария Петровна"):
        pid = client.post("/api/profiles", json={"fio": fio}, headers=emp).json()["id"]
        wait_done(client, emp, pid)
        pids.append(pid)
    calls = []

    def slow(names):
        calls.append(list(names))
        time.sleep(2.0)
        return {n: {"status": "exact"} for n in names}
    monkeypatch.setattr(portal, "check_people", slow)
    order = json.dumps([{"kind": "profile", "id": p} for p in pids])
    took, r = _healthz_while(client, lambda: client.post("/api/merge", headers=emp, data={"order": order}))
    assert r.status_code == 200, r.text
    assert took < 1.0, f"/healthz ждал {took:.1f} с, пока шло объединение"
    assert len(calls) == 1 and len(calls[0]) == 2          # сверка объединения — одним запросом


def test_медленное_чтение_справки_не_вешает_сервис(client, emp, monkeypatch):
    from app import extract
    real = extract.read_source

    def slow(name, raw):
        time.sleep(2.0)
        return real(name, raw)
    monkeypatch.setattr(extract, "read_source", slow)
    took, r = _healthz_while(client, lambda: client.post(
        "/api/profiles/from-doc", headers=emp, files={"file": ("a.txt", "Кравцов Сергей Сергеевич, министр".encode(), "text/plain")}))
    assert r.status_code == 200 and took < 1.0
    jobs.drain(10)


def test_сверка_с_таймаутом():
    from app import portal
    assert portal.CHECK_TIMEOUT <= 5


# ── Н-4: ФИО карточки не по образцу ─────────────────────────────────────

@pytest.mark.parametrize("fio", ["John Smith", "Иванов И. И. (род. 1970)"])
def test_объединение_карточки_с_фио_не_по_образцу(client, emp, fake_iskra, fio):
    fake_iskra.answers = [answer_json(fio=fio), answer_json(fio="Кравцов Сергей Сергеевич")]
    a = client.post("/api/profiles", json={"fio": fio}, headers=emp).json()["id"]
    b = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, a), wait_done(client, emp, b)
    order = json.dumps([{"kind": "profile", "id": a}, {"kind": "profile", "id": b}])
    r = client.post("/api/merge", headers=emp, data={"order": order})
    assert r.status_code == 200, r.text
    assert [it["title"] for it in r.json()["merge"]["items"]] == [fio, "Кравцов Сергей Сергеевич"]
    data = client.get(f"/api/merges/{r.json()['merge']['id']}/docx", headers=emp).content
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    assert "1. " + fio.replace("(", "(") in xml


def test_чужой_файл_в_ошибке_называется_своим_именем(client, emp):
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    order = json.dumps([{"kind": "profile", "id": pid}, {"kind": "file", "index": 0}])
    r = client.post("/api/merge", headers=emp, data={"order": order},
                    files=[("files", ("отчёт.docx", docx(["Отчёт о работе", "текст"]), "application/octet-stream"))])
    assert r.status_code == 400 and r.json()["detail"].startswith("«отчёт.docx» не похож на объективку")


# ── Н-11: архивная объективка ───────────────────────────────────────────

def test_архивную_не_собрать_и_не_сменить_фото(client, emp):
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    client.delete(f"/api/profiles/{pid}", headers=emp)
    assert client.post(f"/api/profiles/{pid}/build", headers=emp).status_code == 409
    assert client.put(f"/api/profiles/{pid}/photo", headers=emp, files={"file": ("a.png", png(5, 5), "image/png")}).status_code == 409
    assert client.get(f"/api/profiles/{pid}/docx?package=1", headers=emp).status_code == 409
    assert client.get(f"/api/profiles/{pid}/docx", headers=emp).status_code == 200      # скачать можно


# ── типовые файлы на входе ──────────────────────────────────────────────

def test_поля_загрузки_принимают_типовые_файлы(client, emp):
    me = client.get("/api/me", headers=emp).json()
    assert me["accept"]["source"] == docs_common.ACCEPT
    assert me["accept"]["merge"] == ".docx,.doc,.rtf,.odt"


@pytest.mark.parametrize("name,make", [
    ("кадры.xlsx", lambda tmp: xlsx({"Лист1": [["ФИО", "Кравцов Сергей Сергеевич"], ["Должность", "министр"]]})),
    ("bio.md", lambda tmp: "# Кравцов Сергей Сергеевич\n\nминистр".encode()),
    ("bio.csv", lambda tmp: "ФИО;Должность\nКравцов Сергей Сергеевич;министр".encode()),
])
def test_по_справке_из_разных_форматов(client, emp, fake_iskra, tmp_path, name, make):
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": (name, make(tmp_path), "application/octet-stream")})
    assert r.status_code == 200, r.text
    wait_done(client, emp, r.json()["id"])
    assert "Кравцов Сергей Сергеевич" in fake_iskra.posts()[-1]["message"]


@needs_soffice
def test_по_справке_doc_с_фото_через_libreoffice(client, emp, fake_iskra, tmp_path):
    doc = soffice_convert(docx(["Кравцов Сергей Сергеевич", "министр просвещения"], image=png(30, 40)), "docx", "doc", tmp_path)
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("справка.doc", doc, "application/msword")})
    assert r.status_code == 200, r.text
    p = wait_done(client, emp, r.json()["id"])
    assert "министр просвещения" in fake_iskra.posts()[-1]["message"]
    assert p["has_photo"] and p["photo_from"] == "из справки"


@needs_soffice
def test_объединение_принимает_doc_и_отказывает_pdf(client, emp, tmp_path):
    from tests.test_merge import built
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    doc = soffice_convert(built("Иванова Мария Петровна"), "docx", "doc", tmp_path)
    order = json.dumps([{"kind": "profile", "id": pid}, {"kind": "file", "index": 0}])
    r = client.post("/api/merge", headers=emp, data={"order": order}, files=[("files", ("ivanova.doc", doc, "application/msword"))])
    assert r.status_code == 200, r.text
    assert [it["title"] for it in r.json()["merge"]["items"]] == ["Кравцов Сергей Сергеевич", "Иванова Мария Петровна"]
    pdf = docs_common.convert(DOC, "a.docx", "pdf")
    r = client.post("/api/merge", headers=emp, data={"order": order}, files=[("files", ("a.pdf", pdf, "application/pdf"))])
    assert r.status_code == 400 and "Объединяются документы Word" in r.json()["detail"]


# ── ревью RC-2026-10-05-4 ───────────────────────────────────────────────

def test_возврат_прежнего_фио_не_возвращает_подтверждение(client, emp, monkeypatch):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    monkeypatch.setattr(portal, "check_people", lambda names: {n: {"status": "close", "suggestions": [{"name": "Кравцова С. С."}]}
                                                               for n in names})
    assert client.post(f"/api/profiles/{pid}/name-ok", headers=emp, json={"fio": "Кравцов Сергей Сергеевич"}).status_code == 200
    for fio in ("Кравцов Сергей Сергеевич2", "Кравцов Сергей Сергеевич"):
        p = client.get(f"/api/profiles/{pid}", headers=emp).json()
        p["data"]["fio"] = fio
        client.put(f"/api/profiles/{pid}", headers=emp, json={"data": p["data"], "rev": p["rev"]})
    p = client.get(f"/api/profiles/{pid}", headers=emp).json()
    assert p["name_confirmed"] is None and not p["name_ok"]                      # «А → Б → А» — заново
    assert client.post(f"/api/profiles/{pid}/build", headers=emp).status_code == 409


def test_архивная_не_идёт_в_объединение(client, emp):
    ids = []
    for fio in ("Кравцов Сергей Сергеевич", "Иванова Мария Петровна"):
        pid = client.post("/api/profiles", json={"fio": fio}, headers=emp).json()["id"]
        wait_done(client, emp, pid)
        ids.append(pid)
    client.delete(f"/api/profiles/{ids[1]}", headers=emp)
    r = client.post("/api/merge", headers=emp, data={"order": json.dumps([{"kind": "profile", "id": i} for i in ids])})
    assert r.status_code == 409 and "в архиве" in r.json()["detail"]


def test_почти_пустая_справка_не_уходит_в_искру(client, emp, fake_iskra):
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("a.txt", "Иванов И.".encode(), "text/plain")})
    assert r.status_code == 400 and "почти нет текста" in r.json()["detail"]
    junk = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(1, 256)) * 40
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("a.doc", junk, "application/msword")})
    assert r.status_code == 400 and fake_iskra.posts() == []


def test_фото_из_враждебного_docx_быстро_и_без_ошибки():
    from app import extract
    xml = "<w:document><w:body>" + "<a:blip " * 100_000 + "</w:body></w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
        z.writestr("word/_rels/document.xml.rels", "<Relationships>" + "<Relationship " * 50_000)
    started = time.monotonic()
    assert extract.first_photo(buf.getvalue()) is None
    assert time.monotonic() - started < 2.0
    assert extract.first_photo(docx(["Иванов Иван Иванович"], image=png(5, 6)))[1] == "png"
