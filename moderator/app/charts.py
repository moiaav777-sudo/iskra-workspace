"""Диаграммы к материалу: ряд чисел из текста → готовый SVG (решение пользователя 02.10).

Чистый Python, без библиотек: сервис живёт на домашнем ПК, тянуть matplotlib ради
четырёх видов диаграмм незачем. Четыре вида — `bar_h` (сравнение), `bar_v` и `line`
(динамика), `donut` (структура долей). Палитра — брендбук пространства.

SVG вставляется прямо в HTML документа «Модератор» и в окно разбора, поэтому:
все тексты экранированы, скриптов, ссылок и внешних шрифтов нет, размер — `viewBox`
(ширина подстраивается под колонку), строка заведомо короче 20 КБ. Интерфейс пропускает svg
через DOMPurify (профиль svg): оформление — только атрибутами (`fill`, `stroke`, `font-*`,
`text-anchor`), без `<style>`, `style=`, классов, `<defs>`/`<use>`, `<image>` и `href`.
Негодные данные — `ValueError` с понятной причиной: фигуру отбрасывают, а не рисуют криво.
"""
from __future__ import annotations

import math
from html import escape

TYPES = ("bar_h", "bar_v", "line", "donut")
MIN_ITEMS, MAX_ITEMS = 2, 12

# палитра брендбука
C_MAIN = "#062a5f"
C_SECOND = "#144696"
C_LIGHT = "#99c2ff"
C_LAVENDER = "#98a8f8"
C_NEUTRAL = "#c9ced6"
C_TEXT = "#232832"
C_RULE = "#e5e5e5"
FONT = "Golos Text, Arial, sans-serif"
SERIES = (C_MAIN, C_SECOND, C_LIGHT, C_LAVENDER, C_NEUTRAL)


# ── данные ──────────────────────────────────────────────────────────────

def _number(v) -> float:
    """Число из ответа модели: 39.5, «39,5», «1 234». Иначе ValueError."""
    if isinstance(v, bool):
        raise ValueError("значение – не число")
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        s = v.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            x = float(s)
        except ValueError:
            raise ValueError(f"значение «{v[:20]}» – не число") from None
    else:
        raise ValueError("значение – не число")
    if not math.isfinite(x) or abs(x) >= 1e15:
        raise ValueError("значение – не конечное число или слишком большое")
    return x


def check_items(items, kind: str = "") -> list[list]:
    """Список пар [подпись, число], 2–12 штук. Иначе ValueError."""
    if not isinstance(items, list):
        raise ValueError("items – не список")
    out: list[list] = []
    for it in items:
        if not isinstance(it, (list, tuple)) or len(it) != 2:
            raise ValueError("элемент items – не пара [подпись, число]")
        label, value = it
        if isinstance(label, bool) or not isinstance(label, (str, int, float)):
            raise ValueError("подпись – не строка")
        label = str(label).strip()[:80]
        out.append([label, _number(value)])
    if not MIN_ITEMS <= len(out) <= MAX_ITEMS:
        raise ValueError(f"элементов {len(out)}, нужно от {MIN_ITEMS} до {MAX_ITEMS}")
    if kind == "donut":
        if any(v < 0 for _, v in out):
            raise ValueError("в кольцевой диаграмме доли не бывают отрицательными")
        if sum(v for _, v in out) <= 0:
            raise ValueError("в кольцевой диаграмме сумма долей равна нулю")
    return out


def check_figure(fig) -> dict:
    """Фигура из ответа модели или из браузера → {title, unit, type, items, source}. Иначе ValueError."""
    if not isinstance(fig, dict):
        raise ValueError("фигура – не объект")
    kind = str(fig.get("type") or "").strip()
    if kind not in TYPES:
        raise ValueError(f"тип «{kind[:20] or '–'}» не из {', '.join(TYPES)}")
    return {
        "title": str(fig.get("title") or "").strip()[:200],
        "unit": str(fig.get("unit") or "").strip()[:20],
        "type": kind,
        "items": check_items(fig.get("items"), kind),
        "source": str(fig.get("source") or "").strip()[:500],
    }


# ── форматирование ──────────────────────────────────────────────────────

