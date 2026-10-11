"""Редактор листа — это тот же `a3-kit.html`, что работает отдельным файлом.

Сервис только вшивает в него данные листа и добавляет кнопку «Сохранить
в библиотеку». Сам файл на диске не меняется: без сервиса он открывается
как раньше, без интернета и без кнопки сохранения.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from . import config

DATA_TAG = re.compile(
    r'(<script[^>]*id="a3-data"[^>]*>)(.*?)(</script>)', re.DOTALL | re.IGNORECASE
)


# Пустой лист: в сервисе показывать пример незачем, человек начинает со своего.
BLANK: dict[str, Any] = {
    "orientation": "landscape",
    "header": {"title": "Название листа", "meta": ["Период", "Кто готовил"]},
    "blocks": [
        {"type": "kpis", "items": [
            {"label": "Показатель", "value": "0", "lead": True},
            {"label": "Показатель", "value": "0"},
            {"label": "Показатель", "value": "0"},
        ]},
        {"type": "text", "title": "О чём лист", "text": "Замените этот текст."},
        {"type": "conclusions", "title": "Выводы", "items": ["Первый вывод."]},
    ],
    "source": {"text": "Источник: ", "date": ""},
}


def read_kit() -> str:
    if not config.KIT_PATH.exists():
        raise HTTPException(status_code=503, detail="Конструктор a3-kit.html не найден")
    return config.KIT_PATH.read_text(encoding="utf-8")


def embed(html: str, data: dict[str, Any] | None) -> str:
    if data is None:
        return html
    text = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    html, count = DATA_TAG.subn(lambda m: m.group(1) + "\n" + text + "\n" + m.group(3), html, count=1)
    if not count:
        raise HTTPException(status_code=503, detail="Конструктор без блока данных — пересоберите a3-kit.html")
    return html


def _insert_before_body(html: str, extra: str) -> str:
    i = html.lower().rfind("</body>")
    return html[:i] + extra + html[i:] if i != -1 else html + extra


def service_page(data: dict[str, Any] | None, state: dict[str, Any], service_js: str) -> str:
    """Лист в сервисе: данные вшиты, добавлены кнопки библиотеки."""
    html = embed(read_kit(), data)
    state_json = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    extra = (
        '\n<script type="application/json" id="a3-service-state">\n'
        + state_json
        + "\n</script>\n<script>\n"
        + service_js
        + "\n</script>\n"
    )
    return _insert_before_body(html, extra)


def plain_page(data: dict[str, Any]) -> str:
    """Файл для скачивания: обычный самодостаточный лист, без сервиса."""
    return embed(read_kit(), data)


def file_name(title: str, ext: str = "html") -> str:
    clean = re.sub(r'[\\/:*?"<>|\n\r]+', " ", title or "A3-лист")
    return " ".join(clean.split())[:80] + "." + ext
