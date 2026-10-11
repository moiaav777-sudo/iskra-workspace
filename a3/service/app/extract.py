"""Текст из загруженной справки. Без сторонних библиотек.

Сама справка на сервере не сохраняется: из файла берётся текст, уходит в
«Искру» и забывается. В базе остаётся только готовый лист.

Принимаются Word (.docx, .doc), Excel (.xlsx, .xls), PowerPoint (.pptx, .ppt), PDF, .rtf, .odt,
.txt, .md, .csv (решение пользователя 05.10). Формат — по содержимому, а не по расширению. .docx —
своим разбором (с колонтитулами и таблицами), остальное — общий модуль пространства `docs_common`
(копия `portal/shared/docs_common.py`, раскладывает `sync_shared.sh`, здесь не править): старые
форматы переводит LibreOffice, PDF — pdftotext, а без неё (VPS) — PyMuPDF.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from . import docs_common

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LIMIT = 120_000  # длиннее справки в «Искру» не отправляем


class Unsupported(Exception):
    pass


def _docx(raw: bytes) -> str:
    # прямо из памяти: на Windows временный файл нельзя открыть второй раз,
    # пока он не закрыт, и разбор падал внутренней ошибкой
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        inside = set(z.namelist())
        names = ["word/document.xml"] + sorted(n for n in inside if n.startswith("word/header"))
        parts = []
        for name in names:
            if name not in inside:
                continue
            root = ElementTree.fromstring(z.read(name))
            for para in root.iter(W + "p"):
                text = "".join(node.text or "" for node in para.iter(W + "t"))
                if text.strip():
                    parts.append(text.strip())
            for table in root.iter(W + "tbl"):
                for row in table.iter(W + "tr"):
                    cells = []
                    for cell in row.iter(W + "tc"):
                        cells.append(" ".join((t.text or "").strip() for t in cell.iter(W + "t")).strip())
                    if any(cells):
                        parts.append(" | ".join(cells))
    return "\n".join(parts)


# обычный текст узнаётся «от противного» — под именем .docx или .pdf он битый или чужой файл
TEXT_EXT = ("", ".txt", ".md", ".csv", ".json", ".html", ".htm")
KINDS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf", "rtf", "odt", "ods", "odp",
         "html", "txt", "md", "csv"}
WHAT = "Word, Excel, PowerPoint, PDF, RTF, ODT или текст"


def from_file(name: str, raw: bytes) -> str:
    kind = docs_common.sniff(raw, name)
    suffix = Path(name or "").suffix.lower()
    if kind in ("txt", "md", "csv") and suffix not in TEXT_EXT:
        raise Unsupported(f"Файл {suffix} не читается: внутри обычный текст или повреждённые данные")
    if kind not in KINDS:
        raise Unsupported(f"Формат {suffix or 'без расширения'} не поддерживается: подходят {WHAT}")
    try:
        text = _docx(raw) if kind == "docx" else docs_common.extract_text(raw, name)
    except docs_common.DocsError as e:
        raise Unsupported(str(e)) from None
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise Unsupported("Файл Word повреждён — откройте его и сохраните заново") from None
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise Unsupported("В файле не нашлось текста")
    return text[:LIMIT]
