"""
Тематический бриф: собирается из справки и приложенных к ней документов
(статья, обращение, выписка из СОЗД, письма ведомств).

Отличие от брифа для региона: там анкета с заранее известными числами, здесь —
свободный текст. Поэтому нейросеть возвращает не поля, а блоки разделов «История»
и «Текущий статус», и к каждому утверждению с числом, датой, номером документа или
фамилией — дословную цитату и имя файла, откуда она взята. Таблица брифа та же
(Вопрос · История · Текущий статус · Проблематика · Варианты решения), свободна
только начинка ячеек.

Только стандартная библиотека.
"""
import json, re, zipfile
from pathlib import Path

import build_brief as bb

KINDS = ("head", "para", "bullet")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")
_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
_INITIALS = re.compile(r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+|\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")
_NUM = re.compile(r"\d+(?:,\d+)?")                    # 18, 284,1
_COMPOUND = re.compile(r"\d+(?:[.\-/]\d+)+")            # 1.1, 44.00.00, 12-10-15/12341, 2017-2018
MIN_QUOTE = 15          # цитата короче не доказывает ничего


class TopicError(Exception):
    pass


# ───────────────────────── ответ нейросети ─────────────────────────

def topic_schema():
    """Строгая JSON-схема ответа: те же правила, что и у брифа для региона."""
    S, NS = {"type": "string"}, {"type": ["string", "null"]}
    block = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "text": S, "q": NS, "src": NS}}
    pick = {"type": "object", "properties": {"text": S, "q": NS, "src": NS}}
    return bb.strict_schema({
        "type": "object",
        "properties": {
            "title": S,
            "history": {"type": "array", "items": block},
            "status": {"type": "array", "items": block},
            "problems": {"type": "array", "items": pick},
            "solutions": {"type": "array", "items": pick},
        },
    })


def prepare_topic(answer_text):
    """Ответ нейросети → (бриф, ошибки структуры). Бросает TopicError, если JSON не разобрать.

    Проблематику и варианты решения нейросеть присылает кандидатами: они переезжают
    в candidates_*, а problems и solutions остаются пустыми, пока человек не отметит нужное —
    так же, как «сигналы» в брифе для региона."""
    try:
        F = bb.parse_answer(answer_text)
    except bb.FactsError as e:
        raise TopicError(str(e)) from e
    errors = validate_topic(F)
    if not errors:
        for src, dst in (("problems", "candidates_problems"), ("solutions", "candidates_solutions")):
            items = F.get(src)
            if isinstance(items, list) and any(isinstance(x, dict) for x in items):
                F[dst] = [x for x in items if isinstance(x, dict)]
                F[src] = []
    return F, errors


def validate_topic(F):
    e = []
    if not isinstance(F, dict):
        return ["ответ должен быть JSON-объектом"]
    if not isinstance(F.get("title"), str) or not F["title"].strip():
        e.append("title: нужна непустая строка — наименование вопроса")
    for name in ("history", "status"):
        blocks = F.get(name)
        if not isinstance(blocks, list) or not blocks:
            e.append(f"{name}: нужен непустой список блоков")
            continue
        for i, b in enumerate(blocks):
            p = f"{name}[{i}]"
            if not isinstance(b, dict):
                e.append(f"{p}: должен быть объект")
                continue
            if b.get("kind") not in KINDS:
                e.append(f"{p}.kind: одно из {', '.join(KINDS)}")
            if not isinstance(b.get("text"), str) or not b["text"].strip():
                e.append(f"{p}.text: нужна непустая строка")
            for k in ("q", "src"):
                if b.get(k) is not None and not isinstance(b.get(k), str):
                    e.append(f"{p}.{k}: строка или null")
    for name in ("problems", "solutions"):
        items = F.get(name)
        if items is None:
            continue
        if not isinstance(items, list):
            e.append(f"{name}: должен быть список")
            continue
        for i, x in enumerate(items):
            if not isinstance(x, dict) or not isinstance(x.get("text"), str) or not x["text"].strip():
                e.append(f"{name}[{i}]: нужен объект с непустым text")
    return e


