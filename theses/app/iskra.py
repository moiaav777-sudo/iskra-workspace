"""Написание тезисов «Искрой» по правилам выбранного человека.

Правила из `docs/` уходят в запрос целиком и без пересказа: они закон, и решать
за них сервис не вправе. Сервис добавляет к ним только своё — тему, хронометраж,
исходные материалы и требование отвечать разбираемым форматом.

Ответ модели никогда не применяется молча:

- он приводится к схеме (`check_schema`) — неизвестное выделение, строка вместо
  списка, лишние поля не должны ронять запрос и класть мусор в документ;
- слова вперемешку из русских и латинских букв приводятся к одному алфавиту, а где это
  неоднозначно — уходят в круг правки и, если не исправились, человеку (`latin.fix_mixed`);
- там, где фактуры нет, правила требуют маркер `[ТРЕБУЕТСЯ …]`.

Числа сервис не перепроверяет — решение пользователя 21.09: «всё будет на моё решение».
Для чисел из открытых источников «Искра» оставляет ссылки, чтобы человеку было где смотреть.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import iskra_common, latin, portal, richtext
from .models import (EMPHASIS, MARKER_RE, MARKERS, MODE_DOCS, NORMAL, WORDS_PER_MINUTE,
                     Bullet, Gap, Ref, Thesis, marker_name, normalize_markers)

ISKRA_BASE = "https://cloud.iskrabot.ru"
RUNS_PATH = "/api/v1/runs"
# Прогон идёт фоном: запускаем, опрашиваем, снимаем. Так делают «Бриф» и A3 — синхронный
# /chat держал запрос по пять минут, и страница выглядела зависшей (задача Оркестратора P1).
POLL_MIN, POLL_MAX = 2.0, 5.0
DEADLINE = 600.0          # дольше десяти минут не ждём: человек должен узнать, что не вышло
POLL_FAILS = 3            # столько сбоев опроса подряд терпим: связь рвётся и на минуту
MAX_SOURCE_CHARS = 200_000
MAX_ASK_CHARS = 8_000


class IskraError(RuntimeError):
    """«Искра» не ответила или ответила не тем. Текст показывается пользователю."""


class NoKey(IskraError):
    """Ключа нет: сервис его не хранит и не спрашивает, ключ кладёт портал."""


class Cancelled(IskraError):
    """Человек нажал «Отменить». Прогон у «Искры» снят, потраченное посчитано."""


# ── управление прогоном из фонового задания ──────────────────────────────

class Control:
    """Связь прогона с заданием: шаг, отмена, предельный срок и расход.

    Задание кладёт своё управление в поток, где идёт прогон (`bind`); код ниже его
    читает через `control()`. Так `_call` сохраняет простую подпись — в тестах его
    подменяют функцией от двух аргументов.
    """

    def __init__(self, on_stage=None, cancelled=None, deadline: float | None = None):
        self._on_stage = on_stage
        self._cancelled = cancelled
        # срок берём при создании, а не при описании класса: иначе смена DEADLINE
        # не действовала бы, а интерфейсу отдаётся именно она (ревьюер)
        self.deadline = DEADLINE if deadline is None else deadline
        # и считаем его от создания — на весь прогон вместе с кругом правки, а не на
        # каждый вызов «Искры»: иначе с кругом правки выходило до двадцати минут (Н1)
        self.until = time.monotonic() + self.deadline
        self.spent = {"in": 0, "out": 0}

    def stage(self, name: str) -> None:
        if self._on_stage:
            self._on_stage(name)

    def cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled())

    def add(self, spent: dict) -> None:
        self.spent["in"] += int(spent.get("in", 0) or 0)
        self.spent["out"] += int(spent.get("out", 0) or 0)


_local = threading.local()


def bind(ctl: Control | None) -> None:
    _local.control = ctl


def control() -> Control:
    ctl = getattr(_local, "control", None)
    if ctl is None:
        ctl = Control()
        _local.control = ctl
    return ctl


def spend(tokens_in: int, tokens_out: int) -> None:
    """Расход «Искры»: в задание (оно отдаст его в журнал) и в запрос, если он есть."""
    control().add({"in": tokens_in, "out": tokens_out})
    portal.note_tokens(tokens_in, tokens_out)


def read_key() -> str:
    return portal.env("ISKRA_API_KEY")


def key_ready() -> bool:
    return bool(read_key())


def _key() -> str:
    key = read_key()
    if not key:
        raise NoKey("Ключ «Искры» не настроен — его записывает портал при выкладке сервиса")
    return key


def _http(method: str, path: str, body: dict | None = None,
          timeout: float = 60) -> tuple[int, dict]:
    """Один обмен с «Искрой». Подменяется в тестах — сети в них нет."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        ISKRA_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _key()})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw[:300]}
        return error.code, payload
    except urllib.error.URLError as error:
        raise IskraError(f"«Искра» недоступна: {error.reason}") from error
    except (TimeoutError, json.JSONDecodeError, http.client.HTTPException,
            ConnectionError, OSError) as error:
        # обрыв на середине ответа (ConnectionReset, RemoteDisconnected, IncompleteRead)
        # тоже временный сбой, а не повод уронить задание без объяснения
        raise IskraError(f"«Искра» не ответила: {error}") from error


