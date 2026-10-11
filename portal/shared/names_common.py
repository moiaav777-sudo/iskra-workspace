"""Склонение ФИО — одно правило на всё пространство.

Нужно в двух местах: «прошу доложить **кого?**» в документе ведущего (винительный) и
«Саммари доклада **кого?**» в «Брифе» (родительный). Раньше «Совещание» склоняло своими
правилами, и «Павел» становился «Павела»: правило «прибавить -а» не знало беглых гласных.
«Бриф» и вовсе просил человека вписывать падеж руками.

Правится этот файл, копии раскладывает `portal/scripts/sync_shared.sh`.

Понимает и полную запись («Кадочников Павел Анатольевич»), и короткую («Кадочников П.А.»).
Пол узнаётся по отчеству (-вна/-чна — женский), без отчества — по фамилии.
"""
from __future__ import annotations

import re

_INITIALS = re.compile(r"^(?:[А-ЯЁ]\.\s*){1,2}$")

# Беглая гласная: Павел → Павла, Лев → Льва, Пётр → Петра. Список короткий и закрытый —
# это имена, а не любые слова, поэтому надёжнее перечислить, чем угадывать.
_FLEETING = {"павел": "павл", "лев": "льв", "пётр": "петр", "петр": "петр"}
_HUSHING = set("гкхжшчщ")      # после них в родительном «-и», а не «-ы»


def _case(src: str, out: str) -> str:
    """Регистр как в исходном: «ПАВЕЛ» → «ПАВЛА», «Павел» → «Павла»."""
    if src.isupper():
        return out.upper()
    if src[:1].isupper():
        return out[:1].upper() + out[1:]
    return out


def _female(parts: list[str]) -> bool:
    if len(parts) >= 3 and not _INITIALS.match(parts[1]):
        return parts[2].lower().endswith(("вна", "чна", "шна"))
    surname = parts[0].lower()
    return surname.endswith(("ова", "ева", "ёва", "ина", "ына", "ская", "цкая", "ая"))


def _given(name: str, female: bool, case: str) -> str:
    low = name.lower()
    if low in _FLEETING and not female:
        return _case(name, _FLEETING[low] + "а")
    if low.endswith("а"):
        stem = low[:-1]
        if case == "acc":
            return _case(name, stem + "у")
        return _case(name, stem + ("и" if stem[-1:] in _HUSHING else "ы"))
    if low.endswith("я"):
        stem = low[:-1]
        if case == "acc":
            return _case(name, stem + "ю")
        return _case(name, stem + "и")
    if female:
        if low.endswith("ь"):                     # Любовь
            return _case(name, low if case == "acc" else low[:-1] + "и")
        return name                               # женское на согласную не склоняется
    if low.endswith(("ий", "ей", "ай", "ой")):   # Дмитрий, Сергей, Николай
        return _case(name, low[:-1] + "я")
    if low.endswith("ь"):                         # Игорь
        return _case(name, low[:-1] + "я")
    return _case(name, low + "а")                 # Иван, Михаил


def _patronymic(patr: str, female: bool, case: str) -> str:
    low = patr.lower()
    if female and low.endswith("на"):
        return _case(patr, low[:-1] + ("у" if case == "acc" else "ы"))
    if low.endswith("ич"):
        return _case(patr, low + "а")
    return patr


def _surname(sur: str, female: bool, case: str) -> str:
    low = sur.lower()
    if "-" in sur:                                # двойная: склоняется каждая часть
        return "-".join(_surname(p, female, case) for p in sur.split("-"))
    if low.endswith(("о", "е", "и", "у", "ю", "ых", "их")):
        return sur                                # Шевченко, Черных — не склоняются
    if female:
        if low.endswith(("ова", "ева", "ёва", "ина", "ына")):
            return _case(sur, low[:-1] + ("у" if case == "acc" else "ой"))
        if low.endswith("ая"):                    # Высоцкая
            return _case(sur, low[:-2] + ("ую" if case == "acc" else "ой"))
        return sur                                # женская на согласную не склоняется
    if low.endswith(("ский", "цкий", "ый", "ой", "ий")):
        return _case(sur, low[:-2] + "ого")       # Высоцкий, Толстой
    if low.endswith("а"):                         # мужская на -а: Кочерга
        stem = low[:-1]
        return _case(sur, stem + ("у" if case == "acc" else ("и" if stem[-1:] in _HUSHING else "ы")))
    if low.endswith("я"):
        return _case(sur, low[:-1] + ("ю" if case == "acc" else "и"))
    if low.endswith("й"):                         # Гайдай
        return _case(sur, low[:-1] + "я")
    if low.endswith("ь"):                         # Гоголь
        return _case(sur, low[:-1] + "я")
    return _case(sur, low + "а")                  # Кадочников, Шмидт


def _decline(fio: str, case: str) -> str:
    parts = " ".join((fio or "").split()).split(" ")
    if not parts or not parts[0]:
        return fio or ""
    female = _female(parts)
    out = [_surname(parts[0], female, case)]
    rest = parts[1:]
    if rest and _INITIALS.match(rest[0].replace(" ", "")):
        out.extend(rest)                           # «Кадочников П.А.» — инициалы не склоняются
    else:
        if len(rest) >= 1:
            out.append(_given(rest[0], female, case))
        if len(rest) >= 2:
            out.append(_patronymic(rest[1], female, case))
        out.extend(rest[2:])
    return " ".join(out)


def accusative(fio: str) -> str:
    """Винительный: «прошу доложить **Кадочникова Павла Анатольевича**»."""
    return _decline(fio, "acc")


def genitive(fio: str) -> str:
    """Родительный: «саммари доклада **Кадочникова Павла Анатольевича**»."""
    return _decline(fio, "gen")
