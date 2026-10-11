"""Умная загрузка списков участников в реестр людей (решение пользователя 21.09).

Администратор прикладывает несколько списков участников (.docx), портал находит в них
людей и показывает таблицу на проверку: ФИО, должность, ведомство, чем запись отличается
от уже внесённой. В реестр попадает только то, что человек отметил и подтвердил, —
разбор сам ничего не записывает. Поэтому здесь нет «Искры»: списки участников устроены
просто (таблица «ФИО | должность» или строки «Фамилия Имя Отчество — должность»),
а ошибку разбора человек видит и правит до записи.

Библиотек для Word в портале нет: .docx — это zip с XML, его хватает прочитать напрямую.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_PEOPLE = 1000

_WORD = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_CAPS = r"[А-ЯЁ]{2,}(?:-[А-ЯЁ]{2,})?"
_PATRONYMIC = r"[А-ЯЁ][а-яё]+(?:вич|вна|ична|инична|оглы|кызы|улы)"
_FULL = re.compile(rf"^({_WORD}|{_CAPS})\s+({_WORD})\s+({_PATRONYMIC})$")
_CAPS_TWO = re.compile(rf"^({_CAPS})\s+({_WORD})$")                 # «ИВАНОВ Иван» — фамилия заглавными
_INITIALS = re.compile(rf"^({_WORD}|{_CAPS})\s+([А-ЯЁ])\.\s?([А-ЯЁ])\.?$")
_INITIALS_FIRST = re.compile(rf"^([А-ЯЁ])\.\s?([А-ЯЁ])\.\s?({_WORD}|{_CAPS})$")
_LEAD = re.compile(r"^\s*(?:№\s*)?\d{1,3}[.)]?\s+")
_SPLIT = re.compile(r"\s+[—–-]\s+|\s*,\s+|\s*:\s+")


def _surname(s: str) -> str:
    return "-".join(p[:1].upper() + p[1:].lower() for p in s.split("-"))


def as_name(text: str) -> str:
    """ФИО, если строка — это ФИО и ничего больше; иначе пустая строка."""
    s = _LEAD.sub("", " ".join((text or "").split())).strip(" .,;")
    if m := _FULL.match(s):
        return f"{_surname(m[1])} {m[2]} {m[3]}"
    if m := _CAPS_TWO.match(s):
        return f"{_surname(m[1])} {m[2]}"
    if m := _INITIALS.match(s + ("." if not s.endswith(".") else "")):
        return f"{_surname(m[1])} {m[2]}.{m[3]}."
    if m := _INITIALS_FIRST.match(s):
        return f"{_surname(m[3])} {m[1]}.{m[2]}."
    return ""


def _name_and_rest(text: str) -> tuple[str, str]:
    """«Иванов Иван Иванович — министр …» → (ФИО, должность)."""
    s = _LEAD.sub("", " ".join(text.split()))
    parts = _SPLIT.split(s, maxsplit=1)
    if len(parts) == 2 and (name := as_name(parts[0])):
        return name, parts[1].strip(" .;")
    return "", ""


def _text(el) -> str:
    out = []
    for node in el.iter():
        if node.tag == W + "t" and node.text:
            out.append(node.text)
        elif node.tag in (W + "tab", W + "br"):
            out.append(" ")
    return " ".join("".join(out).split())


def _cell_paragraphs(tc) -> list[str]:
    return [t for t in (_text(p) for p in tc.iter(W + "p")) if t]


def _is_filler(text: str) -> bool:
    return not text or bool(re.fullmatch(r"[\d\s.№)–—-]+", text))


def _cell_name(cell: list[str]) -> tuple[str, list[str]]:
    """ФИО в начале ячейки и то, что после него. В списках «Повестки» фамилия стоит
    отдельным абзацем заглавными, имя и отчество — следующим."""
    if len(cell) >= 2 and (name := as_name(cell[0] + " " + cell[1])):
        return name, cell[2:]
    if name := as_name(cell[0]):
        return name, cell[1:]
    return "", cell


def _from_row(cells: list[list[str]]) -> dict | None:
    texts = [" ".join(c) for c in cells]
    for i, cell in enumerate(cells):
        if not cell:
            continue
        name, rest = _cell_name(cell)
        if name:
            role = " ".join(rest)                         # ФИО и должность в одной ячейке, разными абзацами
            after = [t for t in texts[i + 1:] if not _is_filler(t)]
            before = [t for t in texts[:i] if not _is_filler(t)]
            if not role and after:
                role = after[0]
                org = ", ".join(after[1:] + before)
            else:
                org = ", ".join(after + before)
            return {"name": name, "role": role, "organization": org}
        name, role = _name_and_rest(cell[0])
        if name:
            return {"name": name, "role": " ".join([role] + cell[1:]).strip(), "organization": ""}
    return None


def parse_docx(data: bytes) -> list[dict]:
    """Люди из списка участников по порядку документа, без повторов."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as ex:
        raise ValueError("Это не документ Word (.docx) или он повреждён") from ex
    body = root.find(W + "body")
    found: list[dict] = []
    pending = ""                                          # ФИО отдельной строкой — должность в следующей
    surname = ""                                          # фамилия заглавными отдельной строкой
    section = ""
    for block in (body if body is not None else []):
        if block.tag == W + "tbl":
            pending = ""
            for tr in block.iter(W + "tr"):
                cells = [_cell_paragraphs(tc) for tc in tr.findall(W + "tc")]
                person = _from_row(cells)
                if person:
                    person["section"] = section
                    found.append(person)
                else:
                    filled = [" ".join(c) for c in cells if not _is_filler(" ".join(c))]
                    if len(filled) == 1 and len(filled[0]) < 200:
                        section = filled[0]
        elif block.tag == W + "p":
            text = _text(block)
            if not text:
                continue
            if surname and (name := as_name(surname + " " + text)):
                surname = ""
                if pending:
                    found.append({"name": pending, "role": "", "organization": ""})
                pending = name
                continue
            surname = text if re.fullmatch(_CAPS, text) else ""
            if surname:
                continue
            name = as_name(text)
            if name:
                if pending:
                    found.append({"name": pending, "role": "", "organization": ""})
                pending = name
                continue
            if pending:
                if len(text) < 400:
                    found.append({"name": pending, "role": text.strip(" .;"), "organization": ""})
                pending = ""
                continue
            name, role = _name_and_rest(text)
            if name:
                found.append({"name": name, "role": role, "organization": ""})
        if len(found) > MAX_PEOPLE:
            break
    if pending:
        found.append({"name": pending, "role": "", "organization": ""})
    seen, out = set(), []
    for p in found:
        k = p["name"].lower().replace("ё", "е")
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out[:MAX_PEOPLE]
