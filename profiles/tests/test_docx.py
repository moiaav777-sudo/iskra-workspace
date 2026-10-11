"""Сборка объективки в Word: структура XML, шапка с фото, таблица опыта работы, подбор одной страницы."""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import docx as python_docx
import pytest

from app import docx_build as dx
from tests.conftest import SAMPLE, answer_json, png, wait_done

NS = {"w": dx.NS["w"], "wp": dx.NS["wp"], "a": dx.NS["a"], "pic": dx.NS["pic"], "r": dx.NS["r"]}
W = "{%s}" % dx.NS["w"]


def parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def doc_tree(data: bytes):
    return ET.fromstring(parts(data)["word/document.xml"])


def texts(el) -> str:
    return "".join(t.text or "" for t in el.iter(W + "t"))


def long_data(career=12, rep=1, awards=6):
    d = answer_json()
    d["career"] = [{"period": f"{1990 + i}–{1991 + i}", "text": " ".join(["заместитель руководителя департамента"] * rep), "src": ""}
                   for i in range(career)]
    d["awards"] = [{"year": str(2000 + i), "text": "Орден «За заслуги перед Отечеством» IV степени", "src": ""} for i in range(awards)]
    return d


def test_документ_читается_и_все_части_разбираются():
    data, fit = dx.build(SAMPLE, (png(300, 350), "png"))
    for name, raw in parts(data).items():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(raw)                       # каждая часть — правильный XML
    d = python_docx.Document(io.BytesIO(data))       # независимая проверка: python-docx открывает
    assert len(d.tables) == 2
    sec = d.sections[0]
    assert (sec.page_width, sec.page_height) == (dx.PAGE_W * 635, dx.PAGE_H * 635)
    assert round(sec.top_margin.cm, 1) == 2.0 and round(sec.bottom_margin.cm, 1) == 2.0
    assert round(sec.left_margin.cm, 1) == 3.0 and round(sec.right_margin.cm, 1) == 1.5
    assert fit["fits"] and fit["size"] == 14 and fit["line"] == 1.15


def test_шапка_слева_фото_справа_фио_должность_ключевые_данные():
    data, _ = dx.build(SAMPLE, (png(300, 350), "png"))
    root = doc_tree(data)
    body = root.find("w:body", NS)
    head = body[0]
    assert head.tag == W + "tbl"
    cells = head.findall("w:tr/w:tc", NS)
    assert len(cells) == 2
    # границ нет
    borders = head.find("w:tblPr/w:tblBorders", NS)
    assert all(b.get(W + "val") == "none" for b in borders)
    # слева — картинка 4 см шириной, высота по пропорции 300×350
    ext = cells[0].find(".//wp:extent", NS)
    assert int(ext.get("cx")) == 4 * 360000 and int(ext.get("cy")) == round(4 * 360000 * 350 / 300)
    assert cells[0].find(".//a:blip", NS).get("{%s}embed" % dx.NS["r"]) == dx.PHOTO_RID
    # справа — ФИО жирным 16 пт, ниже должность, затем «Родился …» и классный чин
    paras = cells[1].findall("w:p", NS)
    assert texts(paras[0]) == "Кравцов Сергей Сергеевич"
    run = paras[0].find("w:r/w:rPr", NS)
    assert run.find("w:b", NS) is not None and run.find("w:sz", NS).get(W + "val") == "32"
    assert texts(paras[1]) == "Министр просвещения Российской Федерации"
    assert texts(paras[2]) == "Родился 12 марта 1970 г., г. Москва"
    assert texts(paras[3]).startswith("Классный чин: действительный")
    assert "Times New Roman" in parts(data)["word/document.xml"].decode()
    p = parts(data)
    assert "word/media/photo.png" in p
    assert b'Target="media/photo.png"' in p["word/_rels/document.xml.rels"]


def test_высокое_фото_не_выше_5_см():
    cx, cy = dx.photo_extent(300, 900)
    assert cy == 5 * 360000 and cx == int(round(5 * 360000 * 300 / 900))
    assert dx.photo_extent(400, 300) == (4 * 360000, 1080000)


def test_без_фото_нет_колонки():
    data, _ = dx.build(SAMPLE, None)
    body = doc_tree(data).find("w:body", NS)
    assert body[0].tag == W + "p" and texts(body[0]) == "Кравцов Сергей Сергеевич"
    assert len(body.findall("w:tbl", NS)) == 1                 # только «Опыт работы»
    assert "word/media/photo.png" not in parts(data)
    assert b"wp:inline" not in parts(data)["word/document.xml"]