def fmt_num(v: float) -> str:
    """По-русски: пробел (неразрывный) между тысячами, запятая, без лишних нулей."""
    if abs(v - round(v)) < 1e-9:
        s = f"{int(round(v)):,}"
    else:
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",").replace("-", "−")


def fmt_value(v: float, unit: str) -> str:
    s = fmt_num(v)
    if not unit:
        return s
    return s + unit if unit in ("%", "‰") else f"{s} {unit}"


def _fit(label: str, width_px: float, size: float) -> str:
    """Обрезать подпись под ширину (символ ≈ 0,56 кегля)."""
    room = max(3, int(width_px / (size * 0.56)))
    return label if len(label) <= room else label[:room - 1].rstrip() + "…"


def _t(x, y, text, size=12, anchor="start", weight="400", fill=C_TEXT) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" '
            f'font-weight="{weight}" fill="{fill}">{escape(text)}</text>')


def _rect(x, y, w, h, fill) -> str:
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{max(h, 0):.1f}" fill="{fill}"></rect>'


def _line(x1, y1, x2, y2, stroke=C_RULE, width=1) -> str:
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"></line>')


def _wrap(width: int, height: int, title: str, body: list[str]) -> str:
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
            f'role="img" '
            f'aria-label="{escape(title or "Диаграмма")}" font-family="{FONT}">')
    title_el = _t(0, 18, _fit(title, width, 14), 14, weight="600") if title else ""
    return head + title_el + "".join(body) + "</svg>"


def _span(values: list[float]) -> tuple[float, float]:
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    if hi == lo:
        hi = lo + 1
    return lo, hi


# ── виды ────────────────────────────────────────────────────────────────

def _bar_h(items, unit, width, top):
    row, label_w = 28, min(200, width * 0.34)
    lo, hi = _span([v for _, v in items])
    # место под подписи значений: справа у положительных, слева у отрицательных
    value_w = max(len(fmt_value(v, unit)) for _, v in items) * 12 * 0.6 + 10
    plot_x = label_w + 8 + (value_w if lo < 0 else 0)
    plot_w = width - plot_x - (value_w if hi > 0 else 4)
    scale = plot_w / (hi - lo)
    zero = plot_x + (-lo) * scale
    body = []
    for i, (label, v) in enumerate(items):
        y = top + i * row
        body.append(_t(label_w, y + 18, _fit(label, label_w, 12), 12, "end"))
        x0, x1 = sorted((zero, zero + v * scale))
        body.append(_rect(x0, y + 6, x1 - x0, row - 12, C_MAIN if i == 0 else C_SECOND))
        vx, anchor = (x1 + 6, "start") if v >= 0 else (x0 - 6, "end")
        body.append(_t(vx, y + 18, fmt_value(v, unit), 12, anchor, "600"))
    body.append(_line(zero, top, zero, top + len(items) * row, C_NEUTRAL))
    return body, top + len(items) * row + 8


def _bar_v(items, unit, width, top):
    plot_h, label_h = 180, 36
    n = len(items)
    slot = width / n
    bar_w = min(56, slot * 0.6)
    lo, hi = _span([v for _, v in items])
    scale = (plot_h - 20) / (hi - lo)
    base = top + 20 + hi * scale              # линия нуля
    body = [_line(0, base, width, base, C_NEUTRAL)]
    for i, (label, v) in enumerate(items):
        cx = slot * i + slot / 2
        y0, y1 = sorted((base, base - v * scale))
        body.append(_rect(cx - bar_w / 2, y0, bar_w, y1 - y0, C_MAIN if i == n - 1 else C_SECOND))
        vy = y0 - 6 if v >= 0 else y1 + 14
        body.append(_t(cx, vy, fmt_value(v, unit), 11, "middle", "600"))
        body.append(_t(cx, top + plot_h + 16, _fit(label, slot - 4, 11), 11, "middle"))
    return body, top + plot_h + label_h - 8


