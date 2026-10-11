"""Синтетические документы для проверки чтения загрузок (решение пользователя 05.10: Word, Excel,
PowerPoint, PDF и текст принимаются везде).

xlsx, pptx, docx и PDF собираются здесь же, без библиотек; старые .doc и .xls — LibreOffice из
текста/таблицы (нет soffice — тест пропускается). Реальных документов в тестах нет.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

FILLER = ("Справка о ходе работ по программе развития образования региона. "
          "Подготовлено для проверки чтения файлов, все сведения выдуманы. ")


def _zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
         'officeDocument" Target="{target}"/></Relationships>')


def docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": _RELS.format(target="word/document.xml"),
        "word/document.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>",
    })


def xlsx(rows: list[list[str]], sheet: str = "Показатели") -> bytes:
    def cell(col: int, row: int, value: str) -> str:
        ref = chr(64 + col) + str(row)
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    data = "".join(f'<row r="{r}">' + "".join(cell(c, r, v) for c, v in enumerate(vals, 1)) + "</row>"
                   for r, vals in enumerate(rows, 1))
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": _RELS.format(target="xl/workbook.xml"),
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{data}</sheetData></worksheet>",
    })


def pptx(slides: list[list[str]]) -> bytes:
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/></Types>',
        "ppt/presentation.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
    }
    for i, lines in enumerate(slides, 1):
        paras = "".join(f"<a:p><a:r><a:t>{escape(x)}</a:t></a:r></a:p>" for x in lines)
        parts[f"ppt/slides/slide{i}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<p:cSld><p:spTree><p:sp><p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
    return _zip(parts)


def pdf(lines: list[str]) -> bytes:
    """Одностраничный PDF с текстовым слоем (латиница, шрифт Helvetica) — собран руками."""
    ops = ["BT", "/F1 12 Tf", "14 TL", "72 760 Td"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({safe}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def pdf_reader() -> bool:
    """Есть чем читать PDF: pdftotext или PyMuPDF."""
    if shutil.which("pdftotext"):
        return True
    for module in ("pymupdf", "fitz"):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


def soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def legacy(source: bytes, source_ext: str, to: str) -> bytes | None:
    """Старый формат (.doc, .xls) из текста/таблицы через LibreOffice; нет его — None."""
    exe = soffice()
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="doc-samples-") as tmp_name:
        tmp = Path(tmp_name)
        src = tmp / f"sample.{source_ext}"
        src.write_bytes(source)
        env = {**os.environ, "HOME": str(tmp)}
        subprocess.run([exe, "--headless", "--norestore", f"-env:UserInstallation={(tmp / 'p').as_uri()}",
                        "--convert-to", to, "--outdir", str(tmp), str(src)],
                       capture_output=True, timeout=180, env=env, cwd=tmp)
        out = tmp / f"sample.{to}"
        return out.read_bytes() if out.is_file() else None
