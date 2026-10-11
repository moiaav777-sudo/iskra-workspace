"""Написание тезисов «Искрой»: ответ модели проверяется, а не применяется на веру.

Сети в тестах нет — обращение к «Искре» подменяется.
"""
from __future__ import annotations

from tests.conftest import add_line, put_lines

import json
import time

import pytest

SOURCE = (
    "Справка о школьных спортивных клубах.\n"
    "Клубы созданы в 80 % школ страны. За два года их число выросло на 12 %.\n"
    "На странице 4 доля школ с клубами указана как 76 %.\n"
    "Тренерские ставки закрыты не полностью.\n"
)

ANSWER = {
    "bullets": [
        {"text": "Сегодня клубы работают в 80 процентах школ.", "emphasis": "strong",
         "part": "масштаб"},
        {"text": "За два года их стало больше на 12 процентов.", "emphasis": "normal"},
        {"text": "Охват в сельских школах ниже [ТРЕБУЕТСЯ ФАКТУРА: охват на селе].",
         "emphasis": "normal"},
    ],
    "refs": [],
    "gaps": [{"marker": "ТРЕБУЕТСЯ ФАКТУРА", "suggestion": "охват на селе"}],
}


@pytest.fixture()
def answer(monkeypatch):
    """Подменить ответ «Искры»."""
    box = {"value": json.dumps(ANSWER, ensure_ascii=False), "asked": []}

    def fake_call(system, user):
        box["asked"].append({"system": system, "user": user})
        return box["value"]

    from app import iskra
    monkeypatch.setattr(iskra, "_call", fake_call)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    return box


def _write(client, emp, tid, body=None):
    """Запустить написание и дождаться исхода: оно фоновое, запрос отвечает сразу."""
    r = client.post(f"/api/theses/{tid}/write", json=body or {}, headers=emp)
    if r.status_code != 202:
        return r, None
    job = r.json()["job"]
    deadline = time.time() + 10
    while job["active"] and time.time() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/jobs/{job['id']}", headers=emp).json()["job"]
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    return r, {"thesis": thesis, "notes": job["notes"], "job": job}


def _thesis(client, emp, **kw):
    payload = {"topic": "О школьных спортивных клубах", "minutes": 7}
    payload.update(kw)
    created = client.post("/api/theses", json=payload, headers=emp).json()
    return created["thesis"]["id"], created["rev"]


def _with_source(client, emp, tid, rev, text=SOURCE):
    files = {"file": ("справка.txt", text.encode("utf-8"), "text/plain")}
    body = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp,
                       files=files).json()
    return body["rev"]


def test_правила_уходят_в_запрос_целиком(client, emp, answer):
    tid, rev = _thesis(client, emp)
    profile = client.get("/api/profiles", headers=emp).json()["profiles"][0]
    client.put(f"/api/theses/{tid}", headers=emp, json={
        "topic": "О школьных клубах", "profile": profile["id"], "minutes": 7, "rev": rev})
    _write(client, emp, tid)
    system = answer["asked"][0]["system"]
    assert "Абсолютный запрет на выдумывание фактуры" in system
    assert "ТРЕБУЕТСЯ ФАКТУРА" in system


def test_из_документов_числа_не_перепроверяются(client, emp, answer):
    """Решение пользователя 21.09: «убери функцию перепроверить цифры, всё будет на моё
    решение». Тезисы ложатся как есть, без статусов чисел и без замечаний о них."""
    tid, rev = _thesis(client, emp)
    rev = _with_source(client, emp, tid, rev)
    answer["value"] = json.dumps({
        "bullets": [{"text": "Обеспечено 260 тысяч семей — этого числа в справке нет."}],
        "indicators": [{"name": "Обеспечено", "value": "260"}],   # прежний вид ответа
    }, ensure_ascii=False)
    _, body = _write(client, emp, tid, {"rev": rev})
    thesis = body["thesis"]
    assert thesis["bullets"][0]["text"].startswith("Обеспечено 260 тысяч")
    assert "indicators" not in thesis and "unchecked" not in thesis
    assert not any("числа" in note for note in body["notes"])
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 200
    system = answer["asked"][0]["system"]
    assert "indicators" not in system and "quote" not in system, "сверку у «Искры» не просим"


