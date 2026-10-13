"""HTML трёх документов записи — для карточки (блоки общего компонента) и для «Совещания».

Решение пользователя 13.10 («каждый раздел — отдельным блоком для форматирования, как в модераторе;
единый формат»): в карточке транскрипция, саммари и поручения показываются блоками
`portal/static/blocks.js` и правятся редактором с оформлением. Оформление человека хранится HTML
(`draft_html`, `summary_html`, у поручения — ключ `html`); пока его нет, HTML выводится из текста
здесь. Разбор и очистка — общий модуль `richtext_common.py` (копия `portal/shared/richtext.py`).

Его зовут и `db.get_record`, и `main.results_payload`, а шаблоны таймкода и говорящего берёт
отсюда же `docx_export` (раньше `_transcript_html`/`_paragraphs_html` жили в `main.py`).
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

from . import orders_docx, richtext_common

# «[05:12] Говорящий 1: текст» — таймкод абзаца и подпись говорящего (их же печатает Word)
PARA = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$", re.DOTALL)
SPEAKER = re.compile(
    r"^((?:Говорящий|Говорящая|Спикер|Участник|Участница|Собеседник|Голос)\s*№?\s*\d{1,2}"
    r"|[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.\s?)?[А-ЯЁ][а-яё-]+"
    r"|[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s?(?:[А-ЯЁ]\.)?"
    r"|[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2}):\s+(.+)$", re.DOTALL)


def transcript_html(text: str, mark: bool = True) -> str:
    """Транскрипция → абзацы: таймкод в `<span class="tc">[05:12]</span>`, говорящий `<b>Имя:</b>`.

    `mark=False` — таймкод без обёртки, как «Совещание» получало до 13.10 (`results_payload`).
    """
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = " ".join(para.split())
        if not para:
            continue
        m = PARA.match(para)
        stamp, body = "", para
        if m:
            stamp = (f'<span class="tc">[{escape(m.group(1))}]</span> ' if mark
                     else f"[{escape(m.group(1))}] ")
            body = m.group(2)
        sm = SPEAKER.match(body)
        who = ""
        if sm:
            who, body = f"<b>{escape(sm.group(1))}:</b> ", sm.group(2)
        out.append(f"<p>{stamp}{who}{escape(body)}</p>")
    return "".join(out)


_STAMP_HEAD = re.compile(r"^(\s*)(\[\d{1,2}:\d{2}(?::\d{2})?\])")


def mark_stamps(html: str) -> str:
    """Очищенный HTML расшифровки → тот же HTML, где таймкод в начале абзаца — `<span class="tc">`.

    Редактор Quill класс `tc` не хранит (у него нет такого формата), поэтому после правки таймкод
    приходит простым текстом; возвращаем пометку здесь — серым он виден и в карточке, и в Word.
    """
    items = richtext_common.blocks(html)
    for block in items:
        if block.kind != "p" or not block.runs:
            continue
        first = block.runs[0]
        m = _STAMP_HEAD.match(first.text)
        if first.tc or not m or first.bold or first.italic or first.underline or first.strike \
                or first.color or first.background or first.href:
            continue
        stamp = richtext_common.Run(text=m.group(2), tc=True)
        rest = first.text[m.end():]
        block.runs[0:1] = [stamp] + ([richtext_common.Run(text=rest)] if rest else [])
    return richtext_common.render(items)


def paragraphs_html(text: str) -> str:
    """Простой текст → абзацы (пустая строка и одиночный перенос делят абзацы)."""
    return richtext_common.from_text(text or "")


def _norm(text: str) -> str:
    return " ".join(orders_docx.dash(text or "").split())


def consistent(saved: str, text: str) -> bool:
    """Сохранённый HTML действителен, только если его текст совпадает с хранимым текстом
    (ревью RC-2026-10-13-2, Н-1). Текст мог поменяться в обход HTML — правкой на старом коде после
    отката, — тогда устаревшее оформление не должно перекрыть более новый текст."""
    return bool(saved) and _norm(richtext_common.text(saved)) == _norm(text)


def draft_html(draft_text: str, saved: str = "") -> str:
    """HTML транскрипции для карточки: сохранённый человеком (если согласован с текстом) или из текста."""
    return saved if consistent(saved, draft_text) else transcript_html(draft_text or "")


def summary_html(summary: str, saved: str = "") -> str:
    return saved if consistent(saved, summary) else paragraphs_html(orders_docx.dash(summary or ""))


def order_html(order: dict[str, Any]) -> str:
    """HTML поручения: сохранённый человеком (ключ `html`, согласован с текстом) или из текста.
    Ответ «Искры» html не несёт."""
    html, text = str(order.get("html") or ""), str(order.get("text") or "")
    return html if consistent(html, text) else paragraphs_html(text)


def order_styled(order: dict[str, Any]) -> bool:
    """У поручения есть оформление сверх простого текста (иначе html — те же абзацы текста)."""
    html, text = str(order.get("html") or ""), str(order.get("text") or "")
    return consistent(html, text) and html != paragraphs_html(text)
