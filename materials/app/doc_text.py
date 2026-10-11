"""Текст документа и название, которое можно предложить человеку.

Читаем ровно столько, сколько нужно хранилищу: заголовок для имени файла и текст
для поиска. Разбор документа по существу — саммари, сверка чисел — живёт в «Брифе»
и в общих модулях портала (`../CLAUDE.md`, п. 5а); сюда это не переносим.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from . import naming, wording

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_CHARS = 200_000
# Заголовок длиннее этого — уже не заголовок, а абзац. Длинные официальные
# названия сюда попадают целиком: укоротит их naming.shorten, по словам.
MAX_TITLE = 200

# Что вообще умеем читать. Остальное хранится как есть: имя даёт человек.
READABLE = {"docx", "pdf", "txt", "md", "pptx", "xlsx"}


def guess_ext(data: bytes) -> str:
    """Какой это файл на самом деле. Нужно, когда имя пришло без расширения.

    Телефон умеет отправить документ телом запроса, но имя доезжает не всегда —
    и тогда «Документ» без расширения сервис прочитать не может вовсе: ни текста
    в поиск, ни названия из заголовка.
    """
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return "zip"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        return "zip"
    if data[:5] == b"{\\rtf":
        return "rtf"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "doc"                      # старый формат Word, Excel, PowerPoint
    try:
        data[:4096].decode("utf-8")
        return "txt"
    except UnicodeDecodeError:
        return ""


def kind_of(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _docx(data: bytes) -> list[str]:
    """Абзацы документа.

    Перенос строки и табуляция внутри абзаца — это пробел. Без этого «…и НТР»
    с новой строки «Ямало-Ненецкого» склеивались в «НТРЯмало-Ненецкого»:
    в самом Word такой заголовок выглядит правильно, ломается только текст.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    breaks = {W + "br", W + "cr", W + "tab"}
    lines = []
    for para in root.iter(W + "p"):
        parts = []
        for node in para.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag in breaks:
                parts.append(" ")
        lines.append("".join(parts).strip())
    return lines


def _pptx(data: bytes) -> list[str]:
    lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for name in slides:
            root = ET.fromstring(z.read(name))
            for para in root.iter(A + "p"):
                parts = []
                for node in para.iter():
                    if node.tag == A + "t":
                        parts.append(node.text or "")
                    elif node.tag == A + "br":
                        parts.append(" ")
                lines.append("".join(parts).strip())
    return lines


def _xlsx(data: bytes) -> list[str]:
    """Для таблицы важны названия листов и общие строки — по ним её и ищут."""
    lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            book = ET.fromstring(z.read("xl/workbook.xml"))
            lines += [sheet.get("name", "") for sheet in book.iter(S + "sheet")]
        except (KeyError, ET.ParseError):
            pass
        try:
            shared = ET.fromstring(z.read("xl/sharedStrings.xml"))
            lines += ["".join(t.text or "" for t in si.iter(S + "t")).strip()
                      for si in shared.iter(S + "si")]
        except (KeyError, ET.ParseError):
            pass
    return lines


def _pdf(data: bytes) -> list[str]:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover — на ПК библиотека стоит
        return []
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc).splitlines()
    except Exception:
        return []


def lines(name: str, data: bytes) -> list[str]:
    """Строки документа. Файл нечитаемый — не беда: хранится как есть, без текста."""
    kind = kind_of(name)
    try:
        if kind == "docx":
            out = _docx(data)
        elif kind == "pptx":
            out = _pptx(data)
        elif kind == "xlsx":
            out = _xlsx(data)
        elif kind == "pdf":
            out = _pdf(data)
        elif kind in ("txt", "md"):
            out = data.decode("utf-8-sig", errors="replace").splitlines()
        else:
            return []
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, OSError):
        return []
    return [re.sub(r"\s+", " ", line).strip() for line in out]


def text(name: str, data: bytes) -> str:
    return "\n".join(x for x in lines(name, data) if x)[:MAX_CHARS]


# Строки-пустышки в начале документа: гриф, адресат, «приложение №…».
JUNK = re.compile(r"^(приложение|утверждаю|согласовано|проект|секретно|дсп|экз\.?|лист \d|стр\.? ?\d"
                  r"|\d+\s*$|[«\"']?\s*$)", re.I)
# Строка целиком в скобках — это уточнение под заголовком («(2026 год)»),
# а не название: само по себе оно не говорит ни о чём.
BRACKETED = re.compile(r"^\(.*\)$")
# Угловой штамп письма: кому оно адресовано. Идёт до заголовка и выглядит как
# «Заместителю Председателя Правительства / Российской Федерации / Д.Н. Чернышенко» —
# заголовком не является ни целиком, ни любой своей строкой.
ADDRESSEE = re.compile(r"^(заместител|первому|министру|руководител|председател|директору"
                       r"|губернатору|начальник|мэру|глав[еы]|полномочному|статс-секретарю"
                       r"|аппарат[ау]|помощнику|советнику)", re.I)