# ───────────────────────── проверка по документам ─────────────────────────

def date_forms(day, month, year):
    """06.10.2025 → как эту дату могли написать в документе."""
    year = int(year)
    if year < 100:
        year += 2000
    day, month = int(day), int(month)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return []
    return [bb.norm(f"{day:02d}.{month:02d}.{year}"), bb.norm(f"{day}.{month}.{year}"),
            bb.norm(f"{day} {MONTHS[month - 1]} {year}"), bb.norm(f"{day:02d} {MONTHS[month - 1]} {year}")]


def checkable(text):
    """Проверяем то, что можно перепутать: числа, даты, номера документов, фамилии.
    Связки и выводы («вопрос требует решения») цитатой не подкрепляются."""
    return bool(_NUM.search(text) or "№" in text or _INITIALS.search(text))


def number_problems(text, qn):
    """Все числа, даты и номера пунктов из текста должны стоять в его цитате (qn — цитата после norm).

    Общая проверка для общего брифа и саммари докладов: «1.1» и «№ 243-ФЗ» сверяются
    как строки, даты — с учётом словесной записи, остальное — как числа.
    """
    why, rest = [], text
    for m in _DATE.finditer(text):                      # даты: 06.10.2025 против «6 октября 2025 г.»
        rest = rest.replace(m.group(0), " ")
        forms = date_forms(*m.groups())
        if forms and not any(f in qn for f in forms):
            why.append(f"дата {m.group(0)} не подтверждена цитатой")
    rest = bb._base_norm(rest)
    for tok in _COMPOUND.findall(rest):                 # номера пунктов и документов сверяем как строку
        rest = rest.replace(tok, " ")
        if bb.norm(tok) not in qn:
            why.append(f"{tok} не найдено в цитате")
    for tok in _NUM.findall(rest):
        val = float(tok.replace(",", ".")) if "," in tok else int(tok)
        if not bb.has_number(qn, val):
            why.append(f"число {tok} не найдено в цитате")
    return why


class TopicChecker:
    """Каждый проверяемый блок должен иметь цитату, найденную в приложенных документах,
    и все его числа и даты должны стоять в этой цитате."""

    def __init__(self, sources):
        # sources: [{"name": имя файла, "text": текст}]
        self.sources = [{"name": s["name"], "norm": bb.norm(s["text"])} for s in sources]
        self.status = {}
        self.log = []

    def source(self, name):
        if not name:
            return None
        n = bb.norm(name)
        for s in self.sources:
            if bb.norm(s["name"]) == n or n in bb.norm(s["name"]) or bb.norm(s["name"]) in n:
                return s
        return None

    def find_quote(self, q, src):
        """Возвращает (источник, почему не нашли). Ищем сперва в названном файле, потом во всех."""
        named = self.source(src)
        qn = bb.norm(q)
        if named and qn in named["norm"]:
            return named, []
        for s in self.sources:
            if qn in s["norm"]:
                why = [] if not src else [f"цитата взята не из «{src}», а из «{s['name']}»"]
                return s, why
        return None, ["цитата не найдена в приложенных документах"]

    def check(self, block, path):
        text = str(block.get("text") or "")
        why = []
        q = block.get("q")
        if not checkable(text):
            self.record(block, path, [], None)          # связка, проверять нечего
            return
        if not isinstance(q, str) or len(bb.norm(q)) < MIN_QUOTE:
            self.record(block, path, ["нет цитаты под числами или фамилиями"], None)
            return
        found, why = self.find_quote(q, block.get("src"))
        if found:
            why += number_problems(text, bb.norm(q))
        self.record(block, path, why, found)

    def record(self, block, path, why, found):
        self.status[id(block)] = (not why, why)
        self.log.append((path, not why, why, block, found["name"] if found else None))

    def ok(self, *facts):
        return all(self.status.get(id(f), (False, []))[0] for f in facts if isinstance(f, dict) and f)

    def bad(self):
        return [(p, why, b) for p, ok, why, b, _ in self.log if not ok]


