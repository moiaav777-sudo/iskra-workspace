"""«Искра» собирает сведения объективки.

Прогон фоновый, как у «Брифа», «Тезисов» и A3: POST /api/v1/runs с `instructions` (правила)
и `message` (что ищем), ответ по JSON-схеме (`response_format: json_schema`, `model.schema()`),
затем опрос GET /api/v1/runs/{id} до конца. Схему «Искра» может не принять — тогда сначала
без строгости, потом без схемы (как `brief/app/iskra.py`), формат проверяет программа.
Расход токенов считается при любом исходе: брошенный прогон снимается (DELETE) и его
расход забирается (`_settle`, как `theses/app/iskra.py`).

Три способа (решение пользователя 05.10):
  name — по ФИО (и подсказке): «Искра» ищет в открытых источниках, только официальных и надёжных,
         и возвращает источники со ссылками (refs) — как «Тезисы» без материалов;
  doc  — по справке: сведения только из текста документа, без интернета; источник — «справка»;
  doc + web — справка, а недостающее — из открытых источников с пометкой, откуда взято.
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

from . import config, iskra_common, model

RUNS_PATH = "/api/v1/runs"
POLL_MIN, POLL_MAX = 2.0, 5.0
DEADLINE = 720.0              # 12 минут: поиск в открытых источниках бывает долгим; дольше — снимаем
POLL_FAILS = 3
MAX_SOURCE_CHARS = 120_000


class IskraError(RuntimeError):
    """«Искра» не ответила или ответила не тем. Текст показывается человеку."""


class NoKey(IskraError):
    pass


class Cancelled(IskraError):
    pass


class Control:
    """Связь прогона с заданием: шаг, отмена, срок и расход."""

    def __init__(self, on_stage=None, cancelled=None, deadline: float | None = None):
        self._on_stage = on_stage
        self._cancelled = cancelled
        self.deadline = DEADLINE if deadline is None else deadline
        self.until = time.monotonic() + self.deadline
        self.spent = {"in": 0, "out": 0}

    def stage(self, text: str) -> None:
        if self._on_stage:
            self._on_stage(text)

    def cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled())

    def add(self, spent: dict) -> None:
        self.spent["in"] += int(spent.get("in", 0) or 0)
        self.spent["out"] += int(spent.get("out", 0) or 0)


def read_key() -> str:
    return config.env("ISKRA_API_KEY")


def key_ready() -> bool:
    return bool(read_key())


def _base() -> str:
    base = config.env("ISKRA_URL", config.ISKRA_URL_DEFAULT).strip().rstrip("/") or config.ISKRA_URL_DEFAULT
    for tail in ("/api/v1/chat", "/api/v1"):
        if base.endswith(tail):
            base = base[: -len(tail)]
    return base


def _http(method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
    """Один обмен с «Искрой». В тестах подменяется — сети в них нет."""
    key = read_key()
    if not key:
        raise NoKey("Ключ «Искры» не настроен — его записывает портал при выкладке сервиса")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(_base() + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "Authorization": "Bearer " + key})
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
        return error.code, payload if isinstance(payload, dict) else {"error": str(payload)[:300]}
    except urllib.error.URLError as error:
        raise IskraError(f"«Искра» недоступна: {error.reason}") from error
    except (TimeoutError, json.JSONDecodeError, http.client.HTTPException, ConnectionError, OSError) as error:
        raise IskraError(f"«Искра» не ответила: {error}") from error


def _answer_of(run: dict) -> str:
    """Текст ответа: структурированный output (объект) или строка — лежат под разными именами."""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    for out in (result.get("output"), run.get("output")):
        if isinstance(out, (dict, list)):
            return json.dumps(out, ensure_ascii=False)
        if isinstance(out, str) and out.strip():
            return out
    for key in ("answer", "text", "result"):
        for src in (result, run):
            value = src.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _settle(run_id: str, last: dict) -> dict:
    """Снять брошенный прогон и забрать его последний расход (расход накопительный — берём свежий)."""
    path = f"{RUNS_PATH}/{urllib.parse.quote(run_id)}"
    for method in ("DELETE", "GET"):
        try:
            status, payload = _http(method, path, timeout=20)
        except IskraError:
            continue
        if status < 400 and isinstance(payload, dict):
            run = iskra_common.unwrap(payload)
            if any(iskra_common.usage(run)[k] for k in ("in", "out")):
                return run
    return last or {}


def _schema_refused(status: int, payload) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower() if payload else ""
    return status == 422 or (status == 400 and any(w in text for w in ("response_format", "schema", "json")))


def call(ctl: Control, instructions: str, message: str, schema: dict | None = None) -> str:
    """Один прогон: запустить, дождаться, вернуть текст ответа. Расход — в ctl при любом исходе."""
    strict = True
    while True:
        body = {"message": message, "instructions": instructions}
        if schema is not None:
            fmt = {"name": "profile", "schema": schema}
            if strict:
                fmt["strict"] = True
            body["response_format"] = {"type": "json_schema", "json_schema": fmt}
        status, payload = _http("POST", RUNS_PATH, body, timeout=120)
        if schema is not None and _schema_refused(status, payload):
            if strict:
                strict = False                     # сперва без строгости
            else:
                schema = None                      # потом без схемы — формат проверит программа
            continue
        break
    if status not in (200, 201, 202):
        raise IskraError(iskra_common.human_error(status, payload))
    run = iskra_common.unwrap(payload if isinstance(payload, dict) else {})
    run_id = str(run.get("run_id") or run.get("id") or "")
    if not run_id:
        raise IskraError("«Искра» не выдала номер прогона")

    pause, last, fails = POLL_MIN, run, 0

    def give_up(error: IskraError):
        ctl.add(iskra_common.usage(_settle(run_id, last)))
        raise error

    def check_limits():
        if ctl.cancelled():
            give_up(Cancelled("Сбор отменён"))
        if time.monotonic() > ctl.until:
            give_up(IskraError(f"«Искра» не ответила за {max(1, int(ctl.deadline // 60))} мин — прогон снят. "
                               "Попробуйте ещё раз"))

    while True:
        check_limits()
        waited = 0.0
        while waited < pause:
            time.sleep(0.25)
            waited += 0.25
            check_limits()
        try:
            status, payload = _http("GET", f"{RUNS_PATH}/{urllib.parse.quote(run_id)}", timeout=60)
        except IskraError as error:
            fails += 1
            if fails >= POLL_FAILS:
                give_up(IskraError(f"{error}. Связь с «Искрой» рвалась {fails} раза подряд — прогон снят"))
            pause = POLL_MAX
            continue
        fails = 0
        if status == 429:
            pause = POLL_MAX
            continue
        if status >= 400:
            give_up(IskraError(iskra_common.human_error(status, payload)))
        last = iskra_common.unwrap(payload if isinstance(payload, dict) else {})
        state = iskra_common.state_of(last)
        if iskra_common.is_done(last):
            ctl.add(iskra_common.usage(last))
            answer = _answer_of(last)
            if not answer:
                raise IskraError("«Искра» закончила, но ответа не прислала")
            return answer
        if state == "interaction_required":
            # вопрос «Искры» («о каком Кравцове речь?») — прогон снимаем, человек уточнит подсказку
            question = ""
            inter = last.get("interaction")
            if isinstance(inter, dict):
                question = str(inter.get("message") or inter.get("text") or inter.get("question") or "")
            give_up(IskraError("«Искра» просит уточнить" + (f": «{question[:200]}»" if question else "")
                               + ". Добавьте подсказку (должность, ведомство) и запустите сбор снова"))
        if iskra_common.is_failed(last):
            ctl.add(iskra_common.usage(last))
            raise IskraError(f"«Искра» прервала прогон: {iskra_common.trouble(last)}. Попробуйте ещё раз")
        stage_text = ""
        events = last.get("events")
        if isinstance(events, list) and events:
            ev = events[-1]
            if isinstance(ev, dict) and ev.get("kind") == "tool":
                stage_text = "«Искра» ищет в источниках…"
        if stage_text:
            ctl.stage(stage_text)
        pause = min(POLL_MAX, pause + 0.5)


# ── что просим ──────────────────────────────────────────────────────────

MODE_RULES = (
    "РЕЖИМ РАБОТЫ — ОБЯЗАТЕЛЬНО:\n"
    "• Это машинный запрос от программы. Не подключай навыки, не запускай код, не создавай документы "
    "Word/PDF — документ соберёт программа.\n"
    "• Не задавай уточняющих вопросов: всё нужное уже в сообщении.\n"
    "• Ответ — только один JSON-объект по схеме, без пояснений до и после.\n\n"
)

TASK = """Ты готовишь объективку — краткую официальную справку о человеке на одну страницу A4.

