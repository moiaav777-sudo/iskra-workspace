"""Вызов «Искры»: справка → JSON листа и круг исправления замечаний.

Повторяет то, как это делает «Бриф»: фоновый прогон `/api/v1/runs`, опрос до
готовности, ответ по строгой схеме. Ключ читается из `service/.env`
(`ISKRA_API_KEY`) при каждом вызове — портал переписывает его без перезапуска.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any

from . import config, iskra_common

BASE = "https://cloud.iskrabot.ru"
# состояния и разбор ответа — из общего модуля (portal/shared/iskra_common.py)
TERMINAL_OK = iskra_common.TERMINAL_OK
TERMINAL_BAD = iskra_common.TERMINAL_BAD
POLL_MIN, POLL_MAX, DEADLINE = 2.0, 5.0, 900.0

AGENT_RULES = (
    "Ты работаешь как API. Не подключай навыки, не запускай команды, не создавай файлов.\n"
    "Ответ — один объект JSON без пояснений, без ```-обрамления."
)

PROBLEM = {
    401: "«Искра» не приняла ключ. Обновите ключ в портале: Администрирование → Ключ Искры.",
    403: "У ключа нет прав на фоновые прогоны. Проверьте ключ в портале.",
    409: "Предыдущий разбор ещё идёт. Подождите и повторите.",
    413: "Справка слишком большая для «Искры». Сократите текст.",
    429: "«Искра» ограничила частоту запросов. Повторите через минуту.",
}


class SparkError(Exception):
    """Разбор не удался. Несёт расход токенов: неудачный прогон тоже стоил денег
    и по контракту с порталом (п. 4) попадает в журнал."""

    def __init__(self, message: str, spent: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.spent = spent or {"in": 0, "out": 0, "total": 0}


def _key() -> str:
    key = config.read_env("ISKRA_API_KEY", "").strip()
    if not key:
        raise SparkError(
            "Ключ «Искры» не настроен. Портал записывает его в service/.env при сохранении "
            "ключа в разделе «Администрирование», либо впишите ISKRA_API_KEY вручную."
        )
    return key


def _call(method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
    url = config.read_env("ISKRA_URL", BASE).rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {_key()}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"detail": raw[:300]}
        return e.code, payload
    except (urllib.error.URLError, OSError) as e:
        raise SparkError(f"«Искра» недоступна: {e}") from None


def _unwrap(payload: dict) -> dict:
    return iskra_common.unwrap(payload)


def _answer(payload: dict) -> str:
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("output", "answer"):
            value = result.get(key)
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.strip():
                return value
    for key in ("output", "answer"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(result, str):
        return result
    raise SparkError("«Искра» вернула пустой ответ")


# Разделы свода, нужные кругу доработки. Круг правит готовый лист, а не собирает
# новый: ему не нужно понимать документ, выбирать блоки под содержание или
# проходить чек-лист первой сборки. Нужно — как сокращать, что значит priority,
# какие бывают поля и типы блоков, что с числами и какой ответ ждут.
# «### Цвет» обязателен, хоть по названию и кажется лишним: в нём не только
# палитра и правило «не больше трёх акцентных», но и схемы всех шести диаграмм —
# `mode`, `highlight`, `center_value`, `null`, цвет строки третьим элементом.
# Без него правка могла сломать диаграмму, а проверка кругов этого не видит
# (замечание ревьюера К2, RC-2026-09-21-3).
FIX_SECTIONS = (
    "### 3. Расставь по важности",
    "### 4. Сожми текст",
    "## Правила данных",
    "## Схема JSON",
    "### Общие поля любого блока",
    "### Типы блоков",
    "### Диаграммы",
    "### Цвет",
    "## Формат ответа",
)


def _prompt() -> str:
    prompt = config.A3_DIR / "a3-PROMPT.md"
    if not prompt.exists():
        prompt = config.A3_DIR / "skill" / "PROMPT.md"
    if not prompt.exists():
        raise SparkError("Не найден a3-PROMPT.md с правилами листа")
    return prompt.read_text(encoding="utf-8")


def _sections(text: str, wanted: tuple[str, ...]) -> str:
    """Выбранные разделы свода по заголовкам, в том же порядке.

    Не нашёлся хоть один — отдаём пусто: свод переписали, и лучше прислать его
    целиком, чем гадать, какая часть правил пропала.
    """
    heads = list(re.finditer(r"^#{2,3} .+$", text, re.M))
    found: dict[str, str] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        found[head.group(0).strip()] = text[head.start():end].rstrip()
    if any(name not in found for name in wanted):
        return ""
    return "\n\n".join(found[name] for name in wanted)


def instructions(for_fix: bool = False) -> str:
    """Правила листа — тот же a3-PROMPT.md, что люди дают любой нейросети.

    Для круга доработки — только нужные ему разделы (см. FIX_SECTIONS).
    """
    text = _prompt()
    if for_fix:
        text = _sections(text, FIX_SECTIONS) or text
    return AGENT_RULES + "\n\n" + text


def _clean(value: Any) -> Any:
    """Служебные поля движка («_chart», «_t» и прочие с подчёркиванием) «Искре» не нужны."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


SHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["header", "blocks"],
    "properties": {
        "orientation": {"type": "string", "enum": ["landscape", "portrait"]},
        "theme": {"type": "string", "enum": ["soft", "strict"]},
        "header": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "eyebrow": {"type": "string"},
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "meta": {"type": "array", "items": {"type": "string"}},
            },
        },
        "blocks": {
            "type": "array",
            # Минимума нет намеренно: строгая схема роняет весь прогон целиком,
            # а тонкий лист — поправимо и видно в проверке под листом.
            # Сколько блоков нужно, сказано в правилах (a3-PROMPT.md).
            "minItems": 1,
            "items": {"type": "object", "required": ["type"], "properties": {"type": {"type": "string"}}},
        },
        "source": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "date": {"type": "string"}},
        },
    },
}


# где «Искра» держит причину отказа: в разных прогонах поле называется по-разному
BAD_KEYS = ("error", "last_error", "failure", "failure_reason", "reason", "status_reason",
            "detail", "message")


def _trouble(run: dict[str, Any], state: str) -> str:
    """Причина отказа: собираем всё, что похоже на объяснение, а не одно поле.

    «Искра» умеет ответить просто «run failed» — тогда объяснения нет вовсе,
    и это тоже надо видеть, а не выдавать за причину.
    """
    found: list[str] = []
    for key in BAD_KEYS:
        value = run.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail") or json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            if value.strip():
                found.append(value.strip())
        elif value not in (None, "", {}, []):
            found.append(json.dumps(value, ensure_ascii=False))
    return "; ".join(dict.fromkeys(found))[:300] or state


def _log_failure(run: dict[str, Any], state: str, schema: bool, run_id: str = "") -> None:
    """В журнал сервиса — только служебные поля прогона.

    Целиком ответ не пишем: в нём может оказаться текст справки, а справка на
    сервере не сохраняется. Имён полей хватает, чтобы в следующий раз понять,
    где «Искра» спрятала причину.
    """
    print(f"[spark] прогон {run_id} не удался: состояние {state},"
          f" строгая схема {'да' if schema else 'нет'},"
          f" причина: {_trouble(run, state)},"
          f" поля ответа: {sorted(run.keys())}", flush=True)


def _schema_trouble(text: str) -> bool:
    """Ответ «Искры» о том, что не сложился строгий формат."""
    low = text.lower()
    return any(word in low for word in ("structured", "schema", "response_format", "утилитарн"))


def _number(raw: dict[str, Any], *names: str) -> int:
    for name in names:
        value = raw.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _usage(run: dict[str, Any]) -> dict[str, int]:
    """Расход токенов за прогон: «Искра» называет поля по-разному, берём любые."""
    raw = run.get("usage")
    if not isinstance(raw, dict):
        result = run.get("result")
        raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return {"in": 0, "out": 0, "total": 0}
    got_in = _number(raw, "prompt_tokens", "input_tokens", "tokens_in", "in")
    got_out = _number(raw, "completion_tokens", "output_tokens", "tokens_out", "out")
    total = _number(raw, "total_tokens", "tokens", "total") or got_in + got_out
    return {"in": got_in, "out": got_out, "total": total}


