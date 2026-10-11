"""Проверки контракта с порталом и работы библиотеки. Без сети и без «Искры»."""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from html import escape  # noqa: E402

from app import audit, config, db, extract, jobs, main, spark  # noqa: E402

SECRET = "stend-secret-1"
ADMIN = {"X-Portal-Secret": SECRET, "X-Portal-User": "admin", "X-Portal-Role": "admin",
         "X-Portal-Name": "%D0%90%D0%B4%D0%BC%D0%B8%D0%BD"}
EMP = {"X-Portal-Secret": SECRET, "X-Portal-User": "emp", "X-Portal-Role": "employee",
       "X-Portal-Name": "emp"}

SHEET = {
    "orientation": "landscape",
    "header": {"title": "Проверка контракта"},
    "blocks": [{"type": "text", "title": "Блок", "text": "Текст"}],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a3.db")
    monkeypatch.setattr(config, "PORTAL_SECRET", SECRET)
    monkeypatch.setattr(config, "DEV", False)
    audit.sent.clear()
    jobs.reset()
    main._last_event.clear()      # серверный предел событий листа — свой у каждой проверки
    main._burst.clear()
    db.init()
    with TestClient(main.app) as c:
        yield c
    jobs.reset()   # начатый разбор не должен дописать лист в базу следующей проверки


def make(client, headers=ADMIN, data=None):
    response = client.post("/api/sheets", json={"data": data or SHEET}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def answer(sheet, tokens=0):
    """Ответ «Искры»: JSON листа и расход токенов — так же, как у живого сервиса."""
    return sheet, {"in": tokens, "out": 0, "total": tokens}


def done(client, response, headers=ADMIN, timeout=10.0):
    """Фоновый разбор: дождаться конца, как это делает браузер опросом."""
    assert response.status_code == 202, response.text
    job = response.json()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/spark/jobs/{job['id']}", headers=headers).json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError("фоновый разбор не закончился")


def test_healthz_bez_vhoda(client):
    assert client.get("/healthz").status_code == 200


def test_bez_sekreta_otkaz(client):
    assert client.get("/api/sheets").status_code == 403
    assert client.get("/api/sheets", headers={**ADMIN, "X-Portal-Secret": "wrong"}).status_code == 403


def test_imya_iz_zagolovka_dekodiruetsya(client):
    sheet = make(client)
    assert sheet["created_name"] == "Админ"
    assert sheet["title"] == "Проверка контракта"
    assert sheet["version"] == 1


def test_chuzhoy_origin_otklonyaetsya(client):
    response = client.post("/api/sheets", json={"data": SHEET},
                           headers={**ADMIN, "Origin": "https://evil.example"})
    assert response.status_code == 403
    # свой Origin проходит
    ok = client.post("/api/sheets", json={"data": SHEET},
                     headers={**ADMIN, "Origin": "http://testserver", "Host": "testserver"})
    assert ok.status_code == 201


def test_sohranenie_rastit_versiyu_i_pishet_istoriyu(client):
    sheet = make(client)
    changed = {**SHEET, "header": {"title": "Новое название"}}
    saved = client.put(f"/api/sheets/{sheet['id']}",
                       json={"data": changed, "version": sheet["version"]}, headers=EMP).json()
    assert saved["version"] == 2
    assert saved["title"] == "Новое название"
    versions = client.get(f"/api/sheets/{sheet['id']}/versions", headers=EMP).json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]


def test_odnovremennaya_pravka_lovitsya(client):
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}", json={"data": SHEET, "version": 1}, headers=ADMIN)
    late = client.put(f"/api/sheets/{sheet['id']}", json={"data": SHEET, "version": 1}, headers=EMP)
    assert late.status_code == 409
    assert "Админ" in late.json()["message"]


def test_vosstanovlenie_versii_daet_novuyu(client):
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}",
               json={"data": {**SHEET, "header": {"title": "Второе"}}, "version": 1}, headers=ADMIN)
    restored = client.post(f"/api/sheets/{sheet['id']}/versions/1/restore", headers=EMP).json()
    assert restored["version"] == 3
    assert restored["title"] == "Проверка контракта"


def test_arhiv_i_udalenie_tolko_adminu(client):
    sheet = make(client)
    assert client.post(f"/api/sheets/{sheet['id']}/archive", headers=EMP).json()["archived"] is True
    assert client.get("/api/sheets", headers=EMP).json()["sheets"] == []
    assert client.get("/api/sheets?archived=true", headers=EMP).json()["sheets"][0]["id"] == sheet["id"]
    assert client.delete(f"/api/sheets/{sheet['id']}", headers=EMP).status_code == 403
    assert client.delete(f"/api/sheets/{sheet['id']}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/sheets/{sheet['id']}", headers=ADMIN).status_code == 404


def test_poisk_po_nazvaniyu_i_soderzhaniyu(client):
    make(client)
    make(client, data={**SHEET, "header": {"title": "Другой лист"},
                       "blocks": [{"type": "text", "text": "редкое слово кварц"}]})
    assert len(client.get("/api/sheets?q=кварц", headers=EMP).json()["sheets"]) == 1
    assert len(client.get("/api/sheets?q=Другой", headers=EMP).json()["sheets"]) == 1


def test_zhurnal_pishet_bylo_stalo(client):
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}",
               json={"data": {**SHEET, "header": {"title": "Второе"}}, "version": 1}, headers=ADMIN)
    записи = [r for r in audit.sent if r["service"] == "a3"]
    assert записи[0]["action"] == "создан лист A3"
    assert "версия 1 → 2" in записи[-1]["details"]
    assert "«Проверка контракта» → «Второе»" in записи[-1]["details"]


