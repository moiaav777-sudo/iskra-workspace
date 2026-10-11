"""Текст приложенного документа — читаем у себя, из памяти.

По этому тексту «Искра» пишет тезисы. Чтение — общий модуль пространства `docs_common` (копия
`portal/shared/docs_common.py`, раскладывает `sync_shared.sh`, здесь не править): Word (.docx, .doc),
Excel (.xlsx, .xls), PowerPoint (.pptx, .ppt), PDF, .rtf, .odt, .txt, .md, .csv (решение пользователя
05.10). Формат — по содержимому, а не по расширению. docx/pptx/xlsx читаются из памяти; старые
форматы переводит LibreOffice во временной папке (так устроен общий модуль), PDF — pdftotext,
а без неё — PyMuPDF.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import docs_common, latin

MAX_CHARS = 300_000
# обычный текст узнаётся «от противного», поэтому под именем .docx или .pdf он — битый или чужой файл
TEXT_EXT = ("", ".txt", ".md", ".csv", ".html", ".htm")
KINDS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf", "rtf", "odt", "ods", "odp",
         "html", "txt", "md", "csv"}


class BadDocument(ValueError):
    """Файл не читается. Текст показывается пользователю по-русски."""


def kind_of(name: str, data: bytes | None = None) -> str:
    """Вид документа: по содержимому, если оно есть, иначе по расширению."""
    if data is not None:
        return docs_common.sniff(data, name)
    ext = Path(name or "").suffix.lower().lstrip(".")
    return ext if ext in KINDS else ""


def extract(name: str, data: bytes) -> str:
    """Текст документа. Пустой результат — тоже ошибка: писать будет не из чего.

    Буквы чтение не меняет: что стоит в файле, то и в тексте. Но автор справки мог
    набрать «IТ» с русской «Т» — такие слова приводим к одному алфавиту сразу, иначе
    «Искра» перепишет их в тезисы как есть (`latin.fix_mixed`).
    """
    kind = docs_common.sniff(data, name)
    ext = Path(name or "").suffix.lower()
    if kind in ("txt", "md", "csv") and ext not in TEXT_EXT:
        raise BadDocument(f"Файл «{name}» не читается: внутри не {ext}, а обычный текст или повреждённые данные")
    if kind not in KINDS:
        raise BadDocument("Такой файл не поддерживается: нужен Word, Excel, PowerPoint, PDF, RTF, ODT или текст")
    try:
        text = docs_common.extract_text(data, name)
    except docs_common.DocsError as error:
        if str(error).startswith(f"В файле «{name}» нет текста"):
            raise BadDocument("В документе не нашлось текста — если это скан, нужен текстовый слой") from error
        raise BadDocument(str(error)) from error
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    text = latin.fix_mixed(text)[0]
    if not text:
        raise BadDocument("В документе не нашлось текста — если это скан, нужен текстовый слой")
    return text[:MAX_CHARS]
