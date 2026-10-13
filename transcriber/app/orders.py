"""Перечень поручений из расшифровки — прогон «Искры» (`/api/v1/runs`, как саммари).

Решение пользователя 02.10: после разбора записи — три документа, третий из них перечень
поручений. «Искра» читает текст расшифровки и возвращает СТРОГО JSON
`{"orders": [{"text", "assignee", "deadline"}]}` — только поручения, которые звучат в
разговоре или прямо из него следуют; срок — только если назван. Ответ приводится к схеме
(`check_schema`, как у соседей — `moderator/app/llm_extract.py`): лишнее отбрасывается,
поручения без текста выкидываются, срок не придумывается.

Документ Word собирает общий модуль `orders_docx.py` (`portal/shared/orders_docx.py`).

Единый формат блоков (решение пользователя 13.10): человек правит поручение редактором с
оформлением, поручение несёт необязательный ключ `html` (очищенный `richtext_common.clean`), а `text`
тогда выводится из него. Word перечня строится по `text` — оформление в Word не идёт (эталон
`orders_docx.py` не меняем). Ответ «Искры» html не несёт.
"""
from __future__ import annotations

import json
import re
import secrets
from typing import Any, Callable

from . import orders_docx, richtext_common, summarizer

MAX_ORDERS = 100
MAX_TEXT = 2000
# HTML поручения с оформлением длиннее текста (теги, цвета) — предел с запасом в 5 раз;
# длиннее — оформление снимается, остаётся текст абзацами (обрезать HTML посреди тега нельзя)
MAX_HTML = 5 * MAX_TEXT

# Постоянный id поручения (ревью RC-2026-10-13-2, К-1): блок карточки привязан к поручению, а не к
# номеру — иначе после удаления соседнего поручения или правки коллеги набранное ложилось на чужое.
# Выдаётся при записи перечня (`ensure_ids`); правка человека его сохраняет (`from_human`).
ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def new_id() -> str:
    return secrets.token_hex(4)


def ensure_ids(items: list[dict[str, Any]], positional: bool = False) -> list[dict[str, Any]]:
    """У каждого поручения — свой id; нет его или повтор — выдаём. `positional=True` — при чтении
    старой записи: id по месту («o1», «o2»…), одинаковый при каждом чтении, пока перечень не
    сохранят (тогда он запишется)."""
    seen: set[str] = set()
    for n, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        oid = item.get("id")
        if not (isinstance(oid, str) and ORDER_ID.match(oid)) or oid in seen:
            oid = f"o{n}" if positional else new_id()
            while oid in seen:
                oid = new_id()
            item["id"] = oid
        seen.add(oid)
    return items

AGENT_RULES = (
    "Ты работаешь как API. Не подключай навыки, не запускай команды, не создавай файлов.\n"
    "Ответ — СТРОГО один JSON-объект без markdown, без ```-обрамления и без пояснений."
)

PROMPT = (
    "Ниже — дословная расшифровка записи совещания или разговора (возможны ошибки "
    "распознавания). Составь перечень поручений по итогам.\n"
    "Правила:\n"
    "1. Только поручения, которые прямо звучат в разговоре или прямо из него следуют. "
    "Ничего не выдумывай; поручений нет — верни пустой список.\n"
    "2. Официальная форма, одним предложением или абзацем: «Кому (И.О. Фамилия в дательном "
    "падеже) … сделать что … (результат)», например «Минпросвещения России (С.С. Кравцову) "
    "подготовить предложения по …». Ведомство или должность — если названы.\n"
    "3. assignee — короткая подпись ответственного («С.С. Кравцов», «Минпросвещения России»); "
    "не назван — пустая строка.\n"
    "4. deadline — срок в виде ДД.ММ.ГГГГ, только если он назван в разговоре; не назван — "
    "пустая строка. Не подставляй сроки от себя.\n"
    "5. Только короткое тире «–», длинное «—» не используй.\n"
    'Схема ответа: {"orders":[{"text":"…","assignee":"…","deadline":"ДД.ММ.ГГГГ или пусто"}]}\n\n'
    "Текст расшифровки:\n\n"
)

STRICT = (
    "Предыдущий ответ был не JSON. Ответь ЕЩЁ РАЗ и верни ТОЛЬКО один JSON-объект по схеме "
    '{"orders":[{"text":"…","assignee":"…","deadline":"…"}]}, без пояснений и markdown.\n\n'
)

# «Искра» иногда пишет в срок не дату, а отписку — это не срок
_NO_DEADLINE = {"", "-", "–", "нет", "не указан", "не указано", "не назван", "не названо",
                "отсутствует", "без срока", "н/д", "нд", "null", "none", "пусто", "не определён",
                "не определен"}


class OrdersError(summarizer.SummaryError):
    """Перечень не получился. Несёт расход токенов — неудачный прогон тоже стоил денег."""


class Cancelled(OrdersError):
    """Человек отменил обработку, пока «Искра» составляла перечень."""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(orders_docx.dash(value).split())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def normalize_deadline(value: Any) -> str:
    """ДД.ММ.ГГГГ (и ГГГГ-ММ-ДД из поля даты) → ДД.ММ.ГГГГ; отписка («не указан») → пусто;
    срок словами («начиная с 1 сентября 2026 г.») — как есть."""
    text = _text(value).strip()
    if text.lower().strip(" .") in _NO_DEADLINE:
        return ""
    parsed = orders_docx.parse_deadline(text)
    if parsed:
        return f"{parsed:%d.%m.%Y}"
    return text[:200]