def test_разделы_по_порядку_и_таблица_опыта_работы():
    data, _ = dx.build(SAMPLE, None)
    body = doc_tree(data).find("w:body", NS)
    heads = [texts(p) for p in body.findall("w:p", NS) if p.find("w:r/w:rPr/w:b", NS) is not None]
    assert heads == ["Кравцов Сергей Сергеевич", "Полномочия:", "Образование:", "Опыт работы:",
                     "Награды, почётные звания, поощрения:"]
    tbl = body.find("w:tbl", NS)
    grid = [int(g.get(W + "w")) for g in tbl.findall("w:tblGrid/w:gridCol", NS)]
    assert grid == list(dx.CAREER_GRID) and sum(grid) == dx.TEXT_W
    rows = tbl.findall("w:tr", NS)
    assert [[texts(c) for c in r.findall("w:tc", NS)] for r in rows] == [
        ["2018–2020", "–", "руководитель Рособрнадзора"], ["1997–2000", "–", "учитель математики"]]
    all_text = texts(body)
    assert "1997 – Московский государственный университет, математик" in all_text
    assert "2020 – Орден Почёта" in all_text and "– Член Правительственной комиссии" in all_text
    assert "edu.gov.ru" not in all_text                       # источники в документ не идут


def test_пустые_разделы_не_выводятся():
    d = answer_json(powers=[], awards=[], key_facts=[])
    body_text = texts(doc_tree(dx.build(d)[0]))
    assert "Полномочия" not in body_text and "Награды" not in body_text
    assert "Образование:" in body_text


def test_длинная_объективка_уменьшает_кегль_и_интервал():
    small = dx.build(SAMPLE)[1]
    mid = dx.build(long_data(career=20))[1]
    big = dx.build(long_data(career=40, rep=2, awards=15))[1]
    assert (small["size"], small["line"]) == (14, 1.15)
    assert mid["fits"] and (mid["size"], mid["line"]) != (14, 1.15)
    assert (mid["size"], mid["line"]) in dx.LADDER
    assert not big["fits"] and (big["size"], big["line"]) == dx.LADDER[-1]
    assert big["warning"].startswith("Объективка не укладывается в одну страницу — сократите")
    assert "«Опыт работы»" in big["warning"]
    # кегль подобранной ступени стоит в каждой строке документа
    data = dx.build(long_data(career=20))[0]
    sizes = set(re.findall(rb'<w:sz w:val="(\d+)"/>', parts(data)["word/document.xml"]))
    assert str(mid["size"] * 2).encode() in sizes


def test_оценка_строк_монотонна():
    text = "заместитель руководителя департамента государственной политики " * 4
    widths = [dx.count_lines(text, w, 14) for w in (100, 200, 300, 468)]
    assert widths == sorted(widths, reverse=True) and widths[-1] >= 2
    assert dx.count_lines(text, 300, 12) <= dx.count_lines(text, 300, 14)
    assert dx.count_lines("", 300, 14) == 1


@pytest.mark.skipif(shutil.which("soffice") is None or shutil.which("pdfinfo") is None,
                    reason="нет LibreOffice — сверка с раскладкой пропущена")
def test_уложенная_объективка_в_раскладке_одна_страница(tmp_path):
    """Сверка оценки с настоящей раскладкой (LibreOffice, шрифт с метриками Times New Roman)."""
    cases = {"short.docx": SAMPLE, "mid.docx": long_data(career=16), "edge.docx": long_data(career=24)}
    fits = {}
    for name, d in cases.items():
        data, fit = dx.build(d, (png(300, 350), "png"))
        (tmp_path / name).write_bytes(data)
        fits[name] = fit
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path)]
                   + [str(tmp_path / n) for n in cases], capture_output=True, timeout=240)
    for name, fit in fits.items():
        pdf = tmp_path / name.replace(".docx", ".pdf")
        pages = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                                                                text=True).stdout).group(1))
        if fit["fits"]:
            assert pages == 1, (name, fit)


def test_api_пересобрать_и_скачать(client, emp):
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    p = wait_done(client, emp, pid)
    r = client.post(f"/api/profiles/{pid}/build", headers=emp)
    assert r.status_code == 200 and r.json()["fit"]["fits"]
    got = client.get(f"/api/profiles/{pid}/docx", headers=emp)
    assert got.status_code == 200 and got.content[:2] == b"PK"
    assert "attachment" in got.headers["content-disposition"]
    # правка — скачивание отдаёт уже новую версию
    d = p["data"]
    d["position"] = "Советник"
    client.put(f"/api/profiles/{pid}", headers=emp, json={"data": d, "rev": client.get(f"/api/profiles/{pid}", headers=emp).json()["rev"]})
    again = client.get(f"/api/profiles/{pid}/docx", headers=emp)
    assert "Советник" in texts(doc_tree(again.content))


