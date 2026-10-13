"""Единый формат блоков: очистка и разбор HTML редактора — общий модуль (решение пользователя 13.10).

Интерфейс (`portal/static/blocks.js`) хранит текст блока как HTML и чистит его DOMPurify; сервер
обязан почистить ещё раз — текст уходит соседям и в Word. Белый список здесь ровно тот, что ставит
общая панель редактора: p, br, strong/b, em/i, u, s/strike, ul, ol, li, a, h1–h3, blockquote, pre,
span; из `style` — только цвет текста и заливка, из `class` — выравнивание и отступ Quill
(`ql-align-*`, `ql-indent-*`), у ссылок — href только http(s)/mailto.

Копия модуля лежит в каждом сервисе, который им пользуется (`app/richtext_common.py`), как у
`orders_docx.py`: тест сервиса сверяет копию с этим файлом байт в байт. Правило — править здесь
и копировать, не наоборот.

    clean(html)      → безопасный HTML по белому списку (списки собраны заново, лишнего нет)
    blocks(html)     → [Block] — абзацы документа с оформленными кусками, для Word
    render(blocks)   → HTML из блоков (то же, что даёт clean)
    text(html)       → плоский текст: абзац, пункт, заголовок — по строке
    paragraphs(html) → [str] — то же списком строк
    from_text(text)  → HTML абзацев из простого текста (пустая строка — новый абзац)
    is_blank(html)   → в блоке нет текста
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]+\))$")
_CLASS = re.compile(r"^ql-(align|indent)-[a-z0-9]+$")
TC_CLASS = "tc"           # таймкод в расшифровке «Транскрибатора»: <span class="tc">[05:12]</span>
_HREF = re.compile(r"^(https?:|mailto:)", re.I)
_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:[,/]\s*([\d.]+%?))?")
_TAG = re.compile(r"<[a-z][^<>]*>", re.I)          # без обратного перебора на входе без «>» (линейно)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")   # Word (python-docx) на них падает

INLINE = {"strong": "bold", "b": "bold", "em": "italic", "i": "italic", "u": "underline",
          "s": "strike", "strike": "strike", "del": "strike"}
BLOCK_KINDS = {"p": "p", "div": "p", "li": "li", "h1": "h", "h2": "h", "h3": "h", "h4": "h",
               "h5": "h", "h6": "h", "blockquote": "quote", "pre": "pre"}
SKIP = {"script", "style", "iframe", "object", "embed", "template", "noscript", "textarea",
        "title", "head", "svg", "math", "select", "xmp", "noembed", "noframes", "applet"}
MAX_LEVEL = 8


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
    tc: bool = False          # таймкод (серым в интерфейсе и в Word)

    def same_style(self, other: "Run") -> bool:
        return (self.bold, self.italic, self.underline, self.strike, self.color, self.background,
                self.href, self.tc) == (other.bold, other.italic, other.underline, other.strike,
                                        other.color, other.background, other.href, other.tc)


@dataclass
class Block:
    """Абзац документа: обычный абзац, пункт списка, заголовок, цитата или моноширинный."""
    kind: str = "p"           # p | li | h | quote | pre
    level: int = 1            # h: уровень заголовка 1–3
    ordered: bool = False     # li: нумерованный список
    indent: int = 0           # li: вложенность; p: отступ панели (ql-indent-N)
    align: str = ""           # center | right | justify
    runs: list[Run] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs).strip()

    def all(self, attr: str) -> bool:
        """Весь текст абзаца выделен одним способом."""
        runs = [r for r in self.runs if r.text.strip()]
        return bool(runs) and all(getattr(r, attr) for r in runs)


def _hex(color: str) -> str:
    """Цвет из редактора («rgb(0, 112, 192)» или «#0070c0») → #rrggbb, иначе пусто."""
    color = (color or "").strip().lower()
    m = _RGB.match(color)
    if m:
        alpha = m.group(4)
        if alpha:
            try:
                a = float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1)
            except ValueError:
                a = 1.0
            if a < 0.5:
                return ""              # прозрачный цвет (rgba(0,0,0,0) со страниц) — это «нет цвета», не чёрный
        r, g, b = (min(255, int(x)) for x in m.groups()[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.match(r"^#[0-9a-f]{6}$", color):
        return color
    if re.match(r"^#[0-9a-f]{3}$", color):
        return "#" + "".join(ch * 2 for ch in color[1:])
    return ""


def _luma(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _style(value: str) -> tuple[str, str]:
    """Из style — только цвет текста и фона."""
    color = background = ""
    for part in (value or "").split(";"):
        name, _, val = part.partition(":")
        name = name.strip().lower()
        if name == "color":
            color = _hex(val)
        elif name == "background-color":
            background = _hex(val)
    return color, background


class _Reader(HTMLParser):
    """HTML редактора → блоки с оформленными кусками. Неизвестные теги и свойства не переживают разбор."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._stack: list[dict] = []          # открытые строчные оформления
        self._lists: list[bool] = []          # открытые списки: ordered?
        self._block: Block | None = None
        self._skip = 0
        self._holders: list[str] = []         # открытые li/blockquote: <p> внутри них — строка того же блока

    def _open(self, kind: str, tag: str, attrs: dict) -> None:
        # <li><p>…</p><p>…</p></li>, <blockquote><p>…</p></blockquote>: абзац внутри пункта или цитаты
        # не закрывает их — это новая строка того же блока (ревью RC-2026-10-13-1, Н-9)
        if kind == "p" and self._holders and self._block is not None and self._block.kind in ("li", "quote"):
            if self._block.runs:
                self._text("\n")
            return
        self._close()
        block = Block(kind=kind)
        if kind == "h":
            block.level = min(3, max(1, int(tag[1]) if tag[1].isdigit() else 2))
        if kind == "li":
            data_list = (attrs.get("data-list") or "").lower()      # Quill 2: вид пункта в самом пункте
            block.ordered = data_list == "ordered" if data_list else bool(self._lists and self._lists[-1])
            block.indent = max(0, len(self._lists) - 1)
        for cls in (attrs.get("class") or "").split():
            if not _CLASS.match(cls):
                continue
            if cls.startswith("ql-align-"):
                block.align = cls[len("ql-align-"):]
            else:
                try:
                    block.indent = min(MAX_LEVEL, max(block.indent, int(cls[len("ql-indent-"):])))
                except ValueError:
                    pass
        self._block = block

    def _close(self) -> None:
        if self._block is not None:
            self.blocks.append(self._block)
            self._block = None

    def _style_now(self) -> dict:
        now = {"bold": False, "italic": False, "underline": False, "strike": False,
               "color": "", "background": "", "href": "", "tc": False}
        for frame in self._stack:
            for key, value in frame.items():
                if key == "tag":
                    continue
                if value:
                    now[key] = value
        return now

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("ul", "ol"):
            self._close()
            self._lists.append(tag == "ol")
            return
        if tag in BLOCK_KINDS:
            self._open(BLOCK_KINDS[tag], tag, a)
            if tag in ("li", "blockquote"):
                self._holders.append(tag)
            return
        if tag == "br":
            self._text("\n")
            return
        frame = {"tag": tag}
        if tag in INLINE:
            frame[INLINE[tag]] = True
        elif tag == "a":
            href = _CONTROL.sub("", (a.get("href") or "")).strip()
            if _HREF.match(href):
                frame["href"] = href[:1000]
        elif tag not in ("span", "font", "mark", "small", "big", "sub", "sup", "code", "label"):
            return                                 # неизвестный строчный тег — просто не оформление
        if tag == "span" and TC_CLASS in (a.get("class") or "").split():
            frame["tc"] = True
        color, background = _style(a.get("style", ""))
        if color:
            frame["color"] = color
        if background:
            frame["background"] = background
        self._stack.append(frame)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self._text("\n")
        elif tag in BLOCK_KINDS and not self._skip:
            self._open(BLOCK_KINDS[tag], tag, {k.lower(): (v or "") for k, v in attrs})
            self._close()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in ("ul", "ol"):
            self._close()
            if self._lists:
                self._lists.pop()
            return
        if tag in BLOCK_KINDS:
            if tag in ("p", "div") and self._holders and self._block is not None and self._block.kind in ("li", "quote"):
                return                             # конец строки внутри пункта/цитаты — блок остаётся открытым
            if tag in ("li", "blockquote") and self._holders:
                self._holders.pop()
            self._close()
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break

    def _text(self, data: str) -> None:
        if self._block is None:
            if not data.strip():
                return
            if self._lists:                        # текст в списке вне <li> — тоже пункт, не теряется
                self._block = Block(kind="li", ordered=self._lists[-1], indent=max(0, len(self._lists) - 1))
            else:
                self._block = Block(kind="p")
        style = self._style_now()
        run = Run(text=data, **style)
        last = self._block.runs[-1] if self._block.runs else None
        if last is not None and last.same_style(run):
            last.text += data
        else:
            self._block.runs.append(run)

    def handle_data(self, data):
        if self._skip or not data:
            return
        data = _CONTROL.sub("", data)
        if self._block is None or self._block.kind != "pre":
            data = re.sub(r"[ \t\r\n]+", " ", data)
            # пробел на стыке с предыдущим куском не удваивается: clean(clean(x)) == clean(x)
            if data.startswith(" ") and self._block is not None and self._block.runs \
                    and self._block.runs[-1].text.endswith((" ", "\n")):
                data = data[1:]
        self._text(data)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def blocks(html_text: str) -> list[Block]:
    """Разобрать HTML редактора на абзацы. Текст без разметки — по абзацу на строку."""
    text = _CONTROL.sub("", html_text or "")
    if not _TAG.search(text):
        return [Block(kind="p", runs=[Run(text=" ".join(line.split()))])
                for line in text.splitlines() if line.strip()]
    reader = _Reader()
    reader.feed(text)
    reader.close()
    reader._close()
    out: list[Block] = []
    for block in reader.blocks:
        runs = [r for r in block.runs if r.text]
        # <br> внутри абзаца остаётся переносом, но край абзаца от переносов и пробелов чистим
        while runs and not runs[0].text.strip():
            runs.pop(0)
        while runs and not runs[-1].text.strip():
            runs.pop()
        if not runs:
            continue
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
        for run in runs:
            # цвет вровень с заливкой — невидимый текст: заливку оставляем, цвет снимаем
            if run.color and run.background and abs(_luma(run.color) - _luma(run.background)) < 0.3:
                run.color = ""
        for run in runs:
            if not run.text.strip():               # одни пробелы: оформление бессмысленно, снимаем — сливается с соседями
                run.bold = run.italic = run.underline = run.strike = run.tc = False
                run.color = run.background = run.href = ""
        merged: list[Run] = []
        for run in runs:
            if merged and merged[-1].same_style(run):
                merged[-1].text += run.text
            else:
                merged.append(run)
        if block.kind != "pre":
            for i in range(1, len(merged)):           # «a </b> b» → один пробел на стыке
                if merged[i].text.startswith(" ") and merged[i - 1].text.endswith(" "):
                    merged[i].text = merged[i].text[1:]
            merged = [r for r in merged if r.text]
            if not merged:
                continue
        block.runs = merged
        out.append(block)
    # вложенность пункта — не глубже предыдущего + 1, первый пункт списка — с нуля: иначе разметку
    # нельзя собрать вложенными списками одинаково при повторной очистке (clean(clean(x)) == clean(x))
    prev = -1
    for block in out:
        if block.kind == "li":
            block.indent = max(0, min(block.indent, prev + 1))
            prev = block.indent
        else:
            prev = -1
    return out


def _run_html(run: Run) -> str:
    text = _html.escape(run.text, quote=False).replace("\n", "<br>")
    style = []
    if run.color:
        style.append(f"color:{run.color}")
    if run.background:
        style.append(f"background-color:{run.background}")
    if style:
        text = f'<span style="{";".join(style)}">{text}</span>'
    if run.tc:
        text = f'<span class="{TC_CLASS}">{text}</span>'
    if run.strike:
        text = f"<s>{text}</s>"
    if run.underline:
        text = f"<u>{text}</u>"
    if run.italic:
        text = f"<em>{text}</em>"
    if run.bold:
        text = f"<strong>{text}</strong>"
    if run.href:
        text = (f'<a href="{_html.escape(run.href, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer">{text}</a>')
    return text


def _attrs(block: Block) -> str:
    classes = []
    if block.align in ("center", "right", "justify"):
        classes.append(f"ql-align-{block.align}")
    if block.kind != "li" and block.indent:
        classes.append(f"ql-indent-{min(MAX_LEVEL, block.indent)}")
    return f' class="{" ".join(classes)}"' if classes else ""


def render(items: list[Block]) -> str:
    """Блоки → чистый HTML: списки собираются заново, лишнего в нём нет."""
    out: list[str] = []
    # Списки — настоящей вложенностью: подпункт глубже — список внутри предыдущего пункта, пункт другого
    # вида на том же уровне — соседний список. Нумерация после маркированных подпунктов продолжается
    # (ревью RC-2026-10-13-5, Н-19); интерфейс (blocks.js) собирает списки так же.
    stack: list[list] = []             # уровни: [ordered, indent, li_open]

    def close_level() -> None:
        ordered, _indent, li_open = stack.pop()
        if li_open:
            out.append("</li>")
        out.append("</ol>" if ordered else "</ul>")

    for block in items:
        inner = "".join(_run_html(r) for r in block.runs)
        if block.kind == "li":
            indent = min(MAX_LEVEL, block.indent)
            while stack and stack[-1][1] > indent:
                close_level()
            if stack and stack[-1][1] == indent and stack[-1][0] != block.ordered:
                close_level()
            if not stack or stack[-1][1] < indent:
                stack.append([block.ordered, indent, False])          # вложенный список — внутри открытого пункта
                out.append("<ol>" if block.ordered else "<ul>")
            elif stack[-1][2]:
                out.append("</li>")
            attr = f' class="ql-align-{block.align}"' if block.align in ("center", "right", "justify") else ""
            out.append(f"<li{attr}>{inner}")
            stack[-1][2] = True
            continue
        while stack:
            close_level()
        if block.kind == "h":
            out.append(f"<h{block.level}{_attrs(block)}>{inner}</h{block.level}>")
        elif block.kind == "quote":
            out.append(f"<blockquote{_attrs(block)}>{inner}</blockquote>")
        elif block.kind == "pre":
            out.append(f"<pre{_attrs(block)}>{inner}</pre>")
        else:
            out.append(f"<p{_attrs(block)}>{inner}</p>")
    while stack:
        close_level()
    return "".join(out)


def clean(html_text: str) -> str:
    """HTML из интерфейса → безопасный HTML по белому списку."""
    return render(blocks(html_text))


def paragraphs(html_text: str) -> list[str]:
    """Абзац, пункт, заголовок — строкой; перенос внутри абзаца — пробелом."""
    out = []
    for block in blocks(html_text):
        line = " ".join(block.text.replace("\n", " ").split())
        if line:
            out.append(line)
    return out


def text(html_text: str) -> str:
    return "\n".join(paragraphs(html_text))


def from_text(plain: str) -> str:
    """Простой текст → HTML абзацев: пустая строка и одиночный перенос делят абзацы."""
    out = []
    for para in re.split(r"\n\s*\n|\n", plain or ""):
        para = " ".join(para.split())
        if para:
            out.append(f"<p>{_html.escape(para, quote=False)}</p>")
    return "".join(out)


def is_blank(html_text: str) -> bool:
    return not text(html_text).strip()


def is_html(value: str) -> bool:
    """Строка похожа на HTML (есть тег) — иначе это простой текст старых записей."""
    return bool(_TAG.search(value or ""))