def _answer_of(run: dict) -> str:
    """Текст ответа из законченного прогона — лежит под разными именами."""
    for key in ("answer", "output", "text", "result"):
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for inner in ("answer", "output", "text", "content"):
                if isinstance(value.get(inner), str) and value[inner].strip():
                    return value[inner]
    return ""


def _settle(run_id: str, last: dict) -> dict:
    """Снять прогон у «Искры» и забрать его последний расход.

    Прогон, который мы бросили (отмена, срок, оборванная связь), без DELETE продолжал бы
    тратить токены, и мимо журнала. После снятия читаем ответ DELETE, а если расхода
    в нём нет — ещё раз спрашиваем прогон: «Искра» пишет расход и в незаконченный.
    Не ответила — остаётся последний замер. Расход в прогоне накопительный, поэтому
    берётся самый свежий, а не сумма.
    """
    path = f"{RUNS_PATH}/{urllib.parse.quote(run_id)}"
    best = last or {}
    for method in ("DELETE", "GET"):
        try:
            status, payload = _http(method, path, timeout=20)
        except IskraError:
            continue
        if status < 400 and isinstance(payload, dict):
            run = iskra_common.unwrap(payload)
            spent = iskra_common.usage(run)
            if spent["in"] or spent["out"]:
                return run
    return best