def test_fayl_lista_samodostatochen(client):
    sheet = make(client)
    response = client.get(f"/api/sheets/{sheet['id']}/file", headers=EMP)
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "Проверка контракта" in response.text
    assert "<script" in response.text  # движок вшит, файл работает без сервиса


def test_redaktor_otdaet_list_s_knopkoy_servisa(client):
    sheet = make(client)
    html = client.get(f"/editor?id={sheet['id']}", headers=EMP).text
    assert "a3-service-state" in html
    assert "Сохранить" in html
    # правка листа должна быть в полосе сервиса: кнопки конструктора прячутся за ней
    assert "a3-svc-edit" in html
    assert "#a3-edit{display:none!important}" in html


def test_stili_i_skripty_sveryayutsya_posle_vykladki(client):
    """Старый `library.js` из кеша ждёт прежних ответов сервиса — сверка обязательна."""
    for name in ("library.js", "library.css", "service.js"):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["etag"]          # сверка дешёвая: при совпадении придёт 304


def test_spravka_razbiraetsya_iskroy(client, monkeypatch):
    monkeypatch.setattr(spark, "draft",
                        lambda text: answer({**SHEET, "header": {"title": "Из справки"}}, 1200))
    started = client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN)
    job = done(client, started)
    assert job["status"] == "done"
    assert job["sheet"]["title"] == "Из справки"
    assert client.post("/api/spark/draft", data={"text": "коротко"}, headers=ADMIN).status_code == 400


def test_dolgiy_razbor_ne_derzhit_soedinenie(client, monkeypatch):
    """Запрос отдаёт номер разбора сразу, а не ждёт «Искру» минутами."""
    go = threading.Event()
    monkeypatch.setattr(spark, "draft", lambda text: (go.wait(5), answer(SHEET, 10))[1])
    started = client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN)
    assert started.status_code == 202          # соединение свободно, «Искра» ещё работает
    job = started.json()
    assert job["status"] in ("queued", "running") and job["id"]
    assert client.get("/api/sheets", headers=EMP).json()["sheets"] == []
    go.set()
    assert done(client, started)["sheet"]["title"] == "Проверка контракта"


def test_razbory_idut_po_ocheredi(client, monkeypatch):
    """«Искра» ведёт один прогон за раз: второй разбор ждёт очереди и знает своё место."""
    go, inside = threading.Event(), threading.Event()

    def slow_draft(text):
        inside.set()
        go.wait(5)
        return answer(SHEET, 5)

    monkeypatch.setattr(spark, "draft", slow_draft)
    first = client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN)
    assert inside.wait(5)
    second = client.post("/api/spark/draft", data={"text": "ю" * 300}, headers=EMP)
    waiting = client.get(f"/api/spark/jobs/{second.json()['id']}", headers=EMP).json()
    assert waiting["status"] == "queued" and waiting["position"] == 1
    assert "очеред" in waiting["message"].lower()
    go.set()
    assert done(client, first)["status"] == "done"
    assert done(client, second, headers=EMP)["status"] == "done"


def test_rashod_tokenov_schitaetsya(client, monkeypatch):
    """Разбор и круг доработки складываются в расход листа — он виден в списке и в журнале."""
    monkeypatch.setattr(spark, "draft", lambda text: answer(SHEET, 1200))
    monkeypatch.setattr(spark, "fix", lambda data, warnings: answer(SHEET, 800))
    sheet = done(client, client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN))["sheet"]
    assert sheet["tokens"] == 1200
    fixed = done(client, client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                                     json={"data": SHEET, "warnings": ["подпись обрезана"],
                                           "version": sheet["version"]}, headers=EMP),
                 headers=EMP)["sheet"]
    assert fixed["tokens"] == 2000
    assert client.get("/api/sheets", headers=EMP).json()["sheets"][0]["tokens"] == 2000
    assert "токенов «Искры»: 800" in audit.sent[-1]["details"]
    # расход уходит отдельными числами: портал складывает его по сотрудникам на вкладке «Искра»
    assert (audit.sent[-1]["tokens_in"], audit.sent[-1]["tokens_out"]) == (800, 0)


def test_neudachnyy_razbor_tozhe_popadaet_v_zhurnal(client, monkeypatch):
    """«Искра» прервала прогон — токены потрачены, и портал должен о них узнать."""
    def broken(text):
        raise spark.SparkError("«Искра» прервала разбор: перегрузка", {"in": 700, "out": 20, "total": 720})

    monkeypatch.setattr(spark, "draft", broken)
    job = done(client, client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN))
    assert job["status"] == "error" and "перегрузка" in job["message"]
    assert client.get("/api/sheets", headers=EMP).json()["sheets"] == []   # лист не создан
    assert (audit.sent[-1]["tokens_in"], audit.sent[-1]["tokens_out"]) == (700, 20)

    sheet = make(client)
    monkeypatch.setattr(spark, "fix",
                        lambda data, warnings: (_ for _ in ()).throw(
                            spark.SparkError("«Искра» не ответила за отведённое время",
                                             {"in": 300, "out": 0, "total": 300})))
    failed = done(client, client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                                      json={"data": SHEET, "warnings": ["подпись обрезана"],
                                            "version": 1}, headers=EMP), headers=EMP)
    assert failed["status"] == "error"
    assert (audit.sent[-1]["tokens_in"], audit.sent[-1]["tokens_out"]) == (300, 0)
    # потраченное впустую тоже видно в карточке листа
    assert client.get("/api/sheets", headers=EMP).json()["sheets"][0]["tokens"] == 300


