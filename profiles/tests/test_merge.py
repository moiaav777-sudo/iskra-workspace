"""Объединение объективок: содержание по фамилиям с гиперссылками и номерами страниц, порядок,
перенос файлов как есть, понятная ошибка на чужой файл."""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile

import docx as python_docx
import pytest

from app import docx_build as dx
from app import merge
from tests.conftest import answer_json, docx, png, wait_done

W = "{%s}" % dx.NS["w"]


def built(fio: str, photo=True, **over) -> bytes:
    return dx.build(answer_json(fio=fio, **over), (png(30, 40), "png") if photo else None)[0]


def doc_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode("utf8")


def toc(data: bytes) -> list[tuple[str, str, str]]:
    """Строки содержания: (текст, закладка гиперссылки, номер страницы в поле PAGEREF)."""
    root = ET.fromstring(doc_xml(data))
    out = []
    for h in root.iter(W + "hyperlink"):
        text = "".join(t.text or "" for t in h.iter(W + "t"))
        instr = "".join(t.text or "" for t in h.iter(W + "instrText"))
        title = re.sub(r"\d+$", "", text)
        out.append((title, h.get(W + "anchor"), text[len(title):], instr.strip()))
    return out


FIO = ["Кравцов Сергей Сергеевич", "Иванова Мария Петровна", "Петров Олег Игоревич"]


def test_содержание_гиперссылки_на_закладки_и_номера_страниц():
    data, info = merge.merge_files([(f + ".docx", built(f), 1) for f in FIO])
    rows = toc(data)
    assert [r[0] for r in rows] == [f"{i}. {f}" for i, f in enumerate(FIO, 1)]
    assert [r[2] for r in rows] == ["2", "3", "4"] and info["pages"] == [2, 3, 4]
    xml = doc_xml(data)
    for _, anchor, _, instr in rows:
        assert instr == f"PAGEREF {anchor} \\h"
        # закладка — в начале объективки, в абзаце с новой страницей
        m = re.search(rf'<w:p><w:pPr><w:pageBreakBefore/>.*?<w:bookmarkStart w:id="(\d+)" w:name="{anchor}"/>'
                      rf'<w:bookmarkEnd w:id="\1"/></w:p>', xml)
        assert m, anchor
    # закладка стоит перед ФИО своей объективки
    for i, f in enumerate(FIO, 1):
        assert xml.index(f'w:name="_P{i}"') < xml.index(f">{f}<", xml.index("Содержание") + 2000)
    ids = re.findall(r'<w:bookmarkStart w:id="(\d+)"', xml)
    assert len(ids) == len(set(ids))
    pics = re.findall(r'<wp:docPr id="(\d+)"', xml)
    assert len(pics) == 3 and len(set(pics)) == 3          # у каждой объективки своё фото, номера сквозные
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert len([n for n in z.namelist() if n.startswith("word/media/")]) == 3
        rels = z.read("word/_rels/document.xml.rels").decode()
        for rid in re.findall(r'r:embed="([^"]+)"', xml):
            assert f'Id="{rid}"' in rels
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(z.read(name))
    assert len(python_docx.Document(io.BytesIO(data)).tables) == 6


def test_порядок_меняется():
    files = [(f + ".docx", built(f), 1) for f in FIO]
    data, info = merge.merge_files([files[2], files[0], files[1]])
    assert info["titles"] == [FIO[2], FIO[0], FIO[1]]
    xml = doc_xml(data)
    body_start = xml.index('w:name="_P1"')
    assert xml.index(FIO[2], body_start) < xml.index(FIO[0], body_start) < xml.index(FIO[1], body_start)


def test_длинная_объективка_сдвигает_номера():
    long = built("Сидоров Пётр Ильич", career=[{"period": str(1980 + i), "text": "должность " * 20, "src": ""} for i in range(40)])
    data, info = merge.merge_files([("a.docx", long, None), ("b.docx", built(FIO[0]), 1)])
    assert info["pages"][0] == 2 and info["pages"][1] >= 4


