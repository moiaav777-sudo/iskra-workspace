"""Поведение движка листа по брендбуку — замечания ревьюера RC-2026-10-02-6, Н-1…Н-9.

Диаграммы и выбор цвета проверяются запуском настоящего `skill/assets/a3.js` в `node` (модуль `vm`):
движок рисует SVG строкой, поэтому заливки и подписи читаются из результата, а не из текста исходника.
Внутренние функции движка закрыты в замыкании — тест открывает их, подменяя в копии исходника строку
`return {` (в самом a3.js точки входа для тестов нет). Нет `node` — эти тесты пропускаются.

CSS проверяется по тексту (`a3.css`): это слабее, чем замер в браузере, — каскад и итоговые цвета
не вычисляются, проверяется наличие правил и специфичность селекторов, посчитанная здесь же.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

A3 = Path(__file__).resolve().parents[2]
CSS_PATH = A3 / "skill" / "assets" / "a3.css"
JS_PATH = A3 / "skill" / "assets" / "a3.js"
CSS = CSS_PATH.read_text(encoding="utf-8")
CSS_NC = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)   # без комментариев
NODE = shutil.which("node")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
let src = fs.readFileSync(process.argv[process.argv.length - 1], "utf8");   // node -e: путь — последний аргумент
const anchor = "  return {\n    render: render,";
if (!src.includes(anchor)) throw new Error("в a3.js не найден экспорт `return { render: render,` — обновите тест");
// typeof — чтобы на старой версии движка тест падал на проверке, а не на отсутствующем имени
const names = ["col", "palette", "inkOn", "AUTO", "SER", "lineChart", "donut", "bullet", "barH", "barV", "WARN"];
src = src.replace(anchor, "  return {\n    __t: {" + names.map(function (n) {
  return n + ": typeof " + n + " !== 'undefined' ? " + n + " : undefined";
}).join(", ") + "},\n    render: render,");
const noop = function () {};
const el = function () {
  return { style: { setProperty: noop }, setAttribute: noop, classList: { add: noop },
    getContext: function () { return { font: "", measureText: function (t) { return { width: String(t).length * 6 }; } }; } };
};
const ctx = {
  console: console,
  document: { readyState: "complete", addEventListener: noop, getElementById: function () { return null; },
    querySelector: function () { return null; }, documentElement: {}, createElement: el },
  window: { addEventListener: noop },
  getComputedStyle: function () { return { getPropertyValue: function () { return "Arial"; } }; }
};
vm.createContext(ctx);
vm.runInContext(src + "\n;this.A3 = A3;", ctx);
const T = ctx.A3.__t;
const out = (function () { __BODY__ })();
process.stdout.write(JSON.stringify(out));
"""


