"""Выгрузка тезисов в Word по образцу пользователя.

Вёрстка не придумана, а снята с `docs/Пример оформления.docx` в `app/template.docx`
(см. `tools/make_template.py`): A4, поля, тема и шрифты, маркированный список,
кегль 16, выключка по ширине, интервал после 6 пт, межстрочный 1,15. Шапки,
колонтитулов и номеров страниц в документе нет — так в образце (подтверждено 19.09).

Что значат цвета (решения пользователя от 19.09):

- **жирный** — акцент при произнесении;
- *синий курсив* — справочное, при нехватке времени опускается;
- красный с жёлтой заливкой — метка `[нужна цифра: …]`: числа в выступлении обязательны,
  поэтому там, где в материалах их нет, стоит место под показатель (п. 3, 39).

С 22.09 оформление в документ идёт из редактора один в один: жирный, курсив,
подчёркивание, цвет, заливка, списки и отступы (`richtext.blocks`).

Числа сервис не перепроверяет и жёлтым не отмечает — решение пользователя 21.09.
"""
from __future__ import annotations

import copy
import io
import re
from pathlib import Path

from . import richtext
from .models import MARKER_RE, Thesis

TEMPLATE = Path(__file__).resolve().parent / "template.docx"
BULLET_PPR = Path(__file__).resolve().parent / "template_bullet.xml"

SIZE_PT = 16          # кегль основного текста, как в образце
# заголовки разделов: полужирные и крупнее текста — h1 на 4 пт, h2 на 2 пт, h3 на 1 пт (13.10)
HEAD_PLUS = {1: 4, 2: 2, 3: 1}
MARKER_COLOR = "C00000"  # красный: недостающая фактура

GAPS_TITLE = "ЧТО НЕОБХОДИМО ДОУТОЧНИТЬ"

# роли кусков текста внутри буллита
PLAIN, MARKER = "plain", "marker"


def _docx():
    import docx
    return docx


def _new_document():
    docx = _docx()
    if TEMPLATE.exists():
        return docx.Document(str(TEMPLATE))
    return docx.Document()


def _bullet_paragraph(doc):
    """Абзац с оформлением буллита из образца."""
    from docx.oxml import parse_xml
    para = doc.add_paragraph()
    if BULLET_PPR.exists():
        para._p.insert(0, parse_xml(BULLET_PPR.read_bytes().decode("utf-8")))
    return para


def _plain_paragraph(doc):
    """Абзац без маркера списка: заголовок блока «Что необходимо доуточнить»."""
    from docx.shared import Pt
    para = doc.add_paragraph()
    para.paragraph_format.space_after = Pt(6)
    para.paragraph_format.line_spacing = 1.15
    return para


# ── разбор текста буллита на куски ───────────────────────────────────────

def _split_by(text: str, pattern: re.Pattern, role: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            out.append((text[last:match.start()], PLAIN))
        out.append((match.group(0), role))
        last = match.end()
    if last < len(text):
        out.append((text[last:], PLAIN))
    return out


def _runs_with_markers(run) -> list[tuple[str, bool]]:
    """Кусок текста → куски (текст, это_метка): метка [нужна цифра: …] красится отдельно."""
    return [(chunk, role == MARKER) for chunk, role in _split_by(run.text, MARKER_RE, MARKER)]


def _write_block(doc, block) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
    from docx.shared import Pt, RGBColor

    if block.kind == "li":
        para = _bullet_paragraph(doc)
        if block.level:
            # вложенный пункт — правее: python-docx считает отступ от поля, а не от пункта
            para.paragraph_format.left_indent = Pt(36 + 18 * block.level)
    else:
        para = _plain_paragraph(doc)
        if block.kind == "h":
            # название раздела (решение пользователя 13.10: разделы — заголовками): отбивка
            # сверху и не отрывается от своего текста на следующую страницу
            para.paragraph_format.space_before = Pt(12)
            para.paragraph_format.keep_with_next = True
        if block.kind == "quote":
            para.paragraph_format.left_indent = Pt(36)
        elif block.kind == "p" and block.level:
            para.paragraph_format.left_indent = Pt(18 * block.level)
    align = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
             "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(block.align)
    if align is not None:
        para.alignment = align

    for run in block.runs:
        for chunk, is_marker in _runs_with_markers(run):
            if not chunk:
                continue
            piece = para.add_run(chunk)
            piece.font.size = Pt(SIZE_PT + (HEAD_PLUS.get(block.head, 2) if block.kind == "h" else 0))
            piece.bold = run.bold or block.kind == "h" or None
            piece.italic = run.italic or block.kind == "quote" or None
            if run.underline or run.href:
                piece.underline = True
            if run.strike:
                piece.font.strike = True
            if block.kind == "pre":
                piece.font.name = "Courier New"
            if run.color:
                piece.font.color.rgb = RGBColor.from_string(run.color.lstrip("#").upper())
            if run.background:
                piece.font.highlight_color = WD_COLOR_INDEX.YELLOW
            if is_marker:
                # место под цифру должно бросаться в глаза: красным и с заливкой
                piece.bold = True
                piece.font.color.rgb = RGBColor.from_string(MARKER_COLOR)
                piece.font.highlight_color = WD_COLOR_INDEX.YELLOW


# ── сборка документа ─────────────────────────────────────────────────────

def build(thesis: Thesis):
    """Документ Word с тезисами: абзацы и оформление — как в редакторе.
    Слова вперемешку с латиницей не пропускает main.py."""
    from docx.shared import Pt, RGBColor

    doc = _new_document()
    html = thesis.html or richtext.html_from_bullets(thesis.bullets)
    for block in richtext.blocks(html):
        _write_block(doc, block)

    gaps = [g for g in thesis.gaps if not g.resolved]
    if gaps:
        _plain_paragraph(doc)
        title = _plain_paragraph(doc)
        run = title.add_run(GAPS_TITLE)
        run.bold = True
        run.font.size = Pt(SIZE_PT)
        run.font.color.rgb = RGBColor.from_string(MARKER_COLOR)
        for gap in gaps:
            para = _bullet_paragraph(doc)
            text = f"[{gap.marker}]"
            if gap.suggestion:
                text += f" — {gap.suggestion}"
            run = para.add_run(text)
            run.bold = True
            run.font.size = Pt(SIZE_PT)
            run.font.color.rgb = RGBColor.from_string(MARKER_COLOR)
    return doc


def to_bytes(doc) -> bytes:
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def file_name(thesis: Thesis, who: str = "") -> str:
    topic = re.sub(r"[\\/:*?\"<>|]", " ", thesis.topic or "без темы").strip()
    topic = " ".join(topic.split())[:60] or "без темы"
    who = " ".join((who or "").split())
    return f"Тезисы — {topic}{(' — ' + who) if who else ''}.docx"
