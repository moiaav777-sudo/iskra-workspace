"""Объективка в Word — своим кодом: zip + XML, без шаблона и без python-docx.

Почему так (см. CLAUDE.md → «Сборка Word»): объединение объективок переносит тела готовых
файлов как есть и работает на уровне XML (приём «Брифа», `merge.py`), и ему нужна «оболочка»
документа — те же части, из которых собирается одиночная объективка. Один способ сборки на оба
пути; ширины колонок в twips и размер фото в EMU задаются точно; python-docx остаётся тестам —
независимой проверкой того, что файл читается.

Вёрстка (решение пользователя 05.10, поля — с образцов): A4, Times New Roman, поля
верх/низ 2 см, лево 3 см, право 1,5 см. Шапка — таблица без границ в две колонки: слева фото
(ширина 4 см, высота по пропорции, не выше 5 см), справа ФИО жирным, должность, ключевые
данные. Без фото колонки нет — шапка на всю ширину. Ниже разделы с жирными подзаголовками:
«Полномочия:» → «Образование:» → «Опыт работы:» (таблица период | – | должность, как в
образце) → «Награды, почётные звания, поощрения:». Пустые разделы не выводятся.

Одна страница: при сборке длина текста оценивается по ширине колонок (перенос по словам,
ширины букв Times New Roman), и подбирается кегль/интервал по лестнице LADDER. Не помещается
и на последней ступени — документ всё равно собирается, а человеку показывается предупреждение.
"""
from __future__ import annotations

import html
import io
import math
import re
import zipfile

from . import model

# ── геометрия ───────────────────────────────────────────────────────────
PAGE_W, PAGE_H = 11906, 16838                       # A4 в twips
MARGIN = {"top": 1134, "right": 850, "bottom": 1134, "left": 1701}   # 2 / 1,5 / 2 / 3 см
TEXT_W = PAGE_W - MARGIN["left"] - MARGIN["right"]  # 9355 twips = 16,5 см
TEXT_H = PAGE_H - MARGIN["top"] - MARGIN["bottom"]  # 14570 twips
EMU_CM, EMU_PT, TW_PT = 360000, 12700, 20
PHOTO_W = 4 * EMU_CM
PHOTO_MAX_H = 5 * EMU_CM
PHOTO_GAP = 284                                      # 0,5 см между фото и текстом
# период | – | должность: в образце 1951/356/7724 (шире поля на 676) — здесь те же доли в ширину поля
CAREER_GRID = (1800, 340, TEXT_W - 1800 - 340)
# кегль, интервал: сначала уменьшаем кегль при 1,15, потом интервал до 1,0
LADDER = ((14, 1.15), (13, 1.15), (12, 1.15), (12, 1.0))
FIT_SAFETY = 0.95                                    # запас на неточность оценки (сверено с раскладкой, ±5 %)

FONT = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman" w:eastAsia="Times New Roman"/>'
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
}
ROOT = ("<w:document " + " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
        + ' mc:Ignorable="w14 wp14">')
SECT = (f'<w:sectPr><w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
        f'<w:pgMar w:top="{MARGIN["top"]}" w:right="{MARGIN["right"]}" w:bottom="{MARGIN["bottom"]}" '
        f'w:left="{MARGIN["left"]}" w:header="708" w:footer="708" w:gutter="0"/>'
        '<w:cols w:space="708"/><w:docGrid w:linePitch="360"/></w:sectPr>')
PHOTO_RID = "rIdPhoto"
MIME = {"jpeg": "image/jpeg", "png": "image/png"}

_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def esc(s) -> str:
    """Длинное тире в документах пространства не используется: «—» → «–» (как у «Брифа»)."""
    return html.escape(_XML_ILLEGAL.sub(" ", str(s)).replace("—", "–"), quote=False)


# ── оценка длины ────────────────────────────────────────────────────────
# Ширины букв Times New Roman в долях кегля (по метрикам шрифта, округлённо).
_WIDE = set("жмфшщыюЖМФШЩЫЮmwMW%@")
_NARROW = set("ijlfrtI1.,;:!'|()[]\"«»")


def _char_em(ch: str) -> float:
    if ch == " ":
        return 0.25
    if ch in _NARROW:
        return 0.3
    if ch in _WIDE:
        return 0.75 if ch.islower() else 0.9
    if ch.isdigit():
        return 0.5
    if ch.isupper():
        return 0.68
    if ch in "–-":
        return 0.5 if ch == "–" else 0.33
    return 0.48


