"""Сбор объективки «Искрой»: по имени, по справке, по справке с открытыми источниками; сбои и расход."""
from __future__ import annotations

import json
import threading

from tests.conftest import answer_json, docx, png, spravka, wait_done


def _by_name(client, emp, fio="Кравцов Сергей Сергеевич", hint=""):
    r = client.post("/api/profiles", json={"fio": fio, "hint": hint}, headers=emp)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_по_имени_ищет_в_официальных_источниках_по_схеме(client, emp, fake_iskra):
    pid = _by_name(client, emp, hint="министр просвещения")
    p = wait_done(client, emp, pid)
    body = fake_iskra.posts()[0]
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]["schema"]
    assert {"fio", "career", "education", "awards", "refs", "key_facts", "powers"} <= set(schema["properties"])
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "ТОЛЬКО ОФИЦИАЛЬНЫЕ И НАДЁЖНЫЕ" in body["instructions"] and "Ничего не выдумывай" in body["instructions"]
    assert "одну страницу A4" in body["instructions"]
    assert "Кравцов Сергей Сергеевич" in body["message"] and "министр просвещения" in body["message"]
    assert p["status"] == "done"
    d = p["data"]
    assert d["fio"] == "Кравцов Сергей Сергеевич"
    # опыт работы — по возрастанию лет, источники со ссылками, ссылка на фото — только текстом
    assert [r["period"] for r in d["career"]] == ["1997–2000", "2018–2020"]
    assert d["refs"][0]["url"] == "https://edu.gov.ru/a"
    assert d["photo_url"] == "https://edu.gov.ru/photo.jpg" and not p["has_photo"]
    # после сбора Word уже собран и подобрана страница
    assert p["fit"]["fits"] is True and p["built_rev"] == p["rev"]


def test_схему_не_приняли_повтор_без_строгости_и_без_схемы(client, emp, fake_iskra):
    fake_iskra.refuse_schema = 2
    pid = _by_name(client, emp)
    p = wait_done(client, emp, pid)
    posts = fake_iskra.posts()
    assert posts[0]["response_format"]["json_schema"]["strict"] is True
    assert "strict" not in posts[1]["response_format"]["json_schema"]
    assert "response_format" not in posts[2]
    assert p["status"] == "done"


def test_ответ_текстом_с_обёрткой_разбирается(client, emp, fake_iskra):
    fake_iskra.answers = ["Вот ответ:\n```json\n" + json.dumps(answer_json(), ensure_ascii=False) + "\n```"]
    p = wait_done(client, emp, _by_name(client, emp))
    assert p["status"] == "done" and p["data"]["position"].startswith("Министр")


