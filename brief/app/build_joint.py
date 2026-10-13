"""
Сводный бриф: несколько брифов — один документ Word.

Два пути, итог одинаковый:

  build_joint  — брифы из реестра: каждый собирается заново из своих фактов теми
                 же сборщиками, что делают отдельные брифы (стили и колонтитулы общие);
  merge_files  — прикреплённые файлы брифов (.docx, в том числе поправленные в Word):
                 тело каждого переносится как есть, меняется только номер вопроса.

  страница 1 — содержание: «1. О системе общего образования … 2»;
  дальше      — вопросы по порядку, каждый с новой страницы.

Номера страниц в содержании — числа в самом файле (pages.write_paged): прикидка при
сборке, с Word (если включён) — его числа, уточнённые в фоне.
Только стандартная библиотека.
"""
import bisect
import html
import io
import random
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

import build_brief as bb
import pages
import build_topic as bt
import build_summary as bs

HEAD_SIZE = 36          # 18 пт — заголовок сводного документа
LINE_SIZE = 32          # 16 пт — строки содержания, как основной текст брифа


def _run(text, b=False, size=LINE_SIZE):
    rpr = bb.FONT + ("<w:b/><w:bCs/>" if b else "") + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{bb.esc(text)}</w:t></w:r>'


def _field(instr, default="1"):
    """Поле Word: номер страницы по закладке. Результат — уже посчитанное число, Word его
    только освежит при обновлении полей."""
    rpr = f'<w:rPr>{bb.FONT}<w:sz w:val="{LINE_SIZE}"/><w:szCs w:val="{LINE_SIZE}"/></w:rPr>'
    return (f'<w:r>{rpr}<w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r>{rpr}<w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>'
            f'<w:r>{rpr}<w:fldChar w:fldCharType="separate"/></w:r>'
            f'<w:r>{rpr}<w:t>{default}</w:t></w:r>'
            f'<w:r>{rpr}<w:fldChar w:fldCharType="end"/></w:r>')


def _p(inner, before=0, after=120, align="both", first=0, tabs=False):
    tab = ('<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="10800"/></w:tabs>' if tabs else "")
    return ('<w:p><w:pPr>' + tab +
            f'<w:spacing w:before="{before}" w:after="{after}" w:line="276" w:lineRule="auto"/>'
            f'<w:ind w:firstLine="{first}"/><w:jc w:val="{align}"/></w:pPr>{inner}</w:p>')


def page_break():
    return '<w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:br w:type="page"/></w:r></w:p>'


TOC_ANCHOR = "_Toc1"    # «К оглавлению» в брифе для региона ведёт сюда


def contents(items, title=None, subtitle=None, pages_of=None):
    """Первая страница: заголовок и содержание с номерами страниц.

    pages_of — {закладка: номер страницы}; без него в полях стоит «1» (только для прикидки)."""
    out = []
    if title:
        out.append(_p(_run(title, b=True, size=HEAD_SIZE), after=60, align="center"))
    if subtitle:
        out.append(_p(_run(subtitle), after=240, align="center"))
    out.append(_p(f'<w:bookmarkStart w:id="1" w:name="{TOC_ANCHOR}"/>' + _run("Содержание", b=True)
                  + '<w:bookmarkEnd w:id="1"/>', after=160, align="center"))
    for i, it in enumerate(items, 1):
        tab = f'<w:r><w:rPr>{bb.FONT}<w:sz w:val="{LINE_SIZE}"/></w:rPr><w:tab/></w:r>'
        name = _run(f"{i}. {it['title']}")
        page = _field(f'PAGEREF {it["anchor"]} \\h', str((pages_of or {}).get(it["anchor"], 1)))
        link = f'<w:hyperlink w:anchor="{it["anchor"]}" w:history="1">{name}{tab}{page}</w:hyperlink>'
        out.append(_p(link, after=80, align="left", tabs=True))
    return out


def build_joint(questions, out_docx, title="Бриф", subtitle=""):
    """Сводный документ по нескольким вопросам.

    questions: [{"kind": region|topic|summary, "facts": {...},
                 "text": текст справки (для region), "sources": [{name,text}] (для topic/summary),
                 "title": как называется вопрос в содержании}]
    """
    shell = bb.shell_parts()
    items, chunks, reports, total, bad_all = [], [], [], 0, []

    for i, q in enumerate(questions, 1):
        anchor, F = f"_Q{i}", q["facts"]
        kind = q.get("kind") or "region"
        if kind == "region":
            part, ck, notes, problems, solutions = bb.region_body(
                q.get("text") or "", F, shell, toc_link=False,
                anchor=anchor, anchor_id=100 + i, title_text=f"{i}. {q['title']}")
            bad = [{"path": p, "why": why, "quote": str(f.get("q") or "")[:300]} for p, why, f in ck.bad()]
        elif kind == "summary":
            part, ck, res = bs.summary_body(q.get("sources") or [], F, shell,
                                            anchor=anchor, anchor_id=100 + i, title_text=f"{i}. {q['title']}")
            bad = res["unverified"]
        else:
            part, ck, _, problems, solutions = bt.topic_body(
                q.get("sources") or [], F, shell, title_text=f"{i}. {q['title']}",
                anchor=anchor, anchor_id=100 + i)
            bad = [{"path": p, "why": why, "quote": (b.get("q") or b.get("text") or "")[:300]}
                   for p, why, b in ck.bad()]

        items.append({"anchor": anchor, "title": q["title"]})
        chunks.append(page_break() + "".join(part))          # каждый вопрос — с новой страницы
        n = len(ck.log)
        total += n
        for b in bad:
            b = dict(b, question=i, question_title=q["title"])
            bad_all.append(b)
        reports.append(f"## {i}. {q['title']}\n\n"
                       f"Вид: {kind}. Проверено фактов: {n}, не подтверждено: {len(bad)}.\n" +
                       ("\n".join(f"- `{b['path']}` — {'; '.join(b['why'])}. Цитата: «{b['quote']}»" for b in bad)
                        or "- расхождений нет"))

    page_of = pages.write_paged(shell, lambda pg: contents(items, title, subtitle, pg),
                                chunks, [it["anchor"] for it in items], out_docx)
    result = {
        "region": title, "title": title, "docx": str(out_docx),
        "facts_total": total, "unverified": bad_all, "consistency": [], "manual": [],
        "signals": [], "problems": [], "solutions": [],
        "questions": [{"title": q["title"], "kind": q.get("kind") or "region"} for q in questions],
        "pages": [page_of[it["anchor"]] for it in items],
    }
    rep = [f"# Проверка сводного брифа: {title}", "",
           f"Вопросов: {len(questions)}. Проверено фактов: {total}, не подтверждено: {len(bad_all)}.", ""]
    rep += reports
    rep_path = Path(out_docx).with_suffix(".проверка.md")
    rep_path.write_text("\n\n".join(rep) + "\n", encoding="utf8")
    result["report"] = str(rep_path)
    return result