def text_width(text: str, size: float, bold: bool = False) -> float:
    return sum(_char_em(c) for c in text) * size * (1.06 if bold else 1.0) * 1.02


def count_lines(text: str, width_pt: float, size: float, bold: bool = False) -> int:
    """Сколько строк займёт абзац при переносе по словам."""
    if not text:
        return 1
    lines, cur = 1, 0.0
    space = text_width(" ", size, bold)
    for word in text.split():
        w = text_width(word, size, bold)
        if w > width_pt:                                   # слово длиннее строки — режется
            if cur:
                lines += 1
            extra = math.ceil(w / width_pt)
            lines += extra - 1
            cur = w - (extra - 1) * width_pt
            continue
        need = w if not cur else cur + space + w
        if need <= width_pt:
            cur = need
        else:
            lines += 1
            cur = w
    return lines


def line_height(size: float, line: float) -> float:
    """Высота строки в пунктах: у Times New Roman «одинарный» — около 1,15 кегля."""
    return size * 1.15 * line


class Style:
    def __init__(self, size: float, line: float):
        self.size, self.line = size, line
        self.half = int(round(size * 2))
        self.line_tw = int(round(240 * line))
        self.lh = line_height(size, line)


# ── разметка ────────────────────────────────────────────────────────────

def run(text: str, size: float, bold: bool = False) -> str:
    half = int(round(size * 2))
    rpr = FONT + ("<w:b/><w:bCs/>" if bold else "") + f'<w:sz w:val="{half}"/><w:szCs w:val="{half}"/>'
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def para(inner: str, st: Style, jc: str = "left", before: int = 0, after: int = 0,
         keep_next: bool = False) -> str:
    keep = "<w:keepNext/>" if keep_next else ""
    rpr = f'<w:rPr>{FONT}<w:sz w:val="{st.half}"/><w:szCs w:val="{st.half}"/></w:rPr>'
    return (f'<w:p><w:pPr>{keep}<w:spacing w:before="{before}" w:after="{after}" '
            f'w:line="{st.line_tw}" w:lineRule="auto"/><w:jc w:val="{jc}"/>{rpr}</w:pPr>{inner}</w:p>')


