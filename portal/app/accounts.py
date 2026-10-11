"""Новая учётная запись: логин из фамилии и временный пароль (решение пользователя 21.09).

Логин — фамилия латиницей: «Кадочников Павел Анатольевич» → `kadochnikov`. Схема читаемая,
как в привычных логинах, а не паспортная («ia», «iu» в логине выглядят странно): й → y,
ю → yu, я → ya, х → kh, ц → ts, щ → shch, мягкий и твёрдый знаки опускаются.
Если такой логин занят — добавляются инициалы (`kadochnikov.pa`), потом номер.

Пароль — 12 знаков из букв и цифр без похожих (0/O, 1/l/I): его переписывают из письма
руками. При первом входе портал всё равно попросит сменить его на свой.
"""
from __future__ import annotations

import re
import secrets

from . import auth, db

_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
       "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
       "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
       "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}

_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def translit(text: str) -> str:
    out = []
    for ch in (text or "").lower():
        if ch in _TR:
            out.append(_TR[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "-"):
            out.append(ch)
    return re.sub(r"-{2,}", "-", "".join(out)).strip("-")


def suggest_login(full_name: str) -> str:
    """Свободный логин по ФИО. Пустое ФИО — пустой ответ: придумывать логин не из чего."""
    words = (full_name or "").split()
    if not words:
        return ""
    base = translit(words[0])[:32]
    if len(base) < 2:
        return ""
    if not db.get_user(base):
        return base
    initials = "".join(translit(w)[:1] for w in words[1:3])
    if initials and not db.get_user(f"{base}.{initials}"):
        return f"{base}.{initials}"
    for n in range(2, 100):
        if not db.get_user(f"{base}{n}"):
            return f"{base}{n}"
    return ""


def generate_password(login: str = "", length: int = 12) -> str:
    """Временный пароль, который проходит правила портала и не содержит логина."""
    while True:
        pw = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (any(c.isdigit() for c in pw) and any(c.islower() for c in pw)
                and any(c.isupper() for c in pw) and not auth.password_problem(pw, login)):
            return pw
