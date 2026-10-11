"""
Саммари докладов: несколько докладов (Word, PPTX, PDF) → один документ Word.

Вёрстка — как в «Повестке» и брифе, без вольностей:

- A4 книжная, поля: левое 2 см, правое 1,5 см, верхнее и нижнее 2 см;
- Times New Roman 14 пт, межстрочный 1,15, выравнивание по ширине,
  красная строка 1,25 см;
- номер страницы в верхнем колонтитуле по центру 12 пт, на первой странице нет;
- заголовки — жирным, по центру; подзаголовки внутри доклада — жирным
  с красной строки; таблицы без границ, строки отбиваются на 6 пт;
- длинное тире не используется (как и в брифе): «—» → «–».

Первая страница — оглавление. Со второй страницы: название документа,
затем «1. Саммари доклада <ФИО> по вопросу «<тема>»» и сам доклад; каждое
следующее саммари начинается с новой страницы.

Как и в брифе, нейросеть только достаёт факты с дословными цитатами: числа
сверяются с самим докладом, неподтверждённое подсвечивается жёлтым.
Только стандартная библиотека.
"""
import re
import zipfile
from pathlib import Path

import build_brief as bb
import build_topic as bt
import names_common
import pages

MIN_QUOTE = 15

CM = 566.93                      # твипы в сантиметре
FONT = bb.FONT                   # Times New Roman
SIZE = 28                        # 14 пт (в твипах половин пункта)
LINE = 276                       # межстрочный 1,15
FIRST_LINE = int(1.25 * CM)      # красная строка 1,25 см
ROW_SPACE = 120                  # отбивка строки таблицы, 6 пт
TEXT_WIDTH = int(17.5 * CM)      # ширина текста при полях 2 и 1,5 см
BAR_FILL = bb.HISTORY_COLOR      # полосы диаграммы — тем же цветом, что «История» брифа
NUM_COLS = (int(10.0 * CM), int(3.5 * CM), int(4.0 * CM))       # показатель | значение | период
BAR_COLS = (int(4.0 * CM), int(10.0 * CM), int(3.5 * CM))       # подпись | полоса | значение
PAGE = ('<w:pgSz w:w="11906" w:h="16838"/>'
        f'<w:pgMar w:top="{int(2 * CM)}" w:right="{int(1.5 * CM)}" w:bottom="{int(2 * CM)}" '
        f'w:left="{int(2 * CM)}" w:header="{int(1.25 * CM)}" w:footer="{int(1.25 * CM)}" w:gutter="0"/>')


class SummaryError(Exception):
    pass


# ───────────────────────── ответ нейросети ─────────────────────────

def summary_schema():
    """Строгая схема ответа: один элемент talks — один доклад."""
    S, N, NS = {"type": "string"}, {"type": ["number", "null"]}, {"type": ["string", "null"]}
    fact = {"type": "object", "properties": {"text": S, "q": S}}
    number = {"type": "object", "properties": {
        "name": S, "value": S, "period": NS, "q": S,
        "series": {"type": ["array", "null"], "items": {"type": "object", "properties": {"label": S, "v": N}}},
    }}
    talk = {"type": "object", "properties": {
        # speaker_gen — тот же докладчик в родительном падеже («доклада Иванова И.И.»):
        # склонять фамилии программой ненадёжно, нейросеть делает это сама, как с названием региона
        "speaker": S, "speaker_gen": NS, "role": NS, "org": NS, "topic": S, "src": NS,
        "key_points": {"type": "array", "items": fact},
        "numbers": {"type": "array", "items": number},
        "problems": {"type": "array", "items": fact},
        "proposals": {"type": "array", "items": fact},
    }}
    return bb.strict_schema({"type": "object", "properties": {
        "title": S, "event": NS, "date": NS, "place": NS,
        "talks": {"type": "array", "items": talk},
    }})


def prepare_summary(answer_text):
    """Ответ нейросети → (саммари, ошибки структуры)."""
    try:
        F = bb.parse_answer(answer_text)
    except bb.FactsError as e:
        raise SummaryError(str(e)) from e
    return F, validate_summary(F)