def test_по_справке_без_интернета(client, emp, fake_iskra):
    fake_iskra.answers = [answer_json(refs=[{"name": "сайт", "url": "https://x.ru", "about": ""}])]
    r = client.post("/api/profiles/from-doc", headers=emp, data={"web": "0"},
                    files={"file": ("справка.docx", spravka(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    p = wait_done(client, emp, r.json()["id"])
    body = fake_iskra.posts()[0]
    assert "В интернете не ищи" in body["instructions"] and "ТОЛЬКО из текста справки" in body["instructions"]
    assert "Министр просвещения" in body["message"] and "СПРАВКА (файл «справка.docx»)" in body["message"]
    d = p["data"]
    assert d["refs"] == [{"name": "справка", "url": "", "about": "справка.docx"}]
    assert all(row["src"] == "справка" for row in d["career"] + d["education"])
    assert d["photo_url"] == ""
    # фото — первая картинка docx справки
    assert p["has_photo"] and p["photo_from"] == "из справки"
    ph = client.get(f"/api/profiles/{p['id']}/photo", headers=emp)
    assert ph.status_code == 200 and ph.content[:4] == b"\x89PNG"


def test_по_справке_с_открытыми_источниками(client, emp, fake_iskra):
    r = client.post("/api/profiles/from-doc", headers=emp, data={"web": "1"},
                    files={"file": ("bio.txt", "Кравцов Сергей Сергеевич — министр. Окончил МГУ.".encode("utf-8"), "text/plain")})
    p = wait_done(client, emp, r.json()["id"])
    body = fake_iskra.posts()[0]
    assert "сначала — всё, что есть в справке" in body["instructions"] and "ОФИЦИАЛЬНЫЕ" in body["instructions"]
    refs = p["data"]["refs"]
    assert refs[0]["name"] == "справка" and any(x["url"] for x in refs[1:])
    assert p["data"]["career"][0]["src"].startswith("https://")


def test_нечитаемая_справка_понятная_ошибка(client, emp):
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("x.docx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 400 and "повреждён" in r.json()["detail"]
    empty = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("x.txt", b"   ", "text/plain")})
    assert empty.status_code == 400 and "не нашлось текста" in empty.json()["detail"]


def test_сбой_искры_ошибка_и_расход_в_журнал(client, emp, fake_iskra):
    from app import portal
    fake_iskra.fail_with = "внутренняя ошибка"
    p = wait_done(client, emp, _by_name(client, emp))
    assert p["status"] == "error" and "внутренняя ошибка" in p["status_text"]
    bad = [r for r in portal.sent if r["action"] == "«Искра» не собрала объективку"]
    assert bad and bad[0]["tokens_in"] == 1000


def test_искра_просит_уточнить(client, emp, fake_iskra):
    fake_iskra.interaction = True
    p = wait_done(client, emp, _by_name(client, emp))
    assert p["status"] == "error" and "уточнить" in p["status_text"] and "подсказку" in p["status_text"]


def test_отмена_снимает_прогон_и_считает_расход(client, emp, fake_iskra):
    from app import portal
    fake_iskra.hold = threading.Event()
    pid = _by_name(client, emp)
    import time
    for _ in range(200):
        if fake_iskra.runs:
            break
        time.sleep(0.01)
    assert client.get(f"/api/profiles/{pid}", headers=emp).json()["running"]
    # пока «Искра» собирает, править нельзя
    rev = client.get(f"/api/profiles/{pid}", headers=emp).json()["rev"]
    locked = client.put(f"/api/profiles/{pid}", headers=emp, json={"data": answer_json(), "rev": rev})
    assert locked.status_code == 409 and "ещё собирает" in locked.json()["detail"]
    client.post(f"/api/profiles/{pid}/cancel", headers=emp)
    p = wait_done(client, emp, pid)
    assert p["status"] == "cancelled"
    assert fake_iskra.deleted == ["run1"]
    rec = [r for r in portal.sent if r["action"] == "Сбор объективки «Искрой» отменён"]
    assert rec and rec[0]["tokens_in"] == 1000


def test_собрать_заново_не_затирает_молча(client, emp, fake_iskra):
    pid = _by_name(client, emp)
    wait_done(client, emp, pid)
    r = client.post(f"/api/profiles/{pid}/iskra", headers=emp, json={"replace": False})
    assert r.status_code == 409 and r.json()["detail"]["needs_replace"]
    fake_iskra.answers = [answer_json(position="Помощник Президента Российской Федерации")]
    assert client.post(f"/api/profiles/{pid}/iskra", headers=emp, json={"replace": True}).status_code == 200
    assert wait_done(client, emp, pid)["data"]["position"] == "Помощник Президента Российской Федерации"


def test_не_нашлось_фио_замечание(client, emp, fake_iskra):
    fake_iskra.answers = [answer_json(fio="", position="", career=[], education=[], awards=[], key_facts=[])]
    r = client.post("/api/profiles/from-doc", headers=emp, files={"file": ("a.txt", ("Текст справки о человеке без имени " * 3).encode(), "text/plain")})
    p = wait_done(client, emp, r.json()["id"])
    assert "ФИО не нашлось" in p["status_text"]


def test_очередь_не_больше_двух_прогонов(client, emp, fake_iskra):
    import time
    fake_iskra.hold = threading.Event()
    ids = [_by_name(client, emp, fio=f"Иванов Иван {n}ович".replace("0", "Петр").replace("1", "Сидор").replace("2", "Олег").replace("3", "Глеб"))
           for n in range(4)]
    time.sleep(0.5)
    assert fake_iskra.max_active <= 2
    states = [client.get(f"/api/profiles/{i}", headers=emp).json()["status"] for i in ids]
    assert states.count("queued") >= 2
    fake_iskra.hold.set()
    for i in ids:
        assert wait_done(client, emp, i)["status"] == "done"
    assert fake_iskra.max_active == 2


def test_повтор_отправки_не_плодит_объективки(client, emp):
    a = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич", "key": "abcdefgh123"}, headers=emp).json()
    b = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич", "key": "abcdefgh123"}, headers=emp).json()
    assert a["id"] == b["id"]


def test_doc_и_png_размеры(tmp_path):
    from app import extract
    from tests.conftest import jpeg_header
    assert extract.image_info(png(30, 40)) == ("png", 30, 40)
    assert extract.image_info(jpeg_header(640, 480)) == ("jpeg", 640, 480)
    assert extract.image_info(b"GIF89a") is None
    assert extract.first_photo(docx(["Иванов Иван Иванович"], image=png(5, 6)))[1] == "png"
    assert extract.first_photo(docx(["Иванов Иван Иванович"])) is None


def test_неожиданный_сбой_разбора_тоже_в_журнал_с_расходом(client, emp, fake_iskra, monkeypatch):
    from app import iskra, portal

    def broken(answer):
        raise ValueError("сломалось")
    monkeypatch.setattr(iskra, "parse_json", broken)
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    p = wait_done(client, emp, pid)
    assert p["status"] == "error" and "ValueError" in p["status_text"]
    rec = [r for r in portal.sent if r["action"] == "«Искра» не собрала объективку"]
    assert len(rec) == 1 and rec[0]["tokens_in"] == 1000 and "ValueError" in rec[0]["details"]