# ───────────────────────── объединение прикреплённых файлов ─────────────────────────
# Человек прикладывает готовые брифы (.docx, в том числе поправленные в Word) и нажимает
# «Объединить» (просьба пользователя 21.09). Факты здесь не пересобираются и «Искра» не
# нужна: тело каждого брифа переносится как есть. Меняется только номер вопроса в первой
# строке второго столбца, закладки и связи файла переименовываются, чтобы брифы не
# перепутали друг другу ссылки, картинки и списки.

MAX_XML = 20 * 1024 * 1024           # распакованный document.xml одного брифа
MAX_PART = 10 * 1024 * 1024          # стили, списки и прочие части
MAX_MEDIA = 15 * 1024 * 1024         # одна картинка
MAX_TOTAL = 60 * 1024 * 1024         # весь архив в распакованном виде (326 КБ «бомбы» → 300 МБ, Н1)
MAX_ENTRIES = 2000
W_MAIN = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_MAIN = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT = ("http://purl.oclc.org/ooxml/wordprocessingml/main", "http://purl.oclc.org/ooxml/officeDocument/relationships")
BROKEN = (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError, OSError, ValueError)
R_ATTR = re.compile(r'\br:(id|embed|link|pict)="([^"]*)"')
REL_IMAGE = "/image"
REL_LINK = "/hyperlink"
SAFE_LINK = re.compile(r"^(?:https?://|mailto:)[^\\\\]*$", re.I)   # file://, \\\\сервер, относительные — нет


def safe_link(target):
    """Ссылку наружу переносим, только если она ведёт в веб или на почту (RC-2026-09-22-2):
    file:// и \\\\сервер\\… при нажатии тянут файл с чужого компьютера."""
    return bool(SAFE_LINK.match(_unescape(str(target or "")).strip()))


class MergeError(ValueError):
    """Файл нельзя вложить в сводный: человеку показывается как есть."""


def _spans(xml, tag, start=0, end=None):
    """Верхнеуровневые элементы <w:tag> в xml[start:end] — с учётом вложенных таких же."""
    end = len(xml) if end is None else end
    opener = re.compile(rf"<w:{tag}(?=[ >/])|</w:{tag}>")
    out, depth, at = [], 0, None
    for m in opener.finditer(xml, start, end):
        if m.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                out.append((at, m.end()))
        else:
            close = xml.index(">", m.start())
            if xml[close - 1] == "/":            # <w:p/> — пустой, без вложений
                if depth == 0:
                    out.append((m.start(), close + 1))
                continue
            if depth == 0:
                at = m.start()
            depth += 1
    return out