def _add(spent: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for key in ("in", "out", "total"):
        spent[key] = spent.get(key, 0) + more.get(key, 0)
    return spent


def _post(body: dict[str, Any], spent: dict[str, int]) -> tuple[int, dict]:
    """Запуск прогона. Связь оборвалась — отказ несёт расход, накопленный до него.

    Первый запуск ничего не стоил, а вот повтор без схемы идёт после сорвавшегося
    прогона, за который уже заплачено.
    """
    try:
        return _call("POST", "/api/v1/runs", body, timeout=120)
    except SparkError as e:
        raise SparkError(str(e), spent) from None


def _run(message: str, timeout: float = DEADLINE, schema: bool = True,
         spent: dict[str, int] | None = None,
         rules: str | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """Прогон «Искры». Возвращает JSON листа и расход токенов за все попытки."""
    spent = spent if spent is not None else {"in": 0, "out": 0, "total": 0}
    rules = rules if rules is not None else instructions()
    body: dict[str, Any] = {"message": message, "instructions": rules}
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "a3_sheet", "schema": SHEET_SCHEMA}}
    status, payload = _post(body, spent)
    if schema and (status == 422 or (status == 400 and _schema_trouble(json.dumps(payload, ensure_ascii=False)))):
        # «Искра» не приняла схему — просим просто JSON, правила и так в инструкции
        body.pop("response_format")
        schema = False
        status, payload = _post(body, spent)
    if status not in (200, 201, 202):
        raise SparkError(PROBLEM.get(status, f"«Искра» ответила {status}: {str(payload)[:200]}"), spent)
    run_id = _unwrap(payload).get("run_id") or _unwrap(payload).get("id")
    if not run_id:
        raise SparkError("«Искра» не выдала номер прогона", spent)

    deadline, pause, last = time.monotonic() + timeout, POLL_MIN, None
    while time.monotonic() < deadline:
        time.sleep(pause)
        pause = min(POLL_MAX, pause + 0.5)
        try:
            status, payload = _call("GET", f"/api/v1/runs/{run_id}", timeout=60)
        except SparkError as e:
            # связь оборвалась посреди прогона, а он уже шёл и тратил токены
            raise SparkError(str(e), _add(spent, _usage(last or {}))) from None
        if status == 429:
            continue
        if status >= 400:
            raise SparkError(PROBLEM.get(status, f"«Искра» ответила {status}"),
                             _add(spent, _usage(last or {})))
        run = _unwrap(payload)
        last = run
        state = str(run.get("status") or "").lower()
        if state in TERMINAL_OK:
            _add(spent, _usage(run))
            try:
                return parse_sheet(_answer(run)), spent
            except Exception as e:  # noqa: BLE001 — любой сбой разбора, не только ожидаемый
                # прогон прошёл и токены потрачены, а ответ негодный (не JSON, пусто,
                # нет блоков, поле не того вида) — расход уходит в журнал вместе с отказом
                why = str(e) if isinstance(e, SparkError) else f"ответ «Искры» не разобран: {e}"
                raise SparkError(why, spent) from None
        if state in TERMINAL_BAD:
            _add(spent, _usage(run))   # неудачный прогон тоже стоил токенов
            trouble = _trouble(run, state)
            _log_failure(run, state, schema, str(run_id))
            # Первый подозреваемый — строгий формат: «Искра» часто не говорит, что
            # именно не сложилось («run failed» и всё). Поэтому повторяем без схемы
            # на любой отказ, а не только на узнаваемый, — так делает «Бриф».
            # Повтор ровно один: во втором заходе schema уже False.
            if schema:
                return _run(message, timeout=timeout, schema=False, spent=spent, rules=rules)
            raise SparkError(
                f"«Искра» прервала разбор: {trouble}. Попробуйте ещё раз — прогон часто"
                " срывается на стороне «Искры»; если повторится, пришлите справку покороче",
                spent)
    # не дождались: прогон снимаем, а то, что он успел потратить, считаем —
    # «Искра» пишет расход и в незаконченный прогон
    if last is not None:
        _add(spent, _usage(last))
    try:
        _call("DELETE", f"/api/v1/runs/{run_id}", timeout=20)
    except SparkError:
        pass    # снять прогон не вышло — не повод терять расход и подменять причину отказа
    raise SparkError("«Искра» не ответила за отведённое время", spent)


