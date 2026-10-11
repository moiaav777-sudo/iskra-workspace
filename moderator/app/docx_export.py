"""Документ «Модератор» в Word — вёрстка по образцу пользователя (../MODERATOR-DOCX-SPEC.md).

Образец (`03. Модератор_сироты.docx`) свёрстан не как «Повестка»: его читает ведущий с листа
за столом, поэтому лист альбомный, буквы крупные, строка длинная.

- A4 альбомный, поля 2 см, Times New Roman 16, по ширине, номер страницы со второй, шапки нет.
- Речь ведущего — маркированный список, пункт на мысль (решение пользователя 21.09: маркеры,
  не номера). Жирный — как набран в редакторе. **Курсива в речи нет**: курсив в документе значит
  «не произносится», а обращение к человеку произносится («слово Анне Сергеевне Петровой»).
- Ремарки — то, что ведущий не произносит, — синим курсивом: название части со временем
  «ЧЧ:ММ–ЧЧ:ММ» (именно часы: ведущий сверяется с часами) и строка доклада:
  «Доклад А.А. Корнеева, Статс-секретаря – заместителя Министра просвещения Российской Федерации.»
- `///` синим — между частями.
- Длинного тире «—» в выходных документах нет, как в «Брифе»: везде «–».

Время считается от начала совещания по длительностям блоков — тем же `block_times`,
что показывает вкладка «Модератор». Без времени начала документ не собирается:
подменять часы на «5 минут» пользователь не хочет.
"""
from __future__ import annotations

import io
import re
from html.parser import HTMLParser

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .models import (SECTION_CLOSING, SECTION_OPENING, SECTION_QUESTION, Meeting,
                     _genitive_role, _initials_genitive, html_to_text)

FONT = "Times New Roman"
SIZE = Pt(16)
DASH = "–"                 # ЧЧ:ММ–ЧЧ:ММ, как в образце
SEPARATOR = "///"
BLUE = RGBColor(0x00, 0x70, 0xC0)   # тот же синий, что в «Брифе» (0070C0)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# Блоки, которые ведущий произносит: они идут пунктами списка. Доклад — не речь ведущего,
# а ремарка «Слово — докладчик».
SPOKEN = {"opening", "intro", "replica", "comment", "question", "order", "problem",
          "decision", "task", "reference", "closing"}


class NoStartTime(ValueError):
    """У совещания нет времени начала — часы ремарок считать не от чего."""


def has_start_time(m: Meeting) -> bool:
    return bool(TIME_RE.match((m.time_start or "").strip()))


# ── разметка редактора → абзацы с жирным и курсивом ─────────────────────

_BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "ul", "ol"}


