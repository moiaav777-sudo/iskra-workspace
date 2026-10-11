"""Лист A3 — строго в цветах брендбука v2.0 (решение пользователя 02.10.2026, `skill/references/brand.md`).

Проверяем исходники движка, а не отрисовку: все HEX-цвета в `a3.css` — из палитры брендбука плюс
зафиксированные в brand.md подложки статусов; в `a3.js` своих цветов нет (кроме белого текста на
тёмно-синем); порядок серий и шрифт — по брендбуку; собранный `a3-kit.html` не отстал от исходников.
"""
from __future__ import annotations

import re
from pathlib import Path

A3 = Path(__file__).resolve().parents[2]
CSS = (A3 / "skill" / "assets" / "a3.css").read_text(encoding="utf-8")
JS = (A3 / "skill" / "assets" / "a3.js").read_text(encoding="utf-8")

BRANDBOOK = {
    "#062a5f", "#144696", "#99c2ff", "#98a8f8",            # синяя гамма
    "#232832", "#414b5f", "#c9ced6", "#e5e5e5", "#e8eef8", "#f8f9fa", "#ffffff", "#fff",   # нейтральные
    "#55a555", "#f84848", "#f5a623",                       # смысловые
    "#2e7d32", "#c62828", "#b45309",                       # смысловые для текста
}
# подложки статусов — сознательное отступление, см. brand.md «Два сознательных отступления»
DERIVED = {"#eaf4ea", "#fdecec", "#fdf3e1", "#eef0f3"}


def _hexes(text: str) -> set[str]:
    return {h.lower() for h in re.findall(r"#[0-9a-fA-F]{3,6}\b", text)}


def test_css_uses_only_brandbook_colours():
    extra = _hexes(CSS) - BRANDBOOK - DERIVED
    assert not extra, f"в a3.css цвета вне палитры брендбука: {sorted(extra)}"


def test_js_has_no_own_colours():
    assert _hexes(JS) <= {"#fff", "#ffffff"}, "a3.js должен брать цвета только через var(--…)"


def test_series_order_and_accents_follow_brandbook():
    root = CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))]
    want = {"--s1": "var(--brand-900)", "--s2": "var(--brand-700)", "--s3": "var(--brand-200)",
            "--s4": "var(--lavender)", "--s5": "var(--neutral)",
            "--brand-200": "#99c2ff", "--lavender": "#98a8f8", "--neutral": "#c9ced6", "--tint": "#e8eef8",
            "--green-ink": "#2e7d32", "--red-ink": "#c62828", "--orange-ink": "#b45309"}
    for token, value in want.items():
        assert re.search(rf"{re.escape(token)}:\s*{re.escape(value)}\s*;", root), f"{token} должен быть {value}"
    # зелёный, оранжевый, красный — не серии и не автоакценты
    for tok in ("--s", "--a"):
        for n in range(1, 6):
            m = re.search(rf"{tok}{n}:\s*([^;]+);", root)
            assert m and not re.search(r"green|orange|red", m.group(1)), f"{tok}{n} не должен быть смысловым цветом"


def test_font_is_golos_with_arial_fallback():
    # шрифт вшит @font-face, а не тянется @import с Google (ревьюер RC-2026-10-02-6, Н-1);
    # подробная проверка вшитого шрифта — test_brand_behaviour.py
    assert re.search(r'--font:\s*"Golos Text",\s*Arial,\s*sans-serif', CSS)
    assert "@import" not in re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert CSS.index("@font-face") < CSS.index("@page")


def test_soft_theme_has_no_palette_of_its_own():
    soft = CSS[CSS.index(".sheet.theme-soft {"):CSS.index("}", CSS.index(".sheet.theme-soft {"))]
    assert not re.search(r"--(brand-\d+|ink|ink-2|rule|paper|surface|green|red|orange|lavender|s[1-5]|a[1-5])\s*:", soft), \
        "мягкая тема меняет только геометрию, цвета — из :root"
    assert "#" not in soft


def test_table_header_and_total_row_follow_brandbook():
    th = CSS[CSS.index(".tbl th {"):CSS.index("}", CSS.index(".tbl th {"))]
    assert "background: var(--brand-900)" in th and "color: #fff" in th
    # селектор итога поднят над чередованием строк (ревьюер RC-2026-10-02-6, Н-3)
    assert re.search(r"\.tbl tr\.hl:nth-child\(n\) td \{[^}]*background: var\(--tint\)[^}]*border-top: [0-9.]+mm solid var\(--brand-700\)", CSS)


def test_kit_is_rebuilt_from_sources():
    for kit in (A3 / "a3-kit.html", A3 / "skill" / "a3-kit.html"):
        page = kit.read_text(encoding="utf-8")
        assert CSS in page, f"{kit.name} отстал от a3.css — пересоберите: python3 skill/scripts/build_kit.py"
        assert JS in page, f"{kit.name} отстал от a3.js — пересоберите: python3 skill/scripts/build_kit.py"