def test_по_теме_недостающее_маркером(client, emp, answer):
    tid, rev = _thesis(client, emp)
    _, body = _write(client, emp, tid, {"rev": rev})
    assert body["thesis"]["open_gaps"] == 1
    assert "маркер [ТРЕБУЕТСЯ" in answer["asked"][0]["user"]


def test_готовый_текст_молча_не_затирается(client, emp, answer):
    tid, rev = _thesis(client, emp)
    add_line(client, emp, tid, **{"text": "Своими руками.", "rev": rev})
    r = client.post(f"/api/theses/{tid}/write", json={}, headers=emp)
    assert r.status_code == 409 and r.json()["detail"]["needs_replace"] is True
    fresh = client.get(f"/api/theses/{tid}", headers=emp).json()
    r2, done = _write(client, emp, tid, {"replace": True, "rev": fresh["rev"]})
    assert r2.status_code == 202 and done["job"]["state"] == "done"


def test_откуда_берём_содержание_следует_из_материалов(client, emp, answer):
    """Отдельного вопроса «по теме или из документов» нет: приложили — значит из них."""
    tid, rev = _thesis(client, emp)
    assert client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]["mode"] == "topic"
    rev = _with_source(client, emp, tid, rev)
    assert client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]["mode"] == "docs"
    source_id = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]["sources"][0]["id"]
    body = client.delete(f"/api/theses/{tid}/sources/{source_id}?rev={rev}", headers=emp).json()
    assert body["thesis"]["mode"] == "topic", "открепили всё — снова пишем по теме"


def test_ошибка_искры_объясняется_по_русски(client, emp, monkeypatch):
    from app import iskra

    def boom(system, user):
        raise iskra.IskraError("«Искра» ответила ошибкой 500: run failed")

    monkeypatch.setattr(iskra, "_call", boom)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    tid, rev = _thesis(client, emp)
    r, done = _write(client, emp, tid, {"rev": rev})
    assert r.status_code == 202
    assert done["job"]["state"] == "failed" and "run failed" in done["job"]["error"]
    assert done["thesis"]["bullets"] == [], "при ошибке текст не меняется"


def test_расход_токенов_уходит_в_журнал(client, emp, monkeypatch):
    from app import iskra, portal
    seen = []
    monkeypatch.setattr(portal, "send_audit", lambda **e: seen.append(e))

    def with_usage(system, user):
        iskra.spend(1200, 350)
        return json.dumps(ANSWER, ensure_ascii=False)

    monkeypatch.setattr(iskra, "_call", with_usage)
    monkeypatch.setattr(iskra, "key_ready", lambda: True)
    tid, rev = _thesis(client, emp)
    _write(client, emp, tid, {"rev": rev})
    written = [e for e in seen if e["action"].startswith("Тезисы написаны")]
    assert written and written[0]["tokens_in"] == 1200 and written[0]["tokens_out"] == 350


