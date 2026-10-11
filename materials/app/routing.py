"""Куда документ из приёмной: команда человека → вид документа → выбор.

Задача пользователя от 27.09: документ приходит с телефона, и рядом с ним можно
оставить пару слов — «напиши бриф», «это в повестку». Слова главнее всего.
Без слов решает вид документа, узнанный теми же приметами, что и нумерация
в папке (`naming.role_of`): повестка и список участников уходят в «Повестку»
сами, справка — только по выбору человека одним нажатием, «Модератор» и «Бриф»
никуда не идут, они уже готовые.

Здесь только решение. Отправляет браузер, под тем человеком, который прислал:
так «Повестка» зовёт «Совещание», без общих секретов (контракт, п. 9).
"""
from __future__ import annotations

from . import naming

# Куда умеем отправлять. Ключи уходят в браузер и в журнал, поэтому — только эти.
TARGETS = {
    "agenda": "Совещание",
    "participants": "Совещание",
    "brief": "Бриф",
    "a3": "A3",
    "keep": "",                     # «просто сохранить» — документ остаётся здесь
}

# Порядок важен: «список участников в повестку» — это участники, а не повестка.
COMMANDS = [
    ("participants", ("участник",)),
    ("agenda", ("повестк",)),
    ("brief", ("бриф", "саммари")),
    ("a3", ("одностраничник", "один лист", "а3", "a3", "дашборд")),
    ("keep", ("сохран", "просто", "в папку", "положи")),
]

# Что умеет узнавать нумерация — и что это значит для маршрута.
KIND_BY_ROLE = {1: "agenda", 2: "participants", 3: "moderator", 4: "brief"}
KIND_TITLES = {"agenda": "повестку", "participants": "список участников",
               "moderator": "документ ведущего", "brief": "бриф"}

MIN_TEXT = 200      # столько знаков просят и «Бриф», и A3, — короче нечего им отдавать
SCAN_TEXT = 50      # меньше — текст не прочитался (скан): «Искре» разбирать нечего


def command(note: str) -> str | None:
    """Что велел человек. Не поняли — None: тогда спросим, а не угадаем."""
    low = (note or "").lower()
    if not low.strip():
        return None
    for target, words in COMMANDS:
        if any(w in low for w in words):
            return target
    return None


def kind_of(filename: str, title: str, head: str) -> tuple[str, bool]:
    """Вид документа и уверенность в нём.

    Три приметы те же, что в папке: имя файла, название, первые строки. Уверены,
    когда приметы не спорят между собой. Спорят — «неуверенно», и выбирать за
    человека нельзя: молча отправленная в «Повестку» справка хуже лишнего вопроса.
    """
    stem = naming.split_ext(filename or "")[0]
    seen = {naming.role_of(clue) for clue in (stem, title, head)}
    seen.discard(None)
    kinds = sorted({KIND_BY_ROLE[r] for r in seen if r in KIND_BY_ROLE})
    if not kinds:
        return "other", True
    if len(kinds) == 1:
        return kinds[0], True
    return "|".join(kinds), False


def decide(note: str, kind: str, sure: bool, text_len: int) -> dict:
    """Решение по документу: `to` — куда отправить сразу, `ask` — какие кнопки
    показать человеку. Пусто и там и там — ничего не делаем, как раньше."""
    said = command(note)
    if note and said:
        return {"to": said, "ask": [], "why": "по команде"}
    if note:
        return {"to": "", "ask": ["brief", "a3", "keep"],
                "why": "команду не поняли — выберите сами"}
    if not sure:
        maybe = [k for k in kind.split("|") if k in ("agenda", "participants")]
        named = " и на ".join(KIND_TITLES[k] for k in kind.split("|"))
        return {"to": "", "ask": maybe + ["brief", "a3", "keep"],
                "why": f"похоже на {named} — выберите сами"}
    if kind in ("agenda", "participants"):
        if text_len < SCAN_TEXT:        # узнали по имени файла, а текста нет — решает человек
            return {"to": "", "ask": [kind, "keep"],
                    "why": f"похоже на {KIND_TITLES[kind]}, но текст не прочитался — скан?"}
        return {"to": kind, "ask": [], "why": f"это {KIND_TITLES[kind]}"}
    if kind in ("moderator", "brief"):
        return {"to": "", "ask": [], "why": ""}
    if text_len < MIN_TEXT:
        return {"to": "", "ask": [], "why": ""}          # картинка, таблица, пустышка
    return {"to": "", "ask": ["brief", "a3", "keep"], "why": "похоже на справку"}
