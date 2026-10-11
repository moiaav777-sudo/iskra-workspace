"""Выгрузка в Word: вёрстка должна совпадать с образцом пользователя."""
from __future__ import annotations

from tests.conftest import add_line, put_lines

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCS = Path(__file__).resolve().parent.parent / "docs"
SAMPLE = DOCS / "Пример оформления.docx"


def _paragraphs(data: bytes) -> list[ET.Element]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    return body.findall(W + "p")


def _section(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    sect = body.find(W + "sectPr")
    out = {}
    for tag in ("pgSz", "pgMar"):
        node = sect.find(W + tag)
        if node is not None:
            out.update({k.split("}")[1]: v for k, v in node.attrib.items()})
    return out


def _text(para: ET.Element) -> str:
    return "".join(t.text or "" for t in para.iter(W + "t"))


def _make(client, emp, bullets):
    created = client.post("/api/theses", json={"topic": "О сиротах", "minutes": 7},
                          headers=emp).json()
    tid, rev = created["thesis"]["id"], created["rev"]
    for bullet in bullets:
        body = add_line(client, emp, tid, **{**bullet, "rev": rev}).json()
        rev = body["rev"]
    return tid, rev


def test_страница_и_поля_как_в_образце(client, emp):
    # Образец — рабочий документ пользователя и намеренно лежит вне git (.gitignore):
    # в чистой копии репозитория, где rc.sh гоняет тесты, его нет. Вёрстка с него уже
    # снята в app/template.docx, остальные тесты этого файла проверяют её без образца.
    if not SAMPLE.is_file():
        pytest.skip(f"нет образца docs/{SAMPLE.name}: он вне git, сравнивать не с чем")
    tid, _ = _make(client, emp, [{"text": "Первый тезис."}])
    data = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content
    assert _section(data) == _section(SAMPLE.read_bytes())


def test_буллиты_кегль_маркер_и_выключка(client, emp):
    tid, _ = _make(client, emp, [{"text": "Первый тезис."}, {"text": "Второй.", "emphasis": "strong"}])
    data = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content
    paras = _paragraphs(data)
    assert [_text(p) for p in paras] == ["Первый тезис.", "Второй."]
    for para in paras:
        ppr = para.find(W + "pPr")
        assert ppr.find(W + "numPr") is not None, "буллит должен быть в маркированном списке"
        assert ppr.find(W + "jc").get(W + "val") == "both"
        assert ppr.find(W + "spacing").get(W + "after") == "120"
    sizes = {s.get(W + "val") for s in paras[0].iter(W + "sz")}
    assert sizes == {"32"}, "кегль 16 пт, как в образце"
    assert paras[1].find(f".//{W}b") is not None, "акцент — жирным"


def test_справочное_идёт_синим_курсивом(client, emp):
    tid, _ = _make(client, emp, [{"text": "Например, случай из мая.", "emphasis": "aside"}])
    para = _paragraphs(client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content)[0]
    assert para.find(f".//{W}i") is not None
    assert para.find(f".//{W}color").get(W + "val") == "0070C0"


def test_недостающая_фактура_красным_и_отдельным_блоком(client, emp):
    tid, _ = _make(client, emp, [
        {"text": "Обеспечено жильём [ТРЕБУЕТСЯ ФАКТУРА: число семей] семей."}])
    data = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content
    texts = [_text(p) for p in _paragraphs(data)]
    assert any("ЧТО НЕОБХОДИМО ДОУТОЧНИТЬ" in t for t in texts)
    assert any("число семей" in t for t in texts[1:]), "предложение повторяется в блоке"
    colors = {c.get(W + "val") for p in _paragraphs(data) for c in p.iter(W + "color")}
    assert "C00000" in colors, "маркер идёт красным"


def test_слово_наполовину_латиницей_в_документ_не_уходит(client, emp):
    """Задача 21.09: «экзаменуемых не приступают к zadанию вовсе». У «z» и «d» русских
    двойников нет — угадывать за человека нельзя, документ ждёт его правки."""
    tid, rev = _make(client, emp, [{"text": "Треть экзаменуемых не приступают к zadанию вовсе."}])
    r = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp)
    assert r.status_code == 409
    assert r.json()["detail"]["mixed_words"] == [{"word": "zadанию", "guess": "заданию", "where": ["текст"]}]
    assert client.get(f"/api/theses/{tid}/moderator-payload", headers=emp).status_code == 409

    line = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]["bullets"][0]
    put_lines(client, emp, tid, [{"id": line["id"],
                                  "text": "Треть экзаменуемых не приступают к заданию вовсе."}], rev)
    assert client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).status_code == 200


def test_буквы_двойники_исправляются_сами(client, emp):
    """«IТ» с русской «Т» и «прoграмма» с латинской «o» на глаз неотличимы от правильных —
    автор имел в виду их, поэтому меняем молча и документ не держим."""
    tid, _ = _make(client, emp, [{"text": "IТ-классы и прoграмма развития."}])
    thesis = client.get(f"/api/theses/{tid}", headers=emp).json()["thesis"]
    assert thesis["bullets"][0]["text"] == "IT-классы и программа развития."
    assert thesis["mixed_words"] == []
    data = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content
    assert _text(_paragraphs(data)[0]) == "IT-классы и программа развития."


def test_цифры_жёлтым_больше_не_красятся(client, emp):
    """Проверка чисел убрана (решение 21.09) — в документе нет ни заливки, ни пояснений."""
    tid, _ = _make(client, emp, [{"text": "Клубы созданы в 80 процентах школ."}])
    paras = _paragraphs(client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp).content)
    assert len(paras) == 1 and paras[0].find(f".//{W}highlight") is None


def test_имя_файла_по_теме(client, emp):
    tid, _ = _make(client, emp, [{"text": "Первый."}])
    r = client.get(f"/api/theses/{tid}/export/theses.docx", headers=emp)
    assert "%D0%A2%D0%B5%D0%B7%D0%B8%D1%81%D1%8B" in r.headers["content-disposition"]