def test_документ_читается_из_памяти(client, emp):
    """DOCX разбирается, .doc старого формата объясняется по-русски."""
    import io
    import docx
    tid, rev = _thesis(client, emp)
    buffer = io.BytesIO()
    document = docx.Document()
    document.add_paragraph("Клубы созданы в 80 % школ страны.")
    document.save(buffer)
    files = {"file": ("справка.docx", buffer.getvalue(),
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    body = client.post(f"/api/theses/{tid}/sources/file?rev={rev}", files=files, headers=emp).json()
    assert body["thesis"]["sources"][0]["chars"] > 10
    assert body["thesis"]["mode"] == "docs", "приложили документ — значит пишем из него"
    bad = {"file": ("старый.docx", b"not a zip at all", "application/msword")}
    r = client.post(f"/api/theses/{tid}/sources/file?rev={body['rev']}", files=bad, headers=emp)
    assert r.status_code == 400 and "не читается" in r.json()["detail"]


def test_текст_документа_переживает_сохранение(client, emp):
    """Писать не из чего, если текст источника потерялся при записи в базу."""
    tid, rev = _thesis(client, emp)
    _with_source(client, emp, tid, rev)
    from app import storage
    stored = storage.get(tid)
    assert SOURCE.strip()[:30] in stored.thesis.sources[0].text
    # а наружу текст не отдаём: в списке видно только имя и размер
    shown = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]["sources"][0]
    assert "text" not in shown and shown["chars"] > 50


def test_числа_из_интернета_приходят_со_ссылкой(client, emp, answer):
    """Материалов нет — «Искра» ищет в открытых источниках и называет, откуда число.
    Это подсказка человеку, а не проверка: статусов и «подтвердить» нет."""
    answer["value"] = json.dumps({
        "bullets": [{"text": "Клубы созданы в 80 процентах школ."}],
        "refs": [{"name": "Росстат, сборник 2026", "url": "https://rosstat.gov.ru/folder/12345",
                  "about": "доля школ с клубами"},
                 {"name": "Сомнительное", "url": "javascript:alert(1)"}],
    }, ensure_ascii=False)
    tid, rev = _thesis(client, emp)
    _, body = _write(client, emp, tid, {"rev": rev})
    first, second = body["thesis"]["refs"]
    assert first == {"name": "Росстат, сборник 2026", "url": "https://rosstat.gov.ru/folder/12345",
                     "about": "доля школ с клубами"}
    assert second["url"] == "", "ссылка не по адресу http(s) не берётся"


def test_в_запросе_сказано_искать_в_источниках(client, emp, answer):
    tid, rev = _thesis(client, emp)
    _write(client, emp, tid, {"rev": rev})
    user = answer["asked"][0]["user"]
    assert "ищи фактуру в открытых источниках" in user
    assert "refs" in user and "refs" in answer["asked"][0]["system"]


# ── «Что нужно в тезисах»: наговорили голосом или написали (задача 21.09, п. 4) ──

def test_что_нужно_уходит_искре(client, emp, answer):
    created = client.post("/api/theses", headers=emp, json={
        "topic": "О школьном спорте", "minutes": 5,
        "ask": "  сделай акцент на тренерах и на сельских школах  "}).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    assert created["thesis"]["ask"] == "сделай акцент на тренерах и на сельских школах"
    _write(client, emp, tid, {"rev": rev})
    user = answer["asked"][0]["user"]
    assert "ЧТО НУЖНО В ТЕЗИСАХ" in user
    assert "сделай акцент на тренерах и на сельских школах" in user
    assert "ошибки распознавания речи" in user, "голос распознаётся с ошибками — понимать по смыслу"


def test_что_нужно_правится_и_пишется_в_журнал(client, emp, monkeypatch):
    from app import portal
    sent = []
    monkeypatch.setattr(portal, "send_audit", lambda **kw: sent.append(kw))
    created = client.post("/api/theses", headers=emp, json={"topic": "О спорте"}).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    body = client.put(f"/api/theses/{tid}", headers=emp, json={
        "topic": "О спорте", "minutes": 7, "ask": "про тренеров", "rev": rev}).json()
    assert body["thesis"]["ask"] == "про тренеров"
    assert "что нужно: «» → «про тренеров»" in sent[-1]["details"]


def test_без_пожеланий_в_запросе_их_нет(client, emp, answer):
    tid, rev = _thesis(client, emp)
    _write(client, emp, tid, {"rev": rev})
    assert "ЧТО НУЖНО В ТЕЗИСАХ" not in answer["asked"][0]["user"]
