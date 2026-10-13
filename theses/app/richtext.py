"""Текст тезисов с оформлением: что человек видит в редакторе, то и уходит в Word.

Задача пользователя 22.09: редактор — как в «Модераторе» (Quill), никакой разметки
Markdown в тексте («**747 тысяч**»), а жирный, курсив, цвет и списки один в один
переезжают в документ. Поэтому текст хранится как HTML редактора, а всё остальное
(пункты для «Совещания», подсчёт слов, поиск, проверка слов) берётся из него.

HTML с браузера — чужой ввод: здесь он приводится к короткому белому списку тегов и
свойств (`clean`), и только такой попадает в базу и обратно на страницу.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# теги, которые умеет и редактор, и выгрузка в Word
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "pre"}
LIST_TAGS = {"ul", "ol"}
INLINE_TAGS = {"strong", "b", "em", "i", "u", "s", "strike", "span", "a", "sub", "sup", "br"}
ALLOWED = BLOCK_TAGS | LIST_TAGS | INLINE_TAGS
_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\))$")
_CLASS = re.compile(r"^ql-(align|indent)-[a-z0-9]+$")
_HREF = re.compile(r"^(https?:|mailto:)", re.I)
_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")

ASIDE_COLOR = "#0070c0"       # синий курсив — справочное (образец оформления)
MARKER_COLOR = "#c00000"      # красный — место под недостающую цифру

# Палитра редактора (Quill) и только она: белый текст на белом — скрытый текст, который
# ушёл бы в Word незамеченным (ревьюер RC-2026-09-22-2). Для цвета текста светлые тона
# исключены, для заливки — тёмные.
PALETTE = {
    "#000000", "#e60000", "#ff9900", "#ffff00", "#008a00", "#0066cc", "#9933ff", "#ffffff",
    "#facccc", "#ffebcc", "#ffffcc", "#cce8cc", "#cce0f5", "#ebd6ff", "#bbbbbb", "#f06666",
    "#ffc266", "#ffff66", "#66b966", "#66a3e0", "#c285ff", "#888888", "#a10000", "#b26b00",
    "#b2b200", "#006100", "#0047b2", "#6b24b2", "#444444", "#5c0000", "#663d00", "#666600",
    "#003700", "#002966", "#3d1466", ASIDE_COLOR, MARKER_COLOR,
}


def _luma(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def text_color(color: str) -> str:
    return color if color in PALETTE and _luma(color) < 0.8 else ""


def background_color(color: str) -> str:
    return color if color in PALETTE and _luma(color) > 0.35 and color != "#ffffff" else ""


@dataclass
class Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str = ""           # #rrggbb
    background: str = ""      # #rrggbb
    href: str = ""


@dataclass
class Block:
    """Абзац документа: пункт списка, обычный абзац, заголовок или цитата."""
    kind: str = "p"           # p | li | h | quote | pre
    ordered: bool = False     # для li: нумерованный список
    level: int = 0            # вложенность списка / отступ
    align: str = ""           # center | right | justify
    runs: list[Run] = field(default_factory=list)
    # уровень заголовка 1–3 (kind "h"): разделы тезисов — заголовки, панель ставит h1–h3,
    # и что выбрано в редакторе, то и сохраняется (решение пользователя 13.10, единый формат блоков)
    head: int = 2

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs).strip()

    def all(self, attr: str) -> bool:
        """Весь текст абзаца выделен одним способом (жирный целиком → «акцент»)."""
        runs = [r for r in self.runs if r.text.strip()]
        return bool(runs) and all(getattr(r, attr) for r in runs)


def _hex(color: str) -> str:
    """Цвет из редактора («rgb(0, 112, 192)» или «#0070c0») → #rrggbb, иначе пусто."""
    color = (color or "").strip().lower()
    m = _RGB.match(color)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.match(r"^#[0-9a-f]{6}$", color):
        return color
    if re.match(r"^#[0-9a-f]{3}$", color):
        return "#" + "".join(ch * 2 for ch in color[1:])
    return ""


def _style(value: str) -> tuple[str, str]:
    """Из style берём только цвет текста и фона."""
    color = background = ""
    for part in (value or "").split(";"):
        name, _, val = part.partition(":")
        name = name.strip().lower()
        if name == "color":
            color = text_color(_hex(val))
        elif name == "background-color":
            background = background_color(_hex(val))
    return color, background


class _Reader(HTMLParser):
    """HTML редактора → блоки с оформленными кусками текста. Заодно чистит: неизвестные
    теги и свойства не переживают разбор."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._stack: list[dict] = []          # открытые строчные оформления
        self._lists: list[bool] = []          # открытые списки: ordered?
        self._block: Block | None = None
        self._pre = 0
        self._skip = 0                        # внутри <script>/<style>: содержимое — не текст

    # ── блоки ────────────────────────────────────────────────────────────
    def _open(self, kind: str, attrs: dict, tag: str = "") -> None:
        self._close()
        block = Block(kind=kind)
        if kind == "h" and tag[1:].isdigit():
            block.head = min(3, max(1, int(tag[1:])))      # h4 панель не ставит — как h3
        if kind == "li":
            block.ordered = bool(self._lists and self._lists[-1])
            block.level = max(0, len(self._lists) - 1)
        for cls in (attrs.get("class") or "").split():
            if _CLASS.match(cls):
                name, value = cls[3:].split("-", 1)
                if name == "align":
                    block.align = value
                elif name == "indent" and value.isdigit():
                    block.level += int(value)
        self._block = block

    def _close(self) -> None:
        if self._block is not None and self._block.runs:
            self.blocks.append(self._block)
        self._block = None

    def _current(self) -> Block:
        if self._block is None:
            self._block = Block(kind="p")
        return self._block

    # ── события разбора ──────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "template"):
            self._skip += 1
            return
        if tag in LIST_TAGS:
            self._close()
            # Quill 2 хранит любой список как <ol>, а вид пункта — в data-list
            self._lists.append(tag == "ol" and attrs.get("data-list") != "bullet")
        elif tag == "li":
            kind = attrs.get("data-list")
            if kind and self._lists:
                self._lists[-1] = kind == "ordered"
            self._open("li", attrs)
        elif tag in ("h1", "h2", "h3", "h4"):
            self._open("h", attrs, tag)
        elif tag == "blockquote":
            self._open("quote", attrs)
        elif tag == "pre":
            self._open("pre", attrs)
            self._pre += 1
        elif tag in ("p", "div"):
            self._open("p", attrs)
        elif tag == "br":
            self._current().runs.append(Run(text="\n"))
        elif tag in INLINE_TAGS:
            fmt = {}
            if tag in ("strong", "b"):
                fmt["bold"] = True
            elif tag in ("em", "i"):
                fmt["italic"] = True
            elif tag == "u":
                fmt["underline"] = True
            elif tag in ("s", "strike"):
                fmt["strike"] = True
            elif tag == "a":
                href = (attrs.get("href") or "").strip()
                if _HREF.match(href):
                    fmt["href"] = href
            color, background = _style(attrs.get("style", ""))
            if color:
                fmt["color"] = color
            if background:
                fmt["background"] = background
            self._stack.append(fmt)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in LIST_TAGS:
            self._close()
            if self._lists:
                self._lists.pop()
        elif tag in BLOCK_TAGS:
            if tag == "pre":
                self._pre = max(0, self._pre - 1)
            self._close()
        elif tag in INLINE_TAGS and tag != "br" and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._skip:
            return
        text = data if self._pre else re.sub(r"[ \t\r\n]+", " ", data)
        if not text:
            return
        if not text.strip() and (self._block is None or not self._block.runs):
            return                          # пробелы между тегами — не текст
        run = Run(text=text)
        for fmt in self._stack:
            for key, value in fmt.items():
                setattr(run, key, value)
        self._current().runs.append(run)


def blocks(html_text: str) -> list[Block]:
    """Разобрать HTML редактора на абзацы. Строки без разметки — по абзацу на строку."""
    text = html_text or ""
    if not re.search(r"<[a-z][^>]*>", text, re.I):
        return [Block(kind="li", runs=[Run(text=line.strip())])
                for line in text.splitlines() if line.strip()]
    reader = _Reader()
    reader.feed(text)
    reader.close()
    reader._close()
    out = []
    for block in reader.blocks:
        # края абзаца без пробелов и без пустых кусков
        runs = [r for r in block.runs if r.text]
        while runs and not runs[0].text.strip():
            runs.pop(0)
        while runs and not runs[-1].text.strip():
            runs.pop()
        if not runs:
            continue
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
        # цвет текста вровень с заливкой — невидимый текст (ревьюер RC-2026-09-22-3):
        # заливку оставляем, цвет снимаем — текст читается
        for run in runs:
            if run.color and run.background and abs(_luma(run.color) - _luma(run.background)) < 0.3:
                run.color = ""
        # соседние куски с одинаковым оформлением — один кусок: иначе после каждого
        # круга «редактор → база → редактор» разметка дробилась бы
        merged: list[Run] = []
        for run in runs:
            prev = merged[-1] if merged else None
            if prev is not None and (prev.bold, prev.italic, prev.underline, prev.strike,
                                     prev.color, prev.background, prev.href) == (
                    run.bold, run.italic, run.underline, run.strike, run.color,
                    run.background, run.href):
                prev.text += run.text
            else:
                merged.append(run)
        block.runs = merged
        out.append(block)
    return out


# ── обратно в HTML: только то, что разрешено ────────────────────────────

def _run_html(run: Run) -> str:
    text = html.escape(run.text, quote=False).replace("\n", "<br>")
    style = []
    if run.color:
        style.append(f"color:{run.color}")
    if run.background:
        style.append(f"background-color:{run.background}")
    if style:
        text = f'<span style="{";".join(style)}">{text}</span>'
    if run.strike:
        text = f"<s>{text}</s>"
    if run.underline:
        text = f"<u>{text}</u>"
    if run.italic:
        text = f"<em>{text}</em>"
    if run.bold:
        text = f"<strong>{text}</strong>"
    if run.href:
        text = f'<a href="{html.escape(run.href, quote=True)}" target="_blank" rel="noopener">{text}</a>'
    return text


def render(items: list[Block]) -> str:
    """Блоки → чистый HTML: списки собираются заново, лишнего в нём нет."""
    out: list[str] = []
    open_list: bool | None = None          # открыт список: ordered? (плоский, как у Quill)

    def close_list() -> None:
        nonlocal open_list
        if open_list is not None:
            out.append("</ol>" if open_list else "</ul>")
            open_list = None

    for block in items:
        inner = "".join(_run_html(r) for r in block.runs)
        cls = []
        if block.align in ("center", "right", "justify"):
            cls.append(f"ql-align-{block.align}")
        if block.kind == "li":
            if open_list is not None and open_list != block.ordered:
                close_list()
            if open_list is None:
                open_list = block.ordered
                out.append("<ol>" if block.ordered else "<ul>")
            if block.level:
                cls.append(f"ql-indent-{min(block.level, 8)}")
            attr = f' class="{" ".join(cls)}"' if cls else ""
            out.append(f"<li{attr}>{inner}</li>")
            continue
        close_list()
        if block.kind == "h":
            tag = f"h{min(3, max(1, block.head))}"
        elif block.kind == "quote":
            tag = "blockquote"
        elif block.kind == "pre":
            tag = "pre"
        else:
            tag = "p"
            if block.level:
                cls.append(f"ql-indent-{min(block.level, 8)}")
        attr = f' class="{" ".join(cls)}"' if cls else ""
        out.append(f"<{tag}{attr}>{inner}</{tag}>")
    close_list()
    return "".join(out)


def clean(html_text: str) -> str:
    """HTML из браузера → HTML, которому можно верить."""
    return render(blocks(html_text))


def lines(html_text: str) -> list[str]:
    """Простой текст по абзацам — для «Совещания», поиска и проверки слов."""
    return [b.text for b in blocks(html_text) if b.text]


# ── из старого вида: пункты с выделением, Markdown в тексте ─────────────

_MD_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)")
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_BULLET = re.compile(r"^\s*(?:[-*•·▪◦]|\d{1,2}[.)])\s+")


def strip_markdown(text: str) -> str:
    """Без знаков разметки — для простого текста (поиск, «Совещание»)."""
    text = _MD_HEAD.sub("", text or "")
    text = _MD_BULLET.sub("", text)
    text = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _MD_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return _MD_CODE.sub(r"\1", text)


_MARKER_TEXT = re.compile(r"\[\s*(?:нужн[аоы]\s+[^\]:]+|ТРЕБУЕТСЯ[^\]:]+)(?::[^\]]*)?\]", re.I)


def _mark_places(escaped: str) -> str:
    """Метки [нужна цифра: …] в готовом (уже экранированном) тексте — красным жирным,
    чтобы место под цифру бросалось в глаза и в редакторе, как в Word."""
    return _MARKER_TEXT.sub(
        lambda m: f'<strong><span style="color:{MARKER_COLOR}">{m.group(0)}</span></strong>', escaped)


def markdown_inline(text: str) -> str:
    """Знаки Markdown в строке → оформление: **жирный**, *курсив*, `код` — просто текст.

    Ответ «Искры» и старые тезисы приходили с такими знаками, и они уходили в Word как есть.
    Метки недостающей цифры заодно красятся красным.
    """
    text = _MD_HEAD.sub("", text or "")
    text = _MD_BULLET.sub("", text)
    parts: list[str] = []
    pos = 0
    pattern = re.compile(_MD_BOLD.pattern + "|" + _MD_ITALIC.pattern + "|" + _MD_CODE.pattern)
    for m in pattern.finditer(text):
        parts.append(_mark_places(html.escape(text[pos:m.start()], quote=False)))
        groups = [g for g in m.groups() if g is not None]
        inner = _mark_places(html.escape(groups[0], quote=False)) if groups else ""
        whole = m.group(0)
        if whole.startswith("__") and re.fullmatch(r"[A-Za-z0-9_]+", groups[0] if groups else ""):
            parts.append(html.escape(whole, quote=False))   # __init__ — имя, а не жирный
        elif whole.startswith("**") or whole.startswith("__"):
            parts.append(f"<strong>{inner}</strong>")
        elif whole.startswith("`"):
            parts.append(inner)
        else:
            parts.append(f"<em>{inner}</em>")
        pos = m.end()
    parts.append(_mark_places(html.escape(text[pos:], quote=False)))
    return "".join(parts)


def _part_title(part: str) -> str:
    """Часть выступления («масштаб») → название раздела («Масштаб»)."""
    part = " ".join((part or "").split())
    return part[:1].upper() + part[1:]


def html_from_bullets(bullets) -> str:
    """Прежний вид (пункт + выделение) → HTML редактора. Заодно снимает Markdown.

    Решение пользователя 13.10 («каждый раздел — отдельным блоком, как в модераторе»): если
    у пунктов две и больше частей выступления (`part`: контекст, масштаб, вывод…), подряд
    идущие пункты одной части собираются под заголовок `<h2>Часть</h2>` — в интерфейсе это
    отдельный блок. Одна часть или ни одной — один список без заголовков, как раньше.
    Пункт без части продолжает раздел перед ним.
    """
    items = []
    for bullet in bullets:
        text = (bullet.text or "").strip()
        if not text:
            continue
        inner = markdown_inline(text)
        if bullet.emphasis == "strong":
            inner = f"<strong>{inner}</strong>"
        elif bullet.emphasis == "aside":
            inner = f'<em><span style="color:{ASIDE_COLOR}">{inner}</span></em>'
        items.append((_part_title(getattr(bullet, "part", "")), f"<li>{inner}</li>"))
    if not items:
        return ""
    parts = {part.lower() for part, _ in items if part}
    if len(parts) < 2:
        return f"<ul>{''.join(li for _, li in items)}</ul>"
    out: list[str] = []
    current = None                      # часть открытой группы
    group: list[str] = []
    for part, li in items:
        if part and part.lower() != (current or "").lower():
            if group:
                out.append(f"<ul>{''.join(group)}</ul>")
                group = []
            out.append(f"<h2>{html.escape(part, quote=False)}</h2>")
            current = part
        group.append(li)
    if group:
        out.append(f"<ul>{''.join(group)}</ul>")
    return "".join(out)


def emphasis_of(block: Block) -> str:
    """Выделение пункта по его оформлению: весь жирный — акцент, весь курсив — справочное."""
    if block.all("bold"):
        return "strong"
    if block.all("italic"):
        return "aside"
    return "normal"


def replace_word(html_text: str, word: str, by: str) -> str:
    """Заменить слово во всём тексте, не трогая разметку («zadанию» → «заданию»)."""
    items = blocks(html_text)
    for block in items:
        for run in block.runs:
            if word in run.text:
                run.text = run.text.replace(word, by)
    return render(items)


# ── предложения: каждое — отдельный пункт (задача пользователя 22.09, п. 6) ──

# после этих сокращений точка — не конец предложения
ABBREVIATIONS = {
    "т.е", "т.д", "т.п", "т.к", "т.н", "т.ч", "и.о", "п", "пп", "ст", "ч", "г", "гг", "с", "стр",
    "млн", "млрд", "тыс", "руб", "коп", "долл", "чел", "экз", "шт", "ед", "кв", "куб", "км", "м",
    "см", "мм", "кг", "т", "га", "мин", "сек", "ч", "им", "ул", "д", "корп", "обл", "р-н", "г-н",
    "гр", "проф", "доц", "акад", "зам", "ген", "дир", "рук", "нач", "отд", "ред", "изд", "см",
    "ср", "напр", "др", "пр", "прим", "рис", "табл", "прил", "разд", "гл", "абз", "вкл", "искл",
    "макс", "мин", "ок", "прибл", "уч", "фед", "муниц", "рег", "внутр", "внеш", "св", "об",
}
_SENTENCE_END = re.compile(r"([.!?…]+)(?:[»\")\]]*)\s+(?=[«\"(\[]?[А-ЯЁA-Z\d])")


def sentences(text: str) -> list[str]:
    """Разбить пункт на предложения, не ломая «т.е.», «Д.Н.», «млн.», «2,5» и «[нужна цифра: …]»."""
    text = " ".join((text or "").split())
    if not text:
        return []
    out: list[str] = []
    start = 0
    depth = 0                              # внутри [нужна цифра: …] не режем
    for m in _SENTENCE_END.finditer(text):
        depth = text.count("[", start, m.start()) - text.count("]", start, m.start())
        if depth > 0:
            continue
        before = text[start:m.start()]
        last = re.split(r"[\s(«\"]+", before)[-1] if before else ""
        last = last.rstrip(".").lower()
        if not last:
            continue
        if re.fullmatch(r"[а-яёa-z](\.[а-яёa-z])*", last):
            continue                        # инициалы: «Д.», «Д.Н.»
        if last in ABBREVIATIONS:
            continue
        if re.fullmatch(r"\d+", last) and m.group(1) == ".":
            # «рис. 3. Второе» — конец предложения; «п. 3.» и нумерация «1. Пункт» — нет
            words = re.split(r"[\s(«\"]+", before.strip())
            prev = words[-2].rstrip(".").lower() if len(words) >= 2 else ""
            if prev not in ("рис", "табл", "прил", "см", "ср") or len(words) < 3:
                continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out
