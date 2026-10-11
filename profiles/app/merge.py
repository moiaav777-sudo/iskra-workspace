"""Объединение объективок: несколько файлов — один документ Word с содержанием.

    страница 1 — «Объективки», «Содержание»: «1. Кравцов Сергей Сергеевич …… 2» — каждая
                 строка — внутренняя гиперссылка Word (w:hyperlink w:anchor) на закладку
                 в начале объективки, номер страницы — поле PAGEREF с готовым числом;
    дальше     — каждая объективка с новой страницы, содержимое как в файле.

Объективки из реестра собираются заново (`docx_build`) и идут тем же путём, что прикреплённые
файлы: тело каждого переносится как есть. Код переноса взят из «Брифа»
(`brief/app/build_joint.py::merge_files` и помощники; копия, а не импорт — у сервисов свои
папки): поля Word, кроме внутренних, снимаются (INCLUDEPICTURE «\\\\сервер\\…», DDE и прочее
наружу не тянут), ссылки — только веб и почта, картинки и списки переименовываются, чтобы
файлы не перепутали друг другу связи. Отличия от «Брифа»: объективка узнаётся по ФИО
в первой строке или первой ячейке (а не по таблице «Вопрос»), номер вопроса не ставится,
номера рисунков (wp:docPr id) делаются сквозными — у каждой объективки своё фото.

Номер страницы — прикидка при сборке (`pages_of`): объективка из реестра знает свою длину
(`docx_build.fit`), у прикреплённого файла она оценивается по объёму текста, как в
`brief/app/pages.py`. Word уточняет поля сам при обновлении (F9); updateFields не ставим.

Новая страница — крошечный абзац с pageBreakBefore (1 пт): абзац с одним разрывом страницы
оставил бы пустую строку наверху следующей страницы и мог бы вытолкнуть объективку на вторую.
Закладка объективки стоит в этом же абзаце — гиперссылка ведёт ровно на начало её страницы.
"""
from __future__ import annotations

import bisect
import html
import io
import math
import random
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

from . import docs_common
from . import docx_build as dx
from . import model

MIN_ITEMS, MAX_ITEMS = 2, 10
MAX_XML = 20 * 1024 * 1024
MAX_PART = 10 * 1024 * 1024
MAX_MEDIA = 15 * 1024 * 1024
MAX_TOTAL = 60 * 1024 * 1024
MAX_ENTRIES = 2000
W_MAIN = dx.NS["w"]
R_MAIN = dx.NS["r"]
STRICT = ("http://purl.oclc.org/ooxml/wordprocessingml/main", "http://purl.oclc.org/ooxml/officeDocument/relationships")
BROKEN = (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError, OSError, ValueError)
R_ATTR = re.compile(r'\br:(id|embed|link|pict)="([^"]*)"')
REL_IMAGE = "/image"
REL_LINK = "/hyperlink"
SAFE_LINK = re.compile(r"^(?:https?://|mailto:)[^\\\\]*$", re.I)
HEAD_SIZE, LINE_SIZE = 16, 14
TOC_TAB = dx.TEXT_W                                  # правый край текста — номер страницы


class MergeError(ValueError):
    """Файл нельзя вложить в объединённый: человеку показывается как есть."""


def _unescape(t: str) -> str:
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def safe_link(target) -> bool:
    return bool(SAFE_LINK.match(_unescape(str(target or "")).strip()))