def validate_summary(F):
    e = []
    talks = F.get("talks")
    if not isinstance(talks, list) or not talks:
        return ["talks: нужен непустой список докладов"]
    for i, t in enumerate(talks):
        if not isinstance(t, dict):
            e.append(f"talks[{i}]: нужен объект")
            continue
        if not str(t.get("speaker") or "").strip():
            e.append(f"talks[{i}].speaker: нужно имя докладчика")
        if not str(t.get("topic") or "").strip():
            e.append(f"talks[{i}].topic: нужна тема доклада")
        for key in ("key_points", "numbers", "problems", "proposals"):
            v = t.get(key)
            if v is not None and not isinstance(v, list):
                e.append(f"talks[{i}].{key}: нужен список")
        for j, n in enumerate(t.get("numbers") or []):
            if isinstance(n, dict) and not str(n.get("value") or "").strip():
                e.append(f"talks[{i}].numbers[{j}].value: нужно значение")
    return e


# ───────────────────────── проверка по докладам ─────────────────────────

class SummaryChecker:
    """Каждый факт с числами должен иметь цитату, найденную в своём документе."""

    def __init__(self, sources):
        self.sources = [{"name": s["name"], "norm": bb.norm(s["text"])} for s in sources]
        self.status = {}
        self.log = []

    def find(self, q, src):
        qn = bb.norm(q)
        named = next((s for s in self.sources if bb.norm(s["name"]) == bb.norm(src or "")), None)
        if named and qn in named["norm"]:
            return named, []
        for s in self.sources:
            if qn in s["norm"]:
                return s, ([] if not src else [f"цитата взята не из «{src}», а из «{s['name']}»"])
        return None, ["цитата не найдена в докладе"]

    def check(self, fact, path, src, text_keys=("text", "value", "name")):
        text = " ".join(str(fact.get(k) or "") for k in text_keys)
        q = fact.get("q")
        if not re.search(r"\d", text):                       # без чисел проверять нечего
            return self.record(fact, path, [])
        if not isinstance(q, str) or len(bb.norm(q)) < MIN_QUOTE:
            return self.record(fact, path, ["нет цитаты под числами"])
        found, why = self.find(q, src)
        if found:
            why += bt.number_problems(text, bb.norm(q))    # та же проверка чисел, что в общем брифе
        self.record(fact, path, why)

    def record(self, fact, path, why):
        self.status[id(fact)] = not why
        self.log.append((path, not why, why, fact))

    def ok(self, *facts):
        return all(self.status.get(id(f), False) for f in facts if isinstance(f, dict) and f)

    def bad(self):
        return [(p, why, f) for p, ok, why, f in self.log if not ok]


def check_summary(sources, F):
    ck = SummaryChecker(sources)
    for i, t in enumerate(F.get("talks") or []):
        if not isinstance(t, dict):
            continue
        src = t.get("src")
        for key in ("key_points", "problems", "proposals"):
            for j, f in enumerate(t.get(key) or []):
                if isinstance(f, dict):
                    ck.check(f, f"talks[{i}].{key}[{j}]", src)
        for j, n in enumerate(t.get("numbers") or []):
            if isinstance(n, dict):
                ck.check(n, f"talks[{i}].numbers[{j}]", src)
    return ck


# ───────────────────────── вёрстка ─────────────────────────

def run(text, b=False, hl=False, size=SIZE):
    rpr = FONT + ("<w:b/><w:bCs/>" if b else "") + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    rpr += '<w:highlight w:val="yellow"/>' if hl else ""
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{bb.esc(text)}</w:t></w:r>'


def para(runs, first=FIRST_LINE, align="both", before=0, after=0, line=LINE, num=None, left=0):
    pr = f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>'
    pr += f'<w:ind w:left="{left}" w:firstLine="{first}"/>' if num is None else \
          f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr><w:ind w:left="{left}" w:firstLine="{first}"/>'
    pr += f'<w:jc w:val="{align}"/>'
    return f"<w:p><w:pPr>{pr}</w:pPr>{''.join(runs)}</w:p>"


def empty():
    return para([], first=0)


def title_p(text, before=0, after=120):
    """Заголовок документа и разделов: по центру, жирным."""
    return para([run(text, b=True)], first=0, align="center", before=before, after=after)


def sub_p(text):
    """Подзаголовок внутри доклада: жирным, с красной строки."""
    return para([run(text, b=True)], before=120, after=60)


