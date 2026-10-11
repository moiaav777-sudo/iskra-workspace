"""Общий модуль чтения документов (`app/docs_common.py` = `portal/shared/docs_common.py`).

Файлы синтетические: docx/xlsx/pptx собираются здесь, старые форматы (.doc, .xls, .ppt, .odt, .rtf)
и PDF делает из них LibreOffice, если он есть (на сервере есть); без него эти тесты пропускаются.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import time
import zipfile
import zlib

import pytest

from app import docs_common as D
from tests.conftest import docx

HAS_SOFFICE = D.soffice() is not None
needs_soffice = pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice")


def xlsx(rows_by_sheet: dict[str, list[list]]) -> bytes:
    """Книга Excel: строки — в общих строках (t="s") и числами."""
    shared, sheets = [], []
    for title, rows in rows_by_sheet.items():
        xml_rows = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        sheets.append((title, "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"
                       f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{t}" sheetId="{i}" r:id="rId{i}"/>' for i, (t, _) in enumerate(sheets, 1))
                   + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        for i, (_, xml) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
    return buf.getvalue()


def pptx(slides: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        for i, lines in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a">'
                       + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines) + "</p:sld>")
    return buf.getvalue()


def soffice_convert(data: bytes, src_ext: str, to: str, tmp_path, infilter: str = "") -> bytes:
    src = tmp_path / f"in.{src_ext}"
    src.write_bytes(data)
    subprocess.run([D.soffice(), "--headless", f"-env:UserInstallation={(tmp_path / 'prof').as_uri()}"]
                   + ([f"--infilter={infilter}"] if infilter else [])
                   + ["--convert-to", to, "--outdir", str(tmp_path), str(src)], capture_output=True, timeout=180)
    return (tmp_path / f"in.{to.split(':')[0]}").read_bytes()


def broken_deflate(data: bytes, part: str = "word/document.xml") -> bytes:
    """Корректный zip, но поток deflate внутри части испорчен (ревью RC-2026-10-05-2, Н-1).

    Архив сперва пересобирается со сжатием (ZIP_DEFLATED): у несжатого порча даёт лишь «Bad CRC-32»,
    и ветка zlib.error осталась бы непроверенной (ревью RC-2026-10-05-4, Н-4)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            z.writestr(info.filename, src.read(info), compress_type=zipfile.ZIP_DEFLATED)
    data = buf.getvalue()
    z = zipfile.ZipFile(io.BytesIO(data))
    info = z.getinfo(part)
    assert info.compress_type == zipfile.ZIP_DEFLATED
    raw = bytearray(data)
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[start] = (raw[start] & 0xF8) | 0x06              # заголовок блока deflate: недопустимый тип блока
    for i in range(start + 1, start + min(info.compress_size, 24)):
        raw[i] ^= 0x5A
    return bytes(raw)


def test_битый_deflate_это_zlib_error():
    """Сама порча должна давать именно zlib.error — иначе тест ниже проверял бы не ту ветку."""
    bad = broken_deflate(DOC_LONG)
    with pytest.raises(zlib.error):
        zipfile.ZipFile(io.BytesIO(bad)).read("word/document.xml")


DOC = docx(["Кравцов Сергей Сергеевич", "Министр просвещения Российской Федерации"])
DOC_LONG = docx(["Кравцов Сергей Сергеевич"] + [f"Строка биографии номер {i}, министр просвещения" for i in range(60)])


def test_интерфейс_модуля():
    assert D.SUPPORTED_EXT == (".docx", ".doc", ".xlsx", ".xls", ".pdf", ".pptx", ".ppt", ".rtf", ".odt", ".txt", ".md", ".csv")
    assert D.ACCEPT == ",".join(D.SUPPORTED_EXT)
    assert issubclass(D.DocsError, ValueError)