def test_фото_загрузить_заменить_убрать(client, emp):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    bad = client.put(f"/api/profiles/{pid}/photo", headers=emp, files={"file": ("a.gif", b"GIF89a....", "image/gif")})
    assert bad.status_code == 400 and "JPEG или PNG" in bad.json()["detail"]
    up = client.put(f"/api/profiles/{pid}/photo", headers=emp, files={"file": ("a.png", png(60, 80), "image/png")})
    assert up.status_code == 200 and up.json()["has_photo"]
    assert portal.sent[-1]["action"] == "Загружено фото объективки"
    client.put(f"/api/profiles/{pid}/photo", headers=emp, files={"file": ("b.png", png(80, 60), "image/png")})
    assert portal.sent[-1]["action"] == "Заменено фото объективки"
    data = client.get(f"/api/profiles/{pid}/docx", headers=emp).content
    ext = doc_tree(data).find(".//wp:extent", NS)
    assert int(ext.get("cy")) == int(4 * 360000 * 60 / 80)
    assert client.delete(f"/api/profiles/{pid}/photo", headers=emp).json()["has_photo"] is False
    assert b"wp:inline" not in parts(client.get(f"/api/profiles/{pid}/docx", headers=emp).content)["word/document.xml"]


def test_сверка_фио_с_реестром_людей(client, emp, monkeypatch):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    # расхождения нет — подтверждать нечего (Н-6)
    monkeypatch.setattr(portal, "check_people", lambda names: {names[0]: {"status": "exact"}})
    r = client.post(f"/api/profiles/{pid}/name-ok", headers=emp, json={"fio": "Кравцов Сергей Сергеевич"})
    assert r.status_code == 409 and "Подтверждать нечего" in r.json()["detail"]["message"]
    monkeypatch.setattr(portal, "check_people", lambda names: {names[0]: {
        "status": "close", "suggestions": [{"name": "Кравцова Светлана Сергеевна", "role": "директор", "organization": "школа"}]}})
    r = client.post(f"/api/profiles/{pid}/build", headers=emp)
    nc = r.json()["detail"]["name_check"]
    assert r.status_code == 409 and nc["status"] == "close" and nc["fio"] == "Кравцов Сергей Сергеевич"
    assert nc["suggestions"] == [{"name": "Кравцова Светлана Сергеевна", "role": "директор", "organization": "школа"}]
    assert client.get(f"/api/profiles/{pid}/docx", headers=emp).status_code == 409
    # подтверждение — только для текущего ФИО; в карточке видно, кто и когда
    assert client.post(f"/api/profiles/{pid}/name-ok", headers=emp, json={"fio": "Другое Имя Отчество"}).status_code == 409
    ok = client.post(f"/api/profiles/{pid}/name-ok", headers=emp, json={"fio": "Кравцов Сергей Сергеевич"})
    assert ok.status_code == 200
    conf = ok.json()["name_confirmed"]
    assert conf["by"] == "Сотрудник" and re.match(r"\d\d\.\d\d\.\d{4} \d\d:\d\d", conf["at"])
    assert ok.json()["name_check"]["status"] == "confirmed" and "Сотрудник" in ok.json()["name_check"]["message"]
    assert portal.sent[-1]["action"] == "Подтверждено ФИО, расходящееся с реестром людей"
    assert "«Кравцова Светлана Сергеевна»" in portal.sent[-1]["details"]
    built = client.post(f"/api/profiles/{pid}/build", headers=emp)
    assert built.status_code == 200 and built.json()["name_check"]["status"] == "confirmed"
    listed = client.get("/api/profiles", headers=emp).json()["items"][0]
    assert listed["name_confirmed"] is True and listed["name_ok_by"] == "Сотрудник"
    # unknown — предложение добавить в реестр (Н-5)
    monkeypatch.setattr(portal, "check_people", lambda names: {names[0]: {"status": "unknown"}})
    p = client.get(f"/api/profiles/{pid}", headers=emp).json()
    p["data"]["fio"] = "Новиков Пётр Ильич"
    client.put(f"/api/profiles/{pid}", headers=emp, json={"data": p["data"], "rev": p["rev"]})
    nc = client.post(f"/api/profiles/{pid}/build", headers=emp).json()["name_check"]
    assert nc["status"] == "unknown" and nc["fio"] == "Новиков Пётр Ильич" and "добавьте в реестр" in nc["message"]
    assert client.get(f"/api/profiles/{pid}", headers=emp).json()["name_confirmed"] is None   # смена ФИО снимает
    monkeypatch.setattr(portal, "check_people", lambda names: {})
    assert client.post(f"/api/profiles/{pid}/build", headers=emp).json()["name_check"]["status"] == "failed"


def test_в_пакет_мероприятия_только_со_сверкой_фио(client, emp, monkeypatch):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": "Кравцов Сергей Сергеевич"}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    monkeypatch.setattr(portal, "check_people", lambda names: {})            # портал не ответил
    assert client.get(f"/api/profiles/{pid}/docx", headers=emp).status_code == 200          # скачать можно
    r = client.get(f"/api/profiles/{pid}/docx?package=1", headers=emp)
    assert r.status_code == 409 and "в пакет мероприятия не кладём" in r.json()["detail"]["message"]
    monkeypatch.setattr(portal, "check_people", lambda names: {names[0]: {"status": "exact"}})
    assert client.get(f"/api/profiles/{pid}/docx?package=1", headers=emp).status_code == 200