class _Runs(HTMLParser):
    """Rich-текст Quill → [[(текст, жирный, курсив, подчёркнутый), …], …] — абзац на пункт."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[list[tuple[str, bool, bool, bool]]] = [[]]
        self.bold = self.italic = self.under = 0

    def _break(self):
        if self.paras[-1]:
            self.paras.append([])

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._break()
        elif tag == "br":
            self._break()
        elif tag in ("strong", "b"):
            self.bold += 1
        elif tag in ("em", "i"):
            self.italic += 1
        elif tag == "u":
            self.under += 1

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._break()
        elif tag in ("strong", "b"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("em", "i"):
            self.italic = max(0, self.italic - 1)
        elif tag == "u":
            self.under = max(0, self.under - 1)

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data)
        if not text.strip() and not self.paras[-1]:
            return
        self.paras[-1].append((text, self.bold > 0, self.italic > 0, self.under > 0, False))


def speech_paragraphs(text: str) -> list[list[tuple[str, bool, bool, bool]]]:
    """Текст блока → пункты речи. Без разметки — пункт на строку."""
    text = text or ""
    if not re.search(r"<[a-z][^>]*>", text, re.I):
        return [[(line.strip(), False, False, False, False)] for line in text.splitlines() if line.strip()]
    parser = _Runs()
    parser.feed(text)
    parser.close()
    out = []
    for runs in parser.paras:
        # края абзаца без пробелов: «  Коллеги» из редактора не должно сдвигать текст
        while runs and not runs[0][0].strip():
            runs = runs[1:]
        while runs and not runs[-1][0].strip():
            runs = runs[:-1]
        if runs:
            runs = list(runs)
            runs[0] = (runs[0][0].lstrip(),) + runs[0][1:]
            runs[-1] = (runs[-1][0].rstrip(),) + runs[-1][1:]
            out.append(runs)
    return out


# ── части документа ─────────────────────────────────────────────────────

def _span(times: list[str]) -> str:
    """['14:05-14:15', '14:00-14:05'] → '14:00–14:15'.

    Берём самое раннее начало и самый поздний конец, а не первый и последний блок: доклады
    считаются по повестке, и в сценарии они стоят не в том порядке, в каком идут по часам.
    """
    pairs = [t.split("-") for t in times if t and "-" in t]
    if not pairs:
        return ""
    return min(a for a, _ in pairs) + DASH + max(b for _, b in pairs)


def _speaker_role(m: Meeting, name: str) -> str:
    low = (name or "").strip().lower()
    for a in m.agenda:
        for i, sp in enumerate(a.speakers):
            if sp.strip().lower() == low and i < len(a.speaker_roles) and a.speaker_roles[i]:
                return a.speaker_roles[i]
    for p in m.participants:
        if p.name.strip().lower() == low and p.role:
            return p.role
    return ""


def _mark(runs: list, phrase: str) -> list:
    """Выделить в абзаце фразу (должность докладчика): она становится синим курсивом."""
    phrase = (phrase or "").strip()
    if not phrase:
        return runs
    out = []
    for text, bold, italic, under, blue in runs:
        at = text.find(phrase)
        if at < 0 or blue:
            out.append((text, bold, italic, under, blue))
            continue
        if text[:at]:
            out.append((text[:at], bold, italic, under, False))
        out.append((phrase, bold, italic, under, True))
        if text[at + len(phrase):]:
            out.append((text[at + len(phrase):], bold, italic, under, False))
    return out


def parts(m: Meeting) -> list[dict]:
    """Сценарий → части по порядку: [{title, time, items: [('remark'|'speech', …)]}]."""
    if not has_start_time(m):
        raise NoStartTime("Не указано время начала совещания")
    times = {int(k): v for k, v in m.block_times().items()}
    indexed = list(enumerate(m.scenario))

    def build(title: str, blocks: list) -> dict | None:
        items = []
        for i, b in blocks:
            if b.kind == "report":
                who = (b.speaker or "").strip()
                # строку «Доклад И.О. Фамилии, должности.» сценарий уже собрал, и её могли поправить руками
                line = " ".join(html_to_text(b.text or "").split())
                if not line and who:
                    line = report_line(who, _speaker_role(m, who))
                if line:
                    items.append(("report", line, times.get(i, "")))
                continue
            if b.kind not in SPOKEN:
                continue
            text = b.text or ""
            if b.kind == "replica" and re.sub(r"<[^>]+>", "", text).strip() in ("Предложение:", ""):
                continue            # заготовка «Предложение: » без текста — говорить нечего
            paras = speech_paragraphs(text)
            if b.kind == "intro" and b.speaker:
                # «…прошу доложить Иванова Ивана Ивановича, Министра строительства.» — должность синим курсивом
                paras = [_mark(runs, _genitive_role(_speaker_role(m, b.speaker))) for runs in paras]
            if paras:
                items.append(("speech", paras, times.get(i, "")))
        if not items:
            return None
        return {"title": title, "time": _span([times.get(i, "") for i, _ in blocks]), "items": items}

    out = []
    opening = build("Вступительное слово", [(i, b) for i, b in indexed if b.section == SECTION_OPENING])
    if opening:
        out.append(opening)
    for a in m.agenda:
        if not a.num.strip():
            continue
        blocks = [(i, b) for i, b in indexed if b.section == SECTION_QUESTION and b.agenda_num == a.num]
        part = build(f"Вопрос {a.num}. {a.title}".strip(), blocks)
        if part:
            out.append(part)
    closing = build("Заключительное слово", [(i, b) for i, b in indexed if b.section == SECTION_CLOSING])
    if closing:
        out.append(closing)
    return out


# ── вёрстка ─────────────────────────────────────────────────────────────

def _font(style_or_run_font, rpr_parent) -> None:
    style_or_run_font.name = FONT
    style_or_run_font.size = SIZE
    rpr = rpr_parent.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), FONT)


def _page_number_footer(section) -> None:
    """Номер страницы со второй: первая страница — свой пустой колонтитул."""
    section.different_first_page_header_footer = True
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    run.font.name = FONT
    run.font.size = Pt(12)


def _clean(text: str) -> str:
    """Длинного тире в выходных документах нет (правило «Брифа»): «—» → «–»."""
    return (text or "").replace("—", DASH)


def _remark(doc, text: str, time: str = "", blue: bool = False) -> None:
    """Ремарка — то, что ведущий не произносит, — курсивом."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(_clean(text) + (f" · {time.replace('-', DASH)}" if time else ""))
    run.italic = True
    if blue:
        run.font.color.rgb = BLUE