def _text(xml):
    return "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml)).replace("&lt;", "<") \
        .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def _renumber(p_xml, n):
    """Номер вопроса в абзаце: прежний «1. » снимается, ставится «n. »; прочая разметка та же."""
    segs = list(re.finditer(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)", p_xml))
    full = "".join(m.group(2) for m in segs)
    lead = re.match(r"^\s*(?:\d+\s*[.)]\s*)?", full).end()
    out, pos, placed = [], 0, False
    for m in segs:
        text = m.group(2)
        cut = min(lead, len(text))
        lead -= cut
        text = text[cut:]
        opening = m.group(1)
        if not placed and text:
            text = f"{n}. " + text
            placed = True
            if "xml:space" not in opening:
                opening = opening[:-1] + ' xml:space="preserve">'
        out.append(p_xml[pos:m.start()] + opening + text + m.group(3))
        pos = m.end()
    out.append(p_xml[pos:])
    return "".join(out)


def _read(z, part, name, limit=MAX_PART):
    """Часть архива с пределом по размеру. Битый архив, шифр, чужое сжатие — MergeError, а не 500.
    Нет такой части — None."""
    try:
        info = z.getinfo(part)
    except KeyError:
        return None
    if info.file_size > limit:
        raise MergeError(f"«{name}» слишком большой для брифа.")
    try:
        return z.read(info)
    except BROKEN:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть. Сохраните его в Word заново.")


def _check_namespaces(raw, name):
    """Разметка Word — только под привычными приставками «w:» и «r:» (RC-2026-09-22-1, К1).

    Дальше тело разбирается по тексту, и фильтр полей узнаёт их по «w:instrText» и
    «w:fldSimple». Под другой приставкой (xmlns:q=…/main, <q:instrText>) или по умолчанию
    (xmlns=…/main) поле прошло бы мимо фильтра. Word так не сохраняет, поэтому такой файл —
    отказ с просьбой пересохранить. Проверяется разбором XML: объявление может стоять на
    любом элементе, адрес может быть записан ссылками на символы."""
    again = f"«{name}» сохранён не так, как сохраняет Word. Откройте его в Word и сохраните как обычный .docx."
    head = raw[:4096].decode("utf8", "replace")
    if "<!DOCTYPE" in raw.decode("utf8", "replace").upper() or "<!ENTITY" in head.upper():
        raise MergeError(again)
    try:
        for _, (prefix, uri) in ET.iterparse(io.BytesIO(raw), events=("start-ns",)):
            if uri in STRICT or (uri == W_MAIN) != (prefix == "w") or (uri == R_MAIN) != (prefix == "r"):
                raise MergeError(again)
    except ET.ParseError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть. Сохраните его в Word заново.")


def _rels(z, path, name):
    data = _read(z, path, name)
    if data is None:
        return {}
    try:
        xml = data.decode("utf8")
    except UnicodeDecodeError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть.")
    out = {}
    for m in re.finditer(r"<Relationship\b[^>]*/?>", xml):
        # значения — как в файле, со ссылками на символы; при записи экранируем заново (RC-22-3: «&» дважды)
        a = {k: _unescape(v) for k, v in re.findall(r'(\w+)="([^"]*)"', m.group(0))}
        out[a.get("Id")] = (a.get("Type", ""), a.get("Target", ""), a.get("TargetMode", ""))
    return out


def _attr(value):
    """Значение атрибута XML: «&», «<» и кавычка — ссылками на символы, ровно один раз."""
    return html.escape(str(value), quote=True)


def _is_page_break_p(p_xml):
    """Абзац, в котором нет ничего, кроме разрыва страницы (и пустых абзацев рядом)."""
    return not _text(p_xml).strip() and "<w:drawing" not in p_xml and "<w:pict" not in p_xml


def _question_spans(body, tables):
    """Границы вопросов в теле файла с несколькими брифами.

    Вопрос начинается со своей таблицы «Вопрос / …» и длится до таблицы следующего: сюда попадают
    абзацы после таблицы («К оглавлению», разрывы страниц). Хвост из пустых абзацев и разрывов
    страниц срезается — каждый вопрос в сводном и так начинается с новой страницы, иначе между
    ними вставали бы пустые листы. Всё до первой таблицы (заголовок файла, его содержание) в
    сводный не идёт: у сводного они свои."""
    spans = []
    for i, (a, b, _c) in enumerate(tables):
        end = tables[i + 1][0] if i + 1 < len(tables) else len(body)
        # хвост — только абзацы верхнего уровня ПОСЛЕ таблицы вопроса: ни абзацы в её ячейках
        # (ревью RC-2026-10-13-4, К-1), ни абзацы внутри второй таблицы или элемента управления
        # после неё (ревью RC-2026-10-13-8, Н-9) не трогаем
        # промежутки таблиц и sdt после таблицы вопроса — слитые и отсортированные, поиск двоичный:
        # перебор «каждый абзац × каждая таблица» рос квадратично (ревью RC-2026-10-13-13, К-1)
        inner: list[list[int]] = []
        for x0, x1 in sorted(_spans(body, "tbl", b, end) + _spans(body, "sdt", b, end)):
            if inner and x0 <= inner[-1][1]:
                inner[-1][1] = max(inner[-1][1], x1)
            else:
                inner.append([x0, x1])
        starts = [x0 for x0, _x1 in inner]

        def inside(pos: int) -> bool:
            k = bisect.bisect_right(starts, pos) - 1
            return k >= 0 and pos < inner[k][1]

        tail = [s for s in _spans(body, "p", b, end) if not inside(s[0])]
        while tail and _is_page_break_p(body[slice(*tail[-1])]) and "w:anchor" not in body[slice(*tail[-1])]:
            cut = tail.pop()[0]
            k = bisect.bisect_left(starts, cut)
            if k < len(starts) and starts[k] < end:
                break                      # за этим абзацем ещё стоит таблица или sdt — хвост на них кончается (RC-12, К-1)
            end = cut
        spans.append((a, max(end, b)))
    return spans


def read_briefs(name, data):
    """Прикреплённый файл → список брифов в нём (обычно один; документ с несколькими вопросами —
    в том числе прежний сводный — по брифу на таблицу «Вопрос / …»). Не бриф — MergeError.

    Решение пользователя 13.10: в документ, где уже десять брифов, одиннадцатый должен вставать
    одиннадцатым — а не вклеиваться целым документом со вторым заголовком и оглавлением."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise MergeError(f"«{name}» — старый формат Word (.doc). Откройте его в Word и сохраните как .docx.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except BROKEN:
        raise MergeError(f"«{name}» — не документ Word (.docx). Объединяются только брифы в Word.")
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_TOTAL:
        raise MergeError(f"«{name}» слишком большой для брифа.")
    raw = _read(z, "word/document.xml", name, MAX_XML)
    if raw is None:
        raise MergeError(f"«{name}» — не документ Word (.docx). Объединяются только брифы в Word.")
    try:
        xml = raw.decode("utf8")
    except UnicodeDecodeError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть.")
    _check_namespaces(raw, name)
    m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
    if not m or "<w:document" not in xml:
        raise MergeError(f"«{name}» повреждён: в нём нет текста документа.")
    body_at = m.start(1)
    body = m.group(1)
    # бриф — это таблица «Вопрос / История / Текущий статус / …»: её первая ячейка «Вопрос»;
    # таких таблиц в файле может быть несколько — тогда это несколько брифов
    tables = []
    for a, b in _spans(body, "tbl"):
        rows = _spans(body, "tr", a, b)
        cells = _spans(body, "tc", *rows[0]) if rows else []
        if len(cells) >= 2 and _text(body[slice(*cells[0])]).strip().rstrip(":").lower() == "вопрос":
            tables.append((a, b, cells[1]))
    if not tables:
        raise MergeError(f"«{name}» не похож на бриф: в нём нет таблицы «Вопрос / История / Текущий статус». "
                         "Объединяются только брифы.")
    if re.search(r'<w:(footnote|endnote)Reference\b', body):
        raise MergeError(f"«{name}»: в брифе есть сноски — их объединение не поддерживается. "
                         "Уберите сноски или перенесите их в текст.")
    root = xml[:xml.index(">", xml.index("<w:document")) + 1]
    rels = _rels(z, "word/_rels/document.xml.rels", name)
    # «_top» (служебная ссылка Word «в начало документа») в сводном ведёт к содержанию — и у одиночного брифа
    toc_anchors = {"_top"}
    if len(tables) == 1:
        spans = [(0, len(body))]
    else:
        spans = _question_spans(body, tables)
        # закладки из отброшенной шапки файла (его заголовок, «Содержание»): ссылки «К оглавлению»
        # на них теперь тоже ведут к содержанию сводного
        toc_anchors |= set(re.findall(r'<w:bookmarkStart\b[^>]*w:name="([^"]*)"', body[:tables[0][0]]))
    file_key = f"{name}:{len(data)}:{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    out = []
    for k, ((a, b, cell), (s0, s1)) in enumerate(zip(tables, spans), 1):
        part = body[s0:s1]
        shift = s0
        paras = [(p0 - shift, p1 - shift) for p0, p1 in _spans(body, "p", *cell) if _text(body[p0:p1]).strip()]
        if not paras:
            raise MergeError(f"«{name}»: в первой строке брифа{f' № {k}' if len(tables) > 1 else ''} не заполнен вопрос.")
        title = re.sub(r"^\s*\d+\s*[.)]\s*", "", " ".join(_text(part[slice(*paras[0])]).split()))
        out.append({"name": name, "zip": z, "root": root, "body": part, "title": title, "title_p": paras[0],
                    "table": (a - shift, b - shift), "rels": rels, "body_at": body_at + s0,
                    "index": k, "count": len(tables), "toc_anchors": toc_anchors,
                    "file_key": file_key, "file_body": body})
    return out


def read_brief(name, data):
    """Первый (обычно единственный) бриф файла — для проверок и прежних вызовов."""
    return read_briefs(name, data)[0]


# «К оглавлению» под таблицей — как у брифа, собранного сервисом (build_brief.region_body)
TOC_LINK_P = ('<w:p/><w:p><w:hyperlink w:anchor="' + TOC_ANCHOR + '" w:history="1"><w:r><w:rPr><w:rStyle w:val="afa"/>'
              + bb.FONT + '<w:b/><w:bCs/><w:i/><w:iCs/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>'
              '<w:t>К оглавлению</w:t></w:r></w:hyperlink></w:p>')


# Поля Word, которые остаются в перенесённом брифе: только внутренние, ничего не тянут
# снаружи (RC-2026-09-21-18, К1). INCLUDEPICTURE «\\\\сервер\\…» открыл бы соединение с чужим
# сервером и отдал бы ему хеш пароля учётки, DDEAUTO запускает программы, INCLUDETEXT
# и ему подобные читают файлы и адреса. У остальных полей снимается код, текст результата
# (то, что было видно на странице) остаётся.
SAFE_FIELDS = {"PAGEREF", "REF", "PAGE", "NUMPAGES", "SECTIONPAGES", "TOC", "SEQ", "NOTEREF"}
_LOCAL_LINK = re.compile(r'HYPERLINK\s+\\l\s+"?[\w.-]+"?(?:\s+\\[a-zA-Z](?:\s+"[^"\\]*")?)*', re.I)
_FIELD_TOKEN = re.compile(
    r'<w:fldChar\b[^>]*?/>|<w:fldChar\b[^>]*>.*?</w:fldChar>'
    r'|<w:(?:instrText|delInstrText)\b[^>]*>.*?</w:(?:instrText|delInstrText)>|<w:(?:instrText|delInstrText)\b[^>]*/>'
    r'|<w:(?:t|delText)\b[^>]*>[^<]*</w:(?:t|delText)>', re.S)


def _unescape(t):
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def field_ok(instr):
    """Можно ли оставить поле с таким кодом в сводном документе."""
    instr = " ".join(_unescape(instr).split())
    if not instr:
        return False
    head = instr.split()[0].upper()
    if head == "HYPERLINK":
        return bool(_LOCAL_LINK.fullmatch(instr))
    return head in SAFE_FIELDS and not re.search(r"\\\\|//|:\\|\bfile:", instr, re.I)


def strip_fields(body):
    """Снять из тела все поля, кроме внутренних (field_ok); результат поля остаётся текстом."""
    fields, stack, toks = [], [], []
    for m in _FIELD_TOKEN.finditer(body):
        tok, ctx, own = m.group(0), [(f, fields[f]["phase"]) for f in stack], None
        if tok.startswith("<w:fldChar"):
            kind = re.search(r'w:fldCharType="(\w+)"', tok)
            kind = kind.group(1) if kind else ""
            if kind == "begin":
                if stack and fields[stack[-1]]["phase"] == "instr":
                    fields[stack[-1]]["nested"] = True        # код поля собирается из другого поля — не доверяем
                fields.append({"instr": "", "phase": "instr", "nested": False, "closed": False})
                own = len(fields) - 1
                stack.append(own)
            elif kind == "separate" and stack:
                own = stack[-1]
                fields[own]["phase"] = "result"
            elif kind == "end" and stack:
                own = stack.pop()
                fields[own]["closed"] = True
        elif "instrText" in tok[:16] or "delInstrText" in tok[:16]:
            if stack and fields[stack[-1]]["phase"] == "instr":
                fields[stack[-1]]["instr"] += re.sub(r"<[^>]*>", "", tok)
        toks.append((m.start(), m.end(), ctx, own, tok))
    ok = [f["closed"] and not f["nested"] and field_ok(f["instr"]) for f in fields]
    out, pos = [], 0
    for a, e, ctx, own, tok in toks:
        drop = (own is not None and not ok[own]) or any(ph == "instr" and not ok[f] for f, ph in ctx)
        if not drop and own is None and ("instrText" in tok[:16]) and not ctx:
            drop = True                                        # код поля без самого поля
        if drop:
            out.append(body[pos:a])
            pos = e
    out.append(body[pos:])
    body = "".join(out)
    # простые поля <w:fldSimple w:instr="…">: чужое — оставить только содержимое
    keep = "w:fldSimpleKEEP"
    inner = re.compile(r'<w:fldSimple\b([^>]*?)(/>|>((?:(?!<w:fldSimple\b).)*?)</w:fldSimple>)', re.S)
    while True:
        m = inner.search(body)
        if not m:
            break
        instr = re.search(r'w:instr="([^"]*)"', m.group(1))
        if instr and field_ok(instr.group(1)):
            new = m.group(0).replace("<w:fldSimple", "<" + keep, 1).replace("</w:fldSimple>", "</" + keep + ">")
        else:
            new = m.group(3) or ""
        body = body[:m.start()] + new + body[m.end():]
    return body.replace(keep, "w:fldSimple")


DANGER = re.compile(r"\b(?:INCLUDE\w*|DDE\w*|LINK|IMPORT|DATABASE|AUTOTEXT\w*|MACROBUTTON|GOTOBUTTON)\b|\\\\|//", re.I)


def _check_xml(root, chunk, name):
    """Бриф после переноса должен разбираться как XML — иначе Word не откроет весь сводный (Н3).

    Заодно вторая линия защиты от полей (RC-2026-09-22-1): разбор по адресам пространств имён,
    а не по приставкам. Остался код опасного поля — значит, фильтр обошли, файл не берём."""
    try:
        tree = ET.fromstring(root + "<w:body>" + chunk + "</w:body></w:document>")
    except ET.ParseError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть. Сохраните его в Word заново.")
    codes = []
    for el in tree.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag.startswith("{" + STRICT[0] + "}"):
            codes.append("strict")
        elif tag in (f"{{{W_MAIN}}}instrText", f"{{{W_MAIN}}}delInstrText"):
            codes.append(el.text or "")
        elif tag == f"{{{W_MAIN}}}fldSimple" and not field_ok(el.get(f"{{{W_MAIN}}}instr") or ""):
            codes.append("fldSimple")
    if any(c in ("strict", "fldSimple") for c in codes) or DANGER.search(" ".join(codes)):
        raise MergeError(f"«{name}» содержит поля Word, которые нельзя переносить в сводный. "
                         "Откройте его в Word и сохраните как обычный .docx.")


def _body_of(b, n, ids, media, new_rels, num_map, style_map=None):
    """Тело n-го брифа для сводного: номер, закладка _Qn, свои закладки, связи, списки и стили."""
    body = b["body"]
    a, e = b["title_p"]
    p = _renumber(body[a:e], n)
    # вопрос мог быть нумерованным заголовком Word (<w:numPr>, нумерация стиля): автоматический номер
    # снимается — иначе рядом с нашим «n. » стоял бы ещё и свой «1.» (документ пользователя 13.10)
    no_num = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr>'
    if "<w:numPr>" in p:
        p = re.sub(r"<w:numPr>.*?</w:numPr>", no_num, p, count=1, flags=re.S)      # на том же месте
    elif "<w:pStyle " in p:
        # у стиля может быть своя нумерация: ставим numPr по порядку схемы pPr —
        # после pStyle, keepNext, keepLines, pageBreakBefore, framePr, widowControl (ревью RC-2026-10-13-4, Н-2)
        head = re.match(r"(.*?<w:pPr>)((?:<w:(?:pStyle|keepNext|keepLines|pageBreakBefore|framePr|widowControl)\b[^>]*?/>)*)",
                        p, flags=re.S)
        if head:
            p = head.group(1) + head.group(2) + no_num + p[head.end():]
    anchor = f"_Q{n}"
    at = p.index("</w:pPr>") + len("</w:pPr>") if "</w:pPr>" in p else p.index(">") + 1
    # номер закладки проставим после переименования чужих, иначе его задело бы совпадение
    # имя закладки тоже ставим после переименования чужих: во вложенном сводном уже есть свои «_Q1…»
    # (13.10) — иначе наша и его слились бы в одну и содержание вело бы не туда
    p = (p[:at] + '<w:bookmarkStart w:id="QID" w:name="QNAME"/>' + p[at:-len("</w:p>")]
         + '<w:bookmarkEnd w:id="QID"/></w:p>')
    body = body[:a] + p + body[e:]
    # окончание файла: его разметка страницы — уже от сводного документа
    body = re.sub(r"<w:sectPr\b.*?</w:sectPr>|<w:sectPr\b[^>]*/>", "", body, flags=re.S)
    body = strip_fields(body)
    # примечания рецензентов в сводный не переносятся (сам текст остаётся)
    body = re.sub(r"<w:comment(?:RangeStart|RangeEnd|Reference)\b[^>]*/>", "", body)
    # закладки брифа: «К оглавлению» (_Toc1) теперь ведёт к содержанию сводного,
    # остальные получают приставку, чтобы брифы не перепутали ссылки друг друга
    names = set(re.findall(r'<w:bookmarkStart\b[^>]*w:name="([^"]*)"', body)) - {"QNAME"}
    old_ids = {}

    def start(m):
        nm = re.search(r'w:name="([^"]*)"', m.group(0)).group(1)
        i = re.search(r'w:id="([^"]*)"', m.group(0)).group(1)
        if nm == "QNAME":
            return m.group(0)
        if nm == TOC_ANCHOR:
            old_ids[i] = None
            return ""
        old_ids[i] = str(next(ids))
        return re.sub(r'w:name="[^"]*"', f'w:name="{(f"_M{n}" + nm)[:40]}"',
                      re.sub(r'w:id="[^"]*"', f'w:id="{old_ids[i]}"', m.group(0)))

    def end(m):
        i = re.search(r'w:id="([^"]*)"', m.group(0)).group(1)
        if i not in old_ids:
            return m.group(0)
        return "" if old_ids[i] is None else re.sub(r'w:id="[^"]*"', f'w:id="{old_ids[i]}"', m.group(0))

    body = re.sub(r"<w:bookmarkStart\b[^>]*/>", start, body)
    body = re.sub(r"<w:bookmarkEnd\b[^>]*/>", end, body)
    body = body.replace('w:id="QID"', f'w:id="{next(ids)}"').replace('w:name="QNAME"', f'w:name="{anchor}"')
    for nm in names - {TOC_ANCHOR}:
        new = (f"_M{n}" + nm)[:40]
        body = body.replace(f'w:anchor="{nm}"', f'w:anchor="{new}"')
        body = re.sub(rf"(\b(?:PAGEREF|REF)\s+){re.escape(nm)}\b", rf"\g<1>{new}", body)
    # ссылки на шапку исходного файла с несколькими брифами (его заголовок, «_top») — к содержанию сводного
    for nm in (b.get("toc_anchors") or set()) - names:
        body = body.replace(f'w:anchor="{nm}"', f'w:anchor="{TOC_ANCHOR}"')
    if f'w:anchor="{TOC_ANCHOR}"' not in body:
        body += TOC_LINK_P                       # у вопроса нет «К оглавлению» — в сводном оно есть у каждого
    # связи: картинки копируются к себе, внешние ссылки заводятся заново, другого не бывает в брифе
    rid_map = {}

    def rel(m):
        rid = m.group(2)
        if rid not in rid_map:
            kind, target, mode = b["rels"].get(rid, ("", "", ""))
            new = f"rIdM{n}x{len(rid_map) + 1}"
            if kind.endswith(REL_LINK):
                if not safe_link(target):
                    rid_map[rid] = "DROPLINK"          # ссылка снимается, её текст остаётся
                    return f'r:{m.group(1)}="DROPLINK"'
                new_rels.append((new, kind, target, mode or "External"))
            elif kind.endswith(REL_IMAGE) and mode != "External":
                src = "word/" + target.lstrip("/") if not target.startswith("/") else target.lstrip("/")
                src = re.sub(r"[^/]+/\.\./", "", src)
                data = _read(b["zip"], src, b["name"], MAX_MEDIA)
                if data is None:
                    raise MergeError(f"«{b['name']}» повреждён: нет картинки {target}.")
                fname = f"m{n}_{Path(target).name}"
                media[f"word/media/{fname}"] = data
                new_rels.append((new, kind, f"media/{fname}", ""))
            else:
                what = kind.rsplit("/", 1)[-1] or "неизвестный"
                raise MergeError(f"«{b['name']}»: в брифе есть встроенный объект ({what}) — "
                                 "его объединение не поддерживается. Уберите объект или вставьте его картинкой.")
            rid_map[rid] = new
        return f'r:{m.group(1)}="{rid_map[rid]}"'

    body = R_ATTR.sub(rel, body)
    body = re.sub(r'<w:hyperlink\b[^>]*r:id="DROPLINK"[^>]*/>', "", body)
    body = re.sub(r'<w:hyperlink\b[^>]*r:id="DROPLINK"[^>]*>(.*?)</w:hyperlink>', r"\1", body, flags=re.S)
    body = re.sub(r'\s+r:\w+="DROPLINK"', "", body)
    body = re.sub(r'(<w:numId w:val=")(\d+)(")',
                  lambda m: m.group(1) + num_map.get(m.group(2), m.group(2)) + m.group(3), body)
    # переименованные стили — в самом конце: выше позиции title_p считались по исходному тексту
    if style_map:
        body = re.sub(r'(<w:(?:pStyle|rStyle|tblStyle) w:val=")([^"]*)(")',
                      lambda m: m.group(1) + style_map.get(m.group(2), m.group(2)) + m.group(3), body)
    return body


class _Ids:
    """Номера закладок по всему документу подряд — повторов Word не любит."""
    def __init__(self, start):
        self.last = start - 1

    def __next__(self):
        self.last += 1
        return self.last


def _import_numbering(numbering, b, n):
    """Списки брифа — в сводный под новыми номерами (у каждого файла они свои)."""
    src = _read(b["zip"], "word/numbering.xml", b["name"])
    if src is None:
        return numbering, {}
    src = src.decode("utf8", "replace")
    abstract_max = max([int(x) for x in re.findall(r'<w:abstractNum\b[^>]*w:abstractNumId="(\d+)"', numbering)] or [0])
    num_max = max([int(x) for x in re.findall(r'<w:num\b[^>]*w:numId="(\d+)"', numbering)] or [0])
    used = set(re.findall(r'<w:numId w:val="(\d+)"', b.get("file_body") or b["body"])) - {"0"}   # на весь файл
    nums = {m.group(1): m.group(0) for m in re.finditer(r'<w:num\b[^>]*w:numId="(\d+)"[^>]*>.*?</w:num>', src, re.S)}
    abstracts = {m.group(1): m.group(0) for m in
                 re.finditer(r'<w:abstractNum\b[^>]*w:abstractNumId="(\d+)"[^>]*>.*?</w:abstractNum>', src, re.S)}
    add_abs, add_num, amap, nmap = [], [], {}, {}
    for old in sorted(used, key=int):
        num = nums.get(old)
        if not num:
            continue
        aid = re.search(r'<w:abstractNumId w:val="(\d+)"', num)
        if aid and aid.group(1) in abstracts and aid.group(1) not in amap:
            abstract_max += 1
            amap[aid.group(1)] = str(abstract_max)
            xml = re.sub(r'w:abstractNumId="\d+"', f'w:abstractNumId="{abstract_max}"', abstracts[aid.group(1)], count=1)
            xml = re.sub(r'<w:nsid w:val="[0-9A-Fa-f]+"/>', f'<w:nsid w:val="{random.getrandbits(32):08X}"/>', xml)
            add_abs.append(xml)
        num_max += 1
        nmap[old] = str(num_max)
        num = re.sub(r'w:numId="\d+"', f'w:numId="{num_max}"', num, count=1)
        num = re.sub(r'\s*w16cid:durableId="\d+"', "", num)
        if aid and aid.group(1) in amap:
            num = num.replace(aid.group(0), f'<w:abstractNumId w:val="{amap[aid.group(1)]}"')
        add_num.append(num)
    if add_abs:
        first_num = re.search(r"<w:num\b", numbering)
        at = first_num.start() if first_num else numbering.rindex("</w:numbering>")
        numbering = numbering[:at] + "".join(add_abs) + numbering[at:]
    if add_num:
        tail = re.search(r"<w:numIdMacAtCleanup\b|</w:numbering>", numbering)
        numbering = numbering[:tail.start()] + "".join(add_num) + numbering[tail.start():]
    return numbering, nmap


def _same_style(a, b):
    """Одно и то же определение стиля (без служебных rsid и пробелов между тегами)."""
    norm = lambda s: re.sub(r">\s+<", "><", re.sub(r'\s+w:rsid\w*="[^"]*"', "", s)).strip()
    return norm(a) == norm(b)


def _import_styles(styles, b, n):
    """Стили брифа — в сводный. Стиль, которого нет в шаблоне, берётся как есть; стиль с тем же
    номером и тем же определением — от шаблона (брифы сервиса собраны из этого же шаблона).

    Стиль с тем же номером, но другим определением — это чужой документ, собранный в другом Word:
    номера стилей («1», «a7», «31») там раздаются заново и совпадают случайно. Раньше побеждал
    шаблон, и бриф пользователя 13.10 получил шапку таблицы заглавными от нашего «Plain Table 3»
    вместо своей «Сетки таблицы». Теперь такой стиль переносится под своим именем с приставкой
    M<n>, а ссылки на него в теле брифа переписываются (style_map) — бриф выглядит как в файле.
    Возвращает (styles, style_map)."""
    src = _read(b["zip"], "word/styles.xml", b["name"])
    if src is None:
        return styles, {}
    src = src.decode("utf8", "replace")
    style_re = r'<w:style\b[^>]*w:styleId="([^"]*)"[^>]*>.*?</w:style>'
    have = {m.group(1): m.group(0) for m in re.finditer(style_re, styles, re.S)}
    defs = {m.group(1): m.group(0) for m in re.finditer(style_re, src, re.S)}
    # стили — на весь файл сразу (он может нести несколько вопросов), а не на каждый вопрос
    want = list(set(re.findall(r'<w:(?:pStyle|rStyle|tblStyle) w:val="([^"]*)"', b.get("file_body") or b["body"])))
    seen, add, style_map = set(), [], {}
    while want:
        sid = want.pop()
        if sid in seen or sid not in defs:
            continue
        seen.add(sid)
        xml = defs[sid]
        if sid in have:
            if _same_style(xml, have[sid]) or _is_default_style(xml):
                # тот же стиль — от шаблона; стиль по умолчанию («Обычный», «Обычная таблица») — только
                # шаблонный: второй «по умолчанию» перекрасил бы весь сводный (ревью RC-2026-10-13-4, К-2)
                continue
            style_map[sid] = f"M{n}_{sid}"[:64]      # с разделителем: M1+«31» и M13+«1» не совпадают (Н-10)
        add.append((sid, xml))
        want += re.findall(r'<w:(?:basedOn|link|next) w:val="([^"]*)"', xml)
    out = []
    for sid, xml in add:
        new = style_map.get(sid, sid)
        xml = re.sub(r'w:styleId="[^"]*"', f'w:styleId="{new}"', xml, count=1)
        xml = re.sub(r'\s+w:default="(?:1|true|on)"', "", xml, count=1)     # «по умолчанию» остаётся у шаблона
        xml = re.sub(r'(<w:(?:basedOn|link|next) w:val=")([^"]*)(")',
                     lambda m: m.group(1) + style_map.get(m.group(2), m.group(2)) + m.group(3), xml)
        if new != sid:                                # имя стиля тоже должно быть своим
            xml = re.sub(r'(<w:name w:val=")([^"]*)(")', lambda m: m.group(1) + m.group(2) + f" (бриф {n})" + m.group(3), xml, count=1)
        out.append(xml)
    if out:
        at = styles.rindex("</w:styles>")
        styles = styles[:at] + "".join(out) + styles[at:]
        if len(styles) > MAX_STYLES:
            raise MergeError(f"«{b['name']}»: слишком много стилей оформления для сводного. Откройте его в Word "
                             "и сохраните как обычный .docx.")
    return styles, style_map


def _is_default_style(xml):
    return bool(re.match(r'<w:style\b[^>]*\sw:default="(?:1|true|on)"', xml))


MAX_STYLES = 4 * 1024 * 1024          # styles.xml сводного после всех вложений (ревью RC-2026-10-13-4, К-3)


def _merge_root(root, parts):
    """Пространства имён из брифов, которых нет в шаблоне: без них Word не прочтёт тело."""
    have = dict(re.findall(r'xmlns:(\w+)="([^"]*)"', root))
    add = {}
    for b in parts:
        for pfx, uri in re.findall(r'xmlns:(\w+)="([^"]*)"', b["root"]):
            if pfx not in have and pfx not in add:
                add[pfx] = uri
    if not add:
        return root
    return root[:-1] + "".join(f' xmlns:{p}="{u}"' for p, u in add.items()) + ">"


def merge_files(files, out_docx, title="Бриф"):
    """Сводный из прикреплённых файлов брифов, в заданном порядке.

    files: [(имя файла, байты)]. Каждый бриф — с новой страницы, содержимое как в файле;
    в первой строке второго столбца номер вопроса «1. », «2. » по порядку. Файл с несколькими
    брифами (прежний сводный) даёт несколько вопросов подряд — номера сквозные.
    """
    parts = [p for name, data in files for p in read_briefs(name, data)]
    shell = bb.shell_parts()
    shell = dict(shell, files=dict(shell["files"]), infos=dict(shell["infos"]))
    numbering = shell["files"]["word/numbering.xml"].decode("utf8")
    styles = shell["files"]["word/styles.xml"].decode("utf8")
    ids = _Ids(1000)
    media, new_rels, items, chunks = {}, [], [], []
    per_file = {}        # списки и стили файла переносятся один раз, сколько бы вопросов он ни нёс (К-3)
    for n, b in enumerate(parts, 1):
        key = b.get("file_key") or n
        if key not in per_file:
            numbering, num_map = _import_numbering(numbering, b, n)
            styles, style_map = _import_styles(styles, b, n)
            if len(numbering) > MAX_STYLES:
                raise MergeError(f"«{b['name']}»: слишком много списков для сводного. Откройте его в Word "
                                 "и сохраните как обычный .docx.")
            per_file[key] = (num_map, style_map)
        num_map, style_map = per_file[key]
        chunk = page_break() + _body_of(b, n, ids, media, new_rels, num_map, style_map)
        _check_xml(_merge_root(shell["root"], [b]), chunk, b["name"])
        chunks.append(chunk)
        items.append({"anchor": f"_Q{n}", "title": b["title"]})
    files_ = shell["files"]
    files_["word/numbering.xml"] = numbering.encode("utf8")
    files_["word/styles.xml"] = styles.encode("utf8")
    if new_rels:
        rels = files_["word/_rels/document.xml.rels"].decode("utf8")
        add = "".join(f'<Relationship Id="{i}" Type="{k}" Target="{_attr(t)}"'
                      + (f' TargetMode="{m}"' if m else "") + "/>" for i, k, t, m in new_rels)
        files_["word/_rels/document.xml.rels"] = rels.replace("</Relationships>", add + "</Relationships>").encode("utf8")
    if media:
        ct_name = "[Content_Types].xml"
        ct = files_[ct_name].decode("utf8")
        for path, data in media.items():
            ext = Path(path).suffix.lstrip(".").lower()
            if f'Extension="{ext}"' not in ct.replace(f'Extension="{ext.upper()}"', f'Extension="{ext}"'):
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml", "emf": "image/x-emf",
                        "wmf": "image/x-wmf", "tif": "image/tiff"}.get(ext, f"image/{ext}")
                ct = ct.replace("<Default ", f'<Default Extension="{ext}" ContentType="{mime}"/><Default ', 1)
            files_[path] = data
            shell["infos"][path] = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        files_[ct_name] = ct.encode("utf8")
    shell["root"] = _merge_root(shell["root"], parts)
    page_of = pages.write_paged(shell, lambda pg: contents(items, title, None, pg),
                                chunks, [it["anchor"] for it in items], out_docx)
    return {"title": title, "docx": str(out_docx),
            "questions": [{"title": b["title"], "file": b["name"], "kind": "file"} for b in parts],
            "pages": [page_of[it["anchor"]] for it in items]}
