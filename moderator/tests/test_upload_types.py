"""Поля загрузки принимают любые типовые файлы (решение пользователя 05.10): Word, Excel, PowerPoint,
PDF, RTF, ODT, текст — формат по содержимому, чтение общим модулем `docs_common`. .doc/.xls —
через LibreOffice (нет — пропуск), PDF — pdftotext или PyMuPDF (нет обоих — пропуск). Файлы
синтетические (`doc_samples.py`), «Искра» подменена.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import docs_common, extract, llm_extract
from tests import doc_samples as S
from tests.test_api import audit, client, create, login, people  # noqa: F401 — фикстуры

STATIC = Path(__file__).resolve().parent.parent / "static"
ROWS = [["Вопрос", "Докладчик"], ["О ремонте школ", "Иванов И.И."], ["Отремонтировано", "48"]]


def test_xlsx_и_pptx_читаются():
    text = extract.extract_bytes(S.xlsx(ROWS), "Повестка.xlsx")
    assert "О ремонте школ" in text and "Иванов И.И." in text
    text = extract.extract_bytes(S.pptx([["О ремонте школ", "Отремонтировано 48 школ"]]), "Доклад.pptx")
    assert "Отремонтировано 48 школ" in text


def test_формат_по_содержимому_а_не_по_имени():
    raw = S.xlsx(ROWS)
    assert extract.kind_of(raw, "повестка.docx") == "xlsx"
    assert "Иванов И.И." in extract.extract_bytes(raw, "повестка.docx")
    assert "не похож на .docx" in extract.problem("повестка.docx", b"just text")
    assert "не принимается" in extract.problem("virus.exe", b"MZ")


@pytest.mark.skipif(not S.pdf_reader(), reason="нет ни pdftotext, ни PyMuPDF")
def test_pdf_читается():
    assert "renovated: 48" in extract.extract_bytes(S.pdf(["Schools total: 512, renovated: 48."]), "Справка.pdf")


def test_doc_и_xls_старого_формата():
    doc = S.legacy("Повестка совещания\nО ремонте школ — Иванов И.И.".encode("utf-8"), "txt", "doc")
    xls = S.legacy("Вопрос,Докладчик\nО ремонте школ,Иванов И.И.\n".encode("utf-8"), "csv", "xls")
    if doc is None or xls is None:
        pytest.skip("нет LibreOffice")
    assert "О ремонте школ" in extract.extract_bytes(doc, "Повестка.doc")
    assert "Иванов И.И." in extract.extract_bytes(xls, "Повестка.xls")


def test_извлечение_и_материалы_принимают_excel(client):
    login(client, "alice")
    r = client.post("/api/extract", files={"file": ("Повестка.xlsx", S.xlsx(ROWS))})
    assert r.status_code == 200, r.text
    assert "Иванов И.И." in r.json()["text"]
    mid = create(client)
    r = client.post(f"/api/meetings/{mid}/materials/upload",
                    files=[("files", ("Цифры.xlsx", S.xlsx(ROWS))), ("files", ("Доклад.pptx", S.pptx([["Итоги: 48"]]))),
                           ("files", ("virus.exe", b"MZ\x90\x00\x00"))])
    files = r.json()["files"]
    assert "Иванов И.И." in files[0]["text"] and "Итоги: 48" in files[1]["text"]
    assert "error" in files[2]


def test_в_искру_excel_уходит_текстом(client, monkeypatch):
    """«Искра» файлом принимала Word, PowerPoint, PDF и текст; Excel и старые форматы — её текстом."""
    seen = []

    def fake(data, name, hint=""):
        seen.append((name, data))
        return {"doc_type": "повестка", "confidence": "высокая", "summary": "", "data": {"agenda": []},
                "schema_problems": []}
    monkeypatch.setattr(llm_extract, "extract_with_file", fake)
    login(client, "alice")
    mid = create(client, date="14.10.2026")
    r = client.post("/api/inbox", data={"meeting": mid}, files={"file": ("Повестка.xlsx", S.xlsx(ROWS))})
    assert r.status_code == 200, r.text
    assert seen[-1][0] == "Повестка.xlsx.txt" and "Иванов И.И." in seen[-1][1].decode("utf-8")
    r = client.post(f"/api/analyze-file?meeting_id={mid}", files={"file": ("Доклад.pptx", S.pptx([["О ремонте"]]))})
    assert r.status_code == 200, r.text
    assert seen[-1][0] == "Доклад.pptx"           # знакомый «Искре» формат — как есть


def test_accept_общий_во_всех_полях():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert f"const DOC_ACCEPT = '{docs_common.ACCEPT}';" in js
    assert "const MAT_ACCEPT = DOC_ACCEPT + ',.html,.htm';" in js       # материалы — ещё и готовая страница
    for field in ("imp-file", "ord-file"):
        assert re.search(r'id="' + field + r'" accept="\' \+ DOC_ACCEPT \+ \'"', js), field
    assert len(re.findall(r'accept="\' \+ MAT_ACCEPT \+ \'"', js)) == 2
    assert not re.search(r'accept="\.docx', js), "старых списков типов не осталось"
    version = re.search(r"static/app\.js\?v=(\d+)", (STATIC / "index.html").read_text(encoding="utf-8"))
    assert int(version.group(1)) >= 58
