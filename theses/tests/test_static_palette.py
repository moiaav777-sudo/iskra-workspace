"""Палитры редактора: всё, что можно выбрать, сервер сохраняет — в любом сочетании.

Жалоба пользователя 02.10 «не могу поменять цвет текста»; ревью RC-2026-10-02-14, Н-3:
сервер (`richtext`) снимает цвет текста на заливке близкой яркости, и из 25×20 сочетаний
прежней палитры 232 теряли цвет после сохранения. Палитры живут в `static/app.js`
(`TEXT_COLORS`, `BACK_COLORS`), правило — в `app/richtext.py`; здесь они сводятся вместе.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import richtext

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


def _palette(name: str) -> list[str]:
    text = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"const " + name + r" = \[(.*?)\];", text, re.S)
    assert m, name + " не найдена в app.js"
    return re.findall(r"'(#[0-9a-f]{6})'", m.group(1))


def _kept(html: str) -> list[richtext.Run]:
    return [run for block in richtext.blocks(richtext.clean(html)) for run in block.runs]


def test_каждый_цвет_и_заливка_сохраняются():
    text, back = _palette("TEXT_COLORS"), _palette("BACK_COLORS")
    assert len(text) >= 12 and len(back) >= 6
    for color in text:
        (run,) = _kept(f'<p><span style="color: {color}">слово</span></p>')
        assert run.color == color, color
    for color in back:
        (run,) = _kept(f'<p><span style="background-color: {color}">слово</span></p>')
        assert run.background == color, color


def test_любое_сочетание_цвета_и_заливки_сохраняется():
    text, back = _palette("TEXT_COLORS"), _palette("BACK_COLORS")
    lost = []
    for color in text:
        for fill in back:
            # так пишет Quill 2 в браузере: rgb с пробелами, цвет и заливка в одном style
            rgb = lambda c: "rgb({}, {}, {})".format(*(int(c[i:i + 2], 16) for i in (1, 3, 5)))  # noqa: E731
            (run,) = _kept(f'<p><span style="color: {rgb(color)}; background-color: {rgb(fill)};">x</span></p>')
            if (run.color, run.background) != (color, fill):
                lost.append(f"{color} на {fill}")
    assert not lost, "сервер снимет цвет: " + ", ".join(lost)