def report_line(who: str, role: str) -> str:
    """«Доклад А.А. Корнеева, Статс-секретаря – заместителя Министра просвещения Российской Федерации.»"""
    text = "Доклад " + _initials_genitive(" ".join(who.split()))
    if role.strip():
        text += ", " + _genitive_role(role.strip())
    return text.rstrip(".") + "."


UNCHECKED_MARK = "ФИО не сверены с реестром людей"
RED = RGBColor(0xC0, 0x00, 0x00)


def render(m: Meeting, unchecked: bool = False) -> bytes:
    """Собрать .docx. Нет времени начала — `NoStartTime`: часы ремарок считать не от чего."""
    content = parts(m)
    doc = Document()

    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Cm(29.7), Cm(21.0)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Cm(2))
    _page_number_footer(section)
    if unchecked:
        # несверенный документ помечен в нём самом — на каждой странице, включая первую
        for footer in (section.footer, section.first_page_footer):
            mark = footer.add_paragraph()
            mark.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = mark.add_run(UNCHECKED_MARK)
            run.bold = True
            run.font.size = Pt(12)
            run.font.color.rgb = RED

    normal = doc.styles["Normal"]
    _font(normal.font, normal.element)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.0
    bullets = doc.styles["List Bullet"]
    _font(bullets.font, bullets.element)
    bullets.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    if unchecked:
        first = doc.add_paragraph()
        run = first.add_run(UNCHECKED_MARK + " — проверьте фамилии перед совещанием")
        run.bold = True
        run.font.color.rgb = RED
    for n, part in enumerate(content):
        if n:
            sep = doc.add_paragraph().add_run(SEPARATOR)
            sep.font.color.rgb = BLUE
        _remark(doc, part["title"], part["time"], blue=True)   # строка части с часами — синим курсивом
        for kind, value, time in part["items"]:
            if kind == "report":
                _remark(doc, value.rstrip(".") + ".", blue=True)
                continue
            for runs in value:
                # пункт — отдельная фраза: заготовки сценария бывают со строчной («по первому вопросу…»)
                first = runs[0][0]
                if first[:1].islower():
                    runs = [(first[:1].upper() + first[1:],) + runs[0][1:]] + list(runs[1:])
                p = doc.add_paragraph(style=bullets)
                for text, bold, italic, under, blue in runs:
                    r = p.add_run(_clean(text))
                    r.bold = bold or None
                    r.underline = under or None
                    if blue:                   # должность в передаче слова — синим курсивом
                        r.italic = True
                        r.font.color.rgb = BLUE
                    # остальной курсив из редактора не переносим: курсив в документе — ремарка

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def filename(m: Meeting, unchecked: bool = False) -> str:
    """«Модератор_<короткое название>.docx» — по нему «Материалы» узнают вид документа.

    Имя короткое и без знаков, запрещённых в Windows: папка совещания лежит глубоко,
    а путь в Windows ограничен 260 знаками.
    """
    topic = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", m.short_title or m.title or "")
    topic = " ".join(topic.split())[:60].rstrip(" .")
    mark = " (ФИО не сверены)" if unchecked else ""
    return f"Модератор_{topic}{mark}.docx" if topic else f"Модератор{mark}.docx"