def test_формат_по_содержимому_а_не_по_расширению():
    assert D.sniff(DOC, "справка.pdf") == "docx"
    assert D.sniff(xlsx({"Л": [["а"]]}), "x.docx") == "xlsx"
    assert D.sniff(pptx([["слайд"]]), "x.bin") == "pptx"
    assert D.sniff(b"%PDF-1.4 ...", "x.docx") == "pdf"
    assert D.sniff(b"{\\rtf1 ...", "x.doc") == "rtf"
    assert D.sniff("ФИО;должность".encode(), "a.csv") == "csv"
    assert D.sniff(b"<html><body>x</body></html>", "a.doc") == "html"
    assert D.sniff(b"\x00\x01\x02binary", "a.xyz") == ""


def test_docx_xlsx_pptx_своими_силами():
    assert D.extract_text(DOC, "a.docx") == "Кравцов Сергей Сергеевич\nМинистр просвещения Российской Федерации"
    book = xlsx({"Кадры": [["ФИО", "Должность", "Год"], ["Кравцов Сергей", "министр", 2020], [None, "заместитель", None]],
                 "Награды": [["Орден Почёта"]]})
    assert D.extract_text(book, "кадры.xlsx") == ("Лист «Кадры»\nФИО\tДолжность\tГод\nКравцов Сергей\tминистр\t2020\n"
                                                   "\tзаместитель\nЛист «Награды»\nОрден Почёта")
    assert D.extract_text(pptx([["Кравцов С.С.", "Министр"], [], ["Награды"]]), "a.pptx") == \
        "Слайд 1.\nКравцов С.С.\nМинистр\nСлайд 3.\nНаграды"


def test_текст_md_csv_и_кодировки():
    assert D.extract_text("Кравцов — министр".encode("cp1251"), "a.txt") == "Кравцов — министр"
    assert D.extract_text("﻿# Кравцов\n\n*министр*".encode(), "a.md") == "# Кравцов\n*министр*"
    assert D.extract_text("ФИО;Должность\nКравцов;министр".encode(), "a.csv") == "ФИО;Должность\nКравцов;министр"


def test_битые_и_чужие_файлы_понятная_ошибка():
    with pytest.raises(D.DocsError, match="повреждён"):
        D.extract_text(broken_deflate(DOC_LONG), "справка.docx")
    with pytest.raises(D.DocsError, match="повреждён"):
        D.extract_text(b"PK\x03\x04 truncated", "a.docx")
    with pytest.raises(D.DocsError, match="повреждён: внутри не документ .docx"):
        D.extract_text(b"broken text", "bad.docx")
    with pytest.raises(D.DocsError, match="не поддерживается"):
        D.extract_text(b"\x00\x01\x02\x03binary", "a.xyz")
    with pytest.raises(D.DocsError, match="не нашлось текста"):
        D.extract_text(b"   ", "a.txt")
    with pytest.raises(D.DocsError, match="защищён паролем"):
        D.extract_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "EncryptedPackage".encode("utf-16-le") + b"\0" * 600, "a.docx")


@needs_soffice
@pytest.mark.parametrize("fmt", ["doc", "rtf", "odt"])
def test_старые_форматы_word_через_libreoffice(tmp_path, fmt):
    data = soffice_convert(DOC, "docx", fmt, tmp_path)
    assert D.sniff(data, "файл") == fmt
    assert D.extract_text(data, f"a.{fmt}") == "Кравцов Сергей Сергеевич\nМинистр просвещения Российской Федерации"
    out = D.to_docx(data, f"a.{fmt}")
    assert D.sniff(out, "x") == "docx"


@needs_soffice
def test_xls_и_ppt_через_libreoffice(tmp_path):
    xls = soffice_convert(xlsx({"Кадры": [["ФИО", "Должность"], ["Кравцов", "министр"]]}), "xlsx", "xls", tmp_path)
    assert D.sniff(xls, "a") == "xls"
    assert "Кравцов\tминистр" in D.extract_text(xls, "a.xls")


@needs_soffice
def test_ppt_через_libreoffice(tmp_path):
    # презентацию делает сам LibreOffice: PDF → импорт в Impress → .ppt
    ppt = soffice_convert(D.convert(DOC, "a.docx", "pdf"), "pdf", "ppt", tmp_path, infilter="impress_pdf_import")
    assert D.sniff(ppt, "x") == "ppt"
    text = D.extract_text(ppt, "a.ppt")
    assert "Кравцов Сергей Сергеевич" in text


