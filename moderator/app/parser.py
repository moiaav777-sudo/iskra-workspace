"""Детерминированный разбор сырых текстов → структура совещания.

Без LLM, без внешних запросов (приватность). Best-effort: распознаёт типовые
форматы повестки, списков участников и поручений; нераспознанное возвращает
как есть — для ручной правки в интерфейсе.
"""
from __future__ import annotations

import re

# ── утилиты ──────────────────────────────────────────────────────────────

AGENDA_NUM = re.compile(r"^\s*([IVXLCDM]+|\d+)\s*[.)]?\s+", re.IGNORECASE)
TIME_RANGE = re.compile(r"(\d{1,2}:\d{2})\s*[—–\-]\s*(\d{1,2}:\d{2})")
SPEAKER_MARK = re.compile(r"(?:докл\.?\s*|докладчик:?\s*|выступа(?:ет|ют):?\s*)(.+)", re.IGNORECASE)
DEADLINE = re.compile(
    r"(?:срок\s*[—–\-]?\s*|до\s+)(\d{1,2}\s+[а-яё]+\s+\d{4}\s*г?\.?|\d{1,2}\s+[а-яё]+\s*г?\.?)",
    re.IGNORECASE,
)
ASSIGNEE_LEAD = re.compile(
    r"^\s*([А-ЯЁ][^,;:()]*(?:\([^)]*\))?)\s*(?:совместно\s+с\s+.*?(?=представ|внес|подгот|реализ|обеспеч|прораб|разраб|утверд|направ|согласуй|организуй))",
    re.IGNORECASE,
)

_ORG_HINTS = (
    "министерство", "агентство", "служба", "департамент", "управление", "правительство",
    "комитет", "центр", "университет", "институт", "компания", "банк", "госкорпорация",
    "фонд", "администрация", "организация",
)
_ROLE_HINTS = (
    "министр", "заместитель", "руководитель", "начальник", "директор", "советник",
    "специалист", "председатель", "и.о.", "профессор", "академик", "ректор", "главный",
)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _is_org(s: str) -> bool:
    low = s.lower()
    return any(h in low for h in _ORG_HINTS) and not any(h in low for h in _ROLE_HINTS)


def _is_role(s: str) -> bool:
    return any(h in s.lower() for h in _ROLE_HINTS)


# ── повестка ─────────────────────────────────────────────────────────────

def parse_agenda(text: str) -> list[dict]:
    items: list[dict] = []
    for line in _lines(text):
        num = ""
        rest = line
        m = AGENDA_NUM.match(rest)
        if m:
            num = m.group(1).upper()
            rest = rest[m.end():].strip()
        t = TIME_RANGE.search(rest)
        time_str = ""
        if t:
            time_str = f"{t.group(1)} — {t.group(2)}"
            rest = (rest[: t.start()] + rest[t.end():]).strip()
        speakers: list[str] = []
        sm = SPEAKER_MARK.search(rest)
        if sm:
            speakers = [s.strip() for s in re.split(r"[;,]|(?:\s+и\s+)", sm.group(1)) if s.strip()]
            rest = (rest[: sm.start()] + rest[sm.end():]).strip()
        title = rest.strip(" -–—").strip()
        if not title and not num:
            continue
        items.append({"num": num, "title": title, "time": time_str, "speakers": speakers})
    return items


# ── участники ────────────────────────────────────────────────────────────

def parse_participants(text: str) -> list[dict]:
    out: list[dict] = []
    for line in _lines(text):
        if len(line) < 3 or line.startswith(("№", "#")):
            continue
        if "—" in line or "–" in line:
            parts = [p.strip() for p in re.split(r"[—–]", line) if p.strip()]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"[;,]", line) if p.strip()]
        if not parts:
            continue
        name = parts[0]
        role, org = "", ""
        if len(parts) >= 3:
            role, org = parts[1], parts[2]
        elif len(parts) == 2:
            second = parts[1]
            if _is_org(second):
                org = second
            else:
                role = second
        out.append({"name": name, "role": role, "organization": org})
    return out


# ── поручения ────────────────────────────────────────────────────────────

def parse_orders(text: str) -> list[dict]:
    out: list[dict] = []
    for line in _lines(text):
        if len(line) < 5:
            continue
        line = re.sub(r"^\s*(\d+)\s*[.)]\s*", "", line)  # убрать порядковый номер
        deadline = ""
        dm = DEADLINE.search(line)
        if dm:
            deadline = dm.group(1).strip()
            line = (line[: dm.start()] + line[dm.end():]).strip(" ,;.-–—")
        assignee = ""
        am = ASSIGNEE_LEAD.match(line)
        if am:
            assignee = am.group(1).strip(" ,.-–—")
            line = line[am.end():].strip()
        out.append({"text": line.strip(), "assignee": assignee, "deadline": deadline})
    return out