def check_topic(sources, F):
    ck = TopicChecker(sources)
    for name in ("history", "status"):
        for i, b in enumerate(F.get(name) or []):
            if isinstance(b, dict):
                ck.check(b, f"{name}[{i}]")
    return ck


# ───────────────────────── сборка документа ─────────────────────────

def blocks_cell(d, blocks, ck):
    out = []
    for b in blocks:
        kind, text = b.get("kind"), str(b.get("text") or "").strip()
        if not text:
            continue
        manual = bool(b.get("manual"))
        if kind == "head":
            # заголовок и так весь жирный (шаблон) — разметка человека тут не нужна, только текст
            out.append(d.p([bb.B(bb.human_plain(text) if manual else text)]))
        elif manual:
            # абзац, написанный человеком: с документами не сверяется и жёлтым не подсвечивается;
            # <strong>…</strong> с экрана (Quill) — единственная разметка, которую он мог выбрать
            parts = bb.human_parts(text)
            out.append(d.bullet_p(parts) if kind == "bullet" else d.p(parts))
        else:
            fact = [(text, False, [b])]
            out.append(d.bullet_p(fact) if kind == "bullet" else d.p(fact))
    return out


def sections_of(F):
    """Абзацы «Истории» и «Текущего статуса» для правки на экране: здесь блоки и есть данные."""
    out = {}
    for name in ("history", "status"):
        out[name] = [{"text": str(b.get("text") or "").strip(), "kind": b.get("kind") if b.get("kind") in bb.SECTION_KINDS else "p",
                      "i": i, "manual": bool(b.get("manual"))}
                     for i, b in enumerate(F.get(name) or []) if isinstance(b, dict) and str(b.get("text") or "").strip()]
    return out


def apply_sections(F, sections):
    """Правки человека с экрана → блоки: тот же текст — блок как был (с цитатой), изменённый —
    без цитаты и с пометкой manual, новый — тоже; убранные с экрана пропадают."""
    for name in ("history", "status"):
        items = (sections or {}).get(name)
        if not isinstance(items, list):
            continue
        old = [b for b in F.get(name) or [] if isinstance(b, dict)]
        new = []
        for it in items:
            if not isinstance(it, dict):
                continue
            text = " ".join(str(it.get("text") or "").split())
            if not text:
                continue
            kind = it.get("kind") if it.get("kind") in bb.SECTION_KINDS else "p"
            i = it.get("i")
            src = old[i] if isinstance(i, int) and 0 <= i < len(old) else None
            if src is not None and " ".join(str(src.get("text") or "").split()) == text:
                new.append(dict(src, kind=kind))
            else:
                new.append({"kind": kind, "text": text, "q": None, "src": (src or {}).get("src"), "manual": True})
        F[name] = new
    return F


def topic_body(sources, F, shell, title_text=None, anchor=None, anchor_id=0):
    """Тело общего брифа — та же таблица «Вопрос / История / …».

    Вынесено отдельно, чтобы такой бриф можно было вложить в сводный документ
    по нескольким справкам."""
    ck = check_topic(sources, F)
    d = bb.Doc(ck, shell["bullet"])
    dh = d.styled(italic=True, color=bb.HISTORY_COLOR)     # «История» — как в брифе для региона

    problems = [x.strip() for x in F.get("problems") or [] if isinstance(x, str) and x.strip()]
    solutions = [x.strip() for x in F.get("solutions") or [] if isinstance(x, str) and x.strip()]
    title = str(title_text or F.get("title") or "").strip()
    head = d.p([bb.B(title)])
    if anchor:                                   # закладка для ссылки из оглавления сводного документа
        head = (f'<w:bookmarkStart w:id="{anchor_id}" w:name="{anchor}"/>' + head +
                f'<w:bookmarkEnd w:id="{anchor_id}"/>')
    rows = [
        (bb.label_cell("Вопрос", bold=True), bb.content_cell([head])),
        (bb.label_cell("История"), bb.content_cell(blocks_cell(dh, F.get("history") or [], ck)
                                                   or [dh.p(["Нет данных в приложенных документах."])])),
        # «Текущий статус»: всё в скобках — синим курсивом (решение пользователя 21.09)
        (bb.label_cell("Текущий статус"), bb.content_cell(blocks_cell(d.with_parens(), F.get("status") or [], ck)
                                                          or [d.p(["Нет данных в приложенных документах."])])),
        (bb.label_cell("Проблематика"), bb.content_cell(bb.listed(d, problems) or [d.p(["Отсутствует."])])),
        (bb.label_cell("Варианты решения"), bb.content_cell(bb.listed(d, solutions) or [d.p(["Не требуются."])])),
    ]
    tbl = ('<w:tbl><w:tblPr><w:tblStyle w:val="af"/><w:tblW w:w="11165" w:type="dxa"/>'
           '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
           '</w:tblPr><w:tblGrid><w:gridCol w:w="2376"/><w:gridCol w:w="8789"/></w:tblGrid>'
           + "".join(f"<w:tr>{a}{b}</w:tr>" for a, b in rows) + "</w:tbl>")
    return [tbl], ck, title, problems, solutions