def cell(paras, width, fill=None):
    shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
    return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}'
            '<w:vAlign w:val="center"/></w:tcPr>' + ("".join(paras) or empty()) + "</w:tc>")


def table(rows, widths):
    """Таблица без границ, как в «Повестке»: ширины колонок заданы жёстко."""
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return ('<w:tbl><w:tblPr>' + f'<w:tblW w:w="{sum(widths)}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/><w:tblBorders>' +
            "".join(f'<w:{s} w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
                    for s in ("top", "left", "bottom", "right", "insideH", "insideV")) +
            '</w:tblBorders><w:tblLook w:val="04A0"/></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>' + "".join(f"<w:tr>{r}</w:tr>" for r in rows) + "</w:tbl>")


def row_p(parts, align="both"):
    """Абзац внутри ячейки: без красной строки, с отбивкой 6 пт сверху и снизу."""
    return para(parts, first=0, align=align, before=ROW_SPACE, after=ROW_SPACE)


def page_break():
    return f'<w:p><w:pPr><w:spacing w:after="0" w:line="{LINE}" w:lineRule="auto"/></w:pPr>' \
           '<w:r><w:br w:type="page"/></w:r></w:p>'


def bookmark(name, num, inner):
    return f'<w:bookmarkStart w:id="{num}" w:name="{name}"/>{inner}<w:bookmarkEnd w:id="{num}"/>'


def bars(series):
    """Ряд значений — полосы: подпись, заливка по доле от наибольшего, значение."""
    values = [(str(x.get("label") or ""), x.get("v")) for x in series
              if isinstance(x, dict) and bb.is_num(x.get("v"))]
    if len(values) < 2:
        return []
    top = max(v for _, v in values) or 1
    rows = []
    for label, v in values:
        filled = max(int(0.2 * CM), int(BAR_COLS[1] * v / top))
        rest = max(1, BAR_COLS[1] - filled)
        line = table([cell([row_p([])], filled, fill=BAR_FILL) + cell([row_p([])], rest)], [filled, rest])
        rows.append(cell([row_p([run(label)])], BAR_COLS[0])
                    + cell([line], BAR_COLS[1])
                    + cell([row_p([run(bb.fmt(v))], align="right")], BAR_COLS[2]))
    return [table(rows, list(BAR_COLS))]


def numbers_block(ck, numbers):
    """«Ключевые цифры»: таблица показателей, под рядами значений — полосы."""
    head = (cell([row_p([run("Показатель", b=True)])], NUM_COLS[0])
            + cell([row_p([run("Значение", b=True)], align="right")], NUM_COLS[1])
            + cell([row_p([run("Период", b=True)])], NUM_COLS[2]))
    rows = [head]
    for n in numbers:
        hl = not ck.ok(n)
        rows.append(cell([row_p([run(str(n.get("name") or ""), hl=hl)])], NUM_COLS[0])
                    + cell([row_p([run(str(n.get("value") or ""), b=True, hl=hl)], align="right")], NUM_COLS[1])
                    + cell([row_p([run(str(n.get("period") or "–"))])], NUM_COLS[2]))
    out = [table(rows, list(NUM_COLS))]
    for n in numbers:
        series = n.get("series")
        if isinstance(series, list) and len(series) > 1:
            out.append(empty())
            out.append(sub_p(str(n.get("name") or "Динамика")))
            out += bars(series)
    return out


def listed(items, ck, bullet):
    out = []
    for x in items:
        if isinstance(x, dict) and str(x.get("text") or "").strip():
            out.append(para([run(str(x["text"]), hl=not ck.ok(x))], num=bullet, first=0, left=FIRST_LINE))
    return out


def genitive(t):
    """Докладчик в родительном падеже — общим правилом пространства (контракт п. 5а),
    своего склонения нет. Человек поправил падеж на экране — берём его правку."""
    manual = str(t.get("gen_manual") or "").strip()
    return manual or names_common.genitive(str(t.get("speaker") or "").strip())


def doubtful(t):
    """ФИО расходится с реестром людей — почти наверняка опечатка, в документе подсвечиваем."""
    return (t.get("check") or {}).get("status") == "close"


