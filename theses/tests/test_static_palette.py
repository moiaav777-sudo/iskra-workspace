"""Палитры редактора: всё, что можно выбрать, сервер сохраняет — в любом сочетании.

Жалоба пользователя 02.10 «не могу поменять цвет текста»; ревью RC-2026-10-02-14, Н-3:
сервер (`richtext`) снимает цвет текста на заливке близкой яркости, и из 25×20 сочетаний
прежней палитры 232 теряли цвет после сохранения.

С 13.10 (решение пользователя, «единый формат блоков») панель редактора рисует общий
компонент портала: палитры — `TEXT_COLORS` / `BACK_COLORS` в `portal/static/blocks.js`, правило
сохранения — по-прежнему в нашем `app/richtext.py`. Здесь они сводятся вместе.

Кандидат к выкладке проверяется снимком одной папки `theses/` (`portal/ops/rc.sh`), портала
рядом нет. Поэтому палитры портала записаны и здесь (`RECORDED_*`, сняты 13.10): перебор пар
идёт всегда, а пока портал рядом — запись сверяется с ним (`test_запись_палитр_совпадает_с_порталом`).
Поменяли палитру в портале — этот тест скажет, что запись устарела, и перебор пройдёт по новой.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import richtext

BLOCKS_JS = Path(__file__).resolve().parent.parent.parent / "portal" / "static" / "blocks.js"

RECORDED_TEXT = ["#e60000", "#008a00", "#0066cc", "#9933ff", "#888888", "#a10000", "#b26b00",
                 "#006100", "#0047b2", "#6b24b2", "#444444", "#5c0000", "#663d00", "#666600", "#003700",
                 "#002966", "#3d1466"]
RECORDED_BACK = ["#ffff00", "#ffff66", "#ffffcc", "#facccc", "#ffebcc", "#cce8cc", "#cce0f5", "#ebd6ff"]


def _portal_palette(name: str) -> list[str] | None:
    if not BLOCKS_JS.is_file():
        return None
    text = BLOCKS_JS.read_text(encoding="utf-8")
    m = re.search(r"var " + name + r" = \[(.*?)\];", text, re.S)
    assert m, name + " не найдена в portal/static/blocks.js"
    return re.findall(r"'(#[0-9a-f]{6})'", m.group(1))


def _palettes() -> tuple[list[str], list[str]]:
    """Палитры, которые человек видит на панели: портала, а без него — записанные."""
    return (_portal_palette("TEXT_COLORS") or RECORDED_TEXT,
            _portal_palette("BACK_COLORS") or RECORDED_BACK)


def _kept(html: str) -> list[richtext.Run]:
    return [run for block in richtext.blocks(richtext.clean(html)) for run in block.runs]


def test_запись_палитр_совпадает_с_порталом():
    if not BLOCKS_JS.is_file():
        pytest.skip("портала рядом нет (снимок одной папки) — перебор идёт по записанным палитрам")
    assert _portal_palette("TEXT_COLORS") == RECORDED_TEXT, "палитра цвета в портале изменилась — обновите запись"
    assert _portal_palette("BACK_COLORS") == RECORDED_BACK, "палитра заливки в портале изменилась — обновите запись"


def test_каждый_цвет_и_заливка_сохраняются():
    text, back = _palettes()
    assert len(text) >= 12 and len(back) >= 6
    for color in text:
        (run,) = _kept(f'<p><span style="color: {color}">слово</span></p>')
        assert run.color == color, color
    for color in back:
        (run,) = _kept(f'<p><span style="background-color: {color}">слово</span></p>')
        assert run.background == color, color


def test_любое_сочетание_цвета_и_заливки_сохраняется():
    text, back = _palettes()
    lost = []
    for color in text:
        for fill in back:
            # так пишет Quill 2 в браузере: rgb с пробелами, цвет и заливка в одном style
            rgb = lambda c: "rgb({}, {}, {})".format(*(int(c[i:i + 2], 16) for i in (1, 3, 5)))  # noqa: E731
            (run,) = _kept(f'<p><span style="color: {rgb(color)}; background-color: {rgb(fill)};">x</span></p>')
            if (run.color, run.background) != (color, fill):
                lost.append(f"{color} на {fill}")
    assert not lost, "сервер снимет цвет: " + ", ".join(lost)