def build_topic(sources, F, out_docx):
    """Собирает DOCX тематического брифа и отчёт проверки."""
    shell = bb.shell_parts()
    body, ck, title, problems, solutions = topic_body(sources, F, shell)
    out_docx = bb.write_docx(shell, body, out_docx)

    bad = [{"path": p, "why": why, "quote": (b.get("q") or b.get("text") or "")[:300]} for p, why, b in ck.bad()]
    checked = [x for x in ck.log if x[4] or not x[1]]
    cand = [str(x.get("text") or "").strip() for x in F.get("candidates_problems") or [] if isinstance(x, dict)]
    cand_sol = [str(x.get("text") or "").strip() for x in F.get("candidates_solutions") or [] if isinstance(x, dict)]
    result = {
        "title": title,
        "region": title,            # заголовок результата на экране общий для обоих видов брифа
        "docx": str(out_docx),
        "facts_total": len(checked),
        "unverified": bad,
        "consistency": [],
        "manual": [],
        "signals": cand,                    # кандидаты в «Проблематику» — галочки на экране
        "solution_signals": cand_sol,       # кандидаты в «Варианты решения»
        "problems": problems,
        "solutions": solutions,
        "sources": [s["name"] for s in sources],
    }
    rep = [f"# Проверка брифа: {title}", "",
           f"Документов приложено: {len(sources)} ({', '.join(result['sources'])}).",
           f"Утверждений с числами и фамилиями: {len(checked)}, не подтверждено: {len(bad)}.", "",
           "## Сверьте вручную (в DOCX подсвечено жёлтым: точной цитаты в документах не нашлось)", ""]
    rep += [f"- `{b['path']}` — {'; '.join(b['why'])}. Цитата: «{b['quote']}»" for b in bad] or ["- нет"]
    rep += ["", "## Откуда взяты подтверждённые факты", ""]
    rep += [f"- `{p}` — {src}" for p, ok, why, b, src in ck.log if ok and src] or ["- нет"]
    rep += ["", "## Кандидаты в «Проблематику» (в бриф вносит куратор)", ""]
    rep += [f"- {x}" for x in cand] or ["- нет"]
    rep += ["", "## Кандидаты в «Варианты решения»", ""]
    rep += [f"- {x}" for x in cand_sol] or ["- нет"]
    result["sections"] = sections_of(F)
    result["manual"] = [f"{blk}: «{bb.human_plain(s['text'])}»" for blk, key in (("История", "history"), ("Текущий статус", "status"))
                        for s in result["sections"][key] if s["manual"]]
    rep += ["", "## Внесено в бриф куратором", ""]
    rep += [f"- проблематика: {x}" for x in problems] + [f"- решение: {x}" for x in solutions]
    rep += [f"- {x}" for x in result["manual"]] or ["- ничего"]
    rep_path = out_docx.with_suffix(".проверка.md")
    rep_path.write_text("\n".join(rep) + "\n", encoding="utf8")
    result["report"] = str(rep_path)
    return result