ЯЗЫК. Всё — по-русски. Латиница только в собственных названиях и адресах сайтов.

ЧТО ЗАПОЛНИТЬ (поля JSON):
- fio — фамилия, имя, отчество полностью, в именительном падеже;
- birth_date — дата рождения, например «12 марта 1970 г.»; birth_place — место рождения, например «г. Курск»;
- position — текущая должность так, как она звучит в официальной биографии; organization — организация,
  если её нет в названии должности;
- category — deputy (депутат, сенатор), official (государственный служащий, руководитель органа власти),
  scientist (учёный, ректор, руководитель научной организации), other;
- key_facts — ключевые данные строками «label: value»: для депутата — фракция, регион(ы) или избирательный
  округ, комитет и должность в нём; для чиновника — ведомство (если его нет в должности), классный чин;
  для учёного — учёная степень, учёное звание, членство в РАН; для остальных — самое существенное;
- powers — полномочия и должности по совместительству (членство в советах, комиссиях), по одной на пункт;
- education — образование: year — год окончания, text — учебное заведение и специальность;
  также учёные степени с годом защиты;
- career — опыт работы по возрастанию лет: period — «1995–1998» или «с 2021 г.», text — должность
  и организация, кратко;
- awards — награды, почётные звания, поощрения: year и text, сначала государственные;
- photo_url — ссылка на официальное фото на официальном сайте, если есть, иначе пустая строка
  (программа сама ничего не скачивает — ссылка нужна человеку);
