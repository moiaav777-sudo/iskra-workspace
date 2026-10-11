"""Общая сверка чисел с оригиналом: одно место на всё пространство.

«Бриф» подсвечивает жёлтым неподтверждённые числа, «Тезисы» подтверждают показатели —
раньше это были два независимых движка на одну задачу, и они неизбежно разошлись бы.
Здесь собрано общее: как выглядит число в русском тексте, как его нормализовать
и как убедиться, что оно действительно есть в источнике.

ЭТОТ ФАЙЛ ПРАВИТСЯ ТОЛЬКО В `portal/shared/numbers_common.py`.
В сервисы он раскладывается скриптом `portal/scripts/sync_shared.sh`.
"""
from __future__ import annotations

import re

# 1 234,5 · 1234.5 · 62,4 % · 15 млн — хвосты единиц отсекаем отдельно
NUMBER = re.compile(r"-?\d[\d\s\u00a0]*(?:[.,]\d+)?")


def normalize(value: str) -> str:
    """Число к единому виду: без пробелов-разделителей, с точкой вместо запятой."""
    text = (value or "").replace("\u00a0", " ").strip()
    text = re.sub(r"(?<=\d)[\s](?=\d)", "", text)
    text = text.replace(",", ".")
    text = re.sub(r"\.0+$", "", text)
    return text


def numbers_in(text: str) -> list[str]:
    """Все числа текста в нормальном виде — для сверки и поиска расхождений."""
    return [normalize(m.group(0)) for m in NUMBER.finditer(text or "")]


def found_in_source(value: str, source: str) -> bool:
    """Есть ли это число в источнике дословно.

    Сравниваем нормализованные значения, а не строки: в справке «62,4», в ответе «62.4» —
    это одно и то же число, и придираться к запятой нельзя.
    """
    needle = normalize(value)
    if not needle:
        return False
    return needle in set(numbers_in(source))


def conflicts_in(source: str) -> list[dict]:
    """Противоречия самого источника: одно и то же число названо по-разному рядом.

    Пока ищем простой и частый случай — доля больше ста процентов.
    Остальные виды расхождений добавляются сюда, а не в сервисы.
    """
    out: list[dict] = []
    for match in re.finditer(r"(-?\d[\d\s\u00a0]*(?:[.,]\d+)?)\s*%", source or ""):
        value = normalize(match.group(1))
        try:
            number = float(value)
        except ValueError:
            continue
        if number > 100 or number < 0:
            out.append({"kind": "доля вне 0–100", "value": match.group(0).strip(),
                        "detail": f"в источнике стоит {match.group(0).strip()}"})
    return out