def talk_title(i, t):
    """Пункт документа: «1. Саммари доклада Иванова И.И. по вопросу «…»»."""
    topic = str(t.get("topic") or "").strip()
    who = genitive(t)
    return f"{i}. Саммари доклада {who} по вопросу «{topic}»" if topic else f"{i}. Саммари доклада {who}"


def title_runs(i, t, b=False):
    """Тот же пункт прогонами: сомнительное ФИО — жёлтым, как неподтверждённый факт."""
    topic = str(t.get("topic") or "").strip()
    tail = f" по вопросу «{topic}»" if topic else ""
    return (run(f"{i}. Саммари доклада ", b=b) + run(genitive(t), b=b, hl=doubtful(t))
            + (run(tail, b=b) if tail else ""))


def summary_body(sources, F, shell, anchor=None, anchor_id=0, title_text=None, with_contents=True):
    """Тело саммари: оглавление (если нужно) и разделы по докладам.

    Вынесено отдельно, чтобы саммари можно было вложить в сводный бриф
    по нескольким справкам.
    """
    ck = check_summary(sources, F)
    bullet = shell["bullet"]

    talks = [t for t in F.get("talks") or [] if isinstance(t, dict)]
    title = str(F.get("title") or "Саммари докладов")
    under = " · ".join(x for x in [str(F.get("event") or "").strip(), str(F.get("place") or "").strip(),
                                   str(F.get("date") or "").strip()] if x)

    body = []
    if title_text:                       # внутри сводного брифа у раздела свой заголовок
        head = para([run(title_text, b=True)], first=0, align="center", after=160)
        body.append(bookmark(anchor, anchor_id, head) if anchor else head)
    if not with_contents:
        body += talks_body(talks, ck, bullet)
        return body, ck, summary_result(F, sources, ck, talks, title_text or title)
    head, chunks, anchors = summary_pages(talks, ck, bullet, title, under)
    body += head(None) + chunks
    return body, ck, summary_result(F, sources, ck, talks, title)


def contents_line(i, t, page):
    """Строка оглавления: название доклада, отточие и номер страницы (поле PAGEREF с готовым числом)."""
    rpr = f'<w:rPr>{FONT}<w:sz w:val="{SIZE}"/><w:szCs w:val="{SIZE}"/></w:rPr>'
    num = (f'<w:r>{rpr}<w:tab/></w:r>'
           f'<w:r>{rpr}<w:fldChar w:fldCharType="begin"/></w:r>'
           f'<w:r>{rpr}<w:instrText xml:space="preserve"> PAGEREF _Talk{i} \\h </w:instrText></w:r>'
           f'<w:r>{rpr}<w:fldChar w:fldCharType="separate"/></w:r>'
           f'<w:r>{rpr}<w:t>{page}</w:t></w:r>'
           f'<w:r>{rpr}<w:fldChar w:fldCharType="end"/></w:r>')
    link = f'<w:hyperlink w:anchor="_Talk{i}" w:history="1">{title_runs(i, t)}{num}</w:hyperlink>'
    tabs = f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{TEXT_WIDTH}"/></w:tabs>'
    return para([link], first=0, before=60, after=60).replace("<w:pPr>", "<w:pPr>" + tabs, 1)


def summary_pages(talks, ck, bullet, title, under):
    """Оглавление и доклады кусками «с новой страницы» — для настоящих номеров страниц.

    Возвращает (head(pages) → абзацы оглавления, куски, закладки кусков)."""
    def head(pages_of):
        out = [title_p("Оглавление", after=240)]
        for i, t in enumerate(talks, 1):
            out.append(contents_line(i, t, (pages_of or {}).get(f"_Talk{i}", 1)))
            who = ", ".join(x for x in [str(t.get("role") or "").strip(), str(t.get("org") or "").strip()] if x)
            if who:
                out.append(para([run(who)], first=0, after=60))
        return out

    parts = talk_parts(talks, ck, bullet)
    lead = [page_break(), title_p(title)] + ([title_p(under, after=240)] if under else [])
    chunks = ["".join(lead + (parts[0] if parts else []))] + \
             ["".join([page_break()] + part) for part in parts[1:]]
    return head, chunks, [f"_Talk{i}" for i in range(1, len(parts) + 1)] if parts else []


