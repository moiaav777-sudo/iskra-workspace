"""Приложить к тезисам можно любой типовой файл (решение пользователя 05.10): Word, Excel,
PowerPoint, PDF, rtf, odt, текст. Формат — по содержимому; .doc/.xls — через LibreOffice (нет —
пропуск), PDF — pdftotext или PyMuPDF (нет обоих — пропуск). Файлы синтетические (`doc_samples.py`).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import doc_samples as S  # noqa: E402

from app import doc_text, docs_common  # noqa: E402

APP_JS = HERE.parent / "static" / "app.js"
INDEX = HERE.parent / "static" / "index.html"


def _thesis(client, emp):
    created = client.post("/api/theses", json={"topic": "О ремонте школ", "minutes": 5}, headers=emp).json()
    return created["thesis"]["id"], created["rev"]


def _upload(client, emp, tid, rev, name, raw):
    return client.post(f"/api/theses/{tid}/sources/file?rev={rev}", headers=emp,
                       files={"file": (name, raw, "application/octet-stream")})


def test_xlsx_читается():
    text = doc_text.extract("Показатели.xlsx", S.xlsx([["Показатель", "2025"], ["Школы", "512"]]))
    assert "Школы" in text and "512" in text


def test_pptx_читается():
    text = doc_text.extract("Доклад.pptx", S.pptx([["О ремонте школ", "Отремонтировано 48 школ"]]))
    assert "Отремонтировано 48 школ" in text


def test_формат_по_содержимому():
    raw = S.xlsx([["Школы", "512"]])
    assert doc_text.kind_of("справка.doc", raw) == "xlsx"
    assert "512" in doc_text.extract("справка.doc", raw)


@pytest.mark.skipif(not S.pdf_reader(), reason="нет ни pdftotext, ни PyMuPDF")
def test_pdf_читается():
    assert "renovated: 48" in doc_text.extract("Справка.pdf", S.pdf(["Schools total: 512, renovated: 48."]))


def test_doc_старого_формата():
    raw = S.legacy("Справка о ремонте школ: отремонтировано 48 школ.".encode("utf-8"), "txt", "doc")
    if raw is None:
        pytest.skip("нет LibreOffice")
    assert "48 школ" in doc_text.extract("Справка.doc", raw)


def test_xls_старого_формата():
    raw = S.legacy("Показатель,Значение\nШколы,512\n".encode("utf-8"), "csv", "xls")
    if raw is None:
        pytest.skip("нет LibreOffice")
    assert "512" in doc_text.extract("Таблица.xls", raw)


def test_чужой_файл_понятный_отказ():
    with pytest.raises(doc_text.BadDocument, match="не поддерживается"):
        doc_text.extract("program.exe", b"MZ\x90\x00" + b"\x00" * 600)


def test_загрузка_excel_и_презентации(client, emp):
    tid, rev = _thesis(client, emp)
    r = _upload(client, emp, tid, rev, "Показатели.xlsx", S.xlsx([["Школы", "512"], ["Отремонтировано", "48"]]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["thesis"]["sources"][0]["kind"] == "xlsx"
    r = _upload(client, emp, tid, body["rev"], "Доклад.pptx", S.pptx([["Итоги: 48 школ"]]))
    assert r.status_code == 200, r.text
    assert [s["kind"] for s in r.json()["thesis"]["sources"]] == ["xlsx", "pptx"]


def test_accept_общий():
    js = APP_JS.read_text(encoding="utf-8")
    assert f"const ACCEPT_DOCS = '{docs_common.ACCEPT}';" in js
    assert re.search(r'static/app\.js\?v=(\d+)', INDEX.read_text(encoding="utf-8")).group(1) != "4", \
        "версия скрипта поднята — браузер не возьмёт старый из кэша"


def test_чтение_файла_не_держит_сервис(client, emp, monkeypatch):
    """Ревьюер RC-2026-10-05-5, К-3: чтение документа (в т.ч. LibreOffice до 120 с) шло прямо в цикле
    событий — один .doc держал весь сервис вместе с /healthz. Теперь чтение в пуле потоков: пока
    медленный файл разбирается, /healthz отвечает сразу."""
    import threading
    import time
    tid, rev = _thesis(client, emp)
    started = threading.Event()

    def slow_extract(name, data):
        started.set()
        time.sleep(1.5)
        return "текст из медленного файла"
    monkeypatch.setattr(doc_text, "extract", slow_extract)
    result = {}
    t = threading.Thread(target=lambda: result.update(r=_upload(client, emp, tid, rev, "медленный.docx", b"x" * 10)))
    t.start()
    assert started.wait(5)
    t0 = time.perf_counter()
    assert client.get("/healthz").status_code == 200
    assert time.perf_counter() - t0 < 0.5           # не ждал окончания чтения файла
    t.join(10)
    assert result["r"].status_code == 200, result["r"].text
