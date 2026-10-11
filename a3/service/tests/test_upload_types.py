"""Справку можно загрузить любым типовым файлом (решение пользователя 05.10): Word, Excel, PowerPoint,
PDF, RTF, ODT, текст — формат по содержимому, чтение общим модулем `docs_common`. На VPS нет
pdftotext — PDF читает PyMuPDF. .doc/.xls — через LibreOffice (нет — пропуск), PDF — pdftotext или
PyMuPDF (нет обоих — пропуск). Файлы синтетические (`doc_samples.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import doc_samples as S  # noqa: E402

from app import docs_common, extract, spark  # noqa: E402
from tests.test_contract import ADMIN, SHEET, answer, client, done  # noqa: E402,F401 — фикстура client

LIBRARY = HERE.parent / "app" / "static" / "library.html"
ROWS = [["Показатель", "2025"], ["Школы региона", "512"], ["Отремонтировано", "48"]]


def test_xlsx_и_pptx():
    assert "Школы региона" in extract.from_file("Показатели.xlsx", S.xlsx(ROWS))
    assert "Отремонтировано 48" in extract.from_file("Доклад.pptx", S.pptx([["Отремонтировано 48 школ"]]))


def test_формат_по_содержимому():
    assert "512" in extract.from_file("справка.docx", S.xlsx(ROWS))       # Excel под именем .docx
    with pytest.raises(extract.Unsupported):
        extract.from_file("справка.docx", b"not a zip")
    with pytest.raises(extract.Unsupported):
        extract.from_file("program.exe", b"MZ\x90\x00" + b"\x00" * 600)


@pytest.mark.skipif(not S.pdf_reader(), reason="нет ни pdftotext, ни PyMuPDF")
def test_pdf():
    assert "renovated: 48" in extract.from_file("Справка.pdf", S.pdf(["Schools: 512, renovated: 48."]))


def test_doc_и_xls_старого_формата():
    doc = S.legacy("Справка о ремонте школ: отремонтировано 48 школ.".encode("utf-8"), "txt", "doc")
    xls = S.legacy("Показатель,Значение\nШколы региона,512\n".encode("utf-8"), "csv", "xls")
    if doc is None or xls is None:
        pytest.skip("нет LibreOffice")
    assert "48 школ" in extract.from_file("Справка.doc", doc)
    assert "512" in extract.from_file("Таблица.xls", xls)


def test_excel_справка_уходит_в_разбор(client, monkeypatch):
    seen = []
    monkeypatch.setattr(spark, "draft", lambda text: seen.append(text) or answer(SHEET))
    rows = ROWS + [["Пояснение", S.FILLER]] * 3
    started = client.post("/api/spark/draft", headers=ADMIN, files={"file": ("Показатели.xlsx", S.xlsx(rows))})
    assert done(client, started)["status"] == "done"
    assert "Школы региона" in seen[0]


def test_accept_общий():
    html = LIBRARY.read_text(encoding="utf-8")
    assert f'<input type="file" id="file" accept="{docs_common.ACCEPT}" hidden>' in html