def talks_body(talks, ck, bullet):
    """Разделы по докладам — общая часть отдельного саммари и сводного брифа."""
    body = []
    for i, part in enumerate(talk_parts(talks, ck, bullet), 1):
        if i > 1:
            body.append(page_break())
        body += part
    return body


def talk_parts(talks, ck, bullet):
    """Каждый доклад отдельным списком абзацев (без разрыва страницы перед ним)."""
    out = []
    for i, t in enumerate(talks, 1):
        body = []
        out.append(body)
        body.append(bookmark(f"_Talk{i}", 100 + i, para([title_runs(i, t, b=True)], before=120, after=120)))
        rest = ", ".join(x for x in [str(t.get("role") or "").strip(), str(t.get("org") or "").strip()] if x)
        body.append(para([run("Докладчик: ", b=True), run(str(t.get("speaker") or "").strip(), hl=doubtful(t))]
                         + ([run(", " + rest)] if rest else [])))
        if t.get("src"):
            body.append(para([run("Источник: ", b=True), run(str(t["src"]))]))
        points = listed(t.get("key_points") or [], ck, bullet)
        body.append(sub_p("Главное"))
        body += points or [para([run("В докладе не заявлено.")])]
        nums = [n for n in t.get("numbers") or [] if isinstance(n, dict)]
        if nums:
            body.append(sub_p("Ключевые цифры"))
            body += numbers_block(ck, nums)
        for name, key in (("Проблемы и риски", "problems"), ("Предложения докладчика", "proposals")):
            items = listed(t.get(key) or [], ck, bullet)
            if items:
                body.append(sub_p(name))
                body += items
    return out


def summary_result(F, sources, ck, talks, title):
    """Итоги проверки саммари — одинаковые для отдельного документа и сводного."""
    bad = [{"path": p, "why": why, "quote": f.get("q") or ""} for p, why, f in ck.bad()]
    return {"region": title, "facts_total": len(ck.log), "unverified": bad, "consistency": [],
            "signals": [], "manual": [], "problems": [], "solutions": [],
            "talks": [{**{k: t.get(k) for k in ("speaker", "role", "org", "topic", "src")},
                       "speaker_gen": genitive(t), "gen_manual": bool(str(t.get("gen_manual") or "").strip()),
                       "check": t.get("check"), "offer": t.get("offer")}
                      for t in talks]}


def build_summary(sources, F, out_docx):
    """Собирает документ саммари и отчёт проверки. sources: [{"name", "text"}]."""
    shell = bb.shell_parts()
    shell["sect"] = re.sub(r"<w:pgSz[^>]*/><w:pgMar[^>]*/>", PAGE, shell["sect"])   # поля свои
    ck = check_summary(sources, F)
    talks = [t for t in F.get("talks") or [] if isinstance(t, dict)]
    title = str(F.get("title") or "Саммари докладов")
    under = " · ".join(x for x in [str(F.get("event") or "").strip(), str(F.get("place") or "").strip(),
                                   str(F.get("date") or "").strip()] if x)
    head, chunks, anchors = summary_pages(talks, ck, shell["bullet"], title, under)
    if anchors:
        page_of = pages.write_paged(shell, head, chunks, anchors, out_docx, geometry=pages.SUMMARY)
    else:
        page_of = {}
        out_docx = bb.write_docx(shell, head(None) + chunks, out_docx)
    result = summary_result(F, sources, ck, talks, title)
    result["docx"] = str(out_docx)
    result["pages"] = [page_of.get(a) for a in anchors]
    bad = result["unverified"]
    rep = [f"# Проверка саммари: {title}", "",
           f"Докладов: {len(talks)}. Фактов проверено: {len(ck.log)}, не подтверждено: {len(bad)}.", "",
           "## Сверьте вручную (в DOCX подсвечено жёлтым: точной цитаты в докладе не нашлось)", ""]
    rep += [f"- `{b['path']}` — {'; '.join(b['why'])}. Цитата: «{b['quote']}»" for b in bad] or ["- нет"]
    rep += ["", "## Документы", ""] + [f"- {s['name']} — {len(s['text'])} знаков" for s in sources]
    rep_path = Path(out_docx).with_suffix(".проверка.md")
    rep_path.write_text("\n".join(rep) + "\n", encoding="utf8")
    result["report"] = str(rep_path)
    return result