def test_krug_ispravleniya_zamechaniy(client, monkeypatch):
    sheet = make(client)
    monkeypatch.setattr(spark, "fix",
                        lambda data, warnings: answer({**SHEET, "header": {"title": "Исправлено"}}))
    started = client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                          json={"data": SHEET, "warnings": ["подпись обрезана"], "version": 1},
                          headers=EMP)
    job = done(client, started, headers=EMP)
    assert job["status"] == "done"
    assert job["sheet"]["title"] == "Исправлено"
    assert job["sheet"]["version"] == 2


def test_sostoyanie_vidno_v_spiske(client, monkeypatch):
    """Пока «Искра» дорабатывает лист, это видно в списке листов."""
    monkeypatch.setattr(spark, "draft", lambda text: answer(SHEET))
    sheet = done(client, client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN))["sheet"]
    assert sheet["state"] == "checking"

    go, inside = threading.Event(), threading.Event()

    def slow_fix(data, warnings):
        inside.set()
        go.wait(5)
        return answer(SHEET)

    monkeypatch.setattr(spark, "fix", slow_fix)
    started = client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                          json={"data": SHEET, "warnings": ["подпись обрезана"], "version": 1},
                          headers=EMP)
    assert inside.wait(5)
    # список показывает доработку, пока она идёт
    assert client.get("/api/sheets", headers=EMP).json()["sheets"][0]["state"] == "fixing"
    go.set()
    assert done(client, started, headers=EMP)["status"] == "done"

    marked = client.post(f"/api/sheets/{sheet['id']}/state",
                         json={"state": "ok", "note": "исправлено «Искрой»"}, headers=EMP).json()
    assert marked["state"] == "ok"
    assert client.get("/api/sheets", headers=EMP).json()["sheets"][0]["state_note"] == "исправлено «Искрой»"


def test_krugi_schitayutsya_u_lista_a_ne_u_vkladki(client, monkeypatch):
    """Два круга — предел для листа: повторное открытие цикл не начинает заново."""
    monkeypatch.setattr(spark, "fix", lambda data, warnings: answer(SHEET))
    sheet = make(client)
    body = {"data": SHEET, "warnings": ["подпись обрезана"], "version": 1}

    def circle() -> dict:
        started = client.post(f"/api/sheets/{sheet['id']}/spark/fix", json=body, headers=EMP)
        return done(client, started, headers=EMP)["sheet"]

    assert circle()["fix_rounds"] == 1
    body["version"] = 2
    assert circle()["fix_rounds"] == 2

    body["version"] = 3
    third = client.post(f"/api/sheets/{sheet['id']}/spark/fix", json=body, headers=EMP)
    assert third.status_code == 409  # сам по себе третий круг не пойдёт

    body["again"] = True             # а по кнопке «Доработать ещё раз» — пойдёт
    assert circle()["fix_rounds"] == 1


def test_broshennyy_krug_ne_visit_vechno(client, monkeypatch):
    """Вкладку закрыли — лист не остаётся «в доработке» навсегда."""
    sheet = make(client)
    db.set_state(sheet["id"], "fixing", "«Искра» дорабатывает, круг 1 из 2")
    assert client.get("/api/sheets", headers=EMP).json()["sheets"][0]["state"] == "fixing"
    monkeypatch.setattr(db, "_older_than", lambda stamp, seconds: True)
    stale = client.get("/api/sheets", headers=EMP).json()["sheets"][0]
    assert stale["state"] == "warn"
    assert "прервана" in stale["state_note"]


