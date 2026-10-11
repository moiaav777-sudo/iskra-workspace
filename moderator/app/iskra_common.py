"""Общий разговор с «Искрой»: одно место на всё пространство.

Раньше клиент «Искры» был написан в каждом сервисе заново — пять реализаций, пять текстов
ошибок, пять мест для починки. 19.09 «run failed» чинили в A3, а те же грабли лежали ещё
в четырёх сервисах. Здесь собрано то, что одинаково у всех: как понять состояние прогона,
где «Искра» прячет причину отказа и расход токенов, и что сказать человеку.

ЭТОТ ФАЙЛ ПРАВИТСЯ ТОЛЬКО В `portal/shared/iskra_common.py`.
В сервисы он раскладывается скриптом `portal/scripts/sync_shared.sh` — правка копии будет затёрта.
"""
from __future__ import annotations

from typing import Any

# Состояния прогона: «Искра» называет их по-разному в разных ответах.
TERMINAL_OK = {"completed", "succeeded", "success", "done", "finished"}
TERMINAL_BAD = {"failed", "error", "cancelled", "canceled", "stopped", "timeout"}

# Где «Искра» прячет причину отказа: поле называется по-разному от прогона к прогону.
TROUBLE_KEYS = ("error", "last_error", "failure", "failure_reason", "reason", "status_reason",
                "detail", "message")

# Что сказать человеку по коду ответа. Тексты одинаковые во всех сервисах: человек ходит между ними.
BY_CODE = {
    401: "«Искра» не приняла ключ. Администратору нужно обновить его в портале.",
    402: "У «Искры» кончилась подписка — оплатите её в кабинете «Искры».",
    403: "У ключа нет прав на этот запрос. Проверьте ключ в портале.",
    409: "Предыдущий разбор ещё идёт. Подождите и повторите.",
    413: "Документ слишком большой для «Искры». Сократите текст или разбейте на части.",
    429: "«Искра» ограничила частоту запросов. Повторите через минуту.",
}


def unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Ответ приходит то плоским, то завёрнутым в run/data — разворачиваем."""
    for key in ("run", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            payload = {**inner, **{k: v for k, v in payload.items() if k != key}}
    return payload


def state_of(run: dict[str, Any]) -> str:
    return str(run.get("status") or "").lower()


def is_done(run: dict[str, Any]) -> bool:
    return state_of(run) in TERMINAL_OK


def is_failed(run: dict[str, Any]) -> bool:
    return state_of(run) in TERMINAL_BAD


def trouble(run: dict[str, Any]) -> str:
    """Причина отказа: собираем всё, что похоже на объяснение, а не одно поле.

    «Искра» умеет ответить просто «run failed» — тогда объяснения нет вовсе, и это надо
    показывать как есть, а не выдавать за причину.
    """
    found: list[str] = []
    for key in TROUBLE_KEYS:
        value = run.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail") or ""
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    # события прогона иногда единственное, где видно, на чём всё встало
    events = run.get("events")
    if isinstance(events, list):
        for event in events[-3:]:
            if isinstance(event, dict):
                text = event.get("error") or event.get("message")
                if isinstance(text, str) and text.strip():
                    found.append(text.strip())
    return "; ".join(dict.fromkeys(found))[:300] or state_of(run)


def usage(run: dict[str, Any]) -> dict[str, int]:
    """Расход токенов: поля называются по-разному, берём любые знакомые."""
    raw = run.get("usage")
    if not isinstance(raw, dict):
        result = run.get("result")
        raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return {"in": 0, "out": 0, "total": 0}

    def num(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    got_in = num("prompt_tokens", "input_tokens", "tokens_in", "in")
    got_out = num("completion_tokens", "output_tokens", "tokens_out", "out")
    return {"in": got_in, "out": got_out, "total": num("total_tokens", "tokens", "total") or got_in + got_out}


def human_error(code: int, payload: Any = None) -> str:
    """Что показать человеку, когда «Искра» ответила ошибкой."""
    if code in BY_CODE:
        return BY_CODE[code]
    if code >= 500 or code == 0:
        return "«Искра» сейчас не отвечает. Сервисы работают, попробуйте позже."
    return f"«Искра» ответила {code}: {str(payload)[:200]}"
