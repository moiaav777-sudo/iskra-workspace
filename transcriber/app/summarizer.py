"""Саммари черновика расшифровки через «Искру»: фоновый прогон `/api/v1/runs` с опросом.

Устройство повторяет `a3/service/app/spark.py` и «Бриф» (прогон → опрос → ответ), но
проще: саммари — обычный текст, без схемы JSON. Ключ читается из `.env` рядом с сервисом
(`ISKRA_API_KEY`) при каждом вызове — портал переписывает его без перезапуска.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import config, iskra_common

BASE = "https://cloud.iskrabot.ru"
TERMINAL_OK = iskra_common.TERMINAL_OK
TERMINAL_BAD = iskra_common.TERMINAL_BAD
POLL_MIN, POLL_MAX, DEADLINE = 2.0, 5.0, 900.0

AGENT_RULES = (
    "Ты работаешь как API. Не подключай навыки, не запускай команды, не создавай файлов.\n"
    "Ответ — связный текст саммари на русском языке, без ```-обрамления и пояснений от себя."
)

PROMPT = (
    "Ниже — черновая расшифровка записи разговора или совещания (могут быть ошибки "
    "распознавания речи). Сделай краткое саммари: главные темы, принятые решения и "
    "договорённости, поручения (кто и что, если названо), открытые вопросы. Пиши по "
    "существу, только по тексту ниже, не придумывай того, чего в нём нет.\n\n"
    "Текст расшифровки:\n\n"
)

# Транскрипция длиннее — делится на части по абзацам (ревью RC-17, Н-4: раньше середина молча
# вырезалась): саммари каждой части, затем итоговое саммари по ним; поручения — по частям с
# объединением без дублей (`app/orders.py`). Ничего не выбрасывается.
MAX_CHARS = 120_000

PART_PROMPT = (
    "Ниже — часть {n} из {total} черновой расшифровки длинной записи совещания или разговора. "
    "Сделай подробное саммари ЭТОЙ части: темы, решения и договорённости, поручения (кто и что, "
    "если названо), открытые вопросы. Только по тексту ниже, ничего не придумывай.\n\n"
    "Текст части:\n\n"
)

MERGE_PROMPT = (
    "Ниже — саммари частей одной длинной записи совещания или разговора, по порядку. Собери из них "
    "одно краткое итоговое саммари всей записи: главные темы, принятые решения и договорённости, "
    "поручения (кто и что, если названо), открытые вопросы. Без повторов, только по тексту ниже.\n\n"
)


class SummaryError(Exception):
    """Саммари не получилось. Несёт расход токенов: неудачный прогон тоже стоил денег
    и по контракту с порталом (п. 4) попадает в журнал."""

    def __init__(self, message: str, spent: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.spent = spent or {"in": 0, "out": 0, "total": 0}


class Cancelled(SummaryError):
    """Человек отменил обработку, пока «Искра» писала саммари."""


def _key() -> str:
    key = config.read_env("ISKRA_API_KEY", "").strip()
    if not key:
        raise SummaryError(
            "Ключ «Искры» не настроен. Портал записывает его в .env при сохранении "
            "ключа в разделе «Администрирование», либо впишите ISKRA_API_KEY вручную.")
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
            status = response.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"detail": raw[:300]}
        return e.code, payload if isinstance(payload, dict) else {"detail": str(payload)[:300]}
    except (urllib.error.URLError, OSError) as e:
        raise SummaryError(f"«Искра» недоступна: {e}") from None
    # 200 не в JSON (шлюз, страница ошибки) — понятная ошибка с расходом, а не зависшая запись
    # (RC-19, Н-14); сам ответ в текст не кладём — там может быть текст записи
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise SummaryError("«Искра» ответила не JSON — попробуйте «Пересобрать» позже") from None
    if not isinstance(payload, dict):
        raise SummaryError("«Искра» ответила не объектом JSON — попробуйте «Пересобрать» позже")
    return status, payload


def _answer(payload: dict) -> str:
    run = iskra_common.unwrap(payload)
    result = run.get("result")
    if isinstance(result, dict):
        for key in ("output", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    for key in ("output", "answer"):
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(result, str) and result.strip():
        return result
    raise SummaryError("«Искра» вернула пустой ответ")


def split_text(text: str, limit: int | None = None) -> list[str]:
    """Текст → части не длиннее `limit` знаков, по границам абзацев (абзац длиннее предела —
    по строкам, затем по словам). Порядок и весь текст сохраняются."""
    limit = int(limit or MAX_CHARS)
    if len(text) <= limit:
        return [text]

    def pieces(block: str) -> list[str]:
        if len(block) <= limit:
            return [block]
        for sep in ("\n", " "):
            if sep in block:
                out, cur = [], ""
                for unit in block.split(sep):
                    for small in (pieces(unit) if len(unit) > limit else [unit]):
                        if cur and len(cur) + len(sep) + len(small) > limit:
                            out.append(cur)
                            cur = small
                        else:
                            cur = f"{cur}{sep}{small}" if cur else small
                if cur:
                    out.append(cur)
                return out
        return [block[i:i + limit] for i in range(0, len(block), limit)]

    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        for chunk in pieces(para):
            if cur and len(cur) + 2 + len(chunk) > limit:
                parts.append(cur)
                cur = chunk
            else:
                cur = f"{cur}\n\n{chunk}" if cur else chunk
    if cur:
        parts.append(cur)
    return parts


def _carry(error: SummaryError, spent: dict[str, int]) -> SummaryError:
    """Ошибка части несёт расход всех уже сделанных прогонов, а не только своего."""
    error.spent = _add(dict(spent), error.spent)
    return error


def summarize(text: str, cancelled: Callable[[], bool] = lambda: False) -> tuple[str, dict[str, int]]:
    """Черновик расшифровки → саммари и расход токенов «Искры» за все попытки.

    `cancelled()` проверяется между опросами: отменили — прогон снимается у «Искры»,
    а уже потраченные токены остаются в возвращённом исключении (контракт п. 4).
    Длинная транскрипция — по частям и итог по ним (`split_text`), без вырезанной середины.
    """
    parts = split_text(text)
    if len(parts) == 1:
        return run(PROMPT + text, AGENT_RULES, cancelled)
    spent: dict[str, int] = {"in": 0, "out": 0, "total": 0}
    partial: list[str] = []
    try:
        for n, part in enumerate(parts, start=1):
            answer, more = run(PART_PROMPT.format(n=n, total=len(parts)) + part, AGENT_RULES, cancelled)
            _add(spent, more)
            partial.append(f"Часть {n} из {len(parts)}:\n{answer.strip()}")
        answer, more = run(MERGE_PROMPT + "\n\n".join(partial), AGENT_RULES, cancelled)
    except SummaryError as e:
        raise _carry(e, spent) from None
    return answer, _add(spent, more)


def run(message: str, instructions: str, cancelled: Callable[[], bool] = lambda: False,
        error: type[SummaryError] | None = None, cancel: type[SummaryError] | None = None,
        what: str = "саммари") -> tuple[str, dict[str, int]]:
    """Один прогон «Искры» (`/api/v1/runs` → опрос) → (ответ, расход). Общий для саммари
    и перечня поручений (`app/orders.py`): ошибки — `error`/`cancel` (по умолчанию
    `SummaryError`/`Cancelled`), в каждой — расход токенов за прогон."""
    error = error or SummaryError
    cancel = cancel or Cancelled
    spent: dict[str, int] = {"in": 0, "out": 0, "total": 0}
    body = {"message": message, "instructions": instructions}
    try:
        status, payload = _call("POST", "/api/v1/runs", body, timeout=60)
    except SummaryError as e:
        raise error(str(e), spent) from None
    if status not in (200, 201, 202):
        raise error(iskra_common.human_error(status, payload), _add(spent, iskra_common.usage(payload)))
    run_id = iskra_common.unwrap(payload).get("run_id") or iskra_common.unwrap(payload).get("id")
    if not run_id:
        raise error("«Искра» не выдала номер прогона", spent)

    deadline, pause, last = time.monotonic() + DEADLINE, POLL_MIN, None
    while time.monotonic() < deadline:
        if cancelled():
            _add(spent, iskra_common.usage(last or {}))
            try:
                _call("DELETE", f"/api/v1/runs/{run_id}", timeout=20)
            except SummaryError:
                pass
            raise cancel(f"Отменено: {what}", spent)
        time.sleep(pause)
        pause = min(POLL_MAX, pause + 0.5)
        try:
            status, payload = _call("GET", f"/api/v1/runs/{run_id}", timeout=60)
        except SummaryError as e:
            raise error(str(e), _add(spent, iskra_common.usage(last or {}))) from None
        if status == 429:
            continue
        if status >= 400:
            raise error(iskra_common.human_error(status, payload),
                        _add(spent, iskra_common.usage(last or {})))
        current = iskra_common.unwrap(payload)
        last = current
        if iskra_common.is_done(current):
            _add(spent, iskra_common.usage(current))
            try:
                return _answer(current).strip(), spent
            except SummaryError as e:
                raise error(str(e), spent) from None
        if iskra_common.is_failed(current):
            _add(spent, iskra_common.usage(current))
            raise error(f"«Искра» прервала {what}: {iskra_common.trouble(current)}", spent)
    if last is not None:
        _add(spent, iskra_common.usage(last))
    try:
        _call("DELETE", f"/api/v1/runs/{run_id}", timeout=20)
    except SummaryError:
        pass
    raise error("«Искра» не ответила за отведённое время", spent)


def _add(spent: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for key in ("in", "out", "total"):
        spent[key] = spent.get(key, 0) + more.get(key, 0)
    return spent
