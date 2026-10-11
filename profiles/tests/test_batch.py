"""Пакетная переработка «Переработать и объединить»: карточки по нашей форме, очередь, предупреждения,
порядок перед сборкой и один файл с кликабельным содержанием."""
from __future__ import annotations

import io
import json
import re
import zipfile

from app import jobs
from tests.conftest import answer_json, docx, png


def _files():
    return [
        ("files", ("ivanova.docx", docx(["Иванова Мария Петровна", "Директор департамента, родилась в 1980 г."], image=png(20, 25)), "application/octet-stream")),
        ("files", ("petrov.txt", "Петров Олег Игоревич, заместитель министра, окончил МГУ в 1995 году.".encode(), "text/plain")),
        ("files", ("bad.docx", b"broken", "application/octet-stream")),
        ("files", ("noname.txt", "Справка без фамилии: работал в разных местах, награждён.".encode(), "text/plain")),
    ]


def _answers():
    long_career = [{"period": str(1970 + i), "text": "заместитель руководителя департамента " * 3, "src": "справка"} for i in range(40)]

    def by_text(body):
        msg = body["message"]
        if "Иванова" in msg:
            return answer_json(fio="Иванова Мария Петровна")
        if "Петров" in msg:
            return answer_json(fio="Петров Олег Игоревич", career=long_career)
        return answer_json(fio="", position="", career=[], education=[], awards=[], key_facts=[])
    return [by_text, by_text, by_text]


def test_пакет_карточки_предупреждения_порядок_и_сборка(client, emp, fake_iskra):
    from app import portal
    fake_iskra.answers = _answers()
    r = client.post("/api/batches", headers=emp, files=_files(), data={"web": "0", "key": "batchkey001"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert [i["file"] for i in b["items"]] == ["ivanova.docx", "petrov.txt", "bad.docx", "noname.txt"]
    assert b["items"][2]["error"] and b["items"][2]["profile"] is None          # нечитаемый — ошибкой в списке
    assert jobs.drain(15)
    b = client.get(f"/api/batches/{b['id']}", headers=emp).json()
    assert b["done"] == 3 and b["failed"] == 1 and b["left"] == 0
    rows = {i["file"]: i for i in b["items"]}
    assert rows["noname.txt"]["warnings"] == ["не нашлось ФИО — впишите его в карточку"]
    assert rows["petrov.txt"]["warnings"] == ["не уложилась в одну страницу"]
    assert rows["ivanova.docx"]["warnings"] == []
    # фото из docx справки
    iv = rows["ivanova.docx"]["profile"]["id"]
    assert client.get(f"/api/profiles/{iv}", headers=emp).json()["has_photo"]
    # карточки — в реестре, по справке без интернета; прогоны шли по очереди не больше двух
    assert len(client.get("/api/profiles", headers=emp).json()["items"]) == 3
    assert all("В интернете не ищи" in p["instructions"] for p in fake_iskra.posts())
    assert fake_iskra.max_active <= 2
    # в сборку идут только карточки с ФИО; порядок меняется
    pe = rows["petrov.txt"]["profile"]["id"]
    nn = rows["noname.txt"]["profile"]["id"]
    assert b["ready"] == [iv, pe]
    r = client.put(f"/api/batches/{b['id']}/order", headers=emp, json={"profiles": [pe, nn, iv]})
    assert r.status_code == 200 and r.json()["ready"] == [pe, iv]
    assert client.put(f"/api/batches/{b['id']}/order", headers=emp, json={"profiles": [pe]}).status_code == 409
    order = [{"kind": "profile", "id": x} for x in r.json()["ready"]]
    m = client.post("/api/merge", headers=emp, data={"order": json.dumps(order), "batch": b["id"]}).json()["merge"]
    assert [it["title"] for it in m["items"]] == ["Петров Олег Игоревич", "Иванова Мария Петровна"]
    assert m["items"][0]["page"] == 2 and m["items"][1]["page"] >= 4       # длинная объективка — больше страницы
    assert client.get(f"/api/batches/{b['id']}", headers=emp).json()["merge_id"] == m["id"]
    data = client.get(f"/api/merges/{m['id']}/docx", headers=emp).content
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    anchors = re.findall(r'<w:hyperlink w:anchor="([^"]+)"', xml)
    assert anchors == ["_P1", "_P2"] and all(f'w:name="{a}"' in xml for a in anchors)
    # после правки карточки файл пересобирается с новыми сведениями
    p = client.get(f"/api/profiles/{iv}", headers=emp).json()
    p["data"]["position"] = "Заместитель министра"
    client.put(f"/api/profiles/{iv}", headers=emp, json={"data": p["data"], "rev": p["rev"]})
    m2 = client.post("/api/merge", headers=emp, data={"order": json.dumps(order), "batch": b["id"]}).json()["merge"]
    xml2 = zipfile.ZipFile(io.BytesIO(client.get(f"/api/merges/{m2['id']}/docx", headers=emp).content)).read("word/document.xml").decode()
    assert "Заместитель министра" in xml2
    assert "Пакетная переработка объективок" in [x["action"] for x in portal.sent]


def test_пакет_лимит_и_повтор(client, emp, fake_iskra):
    many = [("files", (f"{i}.txt", "Иванов Иван Иванович, министр".encode(), "text/plain")) for i in range(11)]
    r = client.post("/api/batches", headers=emp, files=many)
    assert r.status_code == 400 and "от 1 до 10" in r.json()["detail"]
    one = [("files", ("a.txt", "Иванов Иван Иванович, министр".encode(), "text/plain"))]
    a = client.post("/api/batches", headers=emp, files=one, data={"key": "samekey0001"}).json()
    b = client.post("/api/batches", headers=emp, files=one, data={"key": "samekey0001"}).json()
    assert a["id"] == b["id"]
    jobs.drain(10)