def test_sboy_strogogo_formata_povtoryaetsya_bez_shemy(monkeypatch):
    """«structured output: сбой утилитарного вызова» — повтор того же запроса без схемы."""
    calls: list[bool] = []

    def fake_call(method, path, body=None, timeout=60):
        if method == "POST" and path == "/api/v1/runs":
            calls.append("response_format" in (body or {}))
            return 202, {"run_id": f"r{len(calls)}"}
        if calls[-1]:  # первый прогон — со схемой, он и падает
            return 200, {"status": "failed",
                         "error": {"code": "internal", "message": "structured output: сбой утилитарного вызова"}}
        return 200, {"status": "completed", "result": {"output": SHEET}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    sheet, spent = spark.draft("текст справки")
    assert sheet["header"]["title"] == "Проверка контракта"
    assert calls == [True, False]
    assert spent["total"] == 0  # «Искра» расход не сообщила — считаем нулём, а не выдумываем


def test_molchalivyy_sboy_tozhe_povtoryaetsya_bez_shemy(monkeypatch):
    """«run failed» без объяснений — тот же повтор: чаще всего не сложился строгий формат."""
    calls: list[bool] = []

    def fake_call(method, path, body=None, timeout=60):
        if method == "POST" and path == "/api/v1/runs":
            calls.append("response_format" in (body or {}))
            return 202, {"run_id": f"r{len(calls)}"}
        if calls[-1]:
            return 200, {"status": "failed", "error": "run failed"}
        return 200, {"status": "completed", "result": {"output": SHEET}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    sheet, _ = spark.draft("текст справки")
    assert sheet["header"]["title"] == "Проверка контракта"
    assert calls == [True, False]


def test_sboy_bez_shemy_obyasnyaet_chto_delat(monkeypatch):
    """Не помог и повтор — человек должен получить причину и что делать дальше."""
    def fake_call(method, path, body=None, timeout=60):
        if method == "POST" and path == "/api/v1/runs":
            return 202, {"run_id": "r1"}
        return 200, {"status": "failed", "error": "run failed",
                     "usage": {"prompt_tokens": 900, "completion_tokens": 10}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark.draft("текст справки")
    assert "run failed" in str(e.value)
    assert "ещё раз" in str(e.value)
    # оба прогона стоили токенов, и оба сосчитаны
    assert e.value.spent == {"in": 1800, "out": 20, "total": 1820}


def test_prichina_otkaza_sobiraetsya_iz_lyubogo_polya(monkeypatch):
    """Причину «Искра» кладёт в разные поля — сервис должен найти её, а не писать «failed»."""
    assert spark._trouble({"last_error": {"message": "слишком длинный запрос"}}, "failed") \
        == "слишком длинный запрос"
    assert spark._trouble({"failure_reason": "quota exceeded"}, "failed") == "quota exceeded"
    assert spark._trouble({}, "failed") == "failed"       # объяснения нет вовсе — так и скажем


def test_docx_razbiraetsya_iz_pamyati(tmp_path):
    """Разбор идёт из памяти: временный файл на Windows нельзя открыть второй раз."""
    import io
    import zipfile

    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Справка о готовности</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Школ</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>420</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", body)
    text = extract.from_file("справка.docx", buf.getvalue())
    assert "Справка о готовности" in text
    assert "Школ | 420" in text


def test_lyubaya_oshibka_vozvrashchaetsya_kak_json(tmp_path, monkeypatch):
    """Клиент не должен получать «Internal Server Error» вместо JSON."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a3.db")
    monkeypatch.setattr(config, "PORTAL_SECRET", SECRET)
    monkeypatch.setattr(config, "DEV", False)
    db.init()

    def boom(**kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(db, "list_sheets", boom)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        response = c.get("/api/sheets", headers=ADMIN)
    assert response.status_code == 500
    assert "база недоступна" in response.json()["message"]


def test_iskra_nedostupna_ponyatnaya_oshibka(client, monkeypatch):
    def boom(text):
        raise spark.SparkError("«Искра» недоступна: соединение отклонено")
    monkeypatch.setattr(spark, "draft", boom)
    job = done(client, client.post("/api/spark/draft", data={"text": "ц" * 300}, headers=ADMIN))
    assert job["status"] == "error"
    assert "Искра" in job["message"]
    assert client.get("/api/sheets", headers=ADMIN).json()["sheets"] == []   # пустой лист не заводим


def test_neizvestnyy_razbor_ponyatnaya_oshibka(client):
    """Сервис перезапускали — вкладка должна получить объяснение, а не голый 404."""
    response = client.get("/api/spark/jobs/net-takogo", headers=ADMIN)
    assert response.status_code == 404
    assert "перезапуск" in response.json()["message"]


def test_imya_polzovatelya_ekraniruetsya(client):
    """Имя приходит из портала и попадает в атрибут страницы — кавычка не должна ломать разметку."""
    headers = dict(ADMIN)
    headers["X-Portal-Name"] = '%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E'   # "><img src=x onerror=alert(1)>
    page = client.get("/", headers=headers).text
    assert "<img src=x" not in page
    assert "&lt;img" in page or "&quot;&gt;" in page


def test_otladochnyi_rezhim_ne_otklyuchaet_vhod(monkeypatch):
    """A3_DEV=1 рядом с настроенным секретом портала не должен отключать проверку."""
    import importlib
    monkeypatch.setenv("A3_DEV", "1")
    monkeypatch.setenv("PORTAL_SECRET", "nastoyashchiy-sekret")
    cfg = importlib.reload(config)
    assert cfg.DEV is False          # секрет настроен — значит сервис за порталом, вход не отключаем
    monkeypatch.delenv("A3_DEV")
    monkeypatch.delenv("PORTAL_SECRET")
    importlib.reload(config)


def test_vosstanovlenie_versii_lovit_chuzhuyu_pravku(client):
    sheet = make(client)
    sid = sheet["id"]
    client.put(f"/api/sheets/{sid}", json={"data": SHEET, "version": sheet["version"]}, headers=ADMIN)
    # человек смотрит историю, а в это время кто-то сохраняет лист
    client.put(f"/api/sheets/{sid}", json={"data": SHEET, "version": 2}, headers=EMP)
    late = client.post(f"/api/sheets/{sid}/versions/1/restore", json={"version": 2}, headers=ADMIN)
    assert late.status_code == 409
    fresh = client.get(f"/api/sheets/{sid}", headers=ADMIN).json()
    ok = client.post(f"/api/sheets/{sid}/versions/1/restore", json={"version": fresh["version"]}, headers=ADMIN)
    assert ok.status_code == 200


def test_obshchiy_predel_krugov_dorabotki(client, monkeypatch):
    sheet = make(client)
    sid = sheet["id"]
    monkeypatch.setattr(db, "fix_calls", lambda _sid: main.MAX_TOTAL_ROUNDS)
    r = client.post(f"/api/sheets/{sid}/spark/fix",
                    json={"data": SHEET, "warnings": ["подпись обрезана"], "version": sheet["version"], "again": True},
                    headers=ADMIN)
    assert r.status_code == 409 and "раз" in str(r.json())


def test_predel_schitaet_i_sorvavshiesya_zahody(client, monkeypatch):
    """Сорвавшийся круг тоже стоил денег — он должен приближать предел, а не проходить мимо."""
    sheet = make(client)
    sid = sheet["id"]
    monkeypatch.setattr(spark, "fix", lambda data, warnings: (_ for _ in ()).throw(
        spark.SparkError("«Искра» прервала разбор: run failed", {"in": 9000, "out": 0, "total": 9000})))
    body = {"data": SHEET, "warnings": ["подпись обрезана"], "version": 1, "again": True}
    for _ in range(main.MAX_TOTAL_ROUNDS):
        assert done(client, client.post(f"/api/sheets/{sid}/spark/fix", json=body, headers=EMP),
                    headers=EMP)["status"] == "error"
    # ни одной сохранённой версии не появилось, а предел всё равно достигнут
    assert client.get(f"/api/sheets/{sid}", headers=EMP).json()["version"] == 1
    stopped = client.post(f"/api/sheets/{sid}/spark/fix", json=body, headers=EMP)
    assert stopped.status_code == 409
    assert "токенов" in stopped.json()["message"]


def test_zaglushka_vmesto_lista_ne_zatiraet_rabotu(client, monkeypatch):
    """«Отсутствуют исходные данные» вместо листа — это не правка, сохранять нельзя."""
    rich = {**SHEET, "blocks": [{"type": "text", "title": f"Блок {i}", "text": "Текст " * 20}
                                for i in range(8)]}
    sheet = make(client, data=rich)
    stub = {"header": {"title": "Отсутствуют исходные данные"},
            "blocks": [{"type": "text", "title": "Нет данных", "text": "Источник не передан"}]}
    monkeypatch.setattr(spark, "fix", lambda data, warnings: answer(stub, 90000))
    job = done(client, client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                                   json={"data": rich, "warnings": ["подпись обрезана"],
                                         "version": sheet["version"]}, headers=EMP), headers=EMP)
    assert job["status"] == "error" and "оставлен как был" in job["message"]
    after = client.get(f"/api/sheets/{sheet['id']}", headers=EMP).json()
    assert after["version"] == sheet["version"]          # версия не выросла
    assert len(after["data"]["blocks"]) == 8             # лист на месте
    assert after["tokens"] == 90000                      # но потраченное сосчитано
    assert "вернула не тот лист" in audit.sent[-1]["action"]


def test_sokrashchennyy_list_s_kruga_prinimaetsya(client, monkeypatch):
    """Сокращать лист кругу можно — отвергаем только подмену, а не работу."""
    rich = {**SHEET, "blocks": [{"type": "text", "title": f"Блок {i}", "text": "Текст " * 20}
                                for i in range(8)]}
    sheet = make(client, data=rich)
    shorter = {**rich, "blocks": [{"type": "text", "title": f"Блок {i}", "text": "Текст " * 16}
                                  for i in range(6)]}
    monkeypatch.setattr(spark, "fix", lambda data, warnings: answer(shorter, 1000))
    job = done(client, client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                                   json={"data": rich, "warnings": ["подпись обрезана"],
                                         "version": sheet["version"]}, headers=EMP), headers=EMP)
    assert job["status"] == "done"
    assert len(job["sheet"]["data"]["blocks"]) == 6


def test_adresa_sosedey_otdaet_server(client):
    """Совещание — общая ось: адреса «Модератора» и «Материалов» приходят со страницей.

    В самой странице абсолютных адресов быть не должно — так же у «Модератора»
    и «Брифа». Меняется адрес соседа — правится один словарь на сервере.
    """
    html = client.get("/", headers=EMP).text
    assert "data-links=" in html
    for address in ("/moderator/api/meetings/find-or-create", "/materials/api/events/{id}/files"):
        assert escape(address, quote=True) in html or address in html

    script = client.get("/static/library.js").text
    # скрипт берёт адреса из атрибута, а не носит их в себе
    assert "dataset.links" in script
    assert "/moderator/api/meetings" not in script
    assert "/materials/api/events" not in script


def test_list_uhodit_v_sovesсhanie_fajlom(client):
    """Лист для папки совещания — тот же самодостаточный HTML, что и «Скачать»."""
    sheet = make(client)
    response = client.get(f"/api/sheets/{sheet['id']}/file", headers=EMP)
    assert response.status_code == 200
    assert "Проверка контракта" in response.text
    assert "<script" in response.text     # файл в папке совещания открывается без сервиса


def test_tipografika_obshchaya(client):
    """Шрифт и базовый размер — из base.css, своих сервис не объявляет (контракт п. 5б)."""
    css = client.get("/static/library.css").text
    body = css.split("body {", 1)[1].split("}", 1)[0]
    assert "font-family" not in body and "font:" not in body and "font-size" not in body
    # размеры кусков страницы — относительные, от общего, а не новые базовые
    for token in ("var(--text-small)", "var(--text-h2)"):
        assert token in css
    assert "15px" not in css and "-apple-system" not in css

    html = client.get("/", headers=EMP).text
    assert '<link rel="stylesheet" href="/static/base.css">' in html


def test_shapka_servisa_est(client):
    """Шапка сервиса как у соседей: название слева, действия справа."""
    html = client.get("/", headers=EMP).text
    assert 'class="topbar"' in html
    bar = html.split('class="topbar"', 1)[1].split("</header>", 1)[0]
    assert "Инфографика A3" in bar
    assert 'id="blank"' in bar and "Новый лист" in bar     # действие справа, в полосе


def test_data_v_spiske_chelovecheskaya(client):
    """В списке листов дата человеческая, а не машинная «2026-09-20 18:35:07»."""
    script = client.get("/static/library.js").text
    assert "function human(" in script
    assert "human(card.updated_at)" in script              # карточка листа
    assert "human(v.created_at)" in script                 # история версий
    assert "сегодня в " in script and "вчера в " in script


def test_list_v_papku_uhodit_so_svoim_source(client):
    """Листов A3 к совещанию бывает несколько — «Материалы» различают их по source (п. 9)."""
    script = client.get("/static/library.js").text
    assert 'form.append("source"' in script
    assert '"service": "a3"' in script or "service: \"a3\"" in script
    # имя файла номер не несёт: его ставят «Материалы» по старшинству
    assert "mode" not in script.split('form.append("source"', 1)[1].split("\n", 3)[0]


def test_sovesshanie_nazyvaetsya_korotkim_imenem_ot_moderatora(client):
    """Короткое название совещания считает «Модератор» — своего A3 не придумывает (п. 9).

    В списке — `short_title`, полное название — в подсказке. А в find-or-create уходит
    полное: совещание узнаётся по паре «дата + название», короткое завело бы второе.
    """
    script = client.get("/static/library.js").text
    assert "m.short_title || m.title" in script              # чужое короткое имя, своего нет
    assert "option.title = m.title" in script                 # полное — в подсказке
    assert "picked.title" in script                           # в запрос — полное название
    assert "meeting.short_title" in script                    # и в сообщении об успехе


# ── воронка «разбор → лист → совещание» в журнале ─────────────────────────────

def test_skachivanie_lista_pishetsya_v_zhurnal(client):
    """Скачал лист — это видно в журнале; файл для папки совещания скачиванием не считается."""
    sheet = make(client)
    audit.sent.clear()
    client.get(f"/api/sheets/{sheet['id']}/file", headers=EMP)
    assert audit.sent[-1]["action"] == "скачан лист A3"
    assert audit.sent[-1]["login"] == "emp"
    audit.sent.clear()
    client.get(f"/api/sheets/{sheet['id']}/file?purpose=attach", headers=EMP)
    assert audit.sent == []          # это приложение, оно пишется своим событием


def test_pechat_skachivanie_i_prilozhenie_iz_brauzera(client):
    """Что делают с листом в браузере, сервер узнаёт от страницы и пишет в журнал."""
    sheet = make(client)
    audit.sent.clear()
    for body in ({"kind": "print"}, {"kind": "download", "format": "json"},
                 {"kind": "attach", "meeting": "Обеспечение жильем детей-сирот", "how": "файлом в папку"}):
        assert client.post(f"/api/sheets/{sheet['id']}/event", json=body, headers=EMP).status_code == 204
    actions = [r["action"] for r in audit.sent]
    assert actions == ["лист A3 отправлен на печать", "скачан лист A3", "лист A3 приложен к совещанию"]
    assert "JSON" in audit.sent[1]["details"]
    assert "«Обеспечение жильем детей-сирот», файлом в папку" in audit.sent[2]["details"]
    # неизвестное событие не пишется, чужой лист — 404
    assert client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "hack"}, headers=EMP).status_code == 400
    assert client.post("/api/sheets/999/event", json={"kind": "print"}, headers=EMP).status_code == 404


def test_stranica_soobshchaet_o_pechati_i_prilozhenii(client):
    """Печать и файл из редактора, приложение к совещанию — страница о них сообщает."""
    editor = client.get("/static/service.js").text
    assert "beforeprint" in editor and '"download"' in editor
    assert "a3-save-html" in editor and "a3-save-json" in editor
    library = client.get("/static/library.js").text
    assert 'kind: "attach"' in library
    assert "/file?purpose=attach" in library


# ── расход «Искры» при неудаче ────────────────────────────────────────────────

def test_negodnyy_otvet_iskry_ne_teryaet_rashod(monkeypatch):
    """Прогон прошёл, токены ушли, а ответ не JSON — расход должен уйти вместе с отказом."""
    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            return 202, {"run_id": "r1"}
        return 200, {"status": "completed", "result": {"output": "не JSON, а рассуждение"},
                     "usage": {"prompt_tokens": 5000, "completion_tokens": 300}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark.draft("текст справки")
    assert e.value.spent["in"] == 5000 and e.value.spent["out"] == 300


def test_nedozhdalis_rashod_poslednego_zamera_uchten(monkeypatch):
    """Не дождались «Искры» — то, что прогон успел потратить, всё равно считаем."""
    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            return 202, {"run_id": "r1"}
        if method == "DELETE":
            return 200, {}
        return 200, {"status": "running", "usage": {"prompt_tokens": 7000, "completion_tokens": 0}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark._run("сообщение", timeout=0.05)
    assert "не ответила" in str(e.value)
    assert e.value.spent["in"] == 7000


# ── что уходит в правку ───────────────────────────────────────────────────────

def test_v_pravku_uhodit_vyzhimka_svoda_i_list_bez_sluzhebnogo(monkeypatch):
    """Круг правки: без справки, с половиной свода и листом без служебных полей движка."""
    sent: list[dict] = []

    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            sent.append(body)
            return 202, {"run_id": "r1"}
        return 200, {"status": "completed", "result": {"output": SHEET}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    data = {**SHEET, "blocks": [{"type": "text", "title": "Блок", "text": "Текст", "_chart": {"x": 1}}]}
    spark.fix(data, ["подпись обрезана"])

    rules, message = sent[0]["instructions"], sent[0]["message"]
    assert "### 4. Сожми текст" in rules and "## Схема JSON" in rules and "## Формат ответа" in rules
    assert "### 1. Пойми документ" not in rules and "## Чек-лист перед выдачей" not in rules
    assert len(rules) < len(spark.instructions()) * 0.75         # заметно короче полного свода
    assert "_chart" not in message                                 # служебное движка не шлём
    assert '"blocks":[{"type":"text"' in message                  # лист без лишних пробелов


def test_svod_perepisali_uhodit_celikom(monkeypatch):
    """Не нашёлся раздел — свод идёт целиком: лучше переплатить, чем потерять правило."""
    monkeypatch.setattr(spark, "_prompt", lambda: "## Роль и задача\nтекст\n## Схема JSON\nполя")
    assert "## Роль и задача" in spark.instructions(for_fix=True)


# ── замечания ревьюера RC-2026-09-21-3 ────────────────────────────────────────

CHARTS = ("bar_h", "bar_v", "line", "donut", "stacked", "bullet")


def test_v_vyzhimke_dlya_pravki_est_vse_diagrammy_i_palitra():
    """К2: круг правки должен знать схемы всех шести диаграмм и палитру — иначе сломает лист."""
    rules = spark.instructions(for_fix=True)
    for chart in CHARTS:
        assert f'"type": "{chart}"' in rules, chart
    for field in ('"mode"', '"highlight"', '"center_value"', "null", "третьим элементом"):
        assert field in rules, field
    for colour in ("green", "orange", "red", "lavender"):
        assert f"`{colour}`" in rules, colour
    assert "не больше трёх акцентных" in rules


def test_v_vyzhimke_est_kazhdyy_tip_bloka_iz_polnogo_svoda():
    """Любой тип блока, описанный в своде, должен дойти и до правки — даже если свод перепишут."""
    full, rules = spark.instructions(), spark.instructions(for_fix=True)
    kinds = set(re.findall(r'"type":\s*"(\w+)"', full))
    assert set(CHARTS) <= kinds                       # проверка не пустая
    missing = {k for k in kinds if f'"type": "{k}"' not in rules and f'"type":"{k}"' not in rules}
    assert not missing, missing


def test_obryv_svyazi_posredi_progona_ne_teryaet_rashod(monkeypatch):
    """Связь оборвалась, когда прогон уже шёл и тратил токены, — расход уходит с отказом."""
    polls = []

    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            return 202, {"run_id": "r1"}
        polls.append(path)
        if len(polls) == 1:
            return 200, {"status": "running", "usage": {"prompt_tokens": 4200, "completion_tokens": 10}}
        raise spark.SparkError("«Искра» недоступна: соединение сброшено")

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark.draft("текст справки")
    assert "недоступна" in str(e.value)
    assert e.value.spent["in"] == 4200


def test_shapka_strokoy_ne_ronyaet_razbor():
    """«Искра» прислала шапку строкой — это название, а не повод упасть мимо журнала."""
    sheet = spark.parse_sheet('{"header": "Итоги года", "blocks": [{"type": "text", "text": "т"}]}')
    assert sheet["header"] == {"title": "Итоги года"}
    odd = spark.parse_sheet('{"header": ["что-то"], "blocks": [{"type": "text", "text": "т"}]}')
    assert odd["header"]["title"] == "Лист без названия"


def test_lyuboy_sboy_razbora_otveta_idet_s_rashodom(monkeypatch):
    """Даже непредвиденный сбой разбора после оплаченного прогона — отказ с расходом, а не падение."""
    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            return 202, {"run_id": "r1"}
        return 200, {"status": "completed", "result": {"output": SHEET},
                     "usage": {"prompt_tokens": 3000, "completion_tokens": 200}}

    def broken(text):
        raise TypeError("неожиданный вид ответа")

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "parse_sheet", broken)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark.draft("текст")
    assert e.value.spent["in"] == 3000


def test_konflikt_versii_posle_pravki_rashod_uchten(client, monkeypatch):
    """Пока «Искра» думала, лист поправили руками: правку не сохраняем, расход — в журнал и в лист."""
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}", json={"data": SHEET, "version": 1}, headers=ADMIN)  # теперь v2
    monkeypatch.setattr(spark, "fix", lambda data, warnings: answer(SHEET, 6400))
    job = done(client, client.post(f"/api/sheets/{sheet['id']}/spark/fix",
                                   json={"data": SHEET, "warnings": ["подпись обрезана"], "version": 1},
                                   headers=EMP), headers=EMP)
    assert job["status"] == "error" and "изменил" in job["message"]
    record = audit.sent[-1]
    assert record["action"] == "правка «Искрой» не сохранена: лист изменили"
    assert record["tokens_in"] == 6400
    assert client.get(f"/api/sheets/{sheet['id']}", headers=EMP).json()["tokens"] == 6400


def test_sobytiya_lista_ne_razduvayut_zhurnal(client):
    """Серверный предел: одно и то же событие того же человека по листу пишется один раз."""
    sheet = make(client)
    audit.sent.clear()
    for _ in range(20):
        assert client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "print"}, headers=EMP).status_code == 204
    assert [r["action"] for r in audit.sent] == ["лист A3 отправлен на печать"]
    # а HTML и следом JSON — два разных события
    client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "download", "format": "html"}, headers=EMP)
    client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "download", "format": "json"}, headers=EMP)
    assert len([r for r in audit.sent if r["action"] == "скачан лист A3"]) == 2
    # другой человек — своё событие
    client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "print"}, headers=ADMIN)
    assert len([r for r in audit.sent if r["action"] == "лист A3 отправлен на печать"]) == 2


def test_v_zhurnal_sobytiya_tolko_znakomye_znacheniya(client):
    """Свободный текст в `how` и `format` — это чужой текст в журнале: не принимаем."""
    sheet = make(client)
    sid = sheet["id"]
    assert client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "how": "<b>любой</b> текст"},
                       headers=EMP).status_code == 400
    assert client.post(f"/api/sheets/{sid}/event", json={"kind": "download", "format": "exe"},
                       headers=EMP).status_code == 400
    assert client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "how": "ссылкой"},
                       headers=EMP).status_code == 204


def test_sobytie_pishet_tu_versiyu_kotoruyu_smotryat(client):
    """Печатают и старую версию — в журнале должна быть она, а не текущая."""
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}", json={"data": SHEET, "version": 1}, headers=EMP)   # v2
    audit.sent.clear()
    client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "print", "version": 1}, headers=EMP)
    assert audit.sent[-1]["details"] == "версия 1"
    script = client.get("/static/service.js").text
    assert 'kind + ":" + (extra.format' in script              # пауза на странице — по виду и формату
    assert "state.shown_version" in script


# ── замечания ревьюера RC-2026-09-21-4 ────────────────────────────────────────

def test_obryv_na_povtore_bez_shemy_ne_teryaet_rashod(monkeypatch):
    """Первый прогон сорвался и стоил токенов, а на запуске повтора оборвалась связь."""
    posts = []

    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            posts.append(1)
            if len(posts) == 2:
                raise spark.SparkError("«Искра» недоступна: соединение сброшено")
            return 202, {"run_id": "r1"}
        return 200, {"status": "failed", "error": "run failed",
                     "usage": {"prompt_tokens": 8800, "completion_tokens": 40}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark.draft("текст справки")
    assert "недоступна" in str(e.value)
    assert e.value.spent["in"] == 8800


def test_sboy_snyatiya_progona_posle_taymauta_ne_teryaet_rashod(monkeypatch):
    """Не дождались, а снять прогон не дали — отказ всё равно «не ответила» и с расходом."""
    def fake_call(method, path, body=None, timeout=60):
        if method == "POST":
            return 202, {"run_id": "r1"}
        if method == "DELETE":
            raise spark.SparkError("«Искра» недоступна: соединение сброшено")
        return 200, {"status": "running", "usage": {"prompt_tokens": 5100, "completion_tokens": 0}}

    monkeypatch.setattr(spark, "_call", fake_call)
    monkeypatch.setattr(spark, "instructions", lambda for_fix=False: "правила")
    monkeypatch.setattr(spark.time, "sleep", lambda s: None)
    with pytest.raises(spark.SparkError) as e:
        spark._run("сообщение", timeout=0.05)
    assert "не ответила" in str(e.value)
    assert e.value.spent["in"] == 5100


def test_predel_ne_glushit_zakonnye_raznye_sobytiya(client):
    """Два совещания подряд и две версии подряд — разные события, а не повтор."""
    sheet = make(client)
    client.put(f"/api/sheets/{sheet['id']}", json={"data": SHEET, "version": 1}, headers=EMP)   # v2
    sid = sheet["id"]
    audit.sent.clear()
    for meeting in ("Обеспечение жильем детей-сирот", "Подготовка к учебному году"):
        client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "meeting": meeting, "how": "ссылкой"},
                    headers=EMP)
    for version in (1, 2):
        client.post(f"/api/sheets/{sid}/event", json={"kind": "print", "version": version}, headers=EMP)
    assert len([r for r in audit.sent if r["action"] == "лист A3 приложен к совещанию"]) == 2
    assert [r["details"] for r in audit.sent if r["action"] == "лист A3 отправлен на печать"] == \
        ["версия 1", "версия 2"]
    # а настоящий повтор по-прежнему глушится
    client.post(f"/api/sheets/{sid}/event", json={"kind": "print", "version": 2}, headers=EMP)
    assert len([r for r in audit.sent if r["action"] == "лист A3 отправлен на печать"]) == 2


def test_predel_pod_nagruzkoy_iz_mnogih_potokov(client, monkeypatch):
    """Чистка меток идёт под замком: потоки пула не роняют её «словарь изменился»."""
    monkeypatch.setattr(main, "EVENT_PAUSE", 0.0)
    for i in range(6000):                                   # доводим до порога чистки
        main._last_event[("x", i, "print", 1, "", "")] = 0.0
    sheet = make(client)
    errors = []

    def hit():
        try:
            for _ in range(20):
                client.post(f"/api/sheets/{sheet['id']}/event", json={"kind": "print"}, headers=EMP)
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len(main._last_event) < 6000                     # чистка сработала


def test_obshchiy_predel_ne_obhoditsya_vydumannymi_sovesshaniyami(client):
    """RC-5: разными названиями совещаний скрипт обходил точный ключ — общий предел это держит."""
    sheet = make(client)
    sid = sheet["id"]
    audit.sent.clear()
    for i in range(300):
        client.post(f"/api/sheets/{sid}/event",
                    json={"kind": "attach", "meeting": f"Выдуманное совещание {i}", "how": "ссылкой"}, headers=EMP)
    attached = [r for r in audit.sent if r["action"] == "лист A3 приложен к совещанию"]
    assert len(attached) == main.EVENT_BURST                 # 300 запросов → 5 записей
    # другой вид события и другой человек — свой общий предел
    client.post(f"/api/sheets/{sid}/event", json={"kind": "print"}, headers=EMP)
    client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "meeting": "Своё", "how": "ссылкой"},
                headers=ADMIN)
    assert len([r for r in audit.sent if r["action"] == "лист A3 отправлен на печать"]) == 1
    assert len([r for r in audit.sent if r["action"] == "лист A3 приложен к совещанию"]) == main.EVENT_BURST + 1


def test_obshchiy_predel_otpuskaet_cherez_pauzu(client, monkeypatch):
    """Окно скользящее: прошла пауза — события снова пишутся."""
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    sheet = make(client)
    sid = sheet["id"]
    audit.sent.clear()
    for i in range(main.EVENT_BURST + 2):
        client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "meeting": f"С{i}", "how": "ссылкой"},
                    headers=EMP)
    assert len(audit.sent) == main.EVENT_BURST
    clock[0] += main.EVENT_PAUSE + 0.1
    client.post(f"/api/sheets/{sid}/event", json={"kind": "attach", "meeting": "Новое", "how": "ссылкой"},
                headers=EMP)
    assert len(audit.sent) == main.EVENT_BURST + 1