def run_js(body: str):
    if not NODE:
        pytest.skip("нет node — поведенческая проверка a3.js пропущена")
    script = HARNESS.replace("__BODY__", body)
    r = subprocess.run([NODE, "-e", script, "--", str(JS_PATH)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def fills(svg: str, tag: str) -> list[str]:
    return re.findall(rf'<{tag}\b[^>]*?fill="([^"]+)"', svg)


BOX = "{ w: 600, h: 300 }"

# ---------------------------------------------------------------- цвета из JSON и токены (Н-8, Н-9)


def test_col_accepts_engine_tokens():
    """Н-9: автоакценты var(--aN) раньше отвергались, и все одноцветные диаграммы шли первым синим."""
    got = run_js("return ['var(--a1)', 'var(--a3)', 'var(--s4)', 'var(--s5)'].map(function (c) { return T.col(c, 0); });")
    assert got == ["var(--a1)", "var(--a3)", "var(--s4)", "var(--s5)"]


def test_colour_names_limited_to_palette():
    """Н-8: имена — только палитра; чужие имена и hex → автоматический цвет и замечание в плашке."""
    got = run_js("""
      var names = ['gray', 'grey', 'GRAY', 'blue', 'light', 'lightblue', 'lavender', 'green', 'red', 'orange'];
      var bad = ['teal', 'purple', '#123456', 'rgb(1, 2, 3)'];
      var pal = T.palette || function (c) { var r = T.col(c, 99); return r === T.SER[99 % 5] ? null : r; };
      return { ok: names.map(function (c) { return pal(c); }),
               bad: bad.map(function (c) { return pal(c); }),
               col: T.col('purple', 2), warn: T.WARN.slice() };
    """)
    assert got["ok"] == ["var(--s5)", "var(--s5)", "var(--s5)", "var(--s1)", "var(--s3)", "var(--s3)", "var(--s4)",
                         "var(--green)", "var(--red)", "var(--orange)"]
    assert got["bad"] == [None] * 4
    assert got["col"] == "var(--s3)"   # вне палитры — цвет серии по порядку
    for c in ("teal", "purple", "#123456", "rgb(1, 2, 3)"):
        assert any(f"«{c}»" in w for w in got["warn"]), f"нет замечания про «{c}»"


def test_auto_accents_are_blue_only_and_ink_follows_fill():
    """Н-9: серый --a5 не автоакцент; Н-2: на светлом акценте текст тёмно-синий, белый — только на тёмном."""
    got = run_js("return { auto: T.AUTO, ink: T.AUTO.map(function (a) { return T.inkOn(a); }) };")
    assert got["auto"] == ["var(--a1)", "var(--a2)", "var(--a3)", "var(--a4)"]
    assert got["ink"] == ["#fff", "#fff", "var(--brand-900)", "var(--brand-900)"]

# ---------------------------------------------------------------- бары и выделение (Н-6, Н-9)


def test_highlight_is_darker_than_other_bars():
    """Н-6: обычные бары brand-700, выделенный brand-900 — и в обеих диаграммах, и при тёмном автоакценте."""
    got = run_js(f"""
      var items = [['А', 9], ['Б', 7], ['В', 5]];
      return {{
        h: T.barH({BOX}, {{ items: items, highlight: 'Б' }}),
        hDark: T.barH({BOX}, {{ items: items, highlight: 'Б', _auto: 'var(--a2)' }}),
        v: T.barV({BOX}, {{ items: items, highlight: 'Б' }}),
        plain: T.barV({BOX}, {{ items: items }})
      }};
    """)
    for key in ("h", "hDark"):
        bars = fills(got[key], "rect")[1::2]   # дорожка, бар, дорожка, бар…
        assert bars == ["var(--s2)", "var(--s1)", "var(--s2)"], (key, bars)
    assert fills(got["v"], "rect") == ["var(--s2)", "var(--s1)", "var(--s2)"]
    assert set(fills(got["plain"], "rect")) == {"var(--s2)"}   # брендбук: обычные бары #144696


def test_mono_chart_takes_block_auto_accent():
    """Н-9: тёмный автоакцент блока доходит до баров. Светлый (a3, a4) — нет: бары brand-700, выделенный
    brand-900 (решение Оркестратора 02.10 по ревью RC-6: светлая полоса на серой дорожке читается слабо).
    Явный цвет из JSON — как задал человек."""
    got = run_js(f"""
      var items = [['А', 1], ['Б', 2]];
      return {{ a1: T.barV({BOX}, {{ items: items, _auto: 'var(--a1)' }}),
               a3: T.barV({BOX}, {{ items: items, _auto: 'var(--a3)' }}),
               a4h: T.barH({BOX}, {{ items: items, _auto: 'var(--a4)', highlight: 'Б' }}),
               light: T.barV({BOX}, {{ items: items, color: 'light' }}) }};
    """)
    assert set(fills(got["a1"], "rect")) == {"var(--a1)"}
    assert set(fills(got["a3"], "rect")) == {"var(--s2)"}
    assert fills(got["a4h"], "rect")[1::2] == ["var(--s1)", "var(--s2)"]   # Б (2) выше А (1): сортировка по убыванию
    assert set(fills(got["light"], "rect")) == {"var(--s3)"}


def test_bullet_bar_is_always_dark():
    """Н-9: светлый автоакцент на серой дорожке давал 1,3 : 1 — полоса факта только тёмная."""
    got = run_js(f"""
      var items = [['А', 50, 90]];
      return [T.bullet({BOX}, {{ items: items, _auto: 'var(--a3)' }}), T.bullet({BOX}, {{ items: items, _auto: 'var(--a4)' }}),
              T.bullet({BOX}, {{ items: items, _auto: 'var(--a1)' }}), T.bullet({BOX}, {{ items: items }})];
    """)
    assert [fills(s, "rect")[1] for s in got] == ["var(--s2)", "var(--s2)", "var(--a1)", "var(--s2)"]

# ---------------------------------------------------------------- линия (Н-4)


def test_line_series_follow_brandbook_order():
    """Н-4: порядок рядов s1 → s2 → s3 → s4 → s5; раньше лаванда шла третьей."""
    svg = run_js(f"""
      var s = [1, 2, 3, 4, 5].map(function (n) {{ return {{ name: 'Р' + n, values: [n, n + 1] }}; }});
      return T.lineChart({BOX}, {{ x: ['2025', '2026'], series: s }});
    """)
    strokes = re.findall(r'<path\b[^>]*stroke="([^"]+)"', svg)
    assert strokes == ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"]


def test_line_labels_dark_with_series_marker():
    """Н-4: подписи — тёмно-синим (оси — серо-синим), цвет ряда — у кружка-маркера, в том числе у смысловых."""
    svg = run_js(f"""
      return T.lineChart({BOX}, {{ x: ['2024', '2025'], series: [
        {{ name: 'Рост', values: [1, 2], color: 'green' }}, {{ name: 'План', values: [1, 3], color: 'orange' }},
        {{ name: 'Третий', values: [2, 2] }}, {{ name: 'Чужой', values: [0, 1], color: 'teal' }}] }});
    """)
    assert set(fills(svg, "text")) <= {"var(--brand-900)", "var(--ink-2)"}, set(fills(svg, "text"))
    keys = re.findall(r'<circle class="line-key"[^>]*fill="([^"]+)"', svg)
    assert sorted(keys) == sorted(["var(--green)", "var(--orange)", "var(--s1)", "var(--s2)"])

# ---------------------------------------------------------------- кольцо (Н-7)


def test_donut_other_is_neutral_and_last():
    """Н-7: «Прочее» — всегда --s5 и последним, рядовые доли — s1…s4."""
    got = run_js(f"""
      var seven = [['А', 30], ['Б', 20], ['В', 15], ['Г', 12], ['Д', 10], ['Е', 8], ['Ж', 5]];
      var named = [['Прочие регионы', 10], ['А', 50], ['Б', 40]];
      return [T.donut({BOX}, {{ items: seven }}), T.donut({BOX}, {{ items: named }})];
    """)
    seven, named = got
    assert fills(seven, "path") == ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)"]
    assert "Прочее" in seven and ">Д<" not in seven
    assert fills(named, "path") == ["var(--s1)", "var(--s2)", "var(--s5)"]
    assert named.index("Прочие регионы") > named.index(">Б<")

# ---------------------------------------------------------------- шрифт вшит (Н-1)


def test_font_is_embedded_and_css_has_no_external_urls():
    """Н-1: лист работает без сети — Golos Text вшит @font-face с data:, внешних @import и url(http…) нет."""
    assert "@import" not in CSS_NC
    assert not re.search(r"url\(\s*['\"]?\s*(https?:)?//", CSS_NC), "в a3.css внешний url()"
    assert "https://" not in CSS_NC and "http://" not in CSS_NC
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", CSS_NC)
    assert len(faces) >= 2, "ожидаются @font-face Golos Text (латиница и кириллица)"
    ranges = []
    for f in faces:
        assert re.search(r'font-family:\s*"Golos Text"', f)
        lo, hi = map(int, re.search(r"font-weight:\s*(\d+)\s+(\d+)", f).groups())
        assert lo <= 400 and hi >= 700, "начертания 400–700 из одного вариативного файла"
        data = re.search(r"url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\)", f).group(1)
        assert base64.b64decode(data)[:4] == b"wOF2"
        ranges.append(re.search(r"unicode-range:\s*([^;]+);", f).group(1))
    joined = " ".join(ranges)
    assert "U+0400-045F" in joined and "U+0000-00FF" in joined, "нужны кириллица и латиница"
    assert sum(len(f) for f in faces) < 300_000, "шрифт раздувает кит сверх разумного"


def test_kit_has_no_external_requests():
    for kit in (A3 / "a3-kit.html", A3 / "skill" / "a3-kit.html"):
        page = kit.read_text(encoding="utf-8")
        assert "https://" not in page, f"{kit.name}: внешний адрес — лист должен работать без сети"
        assert "fonts.googleapis.com/css" not in page and "fonts.gstatic.com" not in page

# ---------------------------------------------------------------- CSS по тексту (слабые проверки)


def _spec(sel: str) -> tuple[int, int, int]:
    s = re.sub(r"::[\w-]+", " x", sel)
    ids = len(re.findall(r"#[\w-]+", s))
    cls = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+(\([^)]*\))?", s))
    s2 = re.sub(r"\.[\w-]+|\[[^\]]+\]|:[\w-]+(\([^)]*\))?|#[\w-]+", " ", s)
    types = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", s2))
    return ids, cls, types