def parse_sheet(text: str) -> dict[str, Any]:
    """Терпимый разбор: убираем ```-обрамление и текст вокруг объекта."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1] if "```" in clean[3:] else clean[3:]
        clean = clean.split("\n", 1)[1] if clean[:4].lower().startswith("json") else clean
    start, end = clean.find("{"), clean.rfind("}")
    if start == -1 or end <= start:
        raise SparkError("«Искра» вернула не JSON")
    try:
        data = json.loads(clean[start:end + 1])
    except json.JSONDecodeError as e:
        raise SparkError(f"JSON от «Искры» не разобран: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list) or not data["blocks"]:
        raise SparkError("В ответе «Искры» нет блоков листа")
    header = data.get("header")
    if not isinstance(header, dict):
        # «Искра» иногда присылает шапку строкой — это и есть название листа
        data["header"] = {"title": header.strip()} if isinstance(header, str) and header.strip() else {}
    data["header"].setdefault("title", "Лист без названия")
    data.setdefault("orientation", "landscape")
    return data


def is_fix_of(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Вернулся ли с круга тот же лист, а не заглушка вместо него.

    Круг доработки сокращает лист, но не подменяет его. Если блоков стало
    заметно меньше или текст схлопнулся, «Искра» прислала другой лист —
    например «Отсутствуют исходные данные», — и сохранять его поверх работы
    человека нельзя: версия перезапишется, а вернуть придётся руками.
    """
    was = before.get("blocks") or []
    now = after.get("blocks") or []
    if len(now) < max(1, math.ceil(len(was) * 0.6)):
        return False
    size_was = len(json.dumps(was, ensure_ascii=False))
    size_now = len(json.dumps(now, ensure_ascii=False))
    return size_now >= size_was * 0.4


def draft(text: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Справка → JSON листа и расход токенов."""
    return _run("Текст справки:\n\n" + text)


def fix(data: dict[str, Any], warnings: list[str]) -> tuple[dict[str, Any], dict[str, int]]:
    """Замечания самопроверки → исправленный JSON листа и расход токенов."""
    listed = "\n".join(f"- {w}" for w in warnings[:20])
    message = (
        "Лист собран по этому JSON, но проверка вёрстки нашла замечания.\n"
        "Верни исправленный JSON того же листа.\n\n"
        "Главное: лист должен поместиться на одну страницу A3. Миллиметры в замечаниях —"
        " это результат замера вёрстки, а не оценка: если сказано, что не хватает 28 мм,"
        " сократить нужно не меньше, и лучше с запасом.\n"
        "Порядок действий, пока нехватка не закрыта:\n"
        "1) короче формулировки — подписи, заголовки и тексты пунктов;\n"
        "2) убрать повторы: одно число и один факт живут на листе в одном месте;\n"
        "3) объединить близкие пункты в один;\n"
        "4) второстепенным блокам проставить \"priority\": 3 — вёрстка снимет их сама;\n"
        "5) в крайнем случае убрать наименее важные пункты из самых объёмных блоков.\n"
        "Ничего не добавляй: новые блоки, пункты и пояснения делают лист только выше."
        " Структуру, числа и смысл оставшегося сохрани.\n\n"
        f"Замечания:\n{listed}\n\nТекущий JSON:\n"
        f"{json.dumps(_clean(data), ensure_ascii=False, separators=(',', ':'))}"
    )
    return _run(message, timeout=600, rules=instructions(for_fix=True))