- refs — источники: name — чей это источник (сайт ведомства, Госдумы, вуза…), url — прямая ссылка
  на страницу, about — что оттуда взято;
- note — что не удалось найти, сомнения, однофамильцы, расхождения между источниками.
У каждого пункта списков поле src — откуда он: «справка» или ссылка на источник.

ПРАВИЛА:
- Ничего не выдумывай. Чего нет — пустая строка или пустой список. Сомнительное (источники расходятся,
  нет уверенности, что это тот же человек) — не пиши, а отметь в note.
- Кратко: вся объективка должна уложиться в одну страницу A4 шрифтом 14. Опыт работы — до 12–15 строк,
  мелкие должности в одной организации объединяй; награды — главные, до 6–8; формулировки — без лишних слов.
- Даты и годы — цифрами; периоды — через короткое тире «–».
"""

WEB_RULES = """ИСТОЧНИКИ — ТОЛЬКО ОФИЦИАЛЬНЫЕ И НАДЁЖНЫЕ: сайты органов власти и ведомств, Государственной Думы
и Совета Федерации, правительств регионов, вузов и научных организаций, официальные биографии,
государственные информационные агентства. Энциклопедии, блоги, соцсети, форумы и сайты-агрегаторы
можно использовать только чтобы найти официальный источник; сам факт подтверждай официальным.
Каждый факт из открытых источников — с источником в refs и ссылкой в src пункта. Найденное
пересказывай по-русски. Не нашёл подтверждения — не пиши."""


def instructions(mode: str, web: bool) -> str:
    text = MODE_RULES + TASK
    if mode == "name":
        text += "\nОТКУДА БРАТЬ: ищи в открытых источниках в интернете.\n" + WEB_RULES
    elif web:
        text += ("\nОТКУДА БРАТЬ: сначала — всё, что есть в справке (src = «справка»). Чего в справке нет — "
                 "найди в открытых источниках в интернете; у каждого найденного пункта src — ссылка на источник, "
                 "сам источник — в refs. Если открытые источники расходятся со справкой — оставь справку "
                 "и отметь расхождение в note.\n" + WEB_RULES)
    else:
        text += ("\nОТКУДА БРАТЬ: ТОЛЬКО из текста справки в сообщении. В интернете не ищи. "
                 "src у каждого пункта — «справка», refs — пустой список, photo_url — пустая строка.")
    return text


def message(mode: str, fio: str, hint: str, source_name: str, source_text: str) -> str:
    if mode == "name":
        parts = [f"Объективка на: {fio.strip()}."]
        if hint.strip():
            parts.append(f"Подсказка, чтобы не спутать с однофамильцами: {hint.strip()[:500]}.")
        else:
            parts.append("Подсказки нет. Если по этому имени находятся разные известные люди и выбрать "
                         "нельзя — верни пустые поля, а в note перечисли, кого нашёл (должность, ведомство).")
        return "\n".join(parts)
    head = f"СПРАВКА (файл «{source_name}»):\n"
    extra = f"Подсказка: {hint.strip()[:500]}\n" if hint.strip() else ""
    who = f"Человек: {fio.strip()}\n" if fio.strip() else ""
    return who + extra + head + (source_text or "")[:MAX_SOURCE_CHARS]


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
    if data.get("error") and len(data) == 1:
        raise IskraError(f"«Искра» не смогла: {str(data['error'])[:300]}")
    return data


def fill(ctl: Control, mode: str, fio: str = "", hint: str = "", web: bool = False,
         source_name: str = "", source_text: str = "") -> dict:
    """Собрать сведения объективки. Возвращает приведённые к форме сведения и замечания."""
    ctl.stage("«Искра» ищет в открытых источниках…" if mode == "name" or web else "«Искра» читает справку…")
    answer = call(ctl, instructions(mode, web), message(mode, fio, hint, source_name, source_text), model.schema())
    if ctl.cancelled():
        raise Cancelled("Сбор отменён")
    ctl.stage("Проверяю ответ…")
    data = model.clean(parse_json(answer), sort=True)
    notes = []
    if mode == "doc" and not web:
        # без интернета: всё из справки, ссылки «Искры» (если она их всё же дала) не берём
        for key in ("key_facts", "powers", "education", "career", "awards"):
            for row in data[key]:
                row["src"] = model.DOC_SOURCE
        if data["refs"] or data["photo_url"]:
            notes.append("ссылки из ответа «Искры» отброшены: сбор шёл только по справке")
        data["refs"], data["photo_url"] = [], ""
    if mode == "doc":
        data["refs"] = [r for r in data["refs"] if r["url"]]
        data["refs"].insert(0, {"name": model.DOC_SOURCE, "url": "", "about": source_name or "приложенный документ"})
    if mode == "name" and fio.strip() and data["fio"] and data["fio"].lower() != fio.strip().lower():
        notes.append(f"«Искра» записала ФИО как «{data['fio']}» (искали «{fio.strip()}») — проверьте")
    if not data["fio"]:
        if mode == "name" and fio.strip() and any(data[k] for k in ("position", "career", "education")):
            data["fio"] = fio.strip()
        else:
            notes.append("ФИО не нашлось — впишите его в карточку")
    if mode == "name":
        links = [r for r in data["refs"] if r["url"]]
        if not links and any(data[k] for k in ("career", "education", "position")):
            notes.append("«Искра» не дала ни одной ссылки на источник — проверьте сведения перед выгрузкой")
    return {"data": data, "notes": notes}


# ── очередь прогонов ────────────────────────────────────────────────────
# Одновременно — не больше двух прогонов на весь сервис: пакетная переработка десяти справок
# не должна упереться в лимит фоновых прогонов «Искры» (429 too_many_runs). Остальные ждут
# в очереди, человек видит «в очереди».
SLOTS = threading.BoundedSemaphore(2)
