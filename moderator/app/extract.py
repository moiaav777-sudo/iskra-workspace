"""Извлечение текста из загружаемых файлов: Word, Excel, PowerPoint, PDF, RTF, ODT, текст.

Извлечение идёт локально; полученный текст дальше разбирается парсером
parser.py или отправляется на разбор в Искру.

Чтение — общий модуль пространства `docs_common` (копия `portal/shared/docs_common.py`, раскладывает
`sync_shared.sh`, здесь не править; решение пользователя 05.10: любые типовые файлы). Формат — по
содержимому, а не по расширению. Свой путь остался у Word: абзацы и таблицы по порядку следования,
таблицы — markdown (на этом держится разбор повестки «вопрос → докладчики»); старые .doc, .rtf, .odt
сначала переводятся в .docx LibreOffice и идут тем же путём. Остальное (Excel, PowerPoint, PDF —
pdftotext, иначе PyMuPDF, текст, веб-страницы) — `docs_common.extract_text`.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from . import docs_common

SUPPORTED_EXTS = tuple(docs_common.SUPPORTED_EXT)
# виды по содержимому (`docs_common.sniff`), которые читаем
KINDS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf", "rtf", "odt", "ods", "odp",
         "html", "txt", "md", "csv"}
# обычный текст узнаётся «от противного» (нет признаков другого формата) — под именем .docx/.pdf/.exe
# он не документ, а чужой или битый файл
TEXT_KINDS = ("txt", "md", "csv")
TEXT_EXT = ("", ".txt", ".md", ".csv", ".html", ".htm")
WHAT = "Word, Excel, PowerPoint, PDF, RTF, ODT или текст"

MAX_UNZIPPED_BYTES = 300 * 1024 * 1024  # docx — это zip; защита от «zip-бомб»
MAX_ZIP_ENTRIES = 20_000


def kind_of(data: bytes, filename: str) -> str:
    return docs_common.sniff(data, filename)


def listed(filename: str) -> bool:
    """Расширение из принимаемых (проверка до разбора; сам формат решает содержимое)."""
    return Path(filename or "").suffix.lower() in SUPPORTED_EXTS + (".html", ".htm")


def problem(filename: str, data: bytes) -> str:
    """Почему файл не документ ("" — документ). По содержимому, а не по расширению."""
    kind = kind_of(data, filename)
    ext = Path(filename or "").suffix.lower()
    name = filename or "без имени"
    if kind in TEXT_KINDS and ext not in TEXT_EXT:
        if listed(filename):
            return f"Файл «{name}» не похож на {ext}: внутри другой формат"
        return f"Файл «{name}» не принимается: нужны {WHAT}"
    if kind == "encrypted":
        return f"Файл «{name}» защищён паролем — снимите пароль и сохраните заново"
    if kind not in KINDS:
        return f"Файл «{name}» не принимается: нужны {WHAT}"
    return ""


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise ValueError("файл повреждён или это не документ Office")
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ValueError("документ слишком большой после распаковки")


def extract_bytes(data: bytes, filename: str) -> str:
    """Текст документа. Не документ, не читается или пуст — ValueError с объяснением по-русски."""
    reason = problem(filename, data)
    if reason:
        raise ValueError(reason)
    kind = kind_of(data, filename)
    if kind in ("doc", "rtf", "odt") and docs_common.soffice():
        kind, data = "docx", docs_common.to_docx(data, filename)
    if kind == "docx":
        _check_zip(data)
        return _extract_docx(data)
    return docs_common.extract_text(data, filename)


def extract_file(path: str, filename: str) -> str:
    return extract_bytes(Path(path).read_bytes(), filename)


def _extract_docx(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    out: list[str] = []

    def clean(cell) -> str:
        # схлопнуть переносы строк внутри ячейки в один пробел
        return " ".join(cell.text.split())

    def is_separator(s: str) -> bool:
        # ячейка-разделитель (тире/точка) — не несёт данных
        return bool(s) and set(s.strip()) <= {"-", "–", "—", "−", "·", "•", "."}

    def para_md(t: str, style: str) -> str:
        # заголовки превращаем в markdown-заголовки (Искра лучше держит структуру)
        s = (style or "").lower()
        if ("heading" in s) or ("заголовок" in s) or ("title" in s) or s.startswith("toc"):
            return f"## {t}"
        return t

    # идём по элементам тела документа В ПОРЯДКЕ следования:
    # абзацы и таблицы вперемешку. Иначе структура «заголовок → вопрос → докладчики»
    # ломается: раньше все абзацы шли первыми, потом все таблицы.
    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag.endswith("}p"):
            p = Paragraph(child, doc)
            t = p.text.strip()
            if t:
                out.append(para_md(t, (p.style.name if p.style else "") or ""))
        elif tag.endswith("}tbl"):
            table = Table(child, doc)
            rows_md: list[str] = []
            for row in table.rows:
                seen: list[str] = []
                cells: list[str] = []
                for c in row.cells:
                    txt = clean(c)
                    if txt and txt not in seen and not is_separator(txt):
                        seen.append(txt)
                        cells.append(txt)
                if cells:
                    rows_md.append("| " + " | ".join(cells) + " |")
            if rows_md:
                # разделитель заголовка markdown-таблицы (если строк больше одной)
                if len(rows_md) > 1:
                    ncols = max(len(r.split("|")) - 2 for r in rows_md)
                    rows_md.insert(1, "|" + "---|" * max(ncols, 1))
                out.extend(rows_md)
    return "\n".join(out)