# Строки, которые сами по себе ничего не называют: обрывок наименования и подпись.
FRAGMENT = re.compile(r"^(российской федерации|правительства российской федерации"
                      r"|правительства|администрации президента)$", re.I)
INITIALS = re.compile(r"^[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё-]+$")
ADDRESS_BLOCK = 4          # столько строк после адресата ещё относится к штампу
# Бланк организации в шапке документа. «ПРАВИТЕЛЬСТВО РОССИЙСКОЙ ФЕДЕРАЦИИ» —
# это не название проекта постановления, а бумага, на которой он напечатан.
LETTERHEAD = re.compile(r"^(правительство|министерство|администрация|федеральн\w+\s+"
                        r"(служба|агентство)|аппарат|департамент|управление|комитет)\b", re.I)
# Продолжение заголовка, разбитого на абзацы: со строчной буквы или со скобки.
CONTINUES = re.compile(r"^[(«\"']|^[а-яёa-z]")
# Номер письма в угловом штампе: «№ АН-1466 от 15.04.2026». Для входящего документа
# это главное, по чему его ищут, — номер обязан попасть в название.
DOC_NUMBER = re.compile(r"№\s*([A-Za-zА-Яа-яЁё0-9][\w\-/]{2,20})"
                        r"(?:\s+от\s+(\d{2}\.\d{2}\.\d{4}))?")


def _headline(line: str) -> bool:
    if len(line) < 6 or len(line) > MAX_TITLE:
        return False
    if JUNK.match(line) or BRACKETED.match(line) or FRAGMENT.match(line) or INITIALS.match(line):
        return False
    if LETTERHEAD.match(line) and line.count(" ") <= 3:
        return False
    if line[:1].islower():
        # «организационному обеспечению департамента…» — это хвост предыдущей
        # строки, а не заголовок: название документа с маленькой буквы не начинается
        return False
    return line.count(" ") >= 1          # одно слово — скорее колонтитул, чем заголовок


def title_from(name: str, data: bytes) -> str:
    """Название, которое сервис предложит человеку.

    Берём первую осмысленную строку документа: в справках и информациях она и есть
    заголовок («Информация по докладу Минстроя России»). Не нашли — вернём пустое,
    и название будет собрано из имени файла.

    В Word заголовок часто разбит на абзацы — «О строительстве … бульвар Архитекторов»
    и отдельной строкой «(2026 год)». Склеиваем такие строки: иначе вторая половина
    заголовка уезжает в название соседнего документа.
    """
    rows = lines(name, data)[:40]
    skip_until = -1
    for i, line in enumerate(rows):
        if ADDRESSEE.match(line):
            skip_until = i + ADDRESS_BLOCK      # весь угловой штамп разом
            continue
        if i <= skip_until and (FRAGMENT.match(line) or INITIALS.match(line)
                                or line.count(" ") <= 3):
            continue
        if not _headline(line):
            continue
        title = line
        for tail in rows[i + 1:i + 4]:
            if not tail or title.endswith((".", "!", "?", ":", ";")):
                break
            if not CONTINUES.match(tail) or len(title) + len(tail) > MAX_TITLE:
                break
            title = f"{title} {tail}"
        return _with_number(wording.shorten(title.strip(" .;:")), rows, skip=i)
    return ""


def propose(origin: str, data: bytes) -> str:
    """Название, которое сервис предложит человеку: жанр документа и его суть.

    Жанр ищем в трёх местах сразу — в самом заголовке, в имени присланного файла
    и в первых строках текста. «02. Список участников.docx» с шапкой письма внутри
    должен называться «Список участников», а не тем, что попалось первым.
    """
    by_name = naming.from_filename(origin)
    stem = naming.parse(origin).title or naming.split_ext(origin)[0]
    # имя файла человек дал осознанно; заголовок внутри — что попалось первым
    raw = by_name if naming.looks_human(stem) else (title_from(origin, data) or by_name)
    head = "\n".join(lines(origin, data)[:6])
    return wording.entitle(raw, f"{origin}\n{head}")


def _with_number(title: str, rows: list[str], skip: int) -> str:
    """Дописать номер письма, если он есть в штампе и его нет в самом заголовке."""
    if not title or "№" in title:
        return title
    for i, line in enumerate(rows[:15]):
        if i == skip or len(line) > 60:
            continue
        m = DOC_NUMBER.search(line)
        if m:
            when = f" от {m.group(2)}" if m.group(2) else ""
            return f"{title} № {m.group(1)}{when}"
    return title
