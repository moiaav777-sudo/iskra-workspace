"""Сведения объективки: одна форма для «Искры», экрана и документа Word.

    fio            Фамилия Имя Отчество
    birth_date     «12 марта 1970 г.»
    birth_place    «г. Москва»
    position       текущая должность (как пишется в официальной биографии)
    organization   организация, если её нет в должности
    category       deputy | official | scientist | other — подсказка, какие ключевые данные нужны
    key_facts      [{label, value, src}] — фракция, регионы, комитет / ведомство, классный чин /
                   степень, звание
    powers         [{text, src}] — полномочия, должности по совместительству
    education      [{year, text, src}] — год — учебное заведение, специальность
    career         [{period, text, src}] — период — должность
    awards         [{year, text, src}] — год — награда, звание
    photo_url      ссылка на официальное фото (только текстом: сами ничего не скачиваем)
    refs           [{name, url, about}] — источники
    note           что «Искра» не нашла, однофамильцы, расхождения

`src` у пункта — откуда он: «справка» или ссылка на открытый источник. В Word не идёт.
"""
from __future__ import annotations

import re

CATEGORIES = ("deputy", "official", "scientist", "other")
LISTS = {
    "key_facts": ("label", "value", "src"),
    "powers": ("text", "src"),
    "education": ("year", "text", "src"),
    "career": ("period", "text", "src"),
    "awards": ("year", "text", "src"),
    "refs": ("name", "url", "about"),
}
STRINGS = ("fio", "birth_date", "birth_place", "position", "organization", "category", "photo_url", "note")
MAX_TEXT = 600
MAX_ITEMS = 40
SECTION_TITLES = {"powers": "Полномочия", "education": "Образование", "career": "Опыт работы",
                  "awards": "Награды, почётные звания, поощрения", "key_facts": "Ключевые данные"}
DOC_SOURCE = "справка"

_LINK = re.compile(r"^https?://[^\s<>\"']{4,800}$", re.I)
_SPACES = re.compile(r"\s+")


def link(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _LINK.match(text) else ""


def text_of(value, limit: int = MAX_TEXT) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if isinstance(value, list):
        value = " ".join(text_of(v) for v in value if v)
    if not isinstance(value, str):
        return ""
    value = _SPACES.sub(" ", value.replace(" ", " ")).strip()
    # маркеры разметки нейросети в документ не идут
    value = re.sub(r"\*\*|__|`", "", value)
    return value[:limit]


def _items(value, fields: tuple[str, ...]) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:MAX_ITEMS * 2]:
        if isinstance(item, str):
            item = {fields[1] if fields[0] in ("year", "period", "label") else fields[0]: item}
        if not isinstance(item, dict):
            continue
        row = {f: text_of(item.get(f)) for f in fields}
        if "url" in row:
            row["url"] = link(item.get("url"))
        if "src" in row and row["src"] and row["src"].lower() != DOC_SOURCE and not link(row["src"]):
            row["src"] = row["src"][:200]
        if any(v for k, v in row.items() if k != "src"):
            out.append(row)
    return out[:MAX_ITEMS]


def first_year(text: str) -> int:
    m = re.search(r"(1[89]\d\d|20\d\d)", text or "")
    return int(m.group(1)) if m else 9999


def clean(data: dict | None, sort: bool = False) -> dict:
    """Привести сведения к форме. sort=True — по возрастанию лет (ответ «Искры»);
    правку человека не сортируем: порядок он выставляет сам."""
    data = data if isinstance(data, dict) else {}
    out: dict = {k: text_of(data.get(k)) for k in STRINGS}
    out["fio"] = text_of(data.get("fio"), 200)
    out["note"] = text_of(data.get("note"), 1500)
    out["photo_url"] = link(data.get("photo_url"))
    if out["category"] not in CATEGORIES:
        out["category"] = "other" if out["category"] else ""
    for key, fields in LISTS.items():
        out[key] = _items(data.get(key), fields)
    if sort:
        out["education"].sort(key=lambda r: first_year(r["year"] or r["text"]))
        out["career"].sort(key=lambda r: first_year(r["period"] or r["text"]))
        out["awards"].sort(key=lambda r: first_year(r["year"] or r["text"]))
    return out


def schema() -> dict:
    """JSON-схема ответа «Искры» (response_format: json_schema, строгая — все поля обязательны)."""
    s = {"type": "string"}

    def obj(*fields):
        return {"type": "object", "properties": {f: s for f in fields},
                "required": list(fields), "additionalProperties": False}

    props = {k: s for k in STRINGS}
    props["category"] = {"type": "string", "enum": list(CATEGORIES)}
    for key, fields in LISTS.items():
        props[key] = {"type": "array", "items": obj(*fields)}
    props["powers"] = {"type": "array", "items": obj("text", "src")}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ── что видно в документе ──────────────────────────────────────────────

def female(fio: str) -> bool:
    parts = (fio or "").split()
    if len(parts) >= 3 and not re.match(r"^[А-ЯЁ]\.", parts[1]):
        return parts[2].lower().endswith(("вна", "чна", "шна", "кызы", "гызы"))
    return bool(parts) and parts[0].lower().endswith(("ова", "ева", "ёва", "ина", "ына", "ская", "цкая"))


def birth_line(data: dict) -> str:
    date, place = data.get("birth_date") or "", data.get("birth_place") or ""
    if not date and not place:
        return ""
    word = "Родилась" if female(data.get("fio", "")) else "Родился"
    return " ".join(x for x in (word, date + ("," if date and place else ""), place) if x)


def position_line(data: dict) -> str:
    pos, org = data.get("position") or "", data.get("organization") or ""
    if org and org.lower() not in pos.lower():
        return f"{pos}, {org}" if pos else org
    return pos


def key_lines(data: dict) -> list[str]:
    lines = []
    birth = birth_line(data)
    if birth:
        lines.append(birth)
    for row in data.get("key_facts") or []:
        if row.get("label") and row.get("value"):
            lines.append(f"{row['label'].rstrip(':')}: {row['value']}")
        elif row.get("value") or row.get("label"):
            lines.append(row.get("value") or row.get("label"))
    return lines


def dated(left: str, text: str) -> str:
    return f"{left} – {text}" if left and text else (text or left)


_FIO_WORD = re.compile(r"^(?:[А-ЯЁ][а-яё]+|[А-ЯЁ]{2,})(?:-(?:[А-ЯЁ][а-яё]+|[А-ЯЁ]{2,}))?$")
_INITIALS = re.compile(r"^(?:[А-ЯЁ]\.\s?){1,2}$")


def looks_like_fio(text: str) -> bool:
    """«Фамилия Имя Отчество» (или «Фамилия И.О.»): 2–4 слова с заглавной, только кириллица.

    По этому признаку объединение узнаёт объективку: ФИО в первой строке или первой ячейке.
    Восточные отчества («Ибрагим оглы») — четвёртым словом со строчной.
    """
    words = (text or "").replace(" ", " ").split()
    if 2 <= len(words) <= 3 and all(_INITIALS.match(w) for w in words[1:]):
        return bool(_FIO_WORD.match(words[0]))
    if not 2 <= len(words) <= 4:
        return False
    head, tail = words[:3], words[3:]
    if tail and tail[0].lower() not in ("оглы", "кызы", "улы", "гызы"):
        return False
    return all(_FIO_WORD.match(w) for w in head)
