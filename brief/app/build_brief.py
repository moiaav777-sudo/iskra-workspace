"""
Сборка брифа «О системе общего образования <региона>» из большой справки.

Нейросеть выдаёт ФАКТЫ в JSON (каждое число с цитатой). Этот модуль:
  • разбирает и нормализует ответ модели, проверяет структуру;
  • сверяет каждую цитату и каждое число со справкой;
  • сам считает суммы и склоняет слова;
  • собирает DOCX в оформлении брифа и отчёт проверки.

Только стандартная библиотека Python 3.8+.
"""
import html, json, math, re, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHELL = HERE / "shell.docx"

# ───────────────────────── чтение справки ─────────────────────────

class SourceError(Exception):
    pass


MAX_UNZIPPED = 80 * 1024 * 1024      # разумный предел для справки: защита от «zip-бомбы»


def docx_text(path):
    """Текст docx: абзацы (в т.ч. ячейки таблиц) по порядку, по одному на строку."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_UNZIPPED:
                raise SourceError("Документ слишком большой: внутри файла больше 80 МБ текста. "
                                  "Пришлите справку без вложенных картинок и объектов.")
            xml = z.read("word/document.xml").decode("utf8")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise SourceError("Файл не похож на документ Word (.docx). Если справка в формате .doc, "
                          "откройте её в Word и сохраните как .docx.") from e
    lines = []
    for p in re.finditer(r"<w:p[ >].*?</w:p>", xml, re.S):
        s = re.sub(r"<w:tab/>|<w:br[^>]*/>", " ", p.group(0))
        t = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", s, re.S))
        lines.append(html.unescape(t))
    return "\n".join(lines)


def _base_norm(s):
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[   \t]", " ", s)
    s = re.sub(r"[«»„“”\"']", '"', s)
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    # запятая-разделитель (не десятичная) → «;», чтобы «2024, 2025» не слипалось в «2024,2025»,
    # а «году),что» и «году), что» сравнивались одинаково
    s = re.sub(r";\s*", ";", s)
    s = re.sub(r"(?<!\d),\s*|,\s+", ";", s)
    s = re.sub(r"(?<!\d)(\d{1,3})((?: \d{3})+)(?!\d)", lambda m: m.group(1) + m.group(2).replace(" ", ""), s)
    return s


def norm(s):
    """Для сравнения цитат: границы абзацев → ¦, пробелы убраны."""
    s = _base_norm(s).replace("\n", "¦")
    return re.sub(r"\s+", "", s)


def num_token(v):
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return ("%s" % round(v, 2)).replace(".", ",")
    return str(v)


def has_number(hay, v):
    tok = re.escape(num_token(v))
    return re.search(r"(?<![\d,])" + tok + r"(?:,0+)?(?!\d|,\d)", hay) is not None


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

# ───────────────────────── ответ модели ─────────────────────────

class FactsError(Exception):
    pass


def parse_answer(text):
    """JSON из ответа модели: убирает размышления, ```-обёртки и текст вокруг."""
    if not isinstance(text, str) or not text.strip():
        raise FactsError("Нейросеть вернула пустой ответ.")
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise FactsError("В ответе нейросети нет JSON-объекта.")
    def no_const(c):
        raise ValueError(f"недопустимое значение {c}")
    try:
        data = json.loads(t[a:b + 1], parse_constant=no_const)
    except json.JSONDecodeError as e:
        raise FactsError(f"JSON в ответе нейросети повреждён: {e.msg} (строка {e.lineno}, позиция {e.colno}).")
    except (ValueError, RecursionError) as e:
        raise FactsError(f"JSON в ответе нейросети некорректен: {e}")
    if not isinstance(data, dict):
        raise FactsError("Ответ нейросети — не JSON-объект.")
    return data


TEXT_KEYS = {"q", "text", "name", "note", "period", "program", "kind", "type", "nom", "gen", "prep"}
_NUM_RE = re.compile(r"^\s*-?\d[\d   ]*(?:[.,]\d+)?\s*%?\s*$")


def coerce(node, key=None):
    """«12 137» → 12137, «14,19» → 14.19 везде, кроме текстовых полей."""
    if isinstance(node, dict):
        return {k: coerce(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [coerce(x, key) for x in node]
    if isinstance(node, str) and key not in TEXT_KEYS and _NUM_RE.match(node):
        s = re.sub(r"[   %]", "", node).replace(",", ".")
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return node
    if isinstance(node, float) and node.is_integer() and key not in ("share", "rf_share", "v"):
        return int(node)
    return node


def _get(F, path):
    cur = F
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


REQUIRED_STR = ["region.nom", "region.gen", "region.prep"]
REQUIRED_NUM = ["year", "status.dou_groups.v", "status.dou_pupils.v", "status.dou_access.v",
                "status.schools.total", "status.schools.day", "status.schools.students",
                "status.urban.schools", "status.urban.students", "status.rural.schools", "status.rural.students",
                "status.two_shift.v", "status.gpd.groups", "status.gpd.students", "status.gpd.share",
                "status.camp.orgs.v", "status.camp.children.v", "status.camp.tjs.v", "status.camp.svo.v"]
OPTIONAL_V = ["history.tochka_rosta", "history.aop_mtb", "history.computers", "history.sport_halls",
              "history.it_cube", "history.kvant_school", "history.kvant_children", "history.kvant_mobile",
              "history.advisors_schools", "status.three_shift", "status.camp.inclusive_shifts",
              "status.camp.inclusive_children", "status.camp.large_families"]


def validate(F):
    """Список понятных ошибок структуры (пустой — можно собирать)."""
    e = []
    if not isinstance(F, dict):
        return ["ответ должен быть JSON-объектом"]
    for p in ("region", "status", "status.camp"):
        if not isinstance(_get(F, p), dict):
            e.append(f"{p}: нужен объект")
    for p in ("history",):
        if F.get(p) is not None and not isinstance(F[p], dict):
            e.append(f"{p}: нужен объект или null")
    if e:
        return e
    for p in REQUIRED_STR:
        if not isinstance(_get(F, p), str) or not _get(F, p).strip():
            e.append(f"{p}: нужна непустая строка")
    for p in REQUIRED_NUM:
        if not is_num(_get(F, p)):
            e.append(f"{p}: нужно число, сейчас {json.dumps(_get(F, p), ensure_ascii=False)}")
    for p in OPTIONAL_V:
        x = _get(F, p)
        if x is not None and (not isinstance(x, dict) or (x.get("v") is not None and not is_num(x.get("v")))):
            e.append(f"{p}: должно быть null или объект с числом v")

    def items(path, need_num=(), extra=None):
        lst = _get(F, path)
        if lst is None:
            return
        if not isinstance(lst, list):
            e.append(f"{path}: должен быть список")
            return
        for i, it in enumerate(lst):
            if not isinstance(it, dict):
                e.append(f"{path}[{i}]: должен быть объект")
                continue
            for k in need_num:
                if not is_num(it.get(k)):
                    e.append(f"{path}[{i}].{k}: нужно число, сейчас {json.dumps(it.get(k), ensure_ascii=False)}")
            if extra:
                extra(f"{path}[{i}]", it)

    def one(path, need_num):
        x = _get(F, path)
        if x is None:
            return
        if not isinstance(x, dict):
            e.append(f"{path}: должен быть объект или null")
            return
        for k in need_num:
            if not is_num(x.get(k)):
                e.append(f"{path}.{k}: нужно число, сейчас {json.dumps(x.get(k), ensure_ascii=False)}")

    items("history.kindergartens_built", ("objects", "places"))
    items("history.schools_built", ("objects", "places"))
    one("history.schools_repaired", ("buildings", "places", "since"))
    one("history.kindergartens_repaired", ("buildings", "places", "since"))

    def zem(p, it):
        if not isinstance(it.get("period"), (str, int)):
            e.append(f"{p}.period: нужен период, например \"2020-2024\"")
    items("history.zemsky", ("teachers", "young"), zem)
    items("status.school_repairs_current", ("places",))
    items("status.kg_repairs_current", ("places",))
    one("status.school_repairs_next", ("year", "buildings", "places"))
    one("status.kg_repairs_next", ("year", "buildings", "places"))

    def cons(p, it):
        if not isinstance(it.get("type"), str) or it["type"] not in ("school", "kg"):
            e.append(f"{p}.type: должно быть \"school\" или \"kg\"")
        if not isinstance(it.get("program"), str) or it["program"] not in PROGRAMS:
            e.append(f"{p}.program: одно из {', '.join(PROGRAMS)}")
        if not isinstance(it.get("name"), str):
            e.append(f"{p}.name: нужна строка")
    items("status.construction", ("places", "year"), cons)

    def txt(p, it):
        if not isinstance(it.get("text"), str) or not it["text"].strip():
            e.append(f"{p}.text: нужна строка")
    items("status.risks", (), txt)
    items("signals", (), txt)

    def camp(p, it):
        if not isinstance(it.get("kind"), str) or it["kind"] not in CAMP_KINDS:
            e.append(f"{p}.kind: одно из {', '.join(CAMP_KINDS)}")
        if it.get("v") is not None and not is_num(it.get("v")):
            e.append(f"{p}.v: нужно число")
    items("status.camp.types", (), camp)
    for p in ("problems", "solutions", "manual_additions"):
        x = F.get(p)
        if x is not None and not (isinstance(x, list) and all(isinstance(s, str) for s in x)):
            e.append(f"{p}: должен быть список строк")
    return e

# ───────────────────────── проверка по справке ─────────────────────────

SKIP_NUM = {"part_of_previous"}
IMPLIED_ONE = {"objects", "buildings"}        # «школы на 160 мест» — единица подразумевается
KEEP_PREV_YEAR = {"rf_share"}                  # значение по РФ стоит в одной скобке с прошлым годом
STR_CHECK = {"dou_date", "school_year"}
CONTAINERS = {"history", "status", "status.camp"}
_PREV_YEAR = re.compile(r"\([^()]*?(?:за|в)20\d\d[^()]*\)")   # «(за 2025 год – 86)», «(154 чел. в 2024/25 уч. году)»


class Checker:
    """Каждое число факта должно стоять в его цитате (для строк таблиц — в ячейках сразу после названия)."""

    def __init__(self, source_text):
        self.src = norm(source_text)
        self.status = {}      # id(fact) -> (ok, [причины])
        self.log = []         # (путь, ok, причины, факт)

    def run(self, node, path):
        if isinstance(node, list):
            for i, x in enumerate(node):
                self.run(x, f"{path}[{i}]")
        elif isinstance(node, dict):
            if path not in CONTAINERS:
                self.check_fact(node, path)
            for k, x in node.items():
                if isinstance(x, (dict, list)):
                    self.run(x, f"{path}.{k}")

    def record(self, f, path, why):
        ok = not why
        self.status[id(f)] = (ok, why)
        self.log.append((path, ok, why, f))

    def check_fact(self, f, path):
        why = []
        q = f.get("q")
        is_row = "name" in f
        occurrences = []
        qn = ""
        if isinstance(q, str) and q.strip():
            qn = norm(q)
            if len(qn) < 8:
                why.append("цитата слишком короткая, чтобы её проверить")
            else:
                start = 0
                while True:
                    pos = self.src.find(qn, start)
                    if pos < 0:
                        break
                    occurrences.append(pos)
                    start = pos + 1
                if not occurrences:
                    why.append("цитата не найдена в справке")
        elif not isinstance(f.get("text"), str):
            why.append("нет цитаты")

        nums = [(k, v) for k, v in f.items()
                if k not in SKIP_NUM and is_num(v) and v != 0 and not (v == 1 and k in IMPLIED_ONE)]
        if occurrences and nums:
            best_missing = None
            for pos in occurrences:
                missing = []
                for k, v in nums:
                    inside = qn if k in KEEP_PREV_YEAR else _PREV_YEAR.sub("", qn)
                    if has_number(inside, v):
                        continue
                    if is_row:   # остаток ячейки с названием + 4 следующие ячейки строки таблицы
                        after = "¦".join(self.src[pos + len(qn): pos + len(qn) + 1500].split("¦")[:5])
                        before = "¦".join(self.src[max(0, pos - 300): pos].split("¦")[-4:]) if k == "year" else ""
                        if has_number(after, v) or has_number(before, v):
                            continue
                    missing.append(f"число {num_token(v)} ({k}) не найдено в цитате")
                if best_missing is None or len(missing) < len(best_missing):
                    best_missing = missing
            why += best_missing

        last = re.sub(r"\[\d+\]$", "", path).split(".")[-1]
        if last in STR_CHECK and isinstance(f.get("v"), str) and norm(f["v"]) not in self.src:
            why.append(f"«{f['v']}» не найдено в справке")
        if isinstance(f.get("text"), str):
            for tok in re.findall(r"\d+(?:,\d+)?", _base_norm(f["text"])):
                val = float(tok.replace(",", ".")) if "," in tok else int(tok)
                if not has_number(self.src, val):
                    why.append(f"число {tok} из текста не найдено в справке")
        self.record(f, path, why)

    def ok(self, *facts):
        """Непроверенный факт считается неподтверждённым (кроме пустых заглушек)."""
        return all(self.status.get(id(f), (False, []))[0] for f in facts if isinstance(f, dict) and f)

    def bad(self):
        return [(p, why, f) for p, ok, why, f in self.log if not ok]


def check_facts(source_text, F):
    ck = Checker(source_text)
    region = F.get("region") if isinstance(F.get("region"), dict) else {}
    why = [f"«{region.get(k)}» не найдено в справке" for k in ("nom", "gen", "prep")
           if isinstance(region.get(k), str) and norm(region[k]) not in ck.src]
    ck.record(region, "region", why)
    for path, holder in (("year", F), ("status.camp.year", (F.get("status") or {}).get("camp") or {})):
        y = holder.get("year") if isinstance(holder, dict) else None
        if is_num(y):
            ck.record({"v": y}, path, [] if has_number(ck.src, y) else [f"год {num_token(y)} не встречается в справке"])
    ck.run(F.get("history"), "history")
    ck.run(F.get("status"), "status")
    ck.run(F.get("signals"), "signals")
    return ck

# ───────────────────────── русский язык ─────────────────────────

def pl(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt(v):
    if isinstance(v, float) and not v.is_integer():
        return ("%s" % round(v, 2)).replace(".", ",")
    return f"{int(v):,}".replace(",", " ")


def years_span(periods):
    ys = [int(y) for p in periods for y in re.findall(r"\d{4}", str(p))]
    if not ys:
        return ""
    a, b = min(ys), max(ys)
    return f"в {a} году" if a == b else f"в {a}–{b} гг."

# ───────────────────────── DOCX ─────────────────────────

_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def esc(s):
    """В выходных документах длинное тире не используется: «—» → «–».
    Дефис в составных словах («Ханты-Мансийский», «2017-2018») не трогаем."""
    return html.escape(_XML_ILLEGAL.sub(" ", str(s)).replace("—", "–"), quote=False)


HISTORY_COLOR = "0070C0"
FONT = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>'


def rpr(b=False, i=False, u=False, hl=False, color=None):
    x = FONT + ("<w:b/><w:bCs/>" if b else "") + ("<w:i/><w:iCs/>" if i else "")
    x += f'<w:color w:val="{color}"/>' if color else ""
    x += '<w:sz w:val="32"/><w:szCs w:val="32"/>'
    x += '<w:highlight w:val="yellow"/>' if hl else ""
    x += '<w:u w:val="single"/>' if u else ""
    return x


def run(text, b=False, i=False, u=False, hl=False, color=None):
    return f'<w:r><w:rPr>{rpr(b, i, u, hl, color)}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def paren_spans(text):
    """Отрезки [начало, конец) внешних пар круглых скобок, скобки включены.

    Вложенные скобки входят во внешнюю пару. Незакрытая «(» не выделяется вовсе — иначе
    выделение ушло бы до конца абзаца; лишняя «)» без пары тоже не выделяется.
    """
    spans, stack = [], []
    for i, ch in enumerate(text):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, i + 1))
    return spans


class Doc:
    """italic/color — оформление всего блока (в «Истории» по брифу: курсив цвета 0070C0).

    parens — правило «Текущего статуса» (решение пользователя 21.09): всё в круглых скобках,
    вместе со скобками, — курсивом цвета 0070C0, остальное как было.
    """

    def __init__(self, checker, bullet_num, italic=False, color=None, parens=False):
        self.ck = checker
        self.bullet = bullet_num
        self.italic = italic
        self.color = color
        self.parens = parens

    def styled(self, italic=None, color=None, parens=False):
        return Doc(self.ck, self.bullet, italic=italic, color=color, parens=parens)

    def with_parens(self):
        return Doc(self.ck, self.bullet, italic=self.italic, color=self.color, parens=True)

    def pieces(self, items):
        """items: [(текст, оформление прогона)] одного абзаца → прогоны, где скобки оформлены отдельно.

        Скобки считаются по всему абзацу сразу: пара может начинаться в одном куске текста,
        а закрываться в другом (число-факт внутри скобок — отдельный кусок).
        """
        if not self.parens:
            return "".join(self.run(t, **kw) for t, kw in items)
        full = "".join(t for t, _ in items)
        inside = [False] * len(full)
        for a, b in paren_spans(full):
            for k in range(a, b):
                inside[k] = True
        out, pos = [], 0
        for text, kw in items:
            k = 0
            while k < len(text):
                flag = inside[pos + k]
                j = k
                while j < len(text) and inside[pos + j] == flag:
                    j += 1
                style = dict(kw, i=True, color=HISTORY_COLOR) if flag else kw
                out.append(self.run(text[k:j], **style))
                k = j
            pos += len(text)
        return "".join(out)

    def mark(self):
        """Оформление знака абзаца — от него берут вид маркеры списков."""
        return f"<w:rPr>{rpr(i=self.italic, color=self.color)}</w:rPr>" if (self.italic or self.color) else ""

    def run(self, text, **kw):
        kw.setdefault("i", self.italic)
        kw.setdefault("color", self.color)
        return run(text, **kw)

    def segs(self, parts):
        items = []
        for p in parts:
            if isinstance(p, str):
                items.append((p, {}))
            elif isinstance(p, dict):
                # абзац, который поправил человек (parts_from_human): своя цитата не проверяется,
                # жёлтой подсветки тут не бывает — только то оформление, что он сам выбрал (жирный)
                items.append((p.get("text", ""), {"b": bool(p.get("b"))}))
            else:
                text, bold, facts = p
                items.append((text, {"b": bold, "hl": not self.ck.ok(*facts)}))
        return self.pieces(items)

    def p(self, parts, first=709):
        return ('<w:p><w:pPr><w:spacing w:after="0" w:line="276" w:lineRule="auto"/>'
                f'<w:ind w:firstLine="{first}"/><w:jc w:val="both"/>{self.mark()}</w:pPr>{self.segs(parts)}</w:p>')

    def bullet_p(self, parts):
        if not self.bullet:
            return self.p(["• "] + list(parts))
        return ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
                f'<w:numId w:val="{self.bullet}"/></w:numPr>'
                '<w:spacing w:after="0" w:line="276" w:lineRule="auto"/>'
                f'<w:ind w:left="0" w:firstLine="709"/><w:jc w:val="both"/>{self.mark()}</w:pPr>'
                f'{self.segs(parts)}</w:p>')

    def head(self, text, italic=False):
        return ('<w:p><w:pPr><w:spacing w:after="0" w:line="276" w:lineRule="auto"/>'
                f'<w:ind w:firstLine="709"/><w:jc w:val="both"/>{self.mark()}</w:pPr>'
                f'{self.pieces([(text, {"b": True, "u": not italic, "i": italic or self.italic})])}</w:p>')

    def empty(self):
        return f'<w:p><w:pPr><w:spacing w:after="0" w:line="276" w:lineRule="auto"/>{self.mark()}</w:pPr></w:p>'


def B(text, *facts):
    return (text, True, list(facts))


# ───────────────────────── правки человека в «Истории» и «Текущем статусе» ─────────────────────────
# Текст этих блоков программа собирает из фактов, а человек может поправить абзац на экране
# (просьба пользователя 27.09). Факты не трогаются: в F["edits"][блок] лежат правки вида
# {"was": текст собранного абзаца, "now": текст человека} — пустой now убирает абзац,
# пустой was добавляет новый после абзаца after. Правка привязана к тексту, а не к номеру:
# появился новый факт — остальные правки остаются на своих абзацах.

SECTION_KINDS = ("p", "bullet", "head")

# Раздел правят блоком общего редактора (единый формат блоков, решение пользователя 13.10): экран
# разбирает блок обратно в абзацы и отдаёт текст абзаца обычной строкой, где полужирный кусок обёрнут
# в <strong>…</strong> — остальное оформление панели экран снимает (parseSectionHtml в ui.html), сервер
# снимает ещё раз (server.clean_sections). human_parts разбирает такую строку в список {"text", "b"}
# для Doc.segs; вид абзаца (обычный/пункт/заголовок) в этой разметке не участвует — он идёт полем kind.
_HUMAN_TAG_RE = re.compile(r"<(/?)(?:strong|b)\s*>", re.IGNORECASE)
_HUMAN_ENTITIES = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"))


def _unescape_human(s):
    for a, b in _HUMAN_ENTITIES:
        s = s.replace(a, b)
    return s


def human_parts(raw):
    raw = re.sub(r"\s+", " ", _unescape_human(str(raw or ""))).strip()
    pieces, bold, pos = [], False, 0
    for m in _HUMAN_TAG_RE.finditer(raw):
        if m.start() > pos:
            pieces.append((raw[pos:m.start()], bold))
        bold = m.group(1) != "/"
        pos = m.end()
    if pos < len(raw):
        pieces.append((raw[pos:], bold))
    out = []
    for text, b in pieces:
        if out and out[-1]["b"] == b:
            out[-1]["text"] += text
        else:
            out.append({"text": text, "b": b})
    return [p for p in out if p["text"]]


def human_plain(raw):
    return "".join(p["text"] for p in human_parts(raw))


def para_text(xml):
    return re.sub(r"\s+", " ", "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml))).strip() \
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def para_kind(xml):
    if "<w:numPr>" in xml:
        return "bullet"
    if '<w:u w:val="single"/>' in xml or ("<w:b/>" in xml and xml.count("<w:r>") == 1):
        return "head"
    return "p"


def manual_para(d, parts, kind):
    """parts — список {"text", "b"} из human_parts. Заголовок и так весь жирный (шаблон),
    поэтому в нём разметка человека не нужна — только сам текст."""
    if kind == "bullet":
        return d.bullet_p(parts)
    if kind == "head":
        return d.head("".join(p["text"] for p in parts))
    return d.p(parts)


def apply_edits(d, paras, edits):
    """Собранные абзацы блока + правки человека → абзацы документа и описание для экрана.

    Возвращает (абзацы, [{text, kind, orig, manual}]): orig — текст собранного абзаца («» у
    добавленного человеком), по нему правка узнаётся при следующем сохранении."""
    edits = [e for e in (edits or []) if isinstance(e, dict)]
    by_was = {str(e.get("was") or ""): e for e in edits if str(e.get("was") or "").strip()}
    added = [e for e in edits if not str(e.get("was") or "").strip() and str(e.get("now") or "").strip()]
    out, sections = [], []

    def add_after(anchor):
        for e in added:
            if str(e.get("after") or "") == anchor:
                kind = e.get("kind") if e.get("kind") in SECTION_KINDS else "p"
                now_raw = str(e.get("now") or "")
                out.append(manual_para(d, human_parts(now_raw), kind))
                sections.append({"text": now_raw, "kind": kind, "orig": "", "manual": True})

    add_after("")
    for xml in paras:
        text = para_text(xml)
        if not text:
            out.append(xml)
            continue
        kind, e = para_kind(xml), by_was.get(text)
        if e is None:
            out.append(xml)
            sections.append({"text": text, "kind": kind, "orig": text, "manual": False})
        else:
            # вид абзаца человек меняет и у собранного абзаца (блоки, решение пользователя 13.10):
            # правка несёт kind, если вид другой; orig_kind — вид собранного абзаца, по нему
            # edits_from_sections понимает, что смена вида — тоже правка
            new_kind = e.get("kind") if e.get("kind") in SECTION_KINDS else kind
            now_raw = " ".join(str(e.get("now") or "").split())
            parts = human_parts(now_raw)
            if parts:
                out.append(manual_para(d, parts, new_kind))
            sections.append({"text": now_raw, "kind": new_kind, "orig": text, "manual": True, "orig_kind": kind})
        add_after(text)
    return out, sections


def edits_from_sections(items, orig_kinds=None):
    """Список абзацев с экрана → правки: {was, now, kind, after}. Абзац с orig и тем же текстом —
    не правка; пустой text у абзаца с orig — убрать; без orig — добавлен человеком.

    orig_kinds — {текст собранного абзаца: его вид} (из прошлой сборки, apply_edits): у абзаца
    с orig другой вид — тоже правка, она несёт kind. Без него вид собранного абзаца не меняется."""
    edits, last_orig = [], ""
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = " ".join(str(it.get("text") or "").split())
        orig = " ".join(str(it.get("orig") or "").split())
        kind = it.get("kind") if it.get("kind") in SECTION_KINDS else "p"
        if orig:
            # изменилась не только формулировка: одна лишь разметка (жирный) — тоже правка,
            # иначе выделение, которое человек сам выбрал, при пересборке пропадёт
            was_kind = (orig_kinds or {}).get(orig)
            new_kind = bool(text) and was_kind in SECTION_KINDS and kind != was_kind
            if human_plain(text) != orig or _HUMAN_TAG_RE.search(text) or new_kind:
                edits.append(dict({"was": orig, "now": text}, **({"kind": kind} if new_kind else {})))
            last_orig = orig
        elif text:
            edits.append({"was": "", "now": text, "kind": kind, "after": last_orig})
    return edits


def T(text, *facts):
    return (text, False, list(facts))


PROGRAMS = {
    "vld": "ФП «Все лучшее детям» НП «Молодежь и дети»",
    "semya": "ФП «Поддержка семьи» НП «Семья»",
    "fp_non_np": "ФП «Создание условий для обучения, отдыха и оздоровления детей и молодежи», "
                 "не входящий в состав национальных проектов",
    "capex": "капитальные вложения в объекты государственной (муниципальной) собственности",
}

CAMP_KINDS = {
    "stationary": (("стационарная (загородная) организация", "стационарных (загородных) организации",
                    "стационарных (загородных) организаций"), " отдыха детей и их оздоровления"),
    "day": (("лагерь", "лагеря", "лагерей"), " с дневным пребыванием"),
    "labor": (("детский лагерь", "детских лагеря", "детских лагерей"), " труда и отдыха"),
    "tent": (("детский лагерь", "детских лагеря", "детских лагерей"), " палаточного типа"),
    "profile": (("детский специализированный (профильный) лагерь или детский лагерь",
                 "детских специализированных (профильных) лагеря или детских лагеря",
                 "детских специализированных (профильных) лагерей или детских лагерей"),
                " различной тематической направленности"),
}


def _v(f):
    return f.get("v") if isinstance(f, dict) else None


def build_history(d, F):
    h = F.get("history") or {}
    r = F["region"]
    out = [d.bullet_p([B("О реализации национальных проектов")]),
           d.p([f"В {r['prep']} успешно реализованы НП «Образование» и «Демография», "
                "а также реализуется НП «Молодежь и дети» и «Семья»."]),
           d.p(["В регионе создается современная инфраструктура, обеспечивающая потребности жителей."]),
           d.empty()]

    kg = h.get("kindergartens_built") or []
    sch = h.get("schools_built") or []
    kg_n = sum(x["objects"] for x in kg if not x.get("part_of_previous"))
    kg_p = sum(x["places"] for x in kg if not x.get("part_of_previous"))
    sc_n = sum(x["objects"] for x in sch if not x.get("part_of_previous"))
    sc_p = sum(x["places"] for x in sch if not x.get("part_of_previous"))
    notes = [x for x in sch if x.get("note")]
    if kg_n or sc_n:
        if kg_n and sc_n:
            verb = " введены в эксплуатацию "
        elif kg_n:
            verb = pl(kg_n, " введен в эксплуатацию ", " введены в эксплуатацию ", " введены в эксплуатацию ")
        else:
            verb = pl(sc_n, " введена в эксплуатацию ", " введены в эксплуатацию ", " введены в эксплуатацию ")
        parts = [B("С 2019 года"), verb]
        if kg_n:
            parts += [B(f"{fmt(kg_n)} {pl(kg_n, 'детский сад', 'детских сада', 'детских садов')}", *kg), " на ",
                      B(f"{fmt(kg_p)} {pl(kg_p, 'место', 'места', 'мест')}", *kg)]
        if kg_n and sc_n:
            parts.append(" и ")
        if sc_n:
            parts += [B(f"{fmt(sc_n)} {pl(sc_n, 'школа', 'школы', 'школ')}", *sch), " общей мощностью ",
                      B(f"{fmt(sc_p)} {pl(sc_p, 'место', 'места', 'мест')}", *sch)]
        parts.append(", в том числе:" if notes and sc_n else ".")
        out.append(d.p(parts))
        if sc_n:
            for i, x in enumerate(notes):
                end = "." if i == len(notes) - 1 else ";"
                out.append(d.bullet_p([B(f"{fmt(x['objects'])} {pl(x['objects'], 'школа', 'школы', 'школ')}", x), " на ",
                                       B(f"{fmt(x['places'])} {pl(x['places'], 'место', 'места', 'мест')}", x),
                                       f" – {x['note']}{end}"]))

    sr, kr = h.get("schools_repaired"), h.get("kindergartens_repaired")
    if sr or kr:
        parts = []
        if sr:
            n, p_ = sr["buildings"], sr["places"]
            parts += [B(f"С {sr['since']} года", sr), " капитально отремонтировано ",
                      B(f"{fmt(n)} {pl(n, 'здание', 'здания', 'зданий')} школ", sr), " на ",
                      B(f"{fmt(p_)} {pl(p_, 'место', 'места', 'мест')}", sr)]
        if kr:
            n, p_ = kr["buildings"], kr["places"]
            if sr:
                parts.append(" и ")
                if kr.get("since") != sr.get("since"):
                    parts += [B(f"с {kr['since']} года", kr), " – "]
            else:
                parts += [B(f"С {kr['since']} года", kr), " капитально отремонтировано "]
            parts += [B(f"{fmt(n)} {pl(n, 'детский сад', 'детских сада', 'детских садов')}", kr), " на ",
                      B(f"{fmt(p_)} {pl(p_, 'место', 'места', 'мест')}", kr)]
        parts.append(".")
        out.append(d.p(parts))

    items = []
    f = h.get("tochka_rosta")
    if _v(f):
        n = f["v"]
        items.append(["В ", B(f"{fmt(n)} {pl(n, 'школе', 'школах', 'школах')}", f),
                      " функционирует Центр образования " if n == 1 else " функционируют Центры образования ",
                      B("«Точка роста»")])
    f = h.get("aop_mtb")
    if _v(f):
        n = f["v"]
        items.append(["В ", B(f"{fmt(n)} {pl(n, 'организации', 'организациях', 'организациях')}", f),
                      " обновлена материально-техническая база"])
    f = h.get("computers")
    if _v(f):
        n = f["v"]
        items.append([B(f"{fmt(n)} {pl(n, 'образовательная организация', 'образовательные организации', 'образовательных организаций')}", f),
                      pl(n, " оснащена", " оснащены", " оснащены") + " компьютерным оборудованием"])
    f = h.get("sport_halls")
    if _v(f):
        n = f["v"]
        items.append(["Отремонтированы и оборудованы спортивные залы и площадки в ",
                      B(f"{fmt(n)} {pl(n, 'школе', 'школах', 'школах')}", f)])
    if items:
        out.append(d.p([B("С 2019 года:")]))
        for it in items:
            out.append(d.bullet_p(it + ["."]))

    tech = []
    for key, forms, tail in (("it_cube", ("центр", "центра", "центров"), " «IT-куб»"),
                             ("kvant_school", ("школьный технопарк", "школьных технопарка", "школьных технопарков"), " «Кванториум»"),
                             ("kvant_children", ("детский технопарк", "детских технопарка", "детских технопарков"), " «Кванториум»"),
                             ("kvant_mobile", ("мобильный технопарк", "мобильных технопарка", "мобильных технопарков"), " «Кванториум»")):
        f = h.get(key)
        if _v(f):
            tech.append([B(f"{fmt(f['v'])} {pl(f['v'], *forms)}", f), tail])
    if tech:
        out.append(d.p(["А также работают:"]))
        for i, it in enumerate(tech):
            out.append(d.bullet_p(it + ["." if i == len(tech) - 1 else ";"]))

    f = h.get("advisors_schools")
    if _v(f):
        n = f["v"]
        out.append(d.p(["В ", B(f"{fmt(n)} {pl(n, 'школе', 'школах', 'школах')}", f),
                        " введены ставки ", B("советников директора по воспитанию"), "."]))

    z = h.get("zemsky") or []
    if z:
        t = sum(x["teachers"] for x in z)
        y = sum(x["young"] for x in z)
        out.append(d.p(["В рамках программы ", B("«Земский учитель»"), f" {years_span([x['period'] for x in z])} в сельские школы ",
                        pl(t, "переехал ", "переехали ", "переехали "),
                        B(f"{fmt(t)} {pl(t, 'учитель', 'учителя', 'учителей')}", *z),
                        " (включая ", B(f"{fmt(y)} {pl(y, 'молодого специалиста', 'молодых специалистов', 'молодых специалистов')}", *z),
                        " до 35 лет)."]))
    return out


def build_status(d, F):
    s = F["status"]
    r = F["region"]
    Y = F["year"]
    out = [d.head("1.2. О системе общего образования"), d.head("1.2.1. Детсады:")]
    g, pu, ac = s["dou_groups"], s["dou_pupils"], s["dou_access"]
    dd = s.get("dou_date") if isinstance(s.get("dou_date"), dict) else {}
    out.append(d.p([B("Общее количество"), " групп в образовательных организациях, реализующих образовательные "
                    f"программы дошкольного образования (по состоянию на {dd.get('v') or '1 января ' + str(Y) + ' г.'} "
                    "по отчетности 85-К) – ", B(fmt(g["v"]), g, dd), "."]))
    out.append(d.p([B("Численность воспитанников"), " – ", B(f"{fmt(pu['v'])} чел.", pu),
                    " (в возрасте от 2 месяцев до 7 лет и старше)."]))
    out.append(d.p([B("Доступность дошкольного образования"), " для детей (в возрасте от 2 месяцев до 7 лет и старше) – ",
                    B(f"{fmt(ac['v'])} %", ac), "."]))
    out.append(d.empty())

    out.append(d.head("1.2.2. Школы:"))
    sc, ur, ru = s["schools"], s["urban"], s["rural"]
    out.append(d.p(["Общее количество образовательных организаций:"]))
    out.append(d.p([B(f"{fmt(sc['total'])} {pl(sc['total'], 'школа', 'школы', 'школ')}", sc),
                    T(f" (из них дневных – {fmt(sc['day'])})", sc), ", общая численность обучающихся – ",
                    B(f"{fmt(sc['students'])} чел.", sc)]))
    out.append(d.p(["в том числе:"]))
    out.append(d.p([T(f"в городских поселениях – {fmt(ur['schools'])} {pl(ur['schools'], 'школа', 'школы', 'школ')} "
                      f"(численность обучающихся – {fmt(ur['students'])} чел.);", ur)]))
    out.append(d.p([T(f"в сельской местности – {fmt(ru['schools'])} {pl(ru['schools'], 'школа', 'школы', 'школ')} "
                      f"(численность обучающихся – {fmt(ru['students'])} чел.).", ru)]))
    two = s["two_shift"]
    three = s.get("three_shift") if isinstance(s.get("three_shift"), dict) else {"v": 0}
    tail = " Трехсменного режима нет." if not three.get("v") else f" В трехсменном режиме обучаются {fmt(three['v'])} чел."
    out.append(d.p([B(f"В двухсменном режиме обучаются {fmt(two['v'])} чел.", two), B(tail, three)]))
    gp = s["gpd"]
    sy_f = s.get("school_year") if isinstance(s.get("school_year"), dict) else {}
    sy = sy_f.get("v") or f"{Y - 1}/{Y}"
    total_1_4 = f" от общей численности обучающихся 1-4 классов ({fmt(gp['students_1_4'])} чел.)" if is_num(gp.get("students_1_4")) else ""
    out.append(d.p(["Количество ", B("групп продленного дня"), " (для обучающихся 1-4 классов) в общеобразовательных организациях – ",
                    B(fmt(gp["groups"]), gp), ", численность обучающихся – ", B(f"{fmt(gp['students'])} чел.", gp),
                    T(f" ({fmt(gp['share'])} %){total_1_4} ", gp),
                    T(f"(в {sy} учебном году, отчетность ОО-1).", sy_f)]))

    out.append(d.head("1.3. О реализации мероприятий по строительству и модернизации школ и детских садов"))
    scur, snext = s.get("school_repairs_current") or [], s.get("school_repairs_next")
    kcur, knext = s.get("kg_repairs_current") or [], s.get("kg_repairs_next")
    sn = len(scur) + (snext["buildings"] if snext else 0)
    sp = sum(x["places"] for x in scur) + (snext["places"] if snext else 0)
    kn = len(kcur) + (knext["buildings"] if knext else 0)
    kp = sum(x["places"] for x in kcur) + (knext["places"] if knext else 0)
    if sn or kn:
        last = int(max([x["year"] for x in (snext, knext) if x] + [Y]))
        period = f"В период {Y}-{last} гг." if last > Y else f"В {Y} году"
        parts = [period + " осуществляется "]
        sf = scur + ([snext] if snext else [])
        kf = kcur + ([knext] if knext else [])
        if sn:
            parts += [B(f"капитальный ремонт {fmt(sn)} {pl(sn, 'здания', 'зданий', 'зданий')} школ", *sf),
                      T(f" (общей мощностью {fmt(sp)} {pl(sp, 'место', 'места', 'мест')})", *sf)]
        if kn:
            parts += [" и " if sn else "капитальный ремонт ",
                      B(f"{fmt(kn)} {pl(kn, 'детского сада', 'детских садов', 'детских садов')}", *kf),
                      T(f" (общей мощностью {fmt(kp)} {pl(kp, 'место', 'места', 'мест')})", *kf)]
        parts.append(".")
        out.append(d.p(parts))

    cons = s.get("construction") or []
    groups = {}
    for o in cons:
        groups.setdefault((o["type"], o["program"]), []).append(o)

    def obj_phrase(kind, objs):
        n, p_ = len(objs), sum(o["places"] for o in objs)
        noun = pl(n, "школы", "школ", "школ") if kind == "school" else pl(n, "детского сада", "детских садов", "детских садов")
        return [B(f"{fmt(n)} {noun}", *objs), " мощностью ", B(f"{fmt(p_)} {pl(p_, 'место', 'места', 'мест')}", *objs)]

    def period_of(objs):
        ys = sorted({int(o["year"]) for o in objs})
        return f"В {ys[0]} году" if len(ys) == 1 else f"В период {ys[0]}-{ys[-1]} гг."

    order = list(PROGRAMS)
    keys = sorted(groups, key=lambda k: (k[0] != "school", order.index(k[1]) if k[1] in order else len(order)))
    school_keys = [k for k in keys if k[0] == "school"]
    kg_keys = [k for k in keys if k[0] != "school"]
    if len(school_keys) == 1 and len(kg_keys) == 1:
        a, b = groups[school_keys[0]], groups[kg_keys[0]]
        out.append(d.p([B(period_of(a + b)), " запланировано строительство "] + obj_phrase("school", a) +
                       [T(f" ({PROGRAMS[school_keys[0][1]]})"), " и "] + obj_phrase("kg", b) +
                       [T(f" ({PROGRAMS[kg_keys[0][1]]})"), "."]))
    else:
        for k in keys:
            objs = groups[k]
            out.append(d.p([B(period_of(objs)), " запланировано строительство "] + obj_phrase(k[0], objs) +
                           [T(f" ({PROGRAMS[k[1]]}).")]))

    risks, manual = s.get("risks") or [], F.get("manual_additions") or []
    if risks or manual:
        out.append(d.head("Справочно:", italic=True))
        for x in risks:
            out.append(d.p([T(x["text"], x)]))
        for x in manual:
            out.append(d.p([x]))

    c = s["camp"]
    out.append(d.head(f"1.4. О летней оздоровительной кампании {c.get('year') or Y}:"))
    o = c["orgs"]
    types = [t for t in c.get("types") or [] if _v(t)]
    out.append(d.p([f"В рамках ЛОК на территории {r['gen']} осуществляют деятельность ",
                    B(fmt(o["v"]), o), f" {pl(o['v'], 'организация', 'организации', 'организаций')} отдыха детей и их оздоровления"
                    + (", в том числе:" if types else ".")]))
    for i, t in enumerate(types):
        forms, tail = CAMP_KINDS[t["kind"]]
        out.append(d.bullet_p([B(fmt(t["v"]), t), f" {pl(t['v'], *forms)}{tail}" + ("." if i == len(types) - 1 else ";")]))
    ch, tj, sv = c["children"], c["tjs"], c["svo"]
    out.append(d.p([B("За летний период"), pl(ch["v"], " лагеря посетил ", " лагеря посетили ", " лагеря посетили "),
                    B(fmt(ch["v"]), ch), f" {pl(ch['v'], 'ребенок', 'ребенка', 'детей')}, в том числе ", B(fmt(tj["v"]), tj),
                    " – в трудной жизненной ситуации и ", B(fmt(sv["v"]), sv),
                    f" {pl(sv['v'], 'ребенок', 'ребенка', 'детей')} участников СВО."]))
    sh, ic = c.get("inclusive_shifts"), c.get("inclusive_children")
    if _v(sh):
        parts = [pl(sh["v"], "Проведена ", "Проведено ", "Проведено "),
                 B(f"{fmt(sh['v'])} {pl(sh['v'], 'инклюзивная смена', 'инклюзивные смены', 'инклюзивных смен')}", sh)]
        if _v(ic):
            parts += [" для ", B(f"{fmt(ic['v'])} {pl(ic['v'], 'ребенка', 'детей', 'детей')}", ic), " с ОВЗ и детей-инвалидов"]
        lf = c.get("large_families")
        if _v(lf):
            parts += [" и ", B(f"{fmt(lf['v'])} {pl(lf['v'], 'ребенка', 'детей', 'детей')}", lf), " из многодетных семей"]
        out.append(d.p(parts + ["."]))
    return out

# ───────────────────────── сверки и сигналы ─────────────────────────

SUBSET_RE = re.compile(r"из них|в том числе|из указанн")


def mark_subsets(F):
    """«Из них N объектов…» — часть предыдущей строки, а не добавка к ней.

    Нейросеть отмечает такие строки полем part_of_previous, но забывает это делать
    (так в брифе по Удмуртии получилось 13 школ вместо 12: строка «из них 16 мест
    (1 объект)» сложилась с одиннадцатью). Здесь то же самое видно по самой цитате:
    «из них» и числа не больше предыдущей строки. Возвращает список поправок для отчёта.
    """
    notes, h = [], F.get("history") or {}
    for key, word in (("schools_built", "школ"), ("kindergartens_built", "детских садов")):
        items = h.get(key)
        if not isinstance(items, list):
            continue
        for i, x in enumerate(items):
            prev = items[i - 1] if i and isinstance(items[i - 1], dict) else None
            if not isinstance(x, dict) or not prev or x.get("part_of_previous"):
                continue
            if not SUBSET_RE.search(str(x.get("q") or "").lower()):
                continue
            pairs = [(x.get(k), prev.get(k)) for k in ("objects", "places")]
            if not any(is_num(a) and is_num(b) for a, b in pairs):
                continue
            if any(is_num(a) and is_num(b) and a > b for a, b in pairs):
                continue                       # больше предыдущей строки — значит, не её часть
            x["part_of_previous"] = True
            notes.append(f"Строка «{str(x.get('q') or '')[:120]}» — часть предыдущей: "
                         f"в общее число {word} не сложена.")
    return notes


def consistency(F):
    s, w = F["status"], []
    sc, ur, ru = s["schools"], s["urban"], s["rural"]
    if ur["schools"] + ru["schools"] != sc["day"]:
        w.append(f"Городских ({fmt(ur['schools'])}) + сельских ({fmt(ru['schools'])}) школ ≠ дневных школ ({fmt(sc['day'])}).")
    if ur["students"] + ru["students"] != sc["students"]:
        w.append(f"Обучающихся в городе + на селе ({fmt(ur['students'] + ru['students'])}) ≠ всего ({fmt(sc['students'])}).")
    g = s["gpd"]
    if is_num(g.get("students_1_4")) and g["students_1_4"] and abs(g["students"] / g["students_1_4"] * 100 - g["share"]) > 0.05:
        w.append(f"Доля ГПД {fmt(g['share'])} % не сходится с расчётом {fmt(round(g['students'] / g['students_1_4'] * 100, 2))} %.")
    return w


def auto_signals(F):
    s, Y, out = F["status"], F["year"], []
    g = s["gpd"]
    if is_num(g.get("rf_share")) and g["share"] < g["rf_share"]:
        out.append(f"Охват группами продленного дня {fmt(g['share'])} % – ниже среднего по РФ ({fmt(g['rf_share'])} %).")
    for o in s.get("construction") or []:
        rd = o.get("readiness")
        if isinstance(rd, str) and "не строится" in rd.lower():
            out.append(f"{o['name']} ({fmt(o['places'])} мест, срок {o['year']}) – объект не строится.")
        elif is_num(rd):
            if int(o["year"]) <= Y and rd < 100:
                out.append(f"{o['name']} ({fmt(o['places'])} мест): срок ввода {o['year']}, готовность {fmt(rd)} %.")
            elif int(o["year"]) == Y + 1 and rd < 30:
                out.append(f"{o['name']} ({fmt(o['places'])} мест): срок {o['year']}, готовность лишь {fmt(rd)} %.")
    for key, what in (("school_repairs_current", "капремонт школы"), ("kg_repairs_current", "капремонт детского сада")):
        for o in s.get(key) or []:
            rd = o.get("readiness")
            if is_num(rd) and rd < 60:
                out.append(f"{what.capitalize()}: {o.get('name', 'объект')} – срок {Y}, готовность {fmt(rd)} %.")
    return out

# ───────────────────────── сборка ─────────────────────────

def bullet_num_id(numbering_xml):
    abstract_fmt = {}
    for m in re.finditer(r'<w:abstractNum [^>]*w:abstractNumId="(\d+)".*?</w:abstractNum>', numbering_xml, re.S):
        f = re.search(r'<w:lvl w:ilvl="0".*?<w:numFmt w:val="(\w+)"', m.group(0), re.S)
        abstract_fmt[m.group(1)] = f.group(1) if f else ""
    nums = re.findall(r'<w:num w:numId="(\d+)"[^>]*>\s*<w:abstractNumId w:val="(\d+)"/>', numbering_xml)
    bullets = [n for n, a in nums if abstract_fmt.get(a) == "bullet"]
    return "6" if "6" in bullets else (bullets[0] if bullets else None)


def label_cell(text, bold=False):
    return ('<w:tc><w:tcPr><w:tcW w:w="2376" w:type="dxa"/></w:tcPr>'
            f'<w:p>{run(text, b=bold)}</w:p></w:tc>')


def content_cell(paras):
    return '<w:tc><w:tcPr><w:tcW w:w="8789" w:type="dxa"/></w:tcPr>' + "".join(paras) + "</w:tc>"


def listed(d, items):
    """Один пункт — абзацем; несколько — списком. Строка, оканчивающаяся двоеточием, — вводная, без маркера."""
    items = [x.strip() for x in items if x and x.strip()]
    if len(items) <= 1:
        return [d.p([x]) for x in items]
    return [d.p([x]) if x.endswith(":") else d.bullet_p([x]) for x in items]


def safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s)).strip().rstrip(".")
    return s[:80] or "регион"


def shell_parts():
    """Файлы шаблона документа: из них собирается и отдельный бриф, и сводный."""
    with zipfile.ZipFile(SHELL) as z:
        shell = {n: z.read(n) for n in z.namelist()}
        infos = {i.filename: i for i in z.infolist()}
    doc_xml = shell["word/document.xml"].decode("utf8")
    return {
        "files": shell, "infos": infos,
        "root": re.search(r"<w:document[^>]*>", doc_xml).group(0),
        "sect": re.search(r"<w:sectPr.*?</w:sectPr>", doc_xml, re.S).group(0),
        "bullet": bullet_num_id(shell["word/numbering.xml"].decode("utf8")),
    }


def write_docx(shell, body, out_docx):
    """Тело документа + файлы шаблона → готовый .docx. Общий для всех видов брифа."""
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + shell["root"] +
           "<w:body>" + "".join(body) + shell["sect"] + "</w:body></w:document>")
    out_docx = Path(out_docx)
    tmp = out_docx.with_name(out_docx.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in shell["files"].items():
            if name == "word/document.xml":
                data = xml.encode("utf8")
            z.writestr(shell["infos"][name], data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(out_docx)
    return out_docx


def region_body(source_text, F, shell, toc_link=True, anchor="_Toc1", anchor_id=0, title_text=None, info=None):
    """Тело брифа для региона: та самая таблица «Вопрос / История / …».

    Отдельным куском — чтобы такой же бриф можно было вложить в сводный документ
    по нескольким справкам, ничего не переверстывая."""
    subset_notes = mark_subsets(F)      # до сборки: иначе «из них …» сложится в общее число
    ck = check_facts(source_text, F)
    d = Doc(ck, shell["bullet"])
    dh = d.styled(italic=True, color=HISTORY_COLOR)     # «История» (2-я строка, 2-й столбец): весь текст курсивом, 0070C0

    r = F["region"]
    heading = title_text or ("1. О системе общего образования " + r["gen"])
    title = ('<w:p><w:pPr><w:pStyle w:val="1"/><w:spacing w:before="0" w:line="276" w:lineRule="auto"/>'
             f'<w:jc w:val="both"/></w:pPr><w:bookmarkStart w:id="{anchor_id}" w:name="{anchor}"/>'
             f'{run(heading, hl=not ck.ok(r))}<w:bookmarkEnd w:id="{anchor_id}"/></w:p>')
    problems = [x for x in F.get("problems") or [] if x.strip()]
    solutions = [x for x in F.get("solutions") or [] if x.strip()]
    edits = F.get("edits") if isinstance(F.get("edits"), dict) else {}
    hist, sec_h = apply_edits(dh, build_history(dh, F), edits.get("history"))
    ds = d.with_parens()
    stat, sec_s = apply_edits(ds, build_status(ds, F), edits.get("status"))
    if info is not None:
        info["sections"] = {"history": sec_h, "status": sec_s}
    rows = [
        (label_cell("Вопрос", bold=True), content_cell([title])),
        (label_cell("История"), content_cell(hist or [dh.p(["Нет данных в справке."])])),
        # «Текущий статус»: всё в скобках, вместе со скобками, — синим курсивом (решение пользователя 21.09)
        (label_cell("Текущий статус"), content_cell(stat)),
        (label_cell("Проблематика"), content_cell(listed(d, problems) or [d.p(["Отсутствует."])])),
        (label_cell("Варианты решения"), content_cell(listed(d, solutions) or [d.p(["Не требуются."])])),
    ]
    tbl = ('<w:tbl><w:tblPr><w:tblStyle w:val="af"/><w:tblW w:w="11165" w:type="dxa"/>'
           '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>'
           '</w:tblPr><w:tblGrid><w:gridCol w:w="2376"/><w:gridCol w:w="8789"/></w:tblGrid>'
           + "".join(f"<w:tr>{a}{b}</w:tr>" for a, b in rows) + "</w:tbl>")
    toc = ('<w:p/><w:p><w:hyperlink w:anchor="_Toc1" w:history="1"><w:r><w:rPr><w:rStyle w:val="afa"/>' + FONT +
           '<w:b/><w:bCs/><w:i/><w:iCs/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr><w:t>К оглавлению</w:t></w:r></w:hyperlink></w:p>')
    return [tbl] + ([toc] if toc_link else []), ck, subset_notes, problems, solutions


def build(source_text, F, out_docx):
    """Собирает DOCX и отчёт. F — уже нормализованные и проверенные факты.
    Возвращает словарь с итогами проверки."""
    shell = shell_parts()
    info = {}
    body, ck, subset_notes, problems, solutions = region_body(source_text, F, shell, info=info)
    out_docx = write_docx(shell, body, out_docx)
    r = F["region"]

    bad = [{"path": p, "why": why, "quote": f.get("q") or f.get("text") or ""} for p, why, f in ck.bad()]
    result = {
        "region": r["nom"],
        "facts_total": len(ck.log),
        "unverified": bad,
        "consistency": subset_notes + consistency(F),
        "signals": auto_signals(F) + [x["text"] for x in F.get("signals") or []],
        "manual": list(F.get("manual_additions") or []),
        "problems": problems,
        "solutions": solutions,
        "sections": info.get("sections"),
    }
    manual_paras = [f"{blk}: «{human_plain(s['text']) or '— (абзац убран)'}»" + (f" (было: «{s['orig']}»)" if s["orig"] else "")
                    for blk, items in (("История", info["sections"]["history"]), ("Текущий статус", info["sections"]["status"]))
                    for s in items if s["manual"]]
    result["manual"] = result["manual"] + manual_paras
    rep = [f"# Проверка брифа: {r['nom']}", "",
           f"Фактов проверено: {result['facts_total']}, не подтверждено: {len(bad)}.", "",
           "## Сверьте вручную (в DOCX подсвечено жёлтым: точной цитаты в справке не нашлось)", ""]
    rep += [f"- `{b['path']}` — {'; '.join(b['why'])}. Цитата: «{b['quote']}»" for b in bad] or ["- нет"]
    rep += ["", "## Внутренние нестыковки", ""] + ([f"- {x}" for x in result["consistency"]] or ["- нет"])
    rep += ["", "## Сигналы для «Проблематики» (в бриф не вносятся, решает куратор)", ""] + ([f"- {x}" for x in result["signals"]] or ["- нет"])
    rep += ["", "## Внесено вручную, справкой не проверяется", ""] + ([f"- {x}" for x in result["manual"]] or ["- нет"])
    rep_path = out_docx.with_suffix(".проверка.md")
    rep_path.write_text("\n".join(rep) + "\n", encoding="utf8")
    result["docx"] = str(out_docx)
    result["report"] = str(rep_path)
    return result


def strict_schema(node):
    """Строгий режим структурированного вывода: у каждого объекта перечислены все поля
    и запрещены посторонние. Необязательные поля объявлены как nullable, «нет данных» — null."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            node["required"] = list(props)
            node["additionalProperties"] = False
            for v in props.values():
                strict_schema(v)
        if isinstance(node.get("items"), dict):
            strict_schema(node["items"])
    return node


def facts_schema():
    """JSON-схема ответа для нейросетей, умеющих структурированный вывод (Искра: response_format).
    Строгая: посторонних полей нет, все объявленные поля обязательны (необязательные — со значением null)."""
    S, N, NS = {"type": "string"}, {"type": ["number", "null"]}, {"type": ["string", "null"]}

    def fact(*nums, extra=None, nullable=True):
        props = {k: N for k in nums}
        props["q"] = S
        props.update(extra or {})
        return {"type": ["object", "null"] if nullable else "object", "properties": props}

    def arr(item):
        return {"type": "array", "items": item}

    row = {"type": "object",
           "properties": {"name": S, "places": N, "readiness": {"type": ["number", "string", "null"]}, "q": S}}
    nxt = fact("year", "buildings", "places")
    return strict_schema({
        "type": "object",
        "properties": {
            "region": {"type": "object", "properties": {"nom": S, "gen": S, "prep": S}},
            "year": {"type": "number"},
            "history": {"type": "object", "properties": {
                "kindergartens_built": arr(fact("objects", "places", extra={"program": NS}, nullable=False)),
                "schools_built": arr(fact("objects", "places", extra={"program": NS, "note": NS,
                                                                       "part_of_previous": {"type": ["boolean", "null"]}},
                                          nullable=False)),
                "schools_repaired": fact("buildings", "places", "since"),
                "kindergartens_repaired": fact("buildings", "places", "since"),
                **{k: fact("v") for k in ("tochka_rosta", "aop_mtb", "computers", "sport_halls", "it_cube",
                                          "kvant_school", "kvant_children", "kvant_mobile", "advisors_schools")},
                "zemsky": arr(fact("teachers", "young", extra={"period": S}, nullable=False)),
            }},
            "status": {"type": "object", "properties": {
                "dou_date": {"type": ["object", "null"], "properties": {"v": S, "q": S}},
                "dou_groups": fact("v", nullable=False), "dou_pupils": fact("v", nullable=False),
                "dou_access": fact("v", nullable=False),
                "schools": fact("total", "day", "students", nullable=False),
                "urban": fact("schools", "students", nullable=False), "rural": fact("schools", "students", nullable=False),
                "two_shift": fact("v", nullable=False), "three_shift": fact("v"),
                "gpd": fact("groups", "students", "share", extra={"students_1_4": N, "rf_share": N}, nullable=False),
                "school_year": {"type": ["object", "null"], "properties": {"v": S, "q": S}},
                "school_repairs_current": arr(row), "school_repairs_next": nxt,
                "kg_repairs_current": arr(row), "kg_repairs_next": nxt,
                "construction": arr({"type": "object",
                                     "properties": {"type": {"type": "string", "enum": ["school", "kg"]},
                                                    "program": {"type": "string", "enum": list(PROGRAMS)},
                                                    "name": S, "places": N, "year": N,
                                                    "readiness": {"type": ["number", "string", "null"]}, "q": S}}),
                "risks": arr({"type": "object", "properties": {"text": S, "q": S}}),
                "camp": {"type": "object", "properties": {
                    "year": N, "orgs": fact("v", nullable=False),
                    "types": arr(fact("v", extra={"kind": {"type": "string", "enum": list(CAMP_KINDS)}}, nullable=False)),
                    "children": fact("v", nullable=False), "tjs": fact("v", nullable=False), "svo": fact("v", nullable=False),
                    "inclusive_shifts": fact("v"), "inclusive_children": fact("v"), "large_families": fact("v"),
                }},
            }},
            "manual_additions": arr(S), "problems": arr(S), "solutions": arr(S),
            "signals": arr({"type": "object", "properties": {"text": S, "q": S}}),
        },
    })


def prepare_facts(answer_text):
    """Ответ модели → (факты, ошибки структуры). Бросает FactsError, если JSON не разобрать."""
    F = coerce(parse_answer(answer_text))
    return F, validate(F)