def picture(rid: str, cx: int, cy: int, pid: int = 1, name: str = "Фото") -> str:
    return (
        '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{pid}" name="{esc(name)}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{pid}" name="{esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


def photo_extent(width_px: int, height_px: int) -> tuple[int, int]:
    """Размер фото в EMU: ширина 4 см, высота по пропорции, не выше 5 см."""
    width_px, height_px = max(1, int(width_px)), max(1, int(height_px))
    cx = PHOTO_W
    cy = int(round(cx * height_px / width_px))
    if cy > PHOTO_MAX_H:
        cy = PHOTO_MAX_H
        cx = int(round(cy * width_px / height_px))
    return cx, cy


_NO_BORDERS = ('<w:tblBorders>' + "".join(f'<w:{s} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
                                           for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
               + '</w:tblBorders>')


def table(grid: tuple[int, ...], rows: list[list[str]], cell_mar: str = "") -> str:
    tbl = (f'<w:tbl><w:tblPr><w:tblW w:w="{sum(grid)}" w:type="dxa"/>{_NO_BORDERS}'
           '<w:tblLayout w:type="fixed"/>'
           '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/>'
           '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar>'
           '<w:tblLook w:val="0000" w:firstRow="0" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" '
           'w:noHBand="1" w:noVBand="1"/></w:tblPr><w:tblGrid>'
           + "".join(f'<w:gridCol w:w="{g}"/>' for g in grid) + '</w:tblGrid>')
    for cells in rows:
        tbl += '<w:tr><w:trPr><w:cantSplit/></w:trPr>'
        for i, (width, content) in enumerate(zip(grid, cells)):
            mar = cell_mar if (i == 0 and cell_mar) else ""
            tbl += f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{mar}</w:tcPr>{content}</w:tc>'
        tbl += '</w:tr>'
    return tbl + '</w:tbl>'


# ── тело объективки ─────────────────────────────────────────────────────

def body(data: dict, photo: tuple[int, int] | None, st: Style) -> tuple[list[str], float, dict]:
    """Абзацы объективки, её высота в пунктах и высота каждого раздела.

    photo — (cx, cy) в EMU или None. Разметка и оценка считаются в одном месте, чтобы не разошлись.
    """
    out: list[str] = []
    heights: dict[str, float] = {}
    big = st.size + 2

    def ph(text, width_tw, size=st.size, bold=False, before=0, after=0):
        lh = line_height(size, st.line)
        return count_lines(text, width_tw / TW_PT, size, bold) * lh + (before + after) / TW_PT

    # шапка: ФИО, должность, ключевые данные
    head_w = TEXT_W - (photo[0] // (EMU_PT // TW_PT) + PHOTO_GAP if photo else 0)
    fio = data.get("fio") or "Фамилия Имя Отчество"
    head = [para(run(fio, big, bold=True), st, after=80)]
    head_h = ph(fio, head_w, big, True, after=80)
    pos = model.position_line(data)
    if pos:
        head.append(para(run(pos, st.size), st, jc="both", after=80))
        head_h += ph(pos, head_w, after=80)
    for line in model.key_lines(data):
        head.append(para(run(line, st.size), st, jc="both"))
        head_h += ph(line, head_w)
    if photo:
        cx, cy = photo
        photo_tw = cx // (EMU_PT // TW_PT)
        # строка с картинкой — одинарная: «авто» с множителем 1,15 растянул бы её на высоту фото × 1,15
        cell_photo = para(picture(PHOTO_RID, cx, cy), Style(st.size, 1.0))
        mar = f'<w:tcMar><w:right w:w="{PHOTO_GAP}" w:type="dxa"/></w:tcMar>'
        out.append(table((photo_tw + PHOTO_GAP, head_w), [[cell_photo, "".join(head)]], cell_mar=mar))
        head_h = max(head_h, cy / EMU_PT + st.lh * 0.2)
    else:
        out.extend(head)
    heights["head"] = head_h

    def heading(title):
        out.append(para(run(title + ":", st.size, bold=True), st, before=160, after=40, keep_next=True))
        return ph(title + ":", TEXT_W, bold=True, before=160, after=40)

    powers = [r["text"] for r in data.get("powers") or [] if r.get("text")]
    if powers:
        h = heading("Полномочия")
        for text in powers:
            line = "– " + text
            out.append(para(run(line, st.size), st, jc="both"))
            h += ph(line, TEXT_W)
        heights["powers"] = h

    edu = [model.dated(r.get("year", ""), r.get("text", "")) for r in data.get("education") or []]
    edu = [x for x in edu if x]
    if edu:
        h = heading("Образование")
        for line in edu:
            out.append(para(run(line, st.size), st, jc="both"))
            h += ph(line, TEXT_W)
        heights["education"] = h

    career = [r for r in data.get("career") or [] if r.get("text") or r.get("period")]
    if career:
        h = heading("Опыт работы")
        rows = []
        for r in career:
            rows.append([para(run(r.get("period", ""), st.size), st, jc="center"),
                         para(run("–" if r.get("period") else "", st.size), st, jc="center"),
                         para(run(r.get("text", ""), st.size), st, jc="both")])
            lines = max(count_lines(r.get("period", ""), CAREER_GRID[0] / TW_PT, st.size),
                        count_lines(r.get("text", ""), CAREER_GRID[2] / TW_PT, st.size))
            h += lines * st.lh
        out.append(table(CAREER_GRID, rows))
        heights["career"] = h

    awards = [model.dated(r.get("year", ""), r.get("text", "")) for r in data.get("awards") or []]
    awards = [x for x in awards if x]
    if awards:
        h = heading("Награды, почётные звания, поощрения")
        for line in awards:
            out.append(para(run(line, st.size), st, jc="both"))
            h += ph(line, TEXT_W)
        heights["awards"] = h
    return out, sum(heights.values()), heights


SECTION_WORDS = {"powers": "«Полномочия»", "education": "«Образование»", "career": "«Опыт работы»",
                 "awards": "«Награды»", "head": "ключевые данные"}


def fit(data: dict, photo: tuple[int, int] | None) -> tuple[Style, list[str], dict]:
    """Подобрать кегль и интервал так, чтобы объективка уложилась в одну страницу."""
    available = TEXT_H / TW_PT
    for size, line in LADDER:
        st = Style(size, line)
        parts, height, heights = body(data, photo, st)
        if height <= available * FIT_SAFETY:
            return st, parts, {"size": size, "line": line, "fits": True, "height": round(height, 1),
                               "available": round(available, 1), "pages": 1, "warning": ""}
    over = height - available * FIT_SAFETY
    lines = max(1, math.ceil(over / st.lh))
    biggest = sorted((k for k in heights if k != "head"), key=lambda k: -heights[k])[:2]
    what = " или ".join(SECTION_WORDS[k] for k in biggest) or "текст"
    warning = (f"Объективка не укладывается в одну страницу — сократите {what} "
               f"(не помещается примерно {lines} {_lines_word(lines)} при кегле {st.size:g} и интервале "
               f"{str(st.line).replace('.', ',')}).")
    return st, parts, {"size": st.size, "line": st.line, "fits": False, "height": round(height, 1),
                       "available": round(available, 1), "pages": max(2, math.ceil(height * 1.08 / available)),
                       "overflow_lines": lines, "warning": warning}


def _lines_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "строка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "строки"
    return "строк"


# ── части файла ─────────────────────────────────────────────────────────

def _styles() -> str:
    w = NS["w"]
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{w}">'
            f'<w:docDefaults><w:rPrDefault><w:rPr>{FONT}<w:sz w:val="28"/><w:szCs w:val="28"/>'
            '<w:lang w:val="ru-RU" w:eastAsia="ru-RU" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
            '</w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
            '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
            '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
            '<w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar>'
            '<w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
            '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/>'
            '<w:semiHidden/></w:style></w:styles>')


def shell(photo: tuple[bytes, str] | None = None, title: str = "Объективка") -> dict:
    """Части документа без тела: из них собирается и объективка, и объединённый файл."""
    w, r = NS["w"], NS["r"]
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="jpeg" ContentType="image/jpeg"/><Default Extension="png" ContentType="image/png"/>'
          '<Override PartName="/word/document.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/settings.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
          '<Override PartName="/word/numbering.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '<Override PartName="/docProps/app.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>')
    files = {
        "[Content_Types].xml": ct,
        "_rels/.rels": rels,
        "word/styles.xml": _styles(),
        "word/settings.xml": (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="{w}">'
                              '<w:defaultTabStop w:val="708"/><w:characterSpacingControl w:val="doNotCompress"/>'
                              '<w:compat><w:compatSetting w:name="compatibilityMode" '
                              'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'),
        "word/numbering.xml": f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="{w}" xmlns:r="{r}"></w:numbering>',
        "docProps/core.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                              '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                              'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                              'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                              f'<dc:title>{esc(title)}</dc:title><dc:creator>Объективки</dc:creator></cp:coreProperties>'),
        "docProps/app.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                             '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                             '<Application>Объективки</Application></Properties>'),
    }
    if photo:
        data, kind = photo
        files[f"word/media/photo.{kind}"] = data
        doc_rels += (f'<Relationship Id="{PHOTO_RID}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                     f'relationships/image" Target="media/photo.{kind}"/>')
    files["word/_rels/document.xml.rels"] = doc_rels + "</Relationships>"
    files["word/document.xml"] = ""
    files = {k: (v.encode("utf8") if isinstance(v, str) else v) for k, v in files.items()}
    infos = {name: zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)) for name in files}
    return {"files": files, "infos": infos, "root": ROOT, "sect": SECT}


def write(sh: dict, body_parts: list[str]) -> bytes:
    """Тело + части оболочки → байты .docx."""
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + sh["root"] + "<w:body>"
           + "".join(body_parts) + sh["sect"] + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        order = ["[Content_Types].xml"] + [n for n in sh["files"] if n != "[Content_Types].xml"]
        for name in order:
            data = xml.encode("utf8") if name == "word/document.xml" else sh["files"][name]
            z.writestr(sh["infos"][name], data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def build(data: dict, photo: tuple[bytes, str] | None = None) -> tuple[bytes, dict]:
    """Объективка в Word: (байты .docx, итог подбора страницы).

    photo — (байты, «jpeg» | «png»); размеры читаются из файла.
    """
    from .extract import image_info
    extent = None
    if photo:
        info = image_info(photo[0])
        if info and info[0] == photo[1]:
            extent = photo_extent(info[1], info[2])
        else:
            photo = None
    st, parts, result = fit(model.clean(data), extent)
    return write(shell(photo, title="Объективка " + (data.get("fio") or "")), parts), result


def file_name(data: dict) -> str:
    fio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", data.get("fio") or "").strip().rstrip(".")
    return f"Объективка {fio or 'без имени'}"[:90] + ".docx"