def test_total_row_beats_zebra_and_has_visible_rule():
    """Н-3 (по тексту CSS): селектор итога специфичнее чередования, линия ≥ 0,6 мм, серая линия над итогом снята."""
    m = re.search(r"([^{}]*tr\.hl[^{}]*?)\{([^}]*background:\s*var\(--tint\)[^}]*)\}", CSS_NC)
    assert m, "нет правила итоговой строки с фоном --tint"
    zebra = re.search(r"([^{}]*nth-child\(odd\)[^{}]*?)\{", CSS_NC).group(1).strip()
    assert _spec(m.group(1).strip()) > _spec(zebra), (m.group(1), zebra)
    width = float(re.search(r"border-top:\s*([0-9.]+)mm solid var\(--brand-700\)", m.group(2)).group(1))
    assert width >= 0.6
    assert re.search(r"\.tbl tr:has\(\+ tr\.hl\) td \{ border-bottom: 0; \}", CSS_NC)


def test_numbered_list_digits_follow_accent_ink():
    """Н-2 (по тексту CSS и JS): цифры нумерованного списка на акценте — --acc-ink, а его ставит inkOn()."""
    assert re.search(r"\.theme-soft \.block--acc \.t-list--num li::before \{ color: var\(--acc-ink, #fff\); \}", CSS_NC)
    js = JS_PATH.read_text(encoding="utf-8")
    assert 'el.style.setProperty("--acc-ink", inkOn(CUR_ACC));' in js


def test_compare_titles_are_dark():
    """Н-5 (по тексту CSS): заголовок колонки compare — тёмно-синий, цвет колонки — на кромке."""
    rule = re.search(r"\.cmp__title \{([^}]*)\}", CSS_NC).group(1)
    assert "color: var(--brand-900)" in rule and "var(--c)" not in rule
    assert re.search(r"\.cmp \{[^}]*border-top:[^;]*var\(--c\)", CSS_NC)


def test_css_rgba_only_white_black_or_brand():
    """Н-8 (по тексту CSS): прозрачные цвета — только белый, чёрный и brand-900, без чужих синих и жёлтого."""
    allowed = {(255, 255, 255), (0, 0, 0), (6, 42, 95)}
    found = {tuple(map(int, t)) for t in re.findall(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", CSS_NC)}
    assert found <= allowed, f"rgba вне палитры: {sorted(found - allowed)}"