def _line_chart(items, unit, width, top):
    plot_h, pad = 180, 40
    vals = [v for _, v in items]
    lo, hi = min(vals), max(vals)
    if lo >= 0 and lo < 0.5 * hi:
        lo = 0.0
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    span = hi - lo
    lo -= span * 0.08
    hi += span * 0.12
    n = len(items)
    step = (width - 2 * pad) / (n - 1)
    def y(v):
        return top + plot_h - (v - lo) / (hi - lo) * plot_h
    body = [_line(0, top + plot_h * k / 2, width, top + plot_h * k / 2) for k in range(3)]
    pts = [(pad + i * step, y(v)) for i, (_, v) in enumerate(items)]
    body.append('<polyline points="' + " ".join(f"{x:.1f},{yy:.1f}" for x, yy in pts) +
                f'" fill="none" stroke="{C_SECOND}" stroke-width="2.5"></polyline>')
    for i, ((label, v), (x, yy)) in enumerate(zip(items, pts)):
        last = i == n - 1
        body.append(f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="{4.5 if last else 3.5}" '
                    f'fill="{C_MAIN if last else C_SECOND}"></circle>')
        body.append(_t(x, yy - 9, fmt_value(v, unit), 11, "middle", "600"))
        body.append(_t(x, top + plot_h + 18, _fit(label, max(step, 2 * pad) - 4, 11), 11, "middle"))
    return body, top + plot_h + 28


def _arc(cx, cy, r_out, r_in, a0, a1) -> str:
    """Сектор кольца от угла a0 до a1 (радианы, 0 — вверх, по часовой)."""
    if a1 - a0 >= 2 * math.pi - 1e-6:
        a1 = a0 + 2 * math.pi - 1e-4          # полный круг дугой не рисуется
    def p(r, a):
        return cx + r * math.sin(a), cy - r * math.cos(a)
    large = 1 if a1 - a0 > math.pi else 0
    (x0, y0), (x1, y1) = p(r_out, a0), p(r_out, a1)
    (x2, y2), (x3, y3) = p(r_in, a1), p(r_in, a0)
    return (f"M{x0:.2f},{y0:.2f} A{r_out},{r_out} 0 {large} 1 {x1:.2f},{y1:.2f} "
            f"L{x2:.2f},{y2:.2f} A{r_in},{r_in} 0 {large} 0 {x3:.2f},{y3:.2f} Z")


def _donut(items, unit, width, top):
    r_out, r_in = 80, 48
    legend_x, row = 2 * r_out + 38, 22
    block_h = max(2 * r_out + 12, len(items) * row)
    cx, cy = r_out + 10, top + block_h / 2
    legend_top = top + (block_h - len(items) * row) / 2
    total = sum(v for _, v in items)
    body, a = [], 0.0
    for i, (label, v) in enumerate(items):
        color = SERIES[i % len(SERIES)]
        opacity = "" if i < len(SERIES) else ' fill-opacity="0.55"'
        share = v / total
        if share > 0:
            body.append(f'<path d="{_arc(cx, cy, r_out, r_in, a, a + share * 2 * math.pi)}" '
                        f'fill="{color}"{opacity} stroke="#ffffff" stroke-width="1.5"></path>')
        a += share * 2 * math.pi
        ly = legend_top + i * row
        body.append(f'<rect x="{legend_x:.1f}" y="{ly + 4:.1f}" width="12" height="12" fill="{color}"{opacity}></rect>')
        pct = fmt_num(round(share * 100, 1)) + "%"
        value = fmt_value(v, unit) if unit != "%" else ""
        tail = f" – {value}, {pct}" if value else f" – {pct}"
        body.append(_t(legend_x + 18, ly + 15, _fit(label, width - legend_x - 18 - 12 * 0.56 * len(tail), 12) + tail, 12))
    if unit != "%":
        body.append(_t(cx, cy + 5, _fit(fmt_value(total, unit), 2 * r_in - 6, 12), 12, "middle", "600"))
    return body, top + block_h + 8


_DRAW = {"bar_h": _bar_h, "bar_v": _bar_v, "line": _line_chart, "donut": _donut}


def svg(figure: dict, width: int = 560) -> str:
    """Нарисовать фигуру. Негодная фигура — ValueError (её отбрасывают, а не рисуют)."""
    fig = check_figure(figure)
    width = max(320, min(int(width or 560), 1200))
    top = 34 if fig["title"] else 8
    body, height = _DRAW[fig["type"]](fig["items"], fig["unit"], width, top)
    return _wrap(width, int(math.ceil(height)), fig["title"], body)