def _call(system: str, user: str) -> str:
    """Один прогон «Искры»: запустить, дождаться, забрать ответ.

    На каждом круге — сначала отмена и срок, потом опрос: ни 429, ни сбой связи не
    должны уводить мимо предельного срока (ревьюер RC-2026-09-21-10, К3). Потраченное
    считаем в любом исходе: неудачный и отменённый прогон тоже стоили токенов (п. 4).
    """
    ctl = control()
    status, payload = _http("POST", RUNS_PATH, {"message": user, "instructions": system},
                            timeout=120)
    if status not in (200, 201, 202):
        raise IskraError(iskra_common.human_error(status, payload))
    run = iskra_common.unwrap(payload)
    run_id = str(run.get("run_id") or run.get("id") or "")
    if not run_id:
        raise IskraError("«Искра» не выдала номер прогона")

    deadline = ctl.until
    pause, last, fails = POLL_MIN, run, 0

    def give_up(error: IskraError):
        ctl.add(iskra_common.usage(_settle(run_id, last)))
        raise error

    def check_limits():
        if ctl.cancelled():
            give_up(Cancelled("Написание отменено"))
        if time.monotonic() > deadline:
            minutes = max(1, int(ctl.deadline // 60))
            give_up(IskraError(f"«Искра» не ответила за {minutes} мин — прогон снят. "
                               "Попробуйте ещё раз; если повторится, сократите материалы"))

    while True:
        check_limits()
        # ждём мелкими шагами: отмена и срок должны срабатывать быстро
        waited = 0.0
        while waited < pause:
            time.sleep(0.25)
            waited += 0.25
            check_limits()
        try:
            status, payload = _http("GET", f"{RUNS_PATH}/{urllib.parse.quote(run_id)}",
                                    timeout=60)
        except IskraError as error:
            fails += 1
            if fails >= POLL_FAILS:
                give_up(IskraError(f"{error}. Связь с «Искрой» рвалась {fails} раза подряд — "
                                   "прогон снят, попробуйте ещё раз"))
            pause = POLL_MAX
            continue
        fails = 0
        if status == 429:
            pause = POLL_MAX                # чаще спрашивать не просят; срок проверится в начале круга
            continue
        if status >= 400:
            give_up(IskraError(iskra_common.human_error(status, payload)))
        last = iskra_common.unwrap(payload)
        if iskra_common.is_done(last):
            ctl.add(iskra_common.usage(last))
            answer = _answer_of(last)
            if not answer:
                raise IskraError("«Искра» закончила, но ответа не прислала")
            return answer
        if iskra_common.is_failed(last):
            ctl.add(iskra_common.usage(last))
            raise IskraError(f"«Искра» прервала прогон: {iskra_common.trouble(last)}. "
                             "Попробуйте ещё раз — прогон часто срывается на её стороне")
        pause = min(POLL_MAX, pause + 0.5)


# ── что просим ───────────────────────────────────────────────────────────

# Задача Оркестратора P1 от 21.09: в выгруженных тезисах появлялись английские слова.
# Правило стоит в системной части и отдельно оговаривает служебные ключи ответа —
# они латиницей по необходимости, и модель переносила их в текст.
LANGUAGE = """ЯЗЫК. Весь текст тезисов, пояснения и предложения пиши только по-русски.
Латиница допустима лишь в собственных именах и устоявшихся обозначениях, которые
встречаются в представленных материалах: названия систем и программ, аббревиатуры
вроде IT, коды, адреса сайтов. Никаких английских слов, терминов, вставок, пояснений
и рассуждений вслух — ни в тексте, ни в полях ответа. Есть русское слово — пиши его.
Каждое слово пиши целиком одним алфавитом: русские и латинские буквы в одном слове
не смешивай («к заданию», а не «к zadанию»).
Имена полей ответа (bullets, emphasis, part, gaps и другие) и значения normal, strong,
aside — служебные: в сам текст их не переносить.
ВИД. Каждое предложение — отдельный пункт (bullet): в одном пункте одно предложение.
Никаких знаков разметки в тексте: ни **, ни *, ни #, ни `. Выделение — только полем
emphasis. Каждое слово — целиком одним алфавитом."""

FORMAT = """
Ответ — только JSON, без пояснений до и после, такого вида:

{"bullets": [{"text": "…", "emphasis": "normal|strong|aside", "part": "масштаб"}],
 "refs": [{"name": "чей это источник", "url": "ссылка", "about": "какое число оттуда"}],
 "gaps": [{"marker": "нужна цифра", "suggestion": "какого числа не хватает"}]}

Что значат поля:
- bullets — сам текст выступления, по одному предложению на пункт, в том порядке,
  в котором он произносится;
- emphasis: "strong" — произносится с акцентом, "aside" — справочное, при нехватке
  времени опускается, "normal" — обычный буллит;
- part — какая это часть выступления, одним русским словом (контекст, масштаб,
  результат, проблема, вывод, поручение);
- refs — откуда взяты числа, найденные в открытых источниках: название и ссылка.
  Число без источника в выступление не идёт — вместо него ставится маркер. Если все
  числа из представленных материалов, refs — пустой список;
- gaps — чего в материалах не хватает. Метка ставится и в сам текст пункта
  в квадратных скобках, например: [нужна цифра: охват в сельских школах, 2026].
  Если в правилах метки названы иначе ([ТРЕБУЕТСЯ ФАКТУРА] и подобные), пиши их так:
  {markers}.
"""


def _system(rules_text: str, person: str) -> str:
    who = f" для {person}" if person else ""
    head = (f"Ты готовишь тезисы выступления{who} и работаешь строго по правилам ниже. "
            "Правила сильнее любых твоих представлений о том, как надо писать. "
            "Ничего не придумывай сверх представленных материалов.\n\n" + LANGUAGE)
    rules = rules_text.strip() or (
        "Правил для этого человека ещё нет. Пиши короткими буллитами по одной мысли, "
        "ничего не выдумывай и отметь в ответе, что правила не заданы.")
    return head + "\n\n" + rules + "\n\n" + FORMAT.replace("{markers}", ", ".join(MARKERS))


def _user(thesis: Thesis, sources_text: str) -> str:
    words = thesis.minutes * WORDS_PER_MINUTE
    parts = [
        f"Тема выступления: {thesis.topic}",
        f"Где выступление: {thesis.event}" if thesis.event else "",
        f"Хронометраж: {thesis.minutes} мин — это примерно {words} слов вслух.",
    ]
    if (thesis.ask or "").strip():
        # окно «Что нужно в тезисах»: человек мог наговорить это голосом — распознавание
        # речи ошибается в словах, поэтому просим понимать по смыслу, а не дословно
        parts.append(
            "ЧТО НУЖНО В ТЕЗИСАХ — слова выступающего (могли быть наговорены голосом, "
            "в словах возможны ошибки распознавания речи: понимай по смыслу). "
            "Это пожелания к содержанию и акцентам; правила написания сильнее них:\n"
            + thesis.ask.strip()[:MAX_ASK_CHARS])
    if thesis.mode == MODE_DOCS:
        parts.append(
            "Тезисы собираются ТОЛЬКО из материалов ниже. Общие знания не используются: "
            "если сведений в материалах нет, ставь маркер [ТРЕБУЕТСЯ …] и перечисли "
            "такие места в списке недостающего (поле ответа gaps).\n\n"
            "МАТЕРИАЛЫ:\n" + sources_text[:MAX_SOURCE_CHARS])
    else:
        parts.append(
            "Представленных материалов нет — ищи фактуру в открытых источниках. "
            "Числа в выступлении обязательны, но выдуманных быть не должно: для каждого "
            "числа укажи, чей это источник, и дай на него ссылку (поле ответа refs) — "
            "официальная статистика, нормативный акт, сайт ведомства. Найденное в открытых источниках пересказывай по-русски, английские "
            "формулировки источника в текст не переноси. Предпочитай свежие официальные "
            "данные и указывай период, к которому они относятся. "
            "Если найти не удалось или источник сомнительный — не пиши число: ставь "
            "маркер [ТРЕБУЕТСЯ …] с предложением, какой показатель сюда нужен, "
            "и повтори это в списке недостающего (поле ответа gaps).")
    return "\n".join(p for p in parts if p)


# ── ответ модели приводим к схеме ────────────────────────────────────────

def parse_json(answer: str) -> dict:
    """JSON из ответа, даже если модель обернула его в ```json или пояснения."""
    text = (answer or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise IskraError("«Искра» ответила не разбираемым текстом")
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise IskraError(f"Ответ «Искры» не разобрать: {error}") from error
    if not isinstance(data, dict):
        raise IskraError("«Искра» ответила не тем: ожидался объект")
    return data


# Ссылку берём только обычную и только http(s): в интерфейсе по ней кликают.
_LINK = re.compile(r"^https?://[^\s<>\"']{4,500}$", re.I)


def _link(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _LINK.match(text) else ""


def _text_of(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_text_of(v) for v in value if v).strip()
    return ""


def _records(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item if isinstance(item, dict) else {"text": _text_of(item)} for item in value]


def check_schema(data: dict) -> tuple[list[Bullet], list[Ref], list[Gap], list[str]]:
    """Привести ответ к схеме. Расхождения возвращаются замечаниями — их видит человек."""
    notes: list[str] = []

    bullets: list[Bullet] = []
    for item in _records(data.get("bullets")):
        text = normalize_markers(_text_of(item.get("text")))
        if not text:
            continue
        emphasis = _text_of(item.get("emphasis")).lower()
        if emphasis not in EMPHASIS:
            if emphasis:
                notes.append(f"неизвестное выделение «{emphasis}» — поставлено обычное")
            emphasis = NORMAL
        part = _text_of(item.get("part"))
        # каждое предложение — отдельный пункт (задача 22.09, п. 6); текст пока со знаками
        # Markdown — их снимет `html_from_bullets`, превратив в оформление
        for piece in richtext.sentences(text) or [text]:
            bullets.append(Bullet(text=piece, emphasis=emphasis, part=part))
    if not bullets:
        raise IskraError("«Искра» не вернула ни одного тезиса")

    refs: list[Ref] = []
    for item in _records(data.get("refs")):
        name, url = _text_of(item.get("name")), _link(item.get("url"))
        if name or url:
            refs.append(Ref(name=name, url=url, about=_text_of(item.get("about"))))

    gaps: list[Gap] = []
    for item in _records(data.get("gaps")):
        raw = _text_of(item.get("marker"))
        marker = marker_name(raw)
        if raw and raw.strip("[] ").lower() not in MARKERS and raw.strip("[] ").upper() not in (
                "ТРЕБУЕТСЯ ФАКТУРА", "ТРЕБУЕТСЯ СРОК", "ТРЕБУЕТСЯ СУММА", "ТРЕБУЕТСЯ ПРОВЕРКА",
                "ТРЕБУЕТСЯ ПЕРЕЧЕНЬ РЕГИОНОВ", "ТРЕБУЕТСЯ НОРМАТИВНОЕ ОСНОВАНИЕ"):
            notes.append(f"неизвестная метка «{raw}» — записана как «{MARKERS[0]}»")
        gaps.append(Gap(marker=marker, suggestion=_text_of(item.get("suggestion"))))

    return bullets, refs, gaps, notes


def fix_letters(data: dict, words: set[str] | None = None) -> None:
    """Слова вперемешку в тексте и в недостающем — к одному алфавиту, где это однозначно:
    буквы-двойники, а с корпусом (`latin.corpus`) — и транслит, дающий слово человека.

    Остальные («zadачам» без такого слова в материалах) остаются: `foreign_in` их увидит,
    и они уйдут в круг правки.
    """
    for key in ("bullets", "gaps"):
        if data.get(key) is None:
            continue
        # строка вместо записи, запись вместо списка — приводим к записям, чтобы править на месте
        items = _records(data[key])
        for item in items:
            for field in ("text", "suggestion"):
                if isinstance(item.get(field), str):
                    item[field] = latin.fix_mixed(item[field], words)[0]
        data[key] = items


def foreign_in(data: dict, known: set[str]) -> list[str]:
    """Английские слова в том, что уйдёт в документ: тексты тезисов и предложения
    по недостающей фактуре. Слова, которые человек дал сам (материалы, тема, правила), —
    не в счёт: см. `latin.py`."""
    found: list[str] = []
    for item in _records(data.get("bullets")) + _records(data.get("gaps")):
        for key in ("text", "suggestion"):
            for word in latin.foreign(_text_of(item.get(key)), known):
                if word not in found:
                    found.append(word)
    return found


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _skeleton(data: dict) -> tuple:
    """Что круг правки менять не вправе: сколько тезисов, какие места под фактуру
    и какие числа стоят в тексте. Слова он переписывает, это — нет."""
    bullets = [_text_of(b.get("text")) for b in _records(data.get("bullets"))]
    text = "\n".join(bullets)
    markers = sorted(marker_name(m.group(1)) for m in MARKER_RE.finditer(text))
    gaps = sorted(marker_name(_text_of(g.get("marker"))) for g in _records(data.get("gaps")))
    numbers = sorted(_NUMBER.findall(text))
    return len([b for b in bullets if b]), markers, gaps, numbers


def _fix_language(system: str, data: dict, words: list[str]) -> tuple[dict, str]:
    """Один круг: «перепиши эти места по-русски». Возвращает (ответ, почему не принят).

    Результат принимается, только если структура цела — то же число тезисов, те же
    места [ТРЕБУЕТСЯ …] и те же числа (ревьюер, Н2): иначе вместо «чистого» текста
    получили бы потерянный тезис или потерянную пометку. Не принят, не разобрался,
    не успел до срока — остаётся первый ответ, и человек видит почему.
    """
    request = (
        "В тексте тезисов остались слова латиницей, которых нет в представленных материалах, "
        "или слова, где русские и латинские буквы вперемешку: " + ", ".join(words[:30]) + ".\n"
        "Перепиши эти места по-русски, каждое слово целиком русскими буквами. Число тезисов, их порядок, смысл, числа и маркеры "
        "[ТРЕБУЕТСЯ …] не меняй, остальное оставь как есть. Верни ответ целиком в том же виде.\n\n"
        "ТЕКУЩИЙ ОТВЕТ:\n" + json.dumps(data, ensure_ascii=False))
    try:
        fixed = parse_json(_call(system, request))
    except Cancelled:
        raise
    except IskraError as error:
        return data, f"круг правки не удался ({error})"
    fix_letters(fixed, words_known(data))
    if _skeleton(fixed) != _skeleton(data):
        return data, ("круг правки изменил состав тезисов, пометки или числа — "
                      "оставлен первый вариант")
    return fixed, ""


def words_known(data: dict) -> set[str]:
    """Русские слова самого ответа: для транслита в круге правки корпус тот же, что и в первом."""
    return latin.corpus(*(_text_of(b.get("text")) for b in _records(data.get("bullets"))))


def write(thesis: Thesis, rules_text: str, person: str, sources_text: str) -> dict:
    """Написать тезисы. Возвращает разобранный ответ и замечания к нему.

    Шаги отмечаются в управлении прогоном — их видит человек: «Пишу тезисы»
    на время прогона, «Проверяю ответ» — пока он приводится к схеме.
    """
    ctl = control()
    ctl.stage("writing")
    system = _system(rules_text, person)
    answer = _call(system, _user(thesis, sources_text))
    if ctl.cancelled():
        raise Cancelled("Написание отменено")
    ctl.stage("checking")
    data = parse_json(answer)
    # корпус русских слов: материалы, тема, пожелания, правила и сам ответ — по нему транслит
    # («zadанию» при «задание» в справке) исправляется без вопроса (задача 22.09, п. 4)
    russian = latin.corpus(sources_text, thesis.topic, thesis.ask, rules_text) | words_known(data)
    fix_letters(data, russian)
    # «свои» латинские слова — то, что человек дал сам: материалы, тема, пожелания, правила
    known = latin.known_words(sources_text, thesis.topic, thesis.event, thesis.ask, rules_text)
    fixed_round, why_not = False, ""
    words = foreign_in(data, known)
    if words:
        # английское или буквы вперемешку — один круг правки, расход в тот же счёт задания
        ctl.stage("polishing")
        data, why_not = _fix_language(system, data, words)
        fixed_round = True
        if ctl.cancelled():
            raise Cancelled("Написание отменено")
        ctl.stage("checking")
    bullets, refs, gaps, notes = check_schema(data)
    left = foreign_in(data, known)
    if left:
        tail = f" ({why_not})" if why_not else (" (остались и после круга правки)"
                                                  if fixed_round else "")
        mixed = [w for w in left if latin.mixed(w) and not latin.hybrid(w)]
        english = [w for w in left if w not in mixed]
        if english:
            notes.append("Проверьте: английские слова — " + ", ".join(english[:12]) + tail)
        if mixed:
            notes.append("Поправьте: слова наполовину латиницей — " + ", ".join(mixed[:12])
                          + tail + ". Пока они в тексте, документ не собирается")
    elif fixed_round:
        notes.append("английские слова «Искра» переписала по-русски вторым кругом")

    if not rules_text.strip():
        notes.append("правила для этого человека ещё не заданы — текст черновой")

    # оформление: Markdown из текста становится жирным и курсивом, выделение пункта — тоже
    html = richtext.html_from_bullets(bullets)
    for bullet in bullets:
        bullet.text = richtext.strip_markdown(bullet.text)
    return {"bullets": bullets, "html": html, "refs": refs, "gaps": gaps, "notes": notes}
