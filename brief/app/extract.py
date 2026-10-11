"""
Текст из загруженного документа: Word, Excel, PowerPoint, PDF, RTF, ODT, текст, веб-страница.

Ничего не домысливаем: если текста в файле нет (скан, картинки, защищённый
PDF) — говорим об этом прямо, а не собираем саммари из пустоты.

Чтение — общий модуль пространства `docs_common` (копия `portal/shared/docs_common.py`,
раскладывает `sync_shared.sh`, здесь не править; решение пользователя 05.10: любые типовые
файлы). Свои пути остались там, где они уже проверены на справках: .docx — `build_brief.docx_text`
(та же разметка строк, по которой сверяются цитаты), старый .doc — сначала `doc_text.py`, без
LibreOffice. PDF — pdftotext, а если её нет (так на VPS) — PyMuPDF.

Формат определяется по содержимому: под именем .doc часто лежит .docx или страница.
"""
import io
import re
from pathlib import Path

import build_brief as bb
import doc_text
import docs_common

SourceError = bb.SourceError          # ошибки чтения у всех видов брифа одни

# что принимаем (по имени — для файлов задания на диске; загрузку проверяет содержимое)
KINDS = tuple(docs_common.SUPPORTED_EXT) + (".html", ".htm")
# виды по содержимому, которые умеем читать (`docs_common.sniff`)
READABLE = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf", "rtf", "odt", "ods", "odp",
            "html", "txt", "md", "csv"}
PRESENTATIONS = {"pptx", "ppt", "odp"}
WHAT = "Word, Excel, PowerPoint, PDF, RTF, ODT или текст"


def kind_of(name):
    return Path(str(name or "")).suffix.lower()


def supported(name):
    return kind_of(name) in KINDS


def sniff(name, raw):
    """Чем файл является на самом деле (без точки: "docx", "pdf", …; "" — не документ)."""
    return docs_common.sniff(raw, name)


# Обычный текст узнаётся только «от противного» (нет признаков другого формата), поэтому под
# именем .docx, .pdf или .png он — не документ, а чужой или битый файл: отказ с перечнем типов.
TEXT_EXT = ("", ".txt", ".md", ".csv", ".html", ".htm")


def readable(name, raw):
    kind = sniff(name, raw)
    if kind in ("txt", "md", "csv") and kind_of(name) not in TEXT_EXT:
        return False
    return kind in READABLE


def _common(raw, name, kind):
    try:
        return docs_common.extract_text(raw, name)
    except docs_common.DocsError as e:
        if kind == "pdf" and str(e).startswith(f"В файле «{name}» нет текста"):
            raise SourceError(f"В файле «{name}» нет текста — похоже, это скан. Пришлите Word или PDF "
                              "с текстовым слоем: придумывать содержание доклада программа не будет.") from e
        raise SourceError(str(e)) from e


def from_bytes(name, raw):
    """Текст файла по его содержимому (расширению не верим). Пусто — всегда ошибка с объяснением."""
    kind = sniff(name, raw)
    if not readable(name, raw) and kind in READABLE:
        raise SourceError(f"Файл «{name}» не похож на {kind_of(name)}: внутри обычный текст. Нужен {WHAT}.")
    if kind == "docx":
        text = bb.docx_text(io.BytesIO(raw))
    elif kind == "doc":
        try:
            text = doc_text.doc_text(raw)            # свой разбор — без LibreOffice
        except doc_text.NotDoc:
            text = ""
        if len(re.sub(r"\s", "", text or "")) < 40:
            text = _common(raw, name, kind)          # LibreOffice; нет его — понятная просьба сохранить как .docx
    elif kind in READABLE:
        text = _common(raw, name, kind)
    elif kind == "encrypted":
        raise SourceError(f"Файл «{name}» защищён паролем — снимите пароль и сохраните заново.")
    else:
        ext = kind_of(name)
        raise SourceError(f"Формат «{ext or 'без расширения'}» не поддерживается. Нужен {WHAT}.")
    text = re.sub(r"[ \t ]+", " ", text)
    text = "\n".join(line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip())
    if len(re.sub(r"\s", "", text)) < 40:
        if kind == "pdf":
            raise SourceError(f"В файле «{name}» нет текста — похоже, это скан. "
                              "Пришлите Word или PDF с текстовым слоем: придумывать содержание доклада программа не будет.")
        raise SourceError(f"В файле «{name}» не нашлось текста.")
    return text


def from_path(path):
    path = Path(path)
    return from_bytes(path.name, path.read_bytes())