def _html_and_text(item: dict[str, Any]) -> tuple[str, str]:
    """Поручение из редактора: очищенный HTML и текст из него (решение пользователя 13.10)."""
    raw = item.get("html")
    if not isinstance(raw, str):
        return "", _text(item.get("text"))[:MAX_TEXT]
    # предел — до разбора разметки: поручение на мегабайты не должно занимать сервер секундами
    # (ревью RC-2026-10-13-6, Н-3); дальше всё равно остаётся текст не длиннее MAX_TEXT
    raw = raw[:MAX_HTML * 5]
    if richtext_common.is_blank(raw):
        return "", _text(item.get("text"))[:MAX_TEXT]
    html = orders_docx.dash(richtext_common.clean(raw))
    full = _text(richtext_common.text(html))
    text = full[:MAX_TEXT]
    if len(full) > MAX_TEXT or len(html) > MAX_HTML:
        html = richtext_common.from_text(text)
    return html, text


def check_schema(result: Any, html: bool = False) -> list[dict[str, str]]:
    """Ответ «Искры» → список поручений по схеме. Не объект и не список — ValueError.

    `html=True` — правка человека: принимается ключ `html` (ответ «Искры» его не несёт)."""
    if isinstance(result, dict):
        raw = result.get("orders", [])
        if isinstance(raw, dict):
            raw = [raw]
    elif isinstance(result, list):
        raw = result
    else:
        raise ValueError("«Искра» вернула не объект JSON")
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        if html:
            styled, text = _html_and_text(item)
        else:
            styled, text = "", _text(item.get("text"))[:MAX_TEXT]
        if not text:
            continue
        order = {"text": text,
                 "assignee": _text(item.get("assignee"))[:200],
                 "deadline": normalize_deadline(item.get("deadline"))}
        if styled:
            order["html"] = styled
        oid = item.get("id")
        if html and isinstance(oid, str) and ORDER_ID.match(oid):
            order["id"] = oid                    # правка человека — id поручения сохраняется
        out.append(order)
        if len(out) >= MAX_ORDERS:
            break
    return out


def from_human(items: Any) -> list[dict[str, str]]:
    """Правка человеком: тот же разбор, что у ответа «Искры» (строки без текста — прочь),
    плюс оформление поручения (`html`, решение пользователя 13.10)."""
    if not isinstance(items, list):
        raise ValueError("Поручения — список")
    return check_schema({"orders": items}, html=True)


def parse_json(content: str) -> Any:
    """JSON из ответа: снять ```-обёртку, при необходимости вырезать объект из текста."""
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("«Искра» вернула не JSON")


def _add(spent: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for key in ("in", "out", "total"):
        spent[key] = spent.get(key, 0) + int(more.get(key, 0) or 0)
    return spent


def _key(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower().replace("ё", "е")).strip()


def merge(groups: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    """Поручения частей → один перечень по порядку, без дублей (одинаковый текст с точностью до
    регистра и знаков). У дубля берётся срок, если у первого его не было."""
    out: list[dict[str, str]] = []
    seen: dict[str, dict[str, str]] = {}
    for group in groups:
        for item in group:
            key = _key(item["text"])
            if key in seen:
                kept = seen[key]
                if not kept["deadline"] and item["deadline"]:
                    kept["deadline"] = item["deadline"]
                if not kept["assignee"] and item["assignee"]:
                    kept["assignee"] = item["assignee"]
                continue
            seen[key] = dict(item)
            out.append(seen[key])
    return out[:MAX_ORDERS]


def _extract_one(message: str, cancelled: Callable[[], bool]
                 ) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Один прогон (и одна строгая повторная попытка при ответе прозой) → поручения и расход."""
    spent: dict[str, int] = {"in": 0, "out": 0, "total": 0}
    answer, more = summarizer.run(message, AGENT_RULES, cancelled, error=OrdersError,
                                  cancel=Cancelled, what="перечень поручений")
    _add(spent, more)
    try:
        return check_schema(parse_json(answer)), spent
    except ValueError:
        pass
    if cancelled():
        raise Cancelled("Отменено: перечень поручений", spent)
    try:
        answer, more = summarizer.run(STRICT + message, AGENT_RULES, cancelled, error=OrdersError,
                                      cancel=Cancelled, what="перечень поручений")
    except OrdersError as e:
        e.spent = _add(dict(spent), e.spent)
        raise
    _add(spent, more)
    try:
        return check_schema(parse_json(answer)), spent
    except ValueError as e:
        raise OrdersError(f"«Искра» вернула перечень поручений не по схеме: {e}", spent) from None


def extract(text: str, cancelled: Callable[[], bool] = lambda: False
            ) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Текст расшифровки → (поручения, расход токенов за все попытки).

    Ответ прозой — одна повторная попытка со строгим напоминанием (как у «Совещания»);
    расход всех попыток складывается, в том числе при неудаче и отмене. Длинная транскрипция —
    по частям (`summarizer.split_text`), поручения частей объединяются без дублей (`merge`):
    середина записи не теряется (ревью RC-17, Н-4).
    """
    parts = summarizer.split_text(text)
    spent: dict[str, int] = {"in": 0, "out": 0, "total": 0}
    groups: list[list[dict[str, str]]] = []
    for n, part in enumerate(parts, start=1):
        head = PROMPT if len(parts) == 1 else PROMPT.replace(
            "Текст расшифровки:", f"Это часть {n} из {len(parts)} расшифровки — только поручения "
                                  f"из этой части. Текст части:")
        try:
            found, more = _extract_one(head + part, cancelled)
        except OrdersError as e:
            e.spent = _add(dict(spent), e.spent)
            raise
        _add(spent, more)
        groups.append(found)
    return merge(groups), spent
