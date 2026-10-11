"""Очистка HTML, который присылают соседи (вкладка «Результаты», решение пользователя 02.10).

Своей очистки на сервере у сервиса не было: rich-текст сценария чистит интерфейс (DOMPurify).
Документ из «Транскрибатора» хранится в совещании и уходит всем, кто его откроет, поэтому
чистим его и здесь — по белому списку, без сторонних библиотек (`html.parser` из стандартной).

- Теги: p, br, b, i, u, strong, em, ul, ol, li, h2, h3, blockquote, span. Остальные снимаются,
  текст внутри остаётся; div → p, h1 → h2, h4–h6 → h3 (абзацы и заголовки не слипаются).
- script, style, iframe и прочие «исполняемые/служебные» — вместе с содержимым.
- Атрибут только `class`, и только из букв, цифр, `-`, `_` (таймкоды: `<span class="tc">`).
- Текст экранируется заново; комментарии, `<!DOCTYPE>`, инструкции — выбрасываются.
- Незакрытые теги закрываются в конце, лишние закрывающие — пропускаются.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED = {"p", "br", "b", "i", "u", "strong", "em", "ul", "ol", "li", "h2", "h3", "blockquote", "span"}
RENAME = {"div": "p", "h1": "h2", "h4": "h3", "h5": "h3", "h6": "h3"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "textarea",
                     "title", "head", "svg", "math", "select", "xmp", "noembed", "noframes", "applet"}
VOID = {"br"}
_BLOCKS = {"p", "ul", "ol", "h2", "h3", "blockquote"}
_CLASS = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop: list[str] = []      # открытые теги, содержимое которых выбрасываем

    def _tag(self, tag: str) -> str:
        tag = tag.lower()
        return RENAME.get(tag, tag)

    def handle_starttag(self, tag, attrs):
        raw = tag.lower()
        if raw in DROP_WITH_CONTENT:
            self.drop.append(raw)
            return
        if self.drop:
            return
        tag = self._tag(raw)
        if tag not in ALLOWED:
            return
        classes = []
        for name, value in attrs:
            if (name or "").lower() == "class" and value:
                classes += [c for c in value.split() if _CLASS.match(c)]
        attr = f' class="{" ".join(classes[:5])}"' if classes else ""
        # подразумеваемое закрытие, как в браузере: <li>a<li>b, <p>a<p>b — соседи, а не вложенные
        if tag == "li" and self.stack and self.stack[-1] == "li":
            self.handle_endtag("li")
        if tag in _BLOCKS and self.stack and self.stack[-1] == "p":
            self.handle_endtag("p")
        self.out.append(f"<{tag}{attr}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        raw = tag.lower()
        # <script/> не открывает выбрасываемый блок: иначе пропал бы весь документ после него
        if raw in DROP_WITH_CONTENT or self.drop or self._tag(raw) not in ALLOWED:
            return
        self.handle_starttag(raw, attrs)
        if self._tag(raw) not in VOID:
            self.handle_endtag(raw)       # <p/> — пустой элемент

    def handle_endtag(self, tag):
        raw = tag.lower()
        if raw in DROP_WITH_CONTENT:
            if raw in self.drop:
                while self.drop and self.drop.pop() != raw:
                    pass
            return
        if self.drop:
            return
        tag = self._tag(raw)
        if tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if not self.drop and data:
            self.out.append(html.escape(data, quote=False))

    # комментарии, <!DOCTYPE>, <?…?> — не нужны
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def clean_html(source: str) -> str:
    """HTML соседа → безопасный HTML по белому списку."""
    if not source:
        return ""
    parser = _Cleaner()
    parser.feed(str(source))
    parser.close()
    return parser.result().strip()


_BLOCK_END = re.compile(r"</(p|li|h2|h3|blockquote)>|<br>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_text(clean: str) -> str:
    """Очищенный HTML → плоский текст (если сосед прислал только html)."""
    text = _TAG.sub("", _BLOCK_END.sub("\n", clean or ""))
    lines = [" ".join(line.split()) for line in html.unescape(text).splitlines()]
    return "\n".join(line for line in lines if line)