@needs_soffice
def test_pdf_pdftotext_или_pymupdf(tmp_path, monkeypatch):
    pdf = D.convert(DOC, "a.docx", "pdf")
    assert pdf[:5] == b"%PDF-"
    if shutil.which("pdftotext"):
        assert "Кравцов Сергей Сергеевич" in D.extract_text(pdf, "a.pdf")
    # pdftotext нет (как на сервере) — PyMuPDF, если импортируется; нет и его — понятная ошибка
    real_which = shutil.which
    monkeypatch.setattr(D.shutil, "which", lambda name: None if name == "pdftotext" else real_which(name))
    try:
        import pymupdf  # noqa: F401
        assert "Кравцов Сергей Сергеевич" in D.extract_text(pdf, "a.pdf")
    except ImportError:
        pass
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def no_fitz(name, *a, **k):
        if name in ("pymupdf", "fitz"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", no_fitz)
    with pytest.raises(D.DocsError, match="нет ни pdftotext, ни PyMuPDF"):
        D.extract_text(pdf, "a.pdf")


def test_без_libreoffice_понятная_ошибка(monkeypatch):
    monkeypatch.setattr(D, "soffice", lambda: None)
    with pytest.raises(D.DocsError, match="сохраните как .docx"):
        D.extract_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "WordDocument".encode("utf-16-le") + b"\0" * 600, "a.doc")
    with pytest.raises(D.DocsError, match="нужен LibreOffice"):
        D.convert(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "Workbook".encode("utf-16-le") + b"\0" * 600, "a.xls", "xlsx")
    # rtf и odt без LibreOffice — простым разбором
    assert D.extract_text(b"{\\rtf1\\ansi \\'ca\\'f0\\'e0\\'e2\\'f6\\'ee\\'e2\\par}", "a.rtf") == "Кравцов"


def test_конвертация_неподходящей_пары_отказ():
    with pytest.raises(D.DocsError, match="не переводится"):
        D.convert(b"%PDF-1.4", "a.pdf", "docx")
    with pytest.raises(D.DocsError, match="не поддерживается"):
        D.convert(DOC, "a.docx", "exe")


# ── враждебные файлы (ревью RC-2026-10-05-4, Н-1, Н-2, Н-3, Н-7) ───────