def _spans(xml: str, tag: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
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
            if xml[close - 1] == "/":
                if depth == 0:
                    out.append((m.start(), close + 1))
                continue
            if depth == 0:
                at = m.start()
            depth += 1
    return out


def _cut(xml: str, tag: str) -> str:
    """Убрать все элементы <w:tag …>…</w:tag> и <w:tag …/> — линейно, через _spans."""
    out, pos = [], 0
    for a, e in _spans(xml, tag):
        out.append(xml[pos:a])
        pos = e
    out.append(xml[pos:])
    return "".join(out)


def _elements(xml: str, tag: str, key: str) -> dict:
    """{значение атрибута key: текст элемента} для верхнеуровневых <w:tag> — линейно, без `.*?`
    (на самозакрывающихся `<w:style/>` регулярка просматривала файл до конца; ревью RC-2026-10-05-8, Н-2)."""
    out = {}
    for a, e in _spans(xml, tag):
        head_end = xml.find(">", a)
        val = docs_common._attrs(xml[a + len(tag) + 3:head_end]).get(key)
        if val is not None and val not in out:
            out[val] = xml[a:e]
    return out


def _text(xml: str) -> str:
    return _unescape("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml)))


def _read(z, part, name, limit=MAX_PART):
    try:
        info = z.getinfo(part)
    except KeyError:
        return None
    if info.file_size > limit:
        raise MergeError(f"«{name}» слишком большой для объективки.")
    try:
        return z.read(info)
    except BROKEN:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть. Сохраните его в Word заново.")


def _check_namespaces(raw: bytes, name: str) -> None:
    """Разметка Word — только под привычными приставками «w:» и «r:» (ревью «Брифа» RC-2026-09-22-1, К1)."""
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


def _rels(z, path, name) -> dict:
    data = _read(z, path, name)
    if data is None:
        return {}
    try:
        xml = data.decode("utf8")
    except UnicodeDecodeError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть.")
    out = {}
    try:
        for kind, tag, val in docs_common._tags(xml, docs_common._Clock(name)):
            if kind in ("open", "empty") and tag == "Relationship":
                a = docs_common._attrs(val)        # линейно (ревью RC-2026-10-05-8, Н-1)
                out[a.get("Id")] = (a.get("Type", ""), a.get("Target", ""), a.get("TargetMode", ""))
    except docs_common.DocsError as error:
        raise MergeError(str(error)) from error
    return out


def _attr(value) -> str:
    return html.escape(str(value), quote=True)


def read_profile(name: str, data: bytes, title: str | None = None) -> dict:
    """Прикреплённый файл → то, что нужно для вложения. Не объективка — MergeError с причиной.

    title — заголовок содержания, известный заранее (карточка из реестра): тогда ФИО по виду
    не проверяется — оно такое, каким его записал человек (ревью RC-2026-10-05-2, Н-4)."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise MergeError(f"«{name}» — старый формат Word (.doc). Откройте его в Word и сохраните как .docx.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except BROKEN:
        raise MergeError(f"«{name}» — не документ Word (.docx). Объединяются только объективки в Word.")
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_TOTAL:
        raise MergeError(f"«{name}» слишком большой для объективки.")
    raw = _read(z, "word/document.xml", name, MAX_XML)
    if raw is None:
        raise MergeError(f"«{name}» — не документ Word (.docx). Объединяются только объективки в Word.")
    try:
        xml = raw.decode("utf8")
    except UnicodeDecodeError:
        raise MergeError(f"«{name}» повреждён: Word не сможет его открыть.")
    _check_namespaces(raw, name)
    m = re.search(r"<w:body>(.*)</w:body>", xml, re.S)
    if not m or "<w:document" not in xml:
        raise MergeError(f"«{name}» повреждён: в нём нет текста документа.")
    body = m.group(1)
    # объективка — ФИО в первой строке (или в первой ячейке шапки с фото)
    first = next((s for s in _spans(body, "p") if _text(body[slice(*s)]).strip()), None)
    given = " ".join((title or "").split())
    title = given or (" ".join(_text(body[slice(*first)]).split()) if first else "")
    if not given and (not first or not model.looks_like_fio(title)):
        shown = f" (там «{title[:60]}»)" if title else ""
        raise MergeError(f"«{name}» не похож на объективку: в первой строке нет ФИО — «Фамилия Имя Отчество»{shown}. "
                         "Объединяются только объективки.")
    if re.search(r"<w:(footnote|endnote)Reference\b", body):
        raise MergeError(f"«{name}»: в объективке есть сноски — их объединение не поддерживается. "
                         "Уберите сноски или перенесите их в текст.")
    return {"name": name, "zip": z, "root": xml[:xml.index(">", xml.index("<w:document")) + 1],
            "body": body, "title": title, "rels": _rels(z, "word/_rels/document.xml.rels", name)}


# Поля, которые остаются в перенесённой объективке: только внутренние (ревью «Брифа» RC-2026-09-21-18, К1).
SAFE_FIELDS = {"PAGEREF", "REF", "PAGE", "NUMPAGES", "SECTIONPAGES", "TOC", "SEQ", "NOTEREF"}
_LOCAL_LINK = re.compile(r'HYPERLINK\s+\\l\s+"?[\w.-]+"?(?:\s+\\[a-zA-Z](?:\s+"[^"\\]*")?)*', re.I)
# Самозакрывающиеся варианты — первыми, у открытых `.*?` не перескакивает через такой же открывающий тег:
# иначе каждый `<w:instrText/>` просматривал бы файл до конца (квадратично; ревью RC-2026-10-05-8, Н-2).
_FIELD_TOKEN = re.compile(
    r'<w:fldChar\b[^>]*/>|<w:fldChar\b[^>]*(?<!/)>(?:(?!<w:fldChar\b).)*?</w:fldChar>'
    r'|<w:(?:instrText|delInstrText)\b[^>]*/>'
    r'|<w:(?:instrText|delInstrText)\b[^>]*(?<!/)>(?:(?!<w:(?:instrText|delInstrText)\b).)*?</w:(?:instrText|delInstrText)>'
    r'|<w:(?:t|delText)\b[^>]*>[^<]*</w:(?:t|delText)>', re.S)
MAX_FIELD_DEPTH = 50                     # вложенность полей Word; больше — файл не объединяем
MAX_FLDSIMPLE_PASSES = 50


def field_ok(instr: str) -> bool:
    instr = " ".join(_unescape(instr).split())
    if not instr:
        return False
    head = instr.split()[0].upper()
    if head == "HYPERLINK":
        return bool(_LOCAL_LINK.fullmatch(instr))
    return head in SAFE_FIELDS and not re.search(r"\\\\|//|:\\|\bfile:", instr, re.I)


def strip_fields(body: str) -> str:
    """Снять из тела все поля, кроме внутренних (field_ok); результат поля остаётся текстом."""
    fields, stack, toks = [], [], []
    for m in _FIELD_TOKEN.finditer(body):
        if len(stack) > MAX_FIELD_DEPTH:
            # контекст каждого токена — копия стека: при тысячах незакрытых полей это квадратично
            raise MergeError("Поля Word вложены слишком глубоко — откройте файл в Word и сохраните заново.")
        tok, ctx, own = m.group(0), [(f, fields[f]["phase"]) for f in stack], None
        if tok.startswith("<w:fldChar"):
            kind = re.search(r'w:fldCharType="(\w+)"', tok)
            kind = kind.group(1) if kind else ""
            if kind == "begin":
                if stack and fields[stack[-1]]["phase"] == "instr":
                    fields[stack[-1]]["nested"] = True
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
            drop = True
        if drop:
            out.append(body[pos:a])
            pos = e
    out.append(body[pos:])
    body = "".join(out)
    keep = "w:fldSimpleKEEP"
    inner = re.compile(r'<w:fldSimple\b([^>]*?)(/>|>((?:(?!<w:fldSimple\b).)*?)</w:fldSimple>)', re.S)

    def one(m):
        instr = docs_common._attrs(m.group(1)).get("w:instr", "")
        if instr and field_ok(instr):
            return m.group(0).replace("<w:fldSimple", "<" + keep, 1).replace("</w:fldSimple>", "</" + keep + ">")
        return m.group(3) or ""

    # проход — по всему телу сразу (а не поиск с начала после каждой замены — это было бы квадратично);
    # вложенные поля снимаются изнутри наружу, по проходу на уровень
    for _ in range(MAX_FLDSIMPLE_PASSES):
        body, count = inner.subn(one, body)
        if not count:
            break
    else:
        raise MergeError("Поля Word вложены слишком глубоко — откройте файл в Word и сохраните заново.")
    return body.replace(keep, "w:fldSimple")


DANGER = re.compile(r"\b(?:INCLUDE\w*|DDE\w*|LINK|IMPORT|DATABASE|AUTOTEXT\w*|MACROBUTTON|GOTOBUTTON)\b|\\\\|//", re.I)


def _check_xml(root: str, chunk: str, name: str) -> None:
    """Объективка после переноса должна разбираться как XML, и в ней не должно остаться опасных полей."""
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
        raise MergeError(f"«{name}» содержит поля Word, которые нельзя переносить в объединённый файл. "
                         "Откройте его в Word и сохраните как обычный .docx.")


class _Ids:
    """Сквозные номера (закладки, рисунки) по всему документу — повторов Word не любит."""

    def __init__(self, start: int):
        self.last = start - 1

    def __next__(self) -> int:
        self.last += 1
        return self.last


def anchor_of(n: int) -> str:
    return f"_P{n}"


def start_par(anchor: str, bid: int) -> str:
    """Начало объективки: новая страница и закладка, на которую ведёт строка содержания."""
    return ('<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
            '<w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr>'
            f'<w:bookmarkStart w:id="{bid}" w:name="{anchor}"/><w:bookmarkEnd w:id="{bid}"/></w:p>')


def _body_of(b: dict, n: int, ids: _Ids, pics: _Ids, media: dict, new_rels: list, num_map: dict) -> str:
    """Тело n-й объективки для объединённого файла: свои закладки, связи, рисунки и списки."""
    body = b["body"]
    body = _cut(body, "sectPr")                      # линейно, вместе с самозакрывающимися (Н-2)
    body = strip_fields(body)
    body = re.sub(r"<w:comment(?:RangeStart|RangeEnd|Reference)\b[^>]*/>", "", body)
    names = set(re.findall(r'<w:bookmarkStart\b[^>]*w:name="([^"]*)"', body))
    old_ids: dict[str, str] = {}

    def start(m):
        i = re.search(r'w:id="([^"]*)"', m.group(0)).group(1)
        nm = re.search(r'w:name="([^"]*)"', m.group(0)).group(1)
        old_ids[i] = str(next(ids))
        return re.sub(r'w:name="[^"]*"', f'w:name="{(f"_M{n}" + nm)[:40]}"',
                      re.sub(r'w:id="[^"]*"', f'w:id="{old_ids[i]}"', m.group(0)))

    def end(m):
        i = re.search(r'w:id="([^"]*)"', m.group(0)).group(1)
        return re.sub(r'w:id="[^"]*"', f'w:id="{old_ids[i]}"', m.group(0)) if i in old_ids else ""

    body = re.sub(r"<w:bookmarkStart\b[^>]*/>", start, body)
    body = re.sub(r"<w:bookmarkEnd\b[^>]*/>", end, body)
    renamed = {nm: (f"_M{n}" + nm)[:40] for nm in names}
    if renamed:
        body = re.sub(r'w:anchor="([^"]*)"', lambda m: f'w:anchor="{renamed.get(m.group(1), m.group(1))}"', body)
        body = re.sub(r"\b(PAGEREF|REF)([ \t]+)([^\s\\<\"]+)",
                      lambda m: m.group(1) + m.group(2) + renamed.get(m.group(3), m.group(3)), body)
    # номера рисунков — сквозные: у каждой объективки фото с id="1"
    body = re.sub(r'(<wp:docPr\b[^>]*?\bid=")\d+(")', lambda m: f"{m.group(1)}{next(pics)}{m.group(2)}", body)
    rid_map: dict[str, str] = {}

    def rel(m):
        rid = m.group(2)
        if rid not in rid_map:
            kind, target, mode = b["rels"].get(rid, ("", "", ""))
            new = f"rIdM{n}x{len(rid_map) + 1}"
            if kind.endswith(REL_LINK):
                if not safe_link(target):
                    rid_map[rid] = "DROPLINK"
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
            elif kind.endswith(REL_IMAGE):
                rid_map[rid] = "DROPLINK"                   # внешняя картинка (ссылка наружу) не переносится
                return f'r:{m.group(1)}="DROPLINK"'
            else:
                what = kind.rsplit("/", 1)[-1] or "неизвестный"
                raise MergeError(f"«{b['name']}»: в объективке есть встроенный объект ({what}) — "
                                 "его объединение не поддерживается. Уберите объект или вставьте его картинкой.")
            rid_map[rid] = new
        return f'r:{m.group(1)}="{rid_map[rid]}"'

    body = R_ATTR.sub(rel, body)
    body = re.sub(r'<w:hyperlink\b[^>]*r:id="DROPLINK"[^>]*/>', "", body)
    body = re.sub(r'<w:hyperlink\b[^>]*r:id="DROPLINK"[^>]*>(.*?)</w:hyperlink>', r"\1", body, flags=re.S)
    body = re.sub(r'[ \t\r\n]r:\w+="DROPLINK"', "", body)
    body = re.sub(r'(<w:numId w:val=")(\d+)(")',
                  lambda m: m.group(1) + num_map.get(m.group(2), m.group(2)) + m.group(3), body)
    return body


def _import_numbering(numbering: str, b: dict) -> tuple[str, dict]:
    src = _read(b["zip"], "word/numbering.xml", b["name"])
    if src is None:
        return numbering, {}
    src = src.decode("utf8", "replace")
    abstract_max = max([int(x) for x in re.findall(r'<w:abstractNum\b[^>]*w:abstractNumId="(\d+)"', numbering)] or [0])
    num_max = max([int(x) for x in re.findall(r'<w:num\b[^>]*w:numId="(\d+)"', numbering)] or [0])
    used = set(re.findall(r'<w:numId w:val="(\d+)"', b["body"])) - {"0"}
    nums = _elements(src, "num", "w:numId")
    abstracts = _elements(src, "abstractNum", "w:abstractNumId")
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
    if add_abs or add_num:
        numbering = _with_ns(numbering, src, "numbering")
    return numbering, nmap


def _import_styles(styles: str, b: dict) -> str:
    src = _read(b["zip"], "word/styles.xml", b["name"])
    if src is None:
        return styles
    src = src.decode("utf8", "replace")
    have = set(re.findall(r'<w:style\b[^>]*w:styleId="([^"]*)"', styles))
    defs = _elements(src, "style", "w:styleId")
    want = set(re.findall(r'<w:(?:pStyle|rStyle|tblStyle) w:val="([^"]*)"', b["body"]))
    add = []
    while want:
        sid = want.pop()
        if sid in have or sid not in defs:
            continue
        have.add(sid)
        add.append(defs[sid])
        want |= set(re.findall(r'<w:(?:basedOn|link|next) w:val="([^"]*)"', defs[sid]))
    if add:
        at = styles.rindex("</w:styles>")
        styles = _with_ns(styles[:at] + "".join(add) + styles[at:], src, "styles")
    return styles


def _with_ns(xml: str, src: str, tag: str) -> str:
    """Пространства имён из корня чужой части, которых нет в нашей: перенесённые стили и списки
    бывают с приставками w14/w15 — без объявления файл не откроется."""
    m_own = re.search(rf"<w:{tag}\b[^>]*>", xml)
    m_src = re.search(rf"<w:{tag}\b[^>]*>", src)
    if not m_own or not m_src:
        return xml
    have = dict(re.findall(r'xmlns:(\w+)="([^"]*)"', m_own.group(0)))
    add = {p: u for p, u in re.findall(r'xmlns:(\w+)="([^"]*)"', m_src.group(0)) if p not in have}
    if not add:
        return xml
    head = m_own.group(0)[:-1] + "".join(f' xmlns:{p}="{u}"' for p, u in add.items()) + ">"
    return xml[:m_own.start()] + head + xml[m_own.end():]


def _merge_root(root: str, parts: list[dict]) -> str:
    have = dict(re.findall(r'xmlns:(\w+)="([^"]*)"', root))
    add = {}
    for b in parts:
        for pfx, uri in re.findall(r'xmlns:(\w+)="([^"]*)"', b["root"]):
            if pfx not in have and pfx not in add:
                add[pfx] = uri
    if not add:
        return root
    return root[:-1] + "".join(f' xmlns:{p}="{u}"' for p, u in add.items()) + ">"


# ── содержание ──────────────────────────────────────────────────────────

def _run(text: str, b: bool = False, size: int = LINE_SIZE) -> str:
    return dx.run(text, size, bold=b)


def _field(instr: str, value: str) -> str:
    rpr = f'<w:rPr>{dx.FONT}<w:sz w:val="{LINE_SIZE * 2}"/><w:szCs w:val="{LINE_SIZE * 2}"/></w:rPr>'
    return (f'<w:r>{rpr}<w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r>{rpr}<w:instrText xml:space="preserve"> {instr} </w:instrText></w:r>'
            f'<w:r>{rpr}<w:fldChar w:fldCharType="separate"/></w:r>'
            f'<w:r>{rpr}<w:t>{value}</w:t></w:r>'
            f'<w:r>{rpr}<w:fldChar w:fldCharType="end"/></w:r>')


def _p(inner: str, after: int = 120, align: str = "left", tabs: bool = False) -> str:
    tab = f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{TOC_TAB}"/></w:tabs>' if tabs else ""
    return (f'<w:p><w:pPr>{tab}<w:spacing w:before="0" w:after="{after}" w:line="276" w:lineRule="auto"/>'
            f'<w:jc w:val="{align}"/></w:pPr>{inner}</w:p>')


def contents(items: list[dict], title: str, pages_of: dict | None = None) -> list[str]:
    """Первая страница: заголовок и содержание. Каждая строка — гиперссылка на закладку объективки."""
    out = [_p(_run(title, b=True, size=HEAD_SIZE), after=240, align="center"),
           _p(_run("Содержание", b=True), after=200, align="center")]
    for i, it in enumerate(items, 1):
        tab = f'<w:r><w:rPr>{dx.FONT}<w:sz w:val="{LINE_SIZE * 2}"/></w:rPr><w:tab/></w:r>'
        page = _field(f'PAGEREF {it["anchor"]} \\h', str((pages_of or {}).get(it["anchor"], 1)))
        name = _run(f"{i}. " + it["title"])
        link = f'<w:hyperlink w:anchor="{it["anchor"]}" w:history="1">{name}{tab}{page}</w:hyperlink>'
        out.append(_p(link, after=80, tabs=True))
    return out


def _para_height(p: str, width_tw: float) -> float:
    """Высота абзаца чужого файла в пунктах: кегль, интервал, отступы и картинки — из его разметки."""
    sz = re.search(r'<w:sz w:val="(\d+)"', p)
    size = int(sz.group(1)) / 2 if sz else 14.0
    sp = re.search(r"<w:spacing\b[^>]*/>", p)
    sp = sp.group(0) if sp else ""

    def num(attr, default=0):
        m = re.search(rf'w:{attr}="(\d+)"', sp)
        return int(m.group(1)) if m else default
    before = 14.0 if 'w:beforeAutospacing="1"' in sp else num("before") / 20
    after = 14.0 if 'w:afterAutospacing="1"' in sp else num("after") / 20
    rule = re.search(r'w:lineRule="(\w+)"', sp)
    rule = rule.group(1) if rule else "auto"
    line = num("line", 240)
    natural = dx.line_height(size, 1.0)
    lh = natural * line / 240 if rule == "auto" else (line / 20 if rule == "exact" else max(natural, line / 20))
    text = _text(p)
    pics = [int(cy) / dx.EMU_PT for cy in re.findall(r'<wp:extent cx="\d+" cy="(\d+)"', p)]
    breaks = len(re.findall(r'<w:br/>|<w:br w:type="textWrapping"[^>]*/>', p))
    body = (dx.count_lines(text, max(36.0, width_tw / dx.TW_PT), size, "<w:b/>" in p) + breaks) * lh if text else lh
    return max(body, max(pics, default=0)) + before + after


def _table_height(tbl: str) -> float:
    grid = [int(g) for g in re.findall(r'<w:gridCol w:w="(\d+)"', tbl)]
    total = 0.0
    for a, e in _spans(tbl, "tr"):
        row = tbl[a:e]
        best = 0.0
        for i, (ca, ce) in enumerate(_spans(row, "tc")):
            cell = row[ca:ce]
            w = re.search(r'<w:tcW w:w="(\d+)" w:type="dxa"', cell)
            width = int(w.group(1)) if w else (grid[i] if i < len(grid) else dx.TEXT_W / 2)
            best = max(best, sum(_para_height(cell[pa:pe], width) for pa, pe in _spans(cell, "p")))
        total += best
    return total


def estimate_pages(xml: str) -> int:
    """Сколько страниц займёт тело чужого файла: абзацы и таблицы верхнего уровня по их разметке,
    ширина — поле нашей страницы (разметку страницы файла объединение снимает); явный разрыв
    страницы начинает новую. Та же оценка, что подбирает кегль одиночной объективки
    (`docx_build.count_lines`); сверено с раскладкой на образцах."""
    avail = dx.TEXT_H / dx.TW_PT * dx.FIT_SAFETY
    tables = _spans(xml, "tbl")
    starts = [a for a, _ in tables]

    def in_table(pos: int) -> bool:
        k = bisect.bisect_right(starts, pos) - 1
        return k >= 0 and pos < tables[k][1]
    items = [(a, e, True) for a, e in tables]
    items += [(a, e, False) for a, e in _spans(xml, "p") if not in_table(a)]
    pages, height = 0, 0.0
    for a, e, is_table in sorted(items):
        el = xml[a:e]
        h = _table_height(el) if is_table else _para_height(el, dx.TEXT_W)
        if not is_table and ('w:type="page"' in el or "<w:pageBreakBefore/>" in el):
            pages += max(1, math.ceil(height / avail))
            height = 0.0
        height += h
    pages += max(1, math.ceil(height / avail)) if height or not pages else 0
    return max(1, pages)


def merge_files(files: list[tuple], title: str = "Объективки") -> tuple[bytes, dict]:
    """Объединённый файл из объективок в заданном порядке.

    files: [(имя файла, байты, страниц или None[, заголовок])]; страниц — длина объективки, если
    известна (из реестра), иначе прикидка по объёму; заголовок — ФИО карточки для содержания. Возвращает (байты .docx, {titles, pages, files}).
    """
    if not MIN_ITEMS <= len(files) <= MAX_ITEMS:
        raise MergeError(f"Объединяются от {MIN_ITEMS} до {MAX_ITEMS} объективок, а выбрано {len(files)}.")
    parts = []
    for item in files:
        name, data = item[0], item[1]
        b = read_profile(name, data, item[3] if len(item) > 3 else None)
        b["pages_hint"] = item[2] if len(item) > 2 else None
        parts.append(b)
    sh = dx.shell(title=title)
    sh = dict(sh, files=dict(sh["files"]), infos=dict(sh["infos"]))
    numbering = sh["files"]["word/numbering.xml"].decode("utf8")
    styles = sh["files"]["word/styles.xml"].decode("utf8")
    ids, pics = _Ids(1000), _Ids(1)
    media, new_rels, items, chunks, lengths = {}, [], [], [], []
    for n, b in enumerate(parts, 1):
        numbering, num_map = _import_numbering(numbering, b)
        styles = _import_styles(styles, b)
        inner = _body_of(b, n, ids, pics, media, new_rels, num_map)
        _check_xml(_merge_root(sh["root"], [b]), inner, b["name"])
        chunks.append(start_par(anchor_of(n), next(ids)) + inner)
        items.append({"anchor": anchor_of(n), "title": b["title"]})
        lengths.append(b["pages_hint"] or estimate_pages(inner))
    files_ = sh["files"]
    files_["word/numbering.xml"] = numbering.encode("utf8")
    files_["word/styles.xml"] = styles.encode("utf8")
    if new_rels:
        rels = files_["word/_rels/document.xml.rels"].decode("utf8")
        add = "".join(f'<Relationship Id="{i}" Type="{k}" Target="{_attr(t)}"'
                      + (f' TargetMode="{m}"' if m else "") + "/>" for i, k, t, m in new_rels)
        files_["word/_rels/document.xml.rels"] = rels.replace("</Relationships>", add + "</Relationships>").encode("utf8")
    if media:
        ct = files_["[Content_Types].xml"].decode("utf8")
        for path, data in media.items():
            ext = Path(path).suffix.lstrip(".").lower()
            if f'Extension="{ext}"' not in ct.replace(f'Extension="{ext.upper()}"', f'Extension="{ext}"'):
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml", "emf": "image/x-emf",
                        "wmf": "image/x-wmf", "tif": "image/tiff", "gif": "image/gif", "bmp": "image/bmp"}.get(ext, f"image/{ext}")
                ct = ct.replace("<Default ", f'<Default Extension="{ext}" ContentType="{mime}"/><Default ', 1)
            files_[path] = data
            sh["infos"][path] = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        files_["[Content_Types].xml"] = ct.encode("utf8")
    sh["root"] = _merge_root(sh["root"], parts)
    # содержание — на одной странице (до 10 строк), объективки с новой страницы каждая
    first = 1 + estimate_pages("".join(contents(items, title)))
    pages_of, page = {}, first
    for it, length in zip(items, lengths):
        pages_of[it["anchor"]] = page
        page += length
    data = dx.write(sh, contents(items, title, pages_of) + chunks)
    return data, {"titles": [it["title"] for it in items], "pages": [pages_of[it["anchor"]] for it in items],
                  "files": [b["name"] for b in parts]}