def test_чужой_файл_понятная_ошибка():
    stranger = docx(["Справка о ходе реализации программы", "Текст справки"])
    with pytest.raises(merge.MergeError) as e:
        merge.merge_files([("a.docx", built(FIO[0]), 1), ("справка.docx", stranger, None)])
    assert "«справка.docx» не похож на объективку" in str(e.value) and "ФИО" in str(e.value)
    with pytest.raises(merge.MergeError, match="старый формат Word"):
        merge.merge_files([("a.docx", built(FIO[0]), 1), ("b.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 600, None)])
    with pytest.raises(merge.MergeError, match="не документ Word"):
        merge.merge_files([("a.docx", built(FIO[0]), 1), ("c.docx", b"PK not really", None)])
    with pytest.raises(merge.MergeError, match="от 2 до 10"):
        merge.merge_files([("a.docx", built(FIO[0]), 1)])
    with pytest.raises(merge.MergeError, match="от 2 до 10"):
        merge.merge_files([(f"{i}.docx", built(FIO[0]), 1) for i in range(11)])


def test_файл_другой_вёрстки_фото_первым_абзацем_и_опасные_поля():
    """Объективка чужой вёрстки (как образцы): фото первым абзацем, ФИО второй строкой; поле
    INCLUDEPICTURE с адресом и ссылка file:// не переносятся, видимое остаётся."""
    field = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> INCLUDEPICTURE '
             '"\\\\server\\share\\a.jpg" \\* MERGEFORMATINET </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
             '<w:r><w:t>видимое</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
             '<w:p><w:hyperlink r:id="rIdBad"><w:r><w:t>ссылка</w:t></w:r></w:hyperlink></w:p>')
    rel = ('<Relationship Id="rIdBad" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
           'Target="file://server/x" TargetMode="External"/>')
    other = docx(["Иванова Мария Петровна", "родилась 1 мая 1980 г.", "Образование:", "2002 – МГУ"], image=png(10, 12),
                 extra_body=field, rels_extra=rel)
    data, info = merge.merge_files([("other.docx", other, None), ("a.docx", built(FIO[0]), 1)])
    assert info["titles"] == ["Иванова Мария Петровна", FIO[0]]
    xml = doc_xml(data)
    assert "INCLUDEPICTURE" not in xml and "server" not in xml and "видимое" in xml and "ссылка" in xml
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "file://" not in z.read("word/_rels/document.xml.rels").decode()
        assert "word/media/m1_image1.png" in z.namelist()


def test_распознавание_фио():
    from app.model import looks_like_fio
    for ok in ("Кравцов Сергей Сергеевич", "КРАВЦОВ Сергей Сергеевич", "Кравцов С.С.", "Кравцов С. С.",
               "Салтыков-Щедрин Михаил Евграфович", "Алиев Гейдар Ибрагим оглы"):
        assert looks_like_fio(ok), ok
    for bad in ("Справка о ходе реализации программы", "Объективка", "Kravtsov Sergey", "ОБЪЕКТИВКА НА ЧЕЛОВЕКА ТАКОГО-ТО",
                "1. Кравцов Сергей Сергеевич", ""):
        assert not looks_like_fio(bad), bad


def test_api_объединение_из_реестра_и_файлов(client, emp):
    ids = []
    for f in FIO[:2]:
        pid = client.post("/api/profiles", json={"fio": f}, headers=emp).json()["id"]
        wait_done(client, emp, pid)
        ids.append(pid)
    # «Искра» в подмене отдаёт одно и то же ФИО — в карточке второй правим на своё
    p = client.get(f"/api/profiles/{ids[1]}", headers=emp).json()
    p["data"]["fio"] = FIO[1]
    client.put(f"/api/profiles/{ids[1]}", headers=emp, json={"data": p["data"], "rev": p["rev"]})
    order = [{"kind": "profile", "id": ids[1]}, {"kind": "file", "index": 0}, {"kind": "profile", "id": ids[0]}]
    r = client.post("/api/merge", headers=emp, data={"order": json.dumps(order), "title": "Объективки к совещанию"},
                    files=[("files", ("petrov.docx", built(FIO[2]), "application/octet-stream"))])
    assert r.status_code == 200, r.text
    m = r.json()["merge"]
    assert [it["title"] for it in m["items"]] == [FIO[1], FIO[2], FIO[0]]
    assert [it["page"] for it in m["items"]] == [2, 3, 4]
    got = client.get(f"/api/merges/{m['id']}/docx", headers=emp)
    assert got.status_code == 200 and [r[0] for r in toc(got.content)] == [f"1. {FIO[1]}", f"2. {FIO[2]}", f"3. {FIO[0]}"]
    from app import portal
    assert portal.sent[-1]["action"] == "Объединены объективки"


def test_api_чужой_файл_и_лимиты(client, emp):
    pid = client.post("/api/profiles", json={"fio": FIO[0]}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    order = [{"kind": "profile", "id": pid}, {"kind": "file", "index": 0}]
    r = client.post("/api/merge", headers=emp, data={"order": json.dumps(order)},
                    files=[("files", ("справка.docx", docx(["Справка о ходе дел", "текст"]), "application/octet-stream"))])
    assert r.status_code == 400 and "не похож на объективку" in r.json()["detail"]
    one = client.post("/api/merge", headers=emp, data={"order": json.dumps(order[:1])})
    assert one.status_code == 400 and "от 2 до 10" in one.json()["detail"]


def test_api_сверка_фио_в_объединении(client, emp, monkeypatch):
    from app import portal
    pid = client.post("/api/profiles", json={"fio": FIO[0]}, headers=emp).json()["id"]
    wait_done(client, emp, pid)
    order = [{"kind": "profile", "id": pid}, {"kind": "file", "index": 0}]
    files = [("files", ("ivanova.docx", built(FIO[1]), "application/octet-stream"))]

    def registry(names):
        return {n: ({"status": "close", "suggestions": [{"name": "Иванова Мария Павловна"}]} if n == FIO[1]
                    else {"status": "exact"}) for n in names}
    monkeypatch.setattr(portal, "check_people", registry)
    m = client.post("/api/merge", headers=emp, data={"order": json.dumps(order)}, files=files).json()["merge"]
    assert [it["check"] for it in m["items"]] == ["exact", "close"]
    assert m["items"][1]["suggestions"] == ["Иванова Мария Павловна"]
    assert client.get(f"/api/merges/{m['id']}/docx", headers=emp).status_code == 200
    r = client.get(f"/api/merges/{m['id']}/docx?package=1", headers=emp)
    assert r.status_code == 409 and "в пакет мероприятия не кладём" in r.json()["detail"]
    # карточка из реестра с расхождением — объединение не собирается, пока человек не решит
    monkeypatch.setattr(portal, "check_people", lambda names: {n: {"status": "close", "suggestions": [{"name": "X Y Z"}]} for n in names})
    bad = client.post("/api/merge", headers=emp, data={"order": json.dumps(order)}, files=files)
    assert bad.status_code == 409 and "расходится с реестром людей" in bad.json()["detail"]["message"]


# ── враждебный, но правильно оформленный XML (ревью RC-2026-10-05-8, Н-2, Н-4) ──

def _patched(fio: str, part: str, old: bytes, new: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(built(fio)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for i in src.infolist():
            data = src.read(i)
            if i.filename == part:
                assert old in data, part
                data = data.replace(old, new, 1)
            out.writestr(i.filename, data)
    return buf.getvalue()


def _numbering(n: int) -> bytes:
    return (b'<?xml version="1.0"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            + b'<w:num w:numId="1"/>' * n + b'<w:abstractNum w:abstractNumId="1"/>' * n + b"</w:numbering>")


HOSTILE = {
    "стили": ("word/styles.xml", b"</w:styles>", b'<w:style w:styleId="x"/>' * 50_000 + b"</w:styles>"),
    "разметка страниц": ("word/document.xml", b"<w:body>", b"<w:body>" + b"<w:p><w:pPr><w:sectPr/></w:pPr></w:p>" * 20_000),
    "поля": ("word/document.xml", b"<w:body>", b"<w:body><w:p><w:r>" + b"<w:instrText/>" * 50_000 + b"</w:r></w:p>"),
    "простые поля": ("word/document.xml", b"<w:body>", b"<w:body><w:p>" + b'<w:fldSimple w:instr="PAGE"/>' * 30_000 + b"</w:p>"),
    "закладки": ("word/document.xml", b"<w:body>", b"<w:body><w:p>" + b"".join(
        b'<w:bookmarkStart w:id="%d" w:name="b%d"/><w:bookmarkEnd w:id="%d"/>' % (i, i, i) for i in range(3000)) + b"</w:p>"),
    "абзацы и таблицы": ("word/document.xml", b"<w:body>", b"<w:body>" + b"<w:p/>" * 20_000
                          + b'<w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl>' * 3000),
    "связи": ("word/_rels/document.xml.rels", b"</Relationships>", b"<Relationship " + b"a" * 40_000 + b"/></Relationships>"),
}


@pytest.mark.parametrize("what", list(HOSTILE), ids=["styles", "sectpr", "instrtext", "fldsimple", "bookmarks",
                                                      "paras_tables", "rels"])
def test_враждебный_правильный_xml_объединяется_быстро(what):
    part, old, new = HOSTILE[what]
    data = _patched("Иванова Мария Петровна", part, old, new)
    if what == "стили":                                  # стиль должен быть нужен телу — иначе его не переносят
        data = _patched_more(data)
    import time
    refused = []
    started = time.monotonic()
    try:
        out, info = merge.merge_files([("a.docx", built(FIO[0]), 1), ("x.docx", data, None)])
        assert info["titles"][1] == "Иванова Мария Петровна"
        ET.fromstring(zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml"))
    except merge.MergeError as error:
        refused.append((what, str(error)))               # отказ тоже годится — лишь бы быстро
    assert time.monotonic() - started < 2.0, what
    assert what not in ("стили", "разметка страниц", "закладки", "абзацы и таблицы", "связи") or not refused, refused


def _patched_more(data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for i in src.infolist():
            d = src.read(i)
            if i.filename == "word/document.xml":
                d = d.replace(b"<w:body>", b'<w:body><w:p><w:pPr><w:pStyle w:val="x"/></w:pPr></w:p>', 1)
            out.writestr(i.filename, d)
    return buf.getvalue()


def test_списки_из_враждебного_numbering_быстро():
    import time
    data = _patched("Иванова Мария Петровна", "word/document.xml", b"<w:body>",
                    b'<w:body><w:p><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:p>')
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for i in src.infolist():
            out.writestr(i.filename, _numbering(50_000) if i.filename == "word/numbering.xml" else src.read(i))
    started = time.monotonic()
    out, _ = merge.merge_files([("a.docx", built(FIO[0]), 1), ("x.docx", buf.getvalue(), None)])
    assert time.monotonic() - started < 2.0
    assert b'w:numId="1"' in zipfile.ZipFile(io.BytesIO(out)).read("word/numbering.xml")


def test_вложенные_поля_глубже_предела_отказ():
    deep = b"<w:p><w:r>" + b'<w:fldChar w:fldCharType="begin"/>' * (merge.MAX_FIELD_DEPTH + 5) + b"</w:r></w:p>"
    data = _patched("Иванова Мария Петровна", "word/document.xml", b"<w:body>", b"<w:body>" + deep)
    with pytest.raises(merge.MergeError, match="вложены слишком глубоко"):
        merge.merge_files([("a.docx", built(FIO[0]), 1), ("x.docx", data, None)])


def test_закладки_и_ссылки_переименованы_одним_проходом():
    body = (b'<w:p><w:bookmarkStart w:id="5" w:name="_Toc9"/><w:bookmarkEnd w:id="5"/>'
            b'<w:hyperlink w:anchor="_Toc9"><w:r><w:t>link</w:t></w:r></w:hyperlink>'
            b'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGEREF _Toc9 \\h </w:instrText></w:r>'
            b'<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>3</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    data = _patched("Иванова Мария Петровна", "word/document.xml", b"<w:sectPr", body + b"<w:sectPr")
    out, _ = merge.merge_files([("a.docx", built(FIO[0]), 1), ("x.docx", data, None)])
    xml = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode()
    assert 'w:name="_M2_Toc9"' in xml and 'w:anchor="_M2_Toc9"' in xml and "PAGEREF _M2_Toc9" in xml
    assert 'w:anchor="_Toc9"' not in xml