def _mimetype_bomb(path, megabytes: int) -> None:
    """odt, внутри «mimetype» на сотни мегабайт, сжато — сотни килобайт."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_DEFLATED
        with z.open(info, "w", force_zip64=True) as f:
            f.write(b"application/vnd.oasis.opendocument.text")
            chunk = b"a" * (1 << 20)
            for _ in range(megabytes):
                f.write(chunk)
        z.writestr("content.xml", "<x/>")


def test_mimetype_бомба_отказ_без_роста_памяти(tmp_path):
    bomb = tmp_path / "bomb.odt"
    _mimetype_bomb(bomb, 400)
    assert bomb.stat().st_size < 1_000_000
    # отдельный процесс — чтобы честно померить пик памяти
    code = "\n".join([
        "import sys, time, resource",
        "sys.path.insert(0, sys.argv[1])",
        "from app import docs_common as D",
        "data = open(sys.argv[2], 'rb').read()",
        "t = time.monotonic()",
        "for f in (lambda: D.extract_text(data, 'b.odt'), lambda: D.convert(data, 'b.odt', 'docx'),",
        "          lambda: D.to_docx(data, 'b.odt'), lambda: D.sniff(data, 'b.odt')):",
        "    try:",
        "        f()",
        "    except D.DocsError as e:",
        "        print('ERR', str(e)[:40])",
        "print('T', time.monotonic() - t)",
        "print('MB', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)"])
    from pathlib import Path
    out = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent.parent), str(bomb)],
                         capture_output=True, text=True, timeout=60).stdout
    assert out.count("ERR Файл «b.odt» слишком большой") == 3, out      # extract_text, convert, to_docx
    t = float(out.split("T ")[1].split()[0])
    mb = int(out.split("MB ")[1].split()[0])
    assert t < 2.0 and mb < 150, out


def test_часть_больше_предела_и_заголовок_врёт(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_PART", 1000)
    big = docx(["Кравцов Сергей Сергеевич"] + ["x" * 100] * 30)
    with pytest.raises(D.DocsError, match="слишком большой"):
        D.extract_text(big, "a.docx")
    monkeypatch.setattr(D, "MAX_PART", 40 * 1024 * 1024)
    # размер в заголовке занижен — чтение обрывается на нём, файл признаётся повреждённым
    raw = bytearray(big)
    z = zipfile.ZipFile(io.BytesIO(big))
    info = z.getinfo("word/document.xml")
    cd = big.rfind(b"PK\x01\x02" + big[big.find(b"PK\x01\x02") + 4:big.find(b"PK\x01\x02") + 4], 0)
    for at in (info.header_offset + 22,):
        raw[at:at + 4] = (10).to_bytes(4, "little")
    pos = big.find(b"word/document.xml", big.find(b"PK\x01\x02"))
    raw[pos - 46 + 24:pos - 46 + 28] = (10).to_bytes(4, "little")
    with pytest.raises(D.DocsError, match="повреждён"):
        D.extract_text(bytes(raw), "a.docx")
    assert cd >= 0
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as zz:
        zz.writestr("word/document.xml", "<w:document/>")
        for i in range(D.MAX_ENTRIES + 1):
            zz.writestr(f"x/{i}", "")
    with pytest.raises(D.DocsError, match="слишком много частей"):
        D.extract_text(many.getvalue(), "a.docx")
    with pytest.raises(D.DocsError, match="больше 60 МБ"):
        D.extract_text(b"x" * (D.MAX_INPUT + 1), "a.txt")


def _unclosed_docx(n: int) -> bytes:
    xml = '<?xml version="1.0"?><w:document xmlns:w="w"><w:body>' + "<w:p >" * n + "</w:body></w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


@pytest.mark.parametrize("n", [32_000, 200_000])
def test_незакрытые_абзацы_разбираются_за_доли_секунды(n):
    data = _unclosed_docx(n)
    assert len(data) < 3000
    started = time.monotonic()
    with pytest.raises(D.DocsError, match="не нашлось текста"):
        D.extract_text(data, "a.docx")
    assert time.monotonic() - started < 1.5


def test_незакрытые_теги_во_всех_разборщиках():
    """pptx, xlsx, odt, html — тот же приём: линейно, без зависания."""
    def z(parts):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zz:
            for k, v in parts.items():
                zz.writestr(k, v)
        return buf.getvalue()
    cases = [
        ("a.pptx", z({"ppt/presentation.xml": "<p/>", "ppt/slides/slide1.xml": "<a:p >" * 100_000 + "<a:t>Кравцов"})),
        ("a.xlsx", z({"xl/workbook.xml": "<sheet " * 50_000, "xl/sharedStrings.xml": "<si><t>" * 100_000})),
        ("a.html", ("<html>" + "<script " * 100_000).encode()),
    ]
    for name, data in cases:
        started = time.monotonic()
        try:
            D.extract_text(data, name)
        except D.DocsError:
            pass
        assert time.monotonic() - started < 2.0, name


def test_срок_разбора(monkeypatch):
    monkeypatch.setattr(D, "PARSE_TIMEOUT", 0.0)
    with pytest.raises(D.DocsError, match="разбор прерван"):
        D.extract_text(_unclosed_docx(50_000), "a.docx")


def test_мусор_не_отдаётся_как_текст():
    junk = bytes(range(128, 256)) * 400                  # двоичное без нулевых байтов
    with pytest.raises(D.DocsError):
        D.extract_text(junk, "a.xyz")
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(range(1, 256)) * 50
    assert D.sniff(ole, "a.doc") == "ole"
    with pytest.raises(D.DocsError, match="не похож ни на документ Word"):
        D.extract_text(ole, "a.doc")
    with pytest.raises(D.DocsError, match="нечитаемые знаки"):
        D.extract_text("漢字漢字漢字漢字漢字漢字漢字漢字漢字漢字".encode(), "a.txt")


def _odt_with_http_image(port: int) -> bytes:
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
               'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" '
               'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.2">'
               '<office:body><office:text><text:p>Кравцов Сергей Сергеевич</text:p><text:p>'
               '<draw:frame draw:name="i" svg:width="2cm" svg:height="2cm" text:anchor-type="as-char">'
               f'<draw:image xlink:href="http://127.0.0.1:{port}/secret.png" xlink:type="simple" xlink:show="embed" '
               'xlink:actuate="onLoad"/></draw:frame></text:p></office:text></office:body></office:document-content>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("META-INF/manifest.xml", '<?xml version="1.0"?><manifest:manifest '
                   'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                   '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                   '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
        z.writestr("content.xml", content)
    return buf.getvalue()


def _requests_during_convert() -> list:
    """Перевести ODT с картинкой по http на локальный слушатель; вернуть, какие запросы к нему пришли."""
    import http.server
    import threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        text = D.extract_text(_odt_with_http_image(srv.server_address[1]), "a.odt")
        assert "Кравцов Сергей Сергеевич" in text
        time.sleep(0.2)
        return list(hits)
    finally:
        srv.shutdown()


EMPTY_PROFILE = ('<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"/>\n')


@needs_soffice
def test_libreoffice_без_сети_каждая_линия_защиты_отдельно(monkeypatch):
    """ODT с картинкой по http (ревью RC-2026-10-05-4 Н-3, RC-8 Н-3). Две независимые линии: настройки
    временного профиля и мёртвый прокси в окружении. Каждая сама по себе не пускает запрос; без обеих
    запрос уходит — значит, тест действительно видит сеть (и поймает удаление любой из линий)."""
    assert _requests_during_convert() == []                              # обе линии
    monkeypatch.setattr(D, "_DEAD_PROXY", "")
    assert _requests_during_convert() == [], "профиль LibreOffice не запрещает внешние ссылки"
    monkeypatch.setattr(D, "_NO_NETWORK", EMPTY_PROFILE)
    assert _requests_during_convert() == ["/secret.png"], "без обеих линий запрос должен уходить — проверка слепа"
    monkeypatch.undo()
    monkeypatch.setattr(D, "_NO_NETWORK", EMPTY_PROFILE)
    assert _requests_during_convert() == [], "мёртвый прокси в окружении не действует"


def test_профиль_libreoffice_запрещает_внешние_ссылки():
    for key in ("BlockUntrustedRefererLinks", "ooInetHTTPProxyName", "Content/Update"):
        assert key in D._NO_NETWORK
    assert D._DEAD_PROXY == "http://127.0.0.1:9"


def _xlsx_with_cell_attr(letters: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="Л" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c ' + "a" * letters
                   + '><v>1</v></c><c r="B1" t="inlineStr"><is><t>Кравцов Сергей</t></is></c></row></sheetData></worksheet>')
    return buf.getvalue()


@pytest.mark.parametrize("letters", [20_000, 60_000, 200_000])
def test_длинный_атрибут_без_равно_линейно(letters):
    """Ревью RC-2026-10-05-8, Н-1: xlsx в 1 КБ с атрибутом из 200 000 букв держал сервис минуты."""
    data = _xlsx_with_cell_attr(letters)
    assert len(data) < 1500
    started = time.monotonic()
    text = D.extract_text(data, "a.xlsx")                # длинный тег не отказ — его атрибуты пропускаются
    assert "Кравцов Сергей" in text
    assert time.monotonic() - started < 0.5


def test_разбор_атрибутов():
    assert D._attrs(' r="A1" t="s"  s=\'3\' x = "a&amp;b" bad= noq c="9"') == {"r": "A1", "t": "s", "s": "3", "x": "a&b", "c": "9"}
    assert D._attrs("a" * 100_000) == {}
    started = time.monotonic()
    D._attrs(("a" * 1000 + " ") * 60)
    D._attrs("x" * 60_000 + '="1"')
    assert time.monotonic() - started < 0.1


@needs_soffice
def test_libreoffice_не_ходит_в_сеть():
    """ODT с картинкой по http: при переводе LibreOffice не делает ни одного запроса (Н-3)."""
    assert _requests_during_convert() == []


def test_справка_docx_516_байт_с_длинным_атрибутом_в_связях():
    """Ревью RC-2026-10-05-8, Н-1: атрибут из 40 000 букв без «=» в document.xml.rels — фото из справки
    искалось 21 с, и всё это время сервис стоял."""
    from app import extract
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>Кравцов Сергей Сергеевич, '
                   'министр просвещения</w:t></w:r></w:p></w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", "<Relationships><Relationship " + "a" * 40_000 + "/></Relationships>")
    data = buf.getvalue()
    assert len(data) < 700
    started = time.monotonic()
    text, photo = extract.read_source("справка.docx", data)
    assert "Кравцов Сергей Сергеевич" in text and photo is None
    assert time.monotonic() - started < 0.5



# ── длинный тег у обычного документа (ревью RC-2026-10-05-10, Н-1, Н-2) ──

GFX = "A" * (104 * 1024)                                  # o:gfxdata — base64 на 104 КБ, как у фигур Word


def test_docx_с_o_gfxdata_104_кб_читается():
    xml = ('<w:document xmlns:w="w" xmlns:v="v" xmlns:o="o"><w:body>'
           '<w:p><w:r><w:t>Кравцов Сергей Сергеевич</w:t></w:r></w:p>'
           f'<w:p><w:r><w:pict><v:shape id="s1" o:gfxdata="{GFX}" style="width:100pt">'
           '<v:textbox><w:txbxContent><w:p><w:r><w:t>Текст в надписи</w:t></w:r></w:p></w:txbxContent></v:textbox>'
           f'</v:shape><v:rect o:gfxdata="{GFX}"/></w:pict></w:r><w:r><w:t xml:space="preserve" o:x="{GFX}"> и после фигуры</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Министр просвещения</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    started = time.monotonic()
    text = D.extract_text(buf.getvalue(), "справка.docx")
    assert time.monotonic() - started < 0.5
    assert text.split("\n") == ["Кравцов Сергей Сергеевич", "Текст в надписи", "и после фигуры", "Министр просвещения"]


def test_pptx_и_odt_с_большими_данными_читаются(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ppt/presentation.xml", "<p:presentation/>")
        z.writestr("ppt/slides/slide1.xml", f'<p:sld><p:pic descr="{GFX}"><p:blipFill/></p:pic>'
                   '<a:p><a:r><a:t>Кравцов Сергей Сергеевич</a:t></a:r></a:p></p:sld>')
    assert D.extract_text(buf.getvalue(), "a.pptx") == "Слайд 1.\nКравцов Сергей Сергеевич"
    # odt своим разбором (без LibreOffice): картинка внутри — office:binary-data, у кадра длинный атрибут
    monkeypatch.setattr(D, "soffice", lambda: None)
    odt = io.BytesIO()
    with zipfile.ZipFile(odt, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", '<office:document-content><office:body><office:text>'
                   '<text:p>Кравцов Сергей Сергеевич</text:p>'
                   f'<text:p><draw:frame draw:name="{GFX}"><draw:image><office:binary-data>{GFX}</office:binary-data>'
                   '</draw:image></draw:frame>Министр</text:p></office:text></office:body></office:document-content>')
    text = D.extract_text(odt.getvalue(), "a.odt")
    assert text.startswith("Кравцов Сергей Сергеевич\n") and text.endswith("Министр")


def test_длинный_тег_пропускается_а_не_отклоняется():
    clock = D._Clock("x")
    long_open = "<w:t a=\"" + "b" * (D.MAX_TAG + 10) + "\">"
    got = list(D._tags(long_open + "текст</w:t><w:br " + "c" * (D.MAX_TAG + 10) + "/>", clock))
    assert got == [("open", "w:t", ""), ("text", "", "текст"), ("close", "w:t", ""), ("empty", "w:br", "")]
