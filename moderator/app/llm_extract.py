"""Умный разбор документа через Искру – ЕДИНСТВЕННУЮ модель приложения.

Искра сама определяет ТИП входящего документа (повестка / участники / поручения /
модератор / материалы к вопросу) и извлекает данные. Если тип неясен – честно
возвращает «неизвестно» + низкую уверенность, и приложение спрашивает пользователя.
"""
from __future__ import annotations

import html
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .models import AgendaItem, Material, Meeting, Participant, Order, ScenarioBlock, normalize_date, normalize_agenda, renumber_agenda, _is_opening, _is_closing, inject_material, new_id, normalize_deadline, DEADLINE_MAX
from .protocol import protocol_rank, sort_key
from . import iskra_common, portal, storage

ISKRA_BASE = "https://cloud.iskrabot.ru"
ISKRA_FILES_URL = ISKRA_BASE + "/api/v1/chat/files"
RUNS_PATH = "/api/v1/runs"
MODEL = "iskra"

# Разбор идёт фоновым прогоном (../CLAUDE.md п. 5в): синхронный /api/v1/chat у «Искры» ограничен
# 90 с, и большой документ давал 504. POST /runs → опрос GET /runs/{id}?after=N до конца.
POLL_MIN, POLL_MAX = 2.0, 5.0     # пауза опроса растёт 2 → 5 с
DEADLINE = 840.0                  # 14 мин на весь запрос (прогон «Искры» — до 15, nginx ждёт 900 с)
POLL_FAILS = 3                    # столько обрывов связи при опросе подряд терпим
POST_TRIES = 3                    # запуск прогона: повтор после обрыва/429 тем же Idempotency-Key

# подменяются в тестах: ждать по-настоящему там незачем
_sleep = time.sleep
_clock = time.monotonic


class IskraError(RuntimeError):
    """«Искра» не ответила или ответила не тем. Текст уходит человеку."""

SYSTEM_PROMPT = (
    "Ты разбираешь входящий документ для брифа совещания. Определи его ТИП и извлеки данные.\n"
    "Типы документов:\n"
    "- повестка: место, название мероприятия, перечень вопросов с докладчиками и содокладчиками;\n"
    "- участники: список участников (очно или ВКС);\n"
    "- поручения: проект перечня поручений / перечень протокольных решений;\n"
    "- модератор: сценарий ведущего – вступительное слово, подводки, выводы после докладов, заключительное слово;\n"
    "- материалы: материалы к конкретному вопросу (обычно озаглавлены «вопрос 1», «вопрос 2» и т.п.);\n"
    "- неизвестно: если тип не удалось определить.\n"
    "Верни СТРОГО валидный JSON без пояснений и без markdown, по схеме:\n"
    '{"doc_type":"повестка|участники|поручения|модератор|материалы|неизвестно",'
    '"question_num":null,"confidence":"высокая|низкая","summary":"...","data":{...}}\n'
    "Поля data по типам:\n"
    '- повестка: {"title":"...","date":"...","time":"...","location":"...","chair":"...",'
    '"agenda":[{"num":"1","title":"...","time":"...","speakers":["Фамилия Имя Отчество"],"speaker_roles":["Должность"],"speaker_minutes":[15]}]}\n'
    '- участники: {"participants":[{"name":"Фамилия Имя Отчество","role":"...","organization":"...","attendance":"очно|ВКС","is_speaker":true}]}\n'
    '- поручения: {"orders":[{"text":"...","assignee":"...","deadline":"...","agenda_num":"1"}]}\n'
    '- модератор: {"opening":"...","closing":"...","blocks":[{"question_num":"1","kind":"intro|replica|report","text":"...","speaker":"...","minutes":0}]}\n'
    '- материалы: {"question_num":"1","type":"Доклад|Презентация|Справка|...","title":"...","speaker":"...","text":"...",'
    '"summary":"...","intro":"...","figures":[],"theses":["..."],"conclusion":"...","proposals":["..."],'
    '"orders":[{"text":"...","assignee":"...","deadline":""}]}\n'
    '- неизвестно: {}\n'
    "Правила:\n"
    "1. Склеивай перенесённые строки (ФАМИЛИЯ на одной строке, Имя Отчество на следующей – один человек).\n"
    "2. confidence=низкая, если тип определить сложно или текст неоднозначен.\n"
    "3. summary – 1-2 предложения по-русски: какой документ и что в нём найдено.\n"
    "4. Не выдумывай данные, которых нет в тексте.\n"
    "5. question_num – строка с номером вопроса (1, 2, 3...), если понятно к какому вопросу относится.\n"
    "6. Фамилии и имена приводи к виду «Фамилия Имя Отчество».\n"
    "7. Дату указывай в формате ДД.ММ.ГГГГ (например 07.09.2026).\n"
    "8. Если в тексте указана длительность выступления (минут), проставляй её числом в speaker_minutes / minutes.\n"
    "9. Для КАЖДОГО вопроса извлекай ВСЕХ докладчиков и содокладчиков. Не пропускай вопрос, у которого указаны выступающие.\n"
    "10. Для материалов summary пиши подробно (все цифры, значения, факты – без привязки к объёму); theses – 3-4 тезиса по 20–25 слов; "
    "conclusion – вывод в 1-2 предложения; proposals – 1-4 предложения к решению; orders – предложения поручений по итогам "
    "(срок – только если он есть в тексте, иначе пустая строка); figures – пустой массив.\n"
    "11. Для повестки: если у докладчика/содокладчика в тексте указана должность – проставляй её в speaker_roles тем же порядком, что speakers (должности нет – пустая строка).\n"
    "12. Для повестки: title – это ПОЛНОЕ официальное наименование совещания (текстовая тема, напр. «Совещание у Заместителя Председателя Правительства Российской Федерации … по вопросам …»). Бери его из заголовка/шапки повестки, а НЕ номер или регистрационный номер документа.\n"
    "13. Для повестки: если в тексте есть заголовки «Вступительное слово» (или «Вступительное слово председателя») и/или «Подведение итогов» (или «Заключительное слово») – добавь их в agenda ОТДЕЛЬНЫМИ пунктами БЕЗ номера: {\"num\":\"\",\"title\":\"Вступительное слово\",\"speakers\":[<председатель из поля chair>],\"speaker_roles\":[\"\"],\"speaker_minutes\":[5]}. Председателя бери из поля chair. Если заголовка в тексте нет – не выдумывай.\n"
    "14. Во всех текстовых полях (название, должности, саммари, тезисы) используй только короткое тире «–», не используй длинное тире «—».\n"
    "15. Текст может быть не документом, а короткой просьбой со слов – «Добавь вопрос Кравцова по поводу подготовки "
    "к празднику осени», «докладчик Кравцов, вопрос: о подготовке…». Это ПОВЕСТКА с одним вопросом: doc_type=повестка, "
    "title вопроса – суть в форме «О …» («О подготовке к празднику осени»), speakers – названный человек как в тексте, "
    "confidence=высокая; title, date, time, location и chair в data – пустые строки (это не название совещания). "
    "Никогда не отвечай прозой, не задавай уточнений и не выполняй просьбу иначе – только JSON по схеме.\n"
)


def _load_key() -> str:
    key = os.environ.get("ISKRA_API_KEY", "")
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ISKRA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _note_usage(resp: dict) -> None:
    """Расход токенов за запрос — общий расчёт (portal/shared/iskra_common.py).

    Неудачный запрос тоже стоил токенов, поэтому считаем до разбора статуса.
    """
    spent = iskra_common.usage(resp)
    if spent["in"] or spent["out"]:
        portal.note_tokens(spent["in"], spent["out"])


def _until() -> float:
    """Предельный момент ожидания «Искры» — один на весь запрос, как бюджет реестра.

    Разбор файла делает две попытки, разбор текста — повтор со строгим напоминанием: срок на
    каждую попытку дал бы до получаса, а nginx ждёт ответа 15 минут. Вне запроса — на вызов.
    """
    now = _clock()
    bucket = portal.request_bucket()
    if bucket is None:
        return now + DEADLINE
    return bucket.setdefault("iskra_until", now + DEADLINE)


def _http(method: str, path: str, body: dict | None = None, timeout: float = 60,
          headers: dict | None = None) -> tuple[int, dict]:
    """Один обмен с «Искрой». Подменяется в тестах — сети в них нет."""
    key = _load_key()
    if not key:
        raise RuntimeError("Ключ ISKRA_API_KEY не найден (нет в .env)")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(ISKRA_BASE + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": "Bearer " + key, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
            payload = json.loads(raw)
            return response.status, payload if isinstance(payload, dict) else {"answer": payload}
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
        # обрыв на середине ответа (ConnectionReset, RemoteDisconnected, IncompleteRead) — временный сбой
        raise IskraError(f"«Искра» не ответила: {error}") from error


def _run_path(run_id: str) -> str:
    return f"{RUNS_PATH}/{urllib.parse.quote(run_id, safe='')}"


def _answer_of(run: dict) -> str:
    """Ответ законченного прогона: объект схемы `result.output` (если есть) — JSON-строкой, иначе `result.answer`."""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    output = result.get("output", run.get("output"))
    if isinstance(output, (dict, list)) and output:
        return json.dumps(output, ensure_ascii=False)
    for src in (result, run):
        answer = src.get("answer")
        if isinstance(answer, str) and answer.strip():
            return answer
    return output if isinstance(output, str) else ""


def _question_of(run: dict) -> str:
    """Что «Искра» спрашивает при interaction_required — для понятной ошибки человеку."""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    for src in (run.get("interaction"), run.get("policy"), result.get("policy"), result, run):
        if isinstance(src, dict):
            for key in ("question", "message", "text", "answer"):
                value = src.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _settle(run_id: str, last: dict) -> dict:
    """Снять брошенный прогон (срок, обрыв связи, отмена) и забрать его расход.

    Без DELETE прогон продолжал бы тратить токены мимо журнала. Расход берём из ответа DELETE,
    нет его — ещё раз спрашиваем прогон; «Искра» не ответила — остаётся последний замер.
    Расход в прогоне накопительный, поэтому берётся самый свежий, а не сумма (как `theses`).
    """
    for method in ("DELETE", "GET"):
        try:
            status, payload = _http(method, _run_path(run_id), timeout=20)
        except Exception:
            continue
        if status < 400 and isinstance(payload, dict):
            run = iskra_common.unwrap(payload)
            spent = iskra_common.usage(run)
            if spent["in"] or spent["out"]:
                return run
    return last or {}


def _start_run(payload: dict, until: float) -> dict:
    """POST /api/v1/runs. Обрыв связи, 429 и 5xx — повтор тем же Idempotency-Key: «Искра» вернёт
    прежний run_id и второй раз не исполнит (проверено 05.10)."""
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    last_error = ""
    for attempt in range(POST_TRIES):
        if attempt:
            _sleep(max(0.0, min(POLL_MAX, until - _clock())))
        if _clock() >= until:
            break
        try:
            status, body = _http("POST", RUNS_PATH, payload, timeout=120, headers=headers)
        except IskraError as error:
            last_error = str(error)
            continue
        if status in (200, 201, 202):
            run = iskra_common.unwrap(body)
            if not str(run.get("run_id") or run.get("id") or ""):
                raise IskraError("«Искра» не выдала номер прогона")
            return run
        last_error = iskra_common.human_error(status, body)
        if status != 429 and status < 500:
            raise IskraError(last_error)
    raise IskraError(last_error or f"«Искра» не ответила за {max(1, int(DEADLINE // 60))} мин")


def _call_iskra_raw(system: str, user: str, files: list | None = None, conversation_id: str = "") -> str:
    """Один фоновый прогон «Искры»: запустить, дождаться, вернуть текст ответа.

    Расход токенов уходит в журнал (`portal.note_tokens`) при любом исходе — удачном, неудачном
    и снятом по сроку или обрыву связи: такой прогон тоже стоил токенов.
    """
    if not _load_key():
        raise RuntimeError("Ключ ISKRA_API_KEY не найден (нет в .env)")
    payload = {"message": user, "instructions": system}
    if files:
        payload["files"] = files                 # фоновый прогон принимает только {"id": …}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    until = _until()
    minutes = max(1, int(DEADLINE // 60))
    if _clock() >= until:
        raise IskraError(f"«Искра» не ответила за {minutes} мин. Попробуйте ещё раз; "
                         "если повторится — сократите документ")
    run = _start_run(payload, until)
    run_id = str(run.get("run_id") or run.get("id"))
    pause, last, fails, after, settled = POLL_MIN, run, 0, 0, False

    def give_up(error: Exception):
        nonlocal settled
        settled = True
        _note_usage(_settle(run_id, last))
        raise error

    try:
        while True:
            if _clock() >= until:
                give_up(IskraError(f"«Искра» не ответила за {minutes} мин — прогон снят. "
                                   "Попробуйте ещё раз; если повторится — сократите документ"))
            _sleep(max(0.0, min(pause, until - _clock())))
            try:
                status, body = _http("GET", f"{_run_path(run_id)}?after={after}", timeout=60)
            except IskraError as error:
                fails += 1
                if fails >= POLL_FAILS:
                    give_up(IskraError(f"{error}. Связь с «Искрой» рвалась {fails} раза подряд — "
                                       "прогон снят, попробуйте ещё раз"))
                pause = POLL_MAX
                continue
            fails = 0
            if status == 429:
                pause = POLL_MAX                 # чаще спрашивать не просят; срок проверится в начале круга
                continue
            if status >= 400:
                give_up(IskraError(iskra_common.human_error(status, body)))
            last = iskra_common.unwrap(body)
            nxt = last.get("next_after")
            if isinstance(nxt, int) and not isinstance(nxt, bool):
                after = nxt
            state = iskra_common.state_of(last)
            if state == "interaction_required":
                question = _question_of(last)
                give_up(IskraError(
                    "«Искра» вместо разбора задала вопрос (interaction_required)"
                    + (f": «{question[:200]}»" if question else "")
                    + ". Повторите разбор; если повторится — добавьте подсказку о типе документа"))
            if iskra_common.is_done(last):
                settled = True
                _note_usage(last)
                return _answer_of(last)
            if iskra_common.is_failed(last):
                settled = True
                _note_usage(last)
                raise IskraError(f"«Искра» прервала прогон ({state}): {iskra_common.trouble(last)}. "
                                 "Попробуйте ещё раз")
            pause = min(POLL_MAX, pause + 0.5)
    except BaseException:
        # любой другой выход (отмена потока, ошибка в коде) — прогон всё равно снимаем и считаем
        if not settled:
            settled = True
            _note_usage(_settle(run_id, last))
        raise


STRICT_REMINDER = (
    "Предыдущий ответ был не JSON, а текст. Ответь ЕЩЁ РАЗ и верни ТОЛЬКО один JSON-объект по схеме из инструкции, "
    "без пояснений, без markdown и без вопросов. Если ниже не документ, а короткая просьба со слов "
    "(«добавь вопрос …», «докладчик такой-то»), это повестка с одним вопросом.\n\n"
)


def _call_iskra(text: str, strict: bool = False) -> str:
    """strict – повтор после ответа прозой: напоминание про JSON впереди текста (просьба пользователя 02.10:
    «Добавь вопрос Кравцова…» Искра приняла за команду себе и ответила текстом про «Планы»)."""
    return _call_iskra_raw(SYSTEM_PROMPT, (STRICT_REMINDER + text) if strict else text)


def _upload_file_iskra(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """Загрузить файл в Искру напрямую (multipart), вернуть (id файла, id разговора).

    id одноразовый (../CLAUDE.md п. 5в): действует в своём разговоре и на один ход —
    для повтора файл грузится заново."""
    key = _load_key()
    if not key:
        raise RuntimeError("Ключ ISKRA_API_KEY не найден (нет в .env)")
    safe_name = filename.replace("\"", "_").replace("\r", "_").replace("\n", "_")
    boundary = "----iskra" + new_id()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        ISKRA_FILES_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise IskraError(iskra_common.human_error(error.code, error.read().decode("utf-8", "replace")[:200])) from error
    except urllib.error.URLError as error:
        raise IskraError(f"«Искра» недоступна: {error.reason}") from error
    except (TimeoutError, json.JSONDecodeError, http.client.HTTPException, ConnectionError, OSError) as error:
        raise IskraError(f"«Искра» не ответила: {error}") from error
    files = resp.get("files") or []
    fid = (files[0].get("id") if files else "") or resp.get("id") or resp.get("file_id") or ""
    conv = resp.get("conversation_id", "")
    if not fid:
        raise RuntimeError("Искра не вернула id файла: " + json.dumps(resp, ensure_ascii=False)[:300])
    return fid, conv


def extract_with_file(file_bytes: bytes, filename: str = "", hint: str = "") -> dict:
    """Загрузить файл в Искру целиком и разобрать его (без локального извлечения текста).

    Две попытки; на каждую файл грузится заново — id файла одноразовый, повтор с прежним
    давал 404 invalid_file. Срок общий на запрос: истёк — вторую попытку не начинаем."""
    user = "Разбери приложенный файл" + (f" «{filename}»" if filename else "") + "."
    if hint:
        user += "\nПодсказка: документ типа «" + hint + "»."
    last_err = None
    until = _until()
    for attempt in range(2):
        if attempt and _clock() >= until:
            break
        try:
            fid, conv = _upload_file_iskra(file_bytes, filename)
            content = _call_iskra_raw(SYSTEM_PROMPT, user, files=[{"id": fid}], conversation_id=conv).strip()
            return check_schema(_parse_json_result(content))
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err))


def _parse_json_result(content: str) -> dict:
    """Надёжно разобрать JSON от Искры: снять markdown-обёртку, найти JSON-объект."""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return _norm_dashes(json.loads(content))
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return _norm_dashes(json.loads(content[start:end + 1]))
        except json.JSONDecodeError:
            pass
    raise ValueError("Искра вернула невалидный JSON: " + content[:200])

# ── проверка ответа Искры по схеме ──────────────────────────────────────
#
# Искра — языковая модель: она может вернуть список строк там, где в схеме объекты,
# «15 мин» вместо 15 или выдуманный тип документа. Раньше такой ответ доходил до
# apply_to_meeting и валил запрос (500) или клал мусор в бриф. Здесь ответ приводится
# к схеме: лишнее отбрасывается, что удалось — исправляется, замечания видит пользователь.

DOC_TYPES = ("повестка", "участники", "поручения", "модератор", "материалы", "неизвестно")
BLOCK_KINDS = ("intro", "replica", "report", "comment", "question", "order",
               "problem", "decision", "task", "reference")


def _as_text(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _as_int(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.findall(r"\d+", str(v or ""))    # «15 мин» → 15
    return int(digits[0]) if digits else 0


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, "", {}):
        return []
    return [v]


def _as_texts(v) -> list[str]:
    return [t for t in (_as_text(x) for x in _as_list(v)) if t]


def _as_records(v, text_field: str) -> list[dict]:
    """Список объектов; строку принимаем как объект с одним заполненным полем."""
    out = []
    for item in _as_list(v):
        if isinstance(item, dict):
            out.append(item)
        elif _as_text(item):
            out.append({text_field: _as_text(item)})
    return out


def check_schema(result: dict) -> dict:
    """Привести ответ Искры к схеме разбора документа. Замечания — в `schema_problems`."""
    if not isinstance(result, dict):
        raise ValueError("Искра вернула не объект JSON")
    problems: list[str] = []
    doc_type = _as_text(result.get("doc_type")).lower()
    if doc_type not in DOC_TYPES:
        problems.append(f"неизвестный тип документа «{doc_type or '—'}» — считаем тип неопределённым")
        doc_type = "неизвестно"
    confidence = _as_text(result.get("confidence")).lower()
    raw_data = result.get("data")
    if raw_data is not None and not isinstance(raw_data, dict):
        problems.append("поле data пришло не объектом — данные из него не взяты")
        raw_data = {}
    data = _check_data(doc_type, raw_data or {}, problems)
    if problems and confidence != "низкая":
        confidence = "низкая"   # ответ не по схеме — подтверждать его надо внимательнее
    return {
        "doc_type": doc_type,
        "question_num": _as_text(result.get("question_num")),
        "confidence": confidence if confidence in ("высокая", "низкая") else "низкая",
        "summary": _as_text(result.get("summary")),
        "data": data,
        "schema_problems": problems,
    }


def _check_data(doc_type: str, data: dict, problems: list[str]) -> dict:
    if doc_type == "повестка":
        items = []
        for a in _as_records(data.get("agenda"), "title"):
            title = _as_text(a.get("title"))
            if not title:
                continue
            speakers = _as_texts(a.get("speakers"))
            items.append({
                "num": _as_text(a.get("num")), "title": title, "time": _as_text(a.get("time")),
                "speakers": speakers,
                "speaker_roles": _as_texts(a.get("speaker_roles"))[:len(speakers)],
                "speaker_minutes": [_as_int(x) for x in _as_list(a.get("speaker_minutes"))][:len(speakers)],
            })
        dropped = len(_as_list(data.get("agenda"))) - len(items)
        if dropped > 0:
            problems.append(f"в повестке пропущено вопросов без формулировки: {dropped}")
        return {"title": _as_text(data.get("title")), "date": _as_text(data.get("date")),
                "time": _as_text(data.get("time")), "location": _as_text(data.get("location")),
                "chair": _as_text(data.get("chair")), "agenda": items}

    if doc_type == "участники":
        people = []
        for p in _as_records(data.get("participants"), "name"):
            name = _as_text(p.get("name"))
            if not name:
                continue
            people.append({"name": name, "role": _as_text(p.get("role")),
                           "organization": _as_text(p.get("organization")),
                           "attendance": _as_text(p.get("attendance")),
                           "is_speaker": bool(p.get("is_speaker"))})
        dropped = len(_as_list(data.get("participants"))) - len(people)
        if dropped > 0:
            problems.append(f"пропущено участников без ФИО: {dropped}")
        return {"participants": people}

    if doc_type == "поручения":
        orders = []
        for o in _as_records(data.get("orders"), "text"):
            text = _as_text(o.get("text"))
            if not text:
                continue
            orders.append({"text": text, "assignee": _as_text(o.get("assignee")),
                           "deadline": _as_text(o.get("deadline")), "agenda_num": _as_text(o.get("agenda_num"))})
        dropped = len(_as_list(data.get("orders"))) - len(orders)
        if dropped > 0:
            problems.append(f"пропущено поручений без текста: {dropped}")
        return {"orders": orders}

    if doc_type == "модератор":
        blocks = []
        for b in _as_records(data.get("blocks"), "text"):
            kind = _as_text(b.get("kind"))
            blocks.append({"question_num": _as_text(b.get("question_num")),
                           "kind": kind if kind in BLOCK_KINDS else "intro",
                           "text": _as_text(b.get("text")), "speaker": _as_text(b.get("speaker")),
                           "minutes": _as_int(b.get("minutes"))})
        return {"opening": _as_text(data.get("opening")), "closing": _as_text(data.get("closing")),
                "blocks": blocks}

    if doc_type == "материалы":
        return check_material_schema(data, problems, [])   # длина тезисов здесь уверенность не снижает

    return {}


THESES_MAX = 4                    # тезисы идут во вступительное слово: 3–4 буллита (решение пользователя 02.10)
THESIS_WORDS = (12, 30)           # 20–25 слов по правилам тезисов; за этими границами — предупреждение, не обрезка
FIGURES_MAX = 4


def _words(text: str) -> int:
    return len([w for w in text.split() if re.search(r"\w", w)])


def compose_proposal(conclusion: str, proposals: list[str]) -> str:
    """Старое поле `proposal` для старого кода и блока «Предложение:» сценария: вывод + «- » + предложения."""
    parts = [conclusion.strip()] if conclusion.strip() else []
    parts += ["- " + p.strip() for p in proposals if p.strip()]
    return " ".join(parts)


def _check_figures(raw, warnings: list[str]) -> list[dict]:
    """Диаграммы из ответа или из браузера: негодные отбрасываем с замечанием, svg рисуем сами."""
    from . import charts
    out: list[dict] = []
    items = _as_list(raw)
    for n, fig in enumerate(items, 1):
        title = _as_text(fig.get("title")) if isinstance(fig, dict) else ""
        name = f"«{title[:60]}»" if title else f"№{n}"
        if len(out) >= FIGURES_MAX:
            warnings.append(f"диаграмма {name} отброшена: больше {FIGURES_MAX} диаграмм к материалу")
            continue
        try:
            clean = charts.check_figure(fig)
            clean["svg"] = charts.svg(clean)          # присланному svg не верим — рисуем из items
        except ValueError as e:
            warnings.append(f"диаграмма {name} отброшена: {e}")
            continue
        clean["id"] = f"f{len(out) + 1}"
        out.append(clean)
    return out


ORDERS_MAX = 30                   # поручений к одному материалу (ревьюер RC-2026-10-02-15, Н-10)

_INTRO_ADDRESS = re.compile(r"^\s*(?:прошу|просим)\s+(?:также\s+)?доложить\s+(об?|обо)\s+", re.I)


def clean_intro(text: str) -> str:
    """Подводка — «о чём вопрос», без обращения: «Прошу доложить о подключении…» → «О подключении…».

    Слово передают реплики сценария, а подводка идёт и во вход вступительного слова (RC-2026-10-02-15, Н-2).
    """
    t = (text or "").strip()
    m = _INTRO_ADDRESS.match(t)
    if not m:
        return t
    rest = t[m.end():]
    return m.group(1).capitalize() + " " + rest if rest else t


def _check_orders(raw, warnings: list[str] | None = None) -> list[dict]:
    """Поручения: строки старых ответов → {text, assignee:"", deadline:""}; срок только присланный."""
    out = []
    for o in _as_records(raw, "text"):
        text = _as_text(o.get("text"))
        if not text:
            continue
        if len(out) >= ORDERS_MAX:
            if warnings is not None:
                warnings.append(f"поручений больше {ORDERS_MAX} – оставлены первые {ORDERS_MAX}")
            break
        out.append({"id": f"o{len(out) + 1}", "text": text[:1000],
                    "assignee": _as_text(o.get("assignee"))[:300],
                    "deadline": normalize_deadline(_as_text(o.get("deadline")))[:DEADLINE_MAX]})
    return out


def check_material_schema(data: dict, problems: list[str] | None = None,
                          warnings: list[str] | None = None) -> dict:
    """Схема разобранного материала (та же и для отдельного разбора файла материала, и для /materials/apply).

    `problems` — что отброшено (негодные диаграммы); `warnings` — мягкие замечания (длина тезисов).
    Без `warnings` мягкие замечания идут туда же, куда `problems`.
    """
    if problems is None:
        problems = []
    soft = warnings if warnings is not None else problems
    if not isinstance(data, dict):
        problems.append("материал пришёл не объектом")
        data = {}
    theses = _as_texts(data.get("theses"))
    if len(theses) > THESES_MAX:
        soft.append(f"тезисов {len(theses)} – оставлены первые {THESES_MAX}")
        theses = theses[:THESES_MAX]
    for i, t in enumerate(theses, 1):
        n = _words(t)
        if not THESIS_WORDS[0] <= n <= THESIS_WORDS[1]:
            soft.append(f"тезис {i}: {n} слов – по правилам тезисов 20–25")
    conclusion = _as_text(data.get("conclusion"))
    proposals = _as_texts(data.get("proposals"))[:10]
    proposal = compose_proposal(conclusion, proposals) if (conclusion or proposals) else _as_text(data.get("proposal"))
    return {
        "question_num": _as_text(data.get("question_num")),
        "type": _as_text(data.get("type")) or "Материал к вопросу",
        "title": _as_text(data.get("title")),
        "speaker": _as_text(data.get("speaker")),
        "text": _as_text(data.get("text")),
        "summary": _as_text(data.get("summary")),
        "intro": clean_intro(_as_text(data.get("intro"))),
        "figures": _check_figures(data.get("figures"), problems),
        "theses": theses,
        "conclusion": conclusion,
        "proposals": proposals,
        "orders": _check_orders(data.get("orders"), soft),
        "proposal": proposal,
    }


def _norm_dashes(obj):
    """Заменить длинное тире на короткое во всех строковых полях ответа Искры."""
    if isinstance(obj, str):
        return obj.replace("—", "–")
    if isinstance(obj, list):
        return [_norm_dashes(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _norm_dashes(v) for k, v in obj.items()}
    return obj


def extract(text: str, hint: str = "") -> dict:
    """Определить тип документа и извлечь данные (через Искру).

    hint – подсказка от пользователя, если он уточнил тип вручную.
    Возвращает {doc_type, question_num, confidence, summary, data}.
    """
    user = text if not hint else "Подсказка от пользователя: это документ типа \u00ab" + hint + "\u00bb.\n\n" + text
    last_err = None
    strict = False
    for _ in range(2):  # одна повторная попытка при сбое; после ответа прозой – со строгим напоминанием про JSON
        try:
            content = _call_iskra(user, strict=strict).strip()
            return check_schema(_parse_json_result(content))
        except ValueError as e:      # не JSON – Искра ответила текстом; второй раз просим жёстче
            last_err = e
            strict = True
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err))


def _norm(s: str) -> str:
    """Нормализация для сравнения: нижний регистр + схлопывание пробелов."""
    return " ".join((s or "").strip().lower().split())


def _lev(a: str, b: str) -> int:
    """Расстояние Левенштейна."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _surname(name: str) -> str:
    parts = _norm(name).split()
    return parts[0] if parts else ""


def _collect_names(result: dict) -> list[tuple[str, str]]:
    """Все имена из результата разбора: [(имя, где встречается)]."""
    out = []
    data = result.get("data") or {}
    for a in data.get("agenda", []):
        if not isinstance(a, dict):
            continue
        for sp in a.get("speakers", []):
            if sp:
                out.append((sp, "повестка – докладчик"))
    for prt in data.get("participants", []):
        if isinstance(prt, dict) and prt.get("name"):
            out.append((prt["name"], "участник"))
    for o in data.get("orders", []):
        if isinstance(o, dict) and o.get("assignee"):
            out.append((o["assignee"], "поручение – ответственный"))
    for b in data.get("blocks", []):
        if isinstance(b, dict) and b.get("speaker"):
            out.append((b["speaker"], "сценарий – выступающий"))
    return out


def verify_names(result: dict, source_text: str) -> dict:
    """Ограничитель: проверить, что извлечённые имена реально есть в исходнике.

    LLM может «прочитать» фамилию с опечаткой (напр. КАДОЧНИКОВ -> Кадошников).
    Сверяем каждое имя с исходным текстом и помечаем подозрительные.
    Добавляет в result ключ «warnings»: [{"name","field","suggestion"}].
    """
    src = _norm(source_text)
    src_words = [w.strip('.,;:()[]«»"\'––-') for w in src.split()]
    src_words = [w for w in src_words if w]
    warnings = []
    for a in result.get("data", {}).get("agenda", []):
        if not isinstance(a, dict):
            continue
        title = (a.get("title") or "").strip()
        if title and not _is_opening(title) and not _is_closing(title) and not a.get("speakers"):
            warnings.append({"name": title[:60], "field": "повестка – вопрос без докладчика", "suggestion": ""})

    corrections = {}  # старое имя -> исправленное (автозамена опечаток)
    for name, field in _collect_names(result):
        n = _norm(name)
        sn = _surname(name)
        if (n in src) or (sn and sn in src):
            continue
        suggestion = ""
        best = 99
        for w in src_words:
            d = _lev(sn, w)
            if d <= 2 and d < best and abs(len(sn) - len(w)) <= 2 and len(w) >= 3:
                best = d
                suggestion = w
        if suggestion:
            corrected_surname = suggestion.title()
            parts = (name or "").split()
            corrected = corrected_surname + (" " + " ".join(parts[1:]) if len(parts) > 1 else "")
            corrections[name] = corrected
            warnings.append({
                "name": name,
                "field": field,
                "suggestion": corrected,
                "corrected": True,
            })
        else:
            warnings.append({"name": name, "field": field, "suggestion": ""})
    if corrections:
        _apply_corrections(result, corrections)
    result["warnings"] = warnings
    return result


def compute_diff(m: Meeting, result: dict) -> dict:
    """Сверить извлечённое с текущим совещанием: сколько нового, сколько уже есть."""
    doc_type = result.get("doc_type", "неизвестно")
    data = result.get("data") or {}
    diff = {"new": 0, "existing": 0, "details": []}
    if doc_type == "повестка":
        existing = {_norm(a.title) for a in m.agenda if a.title.strip()}
        for a in data.get("agenda", []):
            t = _norm(a.get("title", ""))
            if not t:
                continue
            st = "existing" if t in existing else "new"
            diff["existing" if st == "existing" else "new"] += 1
            diff["details"].append({"title": a.get("title", ""), "status": st})
    elif doc_type == "участники":
        existing = {_norm(p.name) for p in m.participants}
        for p in data.get("participants", []):
            n = _norm(p.get("name", ""))
            if not n:
                continue
            st = "existing" if n in existing else "new"
            diff["existing" if st == "existing" else "new"] += 1
            diff["details"].append({"title": p.get("name", ""), "status": st})
    elif doc_type == "поручения":
        existing = {_norm(o.text) for o in m.orders}
        for o in data.get("orders", []):
            t = _norm(o.get("text", ""))
            if not t:
                continue
            if t in existing:
                diff["existing"] += 1
            else:
                diff["new"] += 1
    return diff


def compute_registry_conflicts(m: "Meeting | None", result: dict) -> list[dict]:
    """Расхождения по должностям участников: новый список / повестка / общий реестр."""
    data = result.get("data") or {}
    if result.get("doc_type") != "участники":
        return []
    conflicts = []
    # записи реестра по всем участникам — одним параллельным заходом, а не по очереди
    known = portal.people_parallel(portal.people_get, [p.get("name", "") for p in data.get("participants", [])
                                                        if isinstance(p, dict)], what="окно расхождений")
    for p in data.get("participants", []):
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        role = p.get("role", "")
        if not name:
            continue
        n = _norm(name)
        # роль из повестки (если человек — докладчик)
        agenda_role = ""
        if m is not None:
            for a in m.agenda:
                for si, sp in enumerate(a.speakers):
                    if _norm(sp) == n:
                        agenda_role = a.speaker_roles[si] if si < len(a.speaker_roles) else ""
                        break
        # роль из общего реестра людей (портал)
        entry = known.get(name)
        registry_role = entry["role"] if entry else ""
        registry_org = entry["organization"] if entry else ""
        roles = {}
        if role:
            roles["new"] = role
        if agenda_role:
            roles["agenda"] = agenda_role
        if registry_role:
            roles["registry"] = registry_role
        if len({_norm(r) for r in roles.values()}) > 1:
            conflicts.append({
                "name": name,
                "new_role": role,
                "agenda_role": agenda_role,
                "registry_role": registry_role,
                "registry_org": registry_org,
            })
    return conflicts


def _apply_corrections(result: dict, corrections: dict) -> None:
    """Заменить имена на исправленные во всех полях результата разбора."""
    data = result.get("data") or {}

    def fix(v):
        return corrections.get(v, v)

    for a in data.get("agenda", []):
        if not isinstance(a, dict):
            continue
        a["speakers"] = [fix(x) for x in a.get("speakers", [])]
    for p in data.get("participants", []):
        if isinstance(p, dict) and p.get("name") in corrections:
            p["name"] = corrections[p["name"]]
    for o in data.get("orders", []):
        if isinstance(o, dict) and o.get("assignee") in corrections:
            o["assignee"] = corrections[o["assignee"]]
    for b in data.get("blocks", []):
        if isinstance(b, dict) and b.get("speaker") in corrections:
            b["speaker"] = corrections[b["speaker"]]


def _attendance(v: str) -> str:
    v = (v or "").strip().lower()
    if "вкс" in v or "видео" in v or "удал" in v:
        return "remote"
    if "очн" in v or "очно" in v:
        return "in_person"
    return "unknown"


def _fillable(v: str) -> bool:
    """Поле можно заполнить, если оно пустое или содержит заглушку («тестовое»)."""
    v = (v or "").strip()
    if not v:
        return True
    low = v.lower()
    return low.startswith("тест") or low.startswith("test")


def apply_to_meeting(m: Meeting, result: dict, replace: bool = False, resolutions: dict | None = None) -> Meeting:
    """Применить результат разбора: добавление, либо (при replace) полная замена раздела."""
    doc_type = result.get("doc_type", "неизвестно")
    data = result.get("data") or {}
    chair_norm = _norm(m.chair) if m.chair else ""  # председатель – в названии совещания, не в участниках
    resolutions = {_norm(k): v for k, v in (resolutions or {}).items()}  # нормализовать ключи имён

    if replace:
        if doc_type == "повестка":
            m.agenda.clear()
            # производные данные старой повестки привязаны к старым номерам вопросов –
            # иначе после замены «подцепятся» к новым вопросам с теми же номерами
            m.materials.clear()
            m.orders.clear()
            m.scenario.clear()
        elif doc_type == "участники":
            m.participants.clear()
        elif doc_type == "поручения":
            m.orders.clear()
        elif doc_type == "модератор":
            m.scenario.clear()

    if doc_type == "повестка":
        if data.get("title"):
            # полное наименование из повестки — в subtitle: title — ключ find-or-create и источник
            # короткого названия, его переписывание завело бы второе совещание (ревьюер RC-22-1, К2)
            m.subtitle = data["title"]
        if _fillable(m.date) and data.get("date"):
            m.date = normalize_date(data["date"])
        if _fillable(m.time_start) and data.get("time"):
            m.time_start = data["time"]
        if _fillable(m.location_primary) and data.get("location"):
            m.location_primary = data["location"]
        if _fillable(m.chair) and data.get("chair"):
            m.chair = data["chair"]
        existing_titles = {_norm(a.title) for a in m.agenda if a.title.strip()}
        for a in data.get("agenda", []):
            t = _norm(a.get("title", ""))
            if not replace and t and t in existing_titles:
                continue  # вопрос уже есть – не дублируем
            spk = [x for x in a.get("speakers", []) if x]
            roles = list(a.get("speaker_roles", []))
            mins = [int(v) for v in a.get("speaker_minutes", [])]
            roles = (roles + [""] * len(spk))[:len(spk)]
            mins = (mins + [5] * len(spk))[:len(spk)]   # по умолчанию 5 минут
            mins = [m if m > 0 else 5 for m in mins]
            m.agenda.append(AgendaItem(
                num=str(a.get("num", "")),
                title=a.get("title", ""),
                time=a.get("time", ""),
                speakers=spk,
                speaker_roles=roles,
                speaker_minutes=mins,
            ))
            if t:
                existing_titles.add(t)
        # если председатель есть, а Искра не выделила вступительное/заключительное слово – добавить
        if m.chair and not any(_is_opening(a.title) for a in m.agenda):
            m.agenda.insert(0, AgendaItem(num="", title="Вступительное слово", speakers=[m.chair], speaker_roles=[""], speaker_minutes=[5]))
        if m.chair and not any(_is_closing(a.title) for a in m.agenda):
            m.agenda.append(AgendaItem(num="", title="Подведение итогов", speakers=[m.chair], speaker_roles=[""], speaker_minutes=[5]))
        renumber_agenda(m)
        # докладчики/содокладчики из повестки – тоже в список участников (без дублей)
        existing_names = {_norm(p.name) for p in m.participants}
        for a in m.agenda:
            if not a.num.strip():
                continue  # вступительное/подведение итогов – председатель, не докладчики
            for si, sp in enumerate(a.speakers):
                if not sp or _norm(sp) in existing_names:
                    continue
                if chair_norm and _norm(sp) == chair_norm:
                    continue  # председатель – в названии совещания
                role = a.speaker_roles[si] if si < len(a.speaker_roles) else ""
                m.participants.append(Participant(
                    name=sp,
                    role=role,
                    organization="",
                    attendance="in_person",
                    is_speaker=True,
                    protocol_rank=protocol_rank(role),
                ))
                existing_names.add(_norm(sp))
                if role:
                    portal.note_person(sp, role)

    elif doc_type == "участники":
        existing = {_norm(p.name): p for p in m.participants}
        # «взять из реестра» — записи по всем таким людям одним параллельным заходом
        from_registry = portal.people_parallel(portal.people_get, [
            p.get("name", "") for p in data.get("participants", [])
            if isinstance(p, dict) and resolutions.get(_norm(p.get("name", ""))) == "registry"],
            what="«взять из реестра»")
        for p in data.get("participants", []):
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if not name:
                continue
            key = _norm(name)
            if chair_norm and key == chair_norm:
                continue  # председатель – в названии совещания, не в списке участников
            role = p.get("role", "")
            organization = p.get("organization", "")
            # разрешение расхождения: откуда брать роль (новый список / повестка / реестр)
            src = resolutions.get(key, "new")
            if src == "registry":
                entry = from_registry.get(name)
                if entry:
                    role = entry["role"]
                    organization = entry["organization"] or organization
            elif src == "agenda":
                for a in m.agenda:
                    for si, sp in enumerate(a.speakers):
                        if _norm(sp) == key:
                            role = a.speaker_roles[si] if si < len(a.speaker_roles) else ""
                            break
            # в реестр не пишем: человек увидит окно «Добавить в реестр?» (../CLAUDE.md п. 5а)
            if role or organization:
                portal.note_person(name, role, organization)
            # добавить/обогатить участника
            if key in existing:
                ex = existing[key]
                if role and not ex.role:
                    ex.role = role
                    ex.protocol_rank = protocol_rank(role)
                if organization and not ex.organization:
                    ex.organization = organization
                continue
            m.participants.append(Participant(
                name=name,
                role=role,
                organization=organization,
                attendance=_attendance(p.get("attendance", "")),
                is_speaker=bool(p.get("is_speaker", False)),
                protocol_rank=protocol_rank(role),
            ))
            existing[key] = m.participants[-1]

    elif doc_type == "поручения":
        existing_texts = {_norm(o.text) for o in m.orders}
        for o in data.get("orders", []):
            if not o.get("text"):
                continue
            if not replace and _norm(o.get("text")) in existing_texts:
                continue
            m.orders.append(Order(
                text=o.get("text", ""),
                assignee=o.get("assignee", ""),
                deadline=normalize_deadline(_as_text(o.get("deadline")))[:DEADLINE_MAX],
                agenda_num=o.get("agenda_num", ""),
                source="file",           # документ с поручениями — в перечне выше поручений из материалов
            ))
        m.orders = file_orders_first(m.orders)

    elif doc_type == "модератор":
        if data.get("opening"):
            m.scenario.append(ScenarioBlock(section="opening", kind="opening", text=str(data["opening"])))
        for b in data.get("blocks", []):
            kind = b.get("kind", "intro")
            m.scenario.append(ScenarioBlock(
                section="question",
                agenda_num=str(b.get("question_num", "")),
                kind=kind if kind in ("intro", "replica", "report", "comment", "question", "order", "problem", "decision", "task", "reference") else "intro",
                text=b.get("text", ""),
                speaker=b.get("speaker", ""),
                minutes=int(b.get("minutes", 0) or 0),
            ))
        if data.get("closing"):
            m.scenario.append(ScenarioBlock(section="closing", kind="closing", text=str(data["closing"])))

    elif doc_type == "материалы":
        data = dict(data)
        data["question_num"] = str(data.get("question_num") or result.get("question_num") or "")
        _store_material(m, data, filename="")

    # документ о составе (участники/повестка) задаёт список заново — снимаем
    # ручную правку порядка и выстраиваем по протокольному старшинству
    if doc_type in ("участники", "повестка"):
        m.participants_manual = False
    if not m.participants_manual:
        m.participants.sort(key=lambda p: sort_key(p.protocol_rank, p.name))
    return m


def _role_from_directory(name: str) -> str:
    """Должность по ФИО из общего реестра людей (портал)."""
    matches = portal.people_search(name)
    if not matches:
        return ""
    low = _norm(name)
    for p in matches:
        if _norm(str(p.get("name", ""))) == low:
            return str(p.get("role") or "")
    return str(matches[0].get("role") or "")


def sync_agenda_participants(m: Meeting, fill_roles: bool = True) -> None:
    """Синхронизация списка участников с докладчиками повестки.

    Докладчик/содокладчик из повестки автоматически попадает в участники;
    если человека убрали из повестки — он убирается из участников (если был там как докладчик).
    Пустые должности дозаполняются из общего реестра людей (портал) — только когда докладчика
    добавляют или импортируют (`fill_roles`). При открытии совещания в портал не ходим:
    открытие не должно ждать соседа (SRE 21.09: GET совещания шёл 10–12 с).
    """
    chair_norm = _norm(m.chair) if m.chair else ""
    speakers: dict[str, tuple[str, str]] = {}  # norm -> (orig, role)
    looked_up: dict[str, str] = {}
    if fill_roles:
        # должности докладчиков без должности — параллельно и с общим бюджетом (SRE 21.09)
        missing = [sp for a in m.agenda if a.num.strip() for si, sp in enumerate(a.speakers)
                   if _norm(sp) and not (chair_norm and _norm(sp) == chair_norm)
                   and not (a.speaker_roles[si] if si < len(a.speaker_roles) else "")]
        looked_up = portal.people_parallel(_role_from_directory, missing, what="должности докладчиков")
    for a in m.agenda:
        if not a.num.strip():
            continue  # вступительное/подведение итогов — председатель, не докладчики
        for si, sp in enumerate(a.speakers):
            n = _norm(sp)
            if not n or (chair_norm and n == chair_norm):
                continue
            role = a.speaker_roles[si] if si < len(a.speaker_roles) else ""
            if not role and fill_roles:
                role = looked_up.get(sp, "")
                if role and si < len(a.speaker_roles):
                    a.speaker_roles[si] = role
            if n not in speakers or role:
                speakers[n] = (sp, role)
    existing = {_norm(p.name): p for p in m.participants}
    for n, (orig, role) in speakers.items():
        p = existing.get(n)
        if p is not None:
            # дозаполнить пустую должность/ранг у уже добавленного участника
            if role and not p.role:
                p.role = role
                p.protocol_rank = protocol_rank(role)
            continue
        m.participants.append(Participant(
            name=orig, role=role, organization="", attendance="in_person",
            is_speaker=True, protocol_rank=protocol_rank(role),
        ))
    # убрать участников-докладчиков, которых больше нет в повестке
    m.participants = [p for p in m.participants
                      if not (p.is_speaker and _norm(p.name) not in speakers)]
    # Если порядок задан вручную (стрелки ↑/↓), протокольный не навязываем:
    # иначе ручная правка терялась бы при каждом открытии совещания.
    if not getattr(m, "participants_manual", False):
        m.participants.sort(key=lambda p: sort_key(p.protocol_rank, p.name))


CLOSING_PROMPT = (
    "Ты — помощник председателя совещания. Напиши ЗАКЛЮЧИТЕЛЬНОЕ СЛОВО (подведение итогов) "
    "по итогам совещания на основе тезисов, выводов и предложений по каждому вопросу.\n"
    "Стиль: официально-деловой, от имени председателя, без местоимений «я».\n"
    "Структура:\n"
    "- каждое предложение — отдельный новый абзац и отдельный пункт маркированного списка;\n"
    "- не объединяй два предложения в один пункт;\n"
    "- не используй нумерованный список.\n"
    "Правила:\n"
    "- Не выдумывай факты, цифры и решения — бери их ТОЛЬКО из предоставленных тезисов, выводов и предложений.\n"
    "- Используй только короткое тире «–», не длинное «—».\n"
    "- Верни ТОЛЬКО текст заключительного слова, без markdown-разметки, без заголовков и пояснений."
)


def _questions(m: Meeting) -> list[AgendaItem]:
    """Вопросы повестки по порядку — без «Вступительного слова» и «Подведения итогов»."""
    return [a for a in m.agenda if a.num.strip() and not _is_opening(a.title) and not _is_closing(a.title)]


def build_closing_input(m: Meeting) -> str:
    """Собрать тезисы, выводы и предложения всех материалов (по вопросам, по порядку)."""
    parts = []
    for q in _questions(m):
        mats = [mat for mat in m.materials if str(mat.question_num) == str(q.num)]
        for mat in mats:
            if not mat.theses and not mat.proposal and not mat.conclusion and not mat.proposals:
                continue
            block = []
            if mat.title:
                block.append("Материал: " + mat.title)
            if mat.theses:
                block.append("Тезисы:\n" + "\n".join("- " + t for t in mat.theses))
            if mat.conclusion or mat.proposals:
                if mat.conclusion:
                    block.append("Вывод: " + mat.conclusion)
                if mat.proposals:
                    block.append("Предложения:\n" + "\n".join("- " + p for p in mat.proposals))
            elif mat.proposal:
                block.append("Предложение: " + mat.proposal)
            parts.append("Вопрос " + str(q.num) + ": " + q.title + "\n" + "\n".join(block))
    return "\n\n".join(parts)


# сокращения, после которых точка не кончает предложение (ревьюер RC-2026-10-02-15, Н-10).
# «тыс.», «млн», «руб.» сюда не входят: ими предложение кончается часто, а Искра пишет числа словами
_NOT_SENTENCE_END = re.compile(r"(?:^|[\s(])(?:г|гг|ст|п|пп|им|ул|д|рис|табл|т\.\s?[едп])\.$", re.I)


def split_bullets(text: str) -> list[str]:
    """Ответ Искры → предложения по одному: маркеры и нумерация сняты, точка в конце.

    Инициал («А.А. Корнеев») предложение не обрывает.
    """
    raw = (text or "").replace("**", "").replace("__", "").strip()
    if not raw:
        return []
    # Убираем возможные маркеры/нумерацию от Искры и режем на предложения.
    raw = re.sub(r"(?m)^\s*(?:[-*•·▪–]|\d{1,2}[.)])\s+", "", raw)
    raw = re.sub(r"(?<=[.!?…])\s+(?:[-*•·▪]|\d{1,2}[.)])\s+", " ", raw)
    items = []
    for line in raw.split("\n"):              # строка ответа — граница пункта, даже без точки
        parts: list[str] = []
        for part in re.split(r"(?<![\s.][А-ЯЁA-Z]\.)(?<=[.!?…])\s+(?=[А-ЯA-ZЁ«])", " " + line):
            if parts and _NOT_SENTENCE_END.search(parts[-1]):
                parts[-1] += " " + part          # «в г. Москве», «ст. 12» — не конец предложения
            else:
                parts.append(part)
        for part in parts:
            s = " ".join(part.split()).strip()
            if not s:
                continue
            if s[-1] not in ".!?…":
                s += "."
            items.append(s)
    return items


def format_closing_as_bullets(text: str) -> str:
    """Заключение: каждое предложение — отдельный маркированный абзац."""
    items = split_bullets(text)
    if not items:
        return ""
    return "<ul>" + "".join("<li>" + html.escape(s) + "</li>" for s in items) + "</ul>"


def generate_closing(input_text: str) -> str:
    """Искра пишет заключение; приложение сохраняет его маркированным списком."""
    content = str(_call_iskra_raw(CLOSING_PROMPT, input_text)).strip()
    return format_closing_as_bullets(_norm_dashes(content))


# ── вступительное слово из материалов (решение пользователя 02.10) ───────

WORDS_PER_MINUTE = 120            # как в «Тезисах» (theses/app/models.py): темп чтения вслух

OPENING_PROMPT = (
    "Ты — помощник председателя совещания. Напиши ВСТУПИТЕЛЬНОЕ СЛОВО председателя по материалам ниже.\n"
    "Стиль: официально-деловой, устная речь, от имени председателя, без местоимения «я» "
    "(«Коллеги, …», «Наша задача – …», «Важно обеспечить …»).\n"
    "Структура – по порядку:\n"
    "1) рамка и повод совещания: зачем собрались, государственный контекст;\n"
    "2) масштаб и приоритеты – из блока «Общие материалы» (если он есть): 1–3 пункта;\n"
    "3) по каждому вопросу повестки по порядку – 1–2 пункта: что обсуждаем и почему это важно, "
    "и кто доложит: «по второму вопросу доложит Кравцов Сергей Сергеевич, Министр просвещения» – ФИО и должность "
    "в именительном падеже, ТОЛЬКО в том виде, как в строке «Докладчики:» этого вопроса, не сокращай. "
    "Слово никому не передавай – только назови, кто доложит по вопросу; передача слова идёт репликами сценария "
    "по каждому вопросу. Не пиши «прошу доложить», «прошу Вас», «слово предоставляется». "
    "Нет строки «Докладчики:» у вопроса – докладчика не называй.\n"
    "Вступительное слово подводит к докладам, но НЕ пересказывает их:\n"
    "- не повторяй цифры и выводы из тезисов – допустимы 1–2 самые верхнеуровневые цифры на всё слово, округлённые "
    "(«около 260 тысяч», «более 2,5 тысячи»);\n"
    "- не повторяй предложения к решению и поручения – их скажут после докладов;\n"
    "- не выдумывай цифры, сроки, решения и поручения – только то, что есть в материалах;\n"
    "- без канцеляризмов («представляется целесообразным», «в рамках реализации», «следует отметить», "
    "«осуществлять комплекс мероприятий») – называй конкретное действие;\n"
    "- один пункт – одна мысль, 15–30 слов.\n"
    "Оформление: каждое предложение – отдельный пункт маркированного списка, каждый с новой строки, начиная с «- »; "
    "без нумерации, заголовков, markdown и пояснений. Только короткое тире «–», не длинное «—».\n"
    "Верни ТОЛЬКО текст вступительного слова."
)


def _opening_block(m: Meeting) -> ScenarioBlock | None:
    return next((b for b in m.scenario if b.section == "opening"), None)


def opening_words(m: Meeting) -> tuple[int, int]:
    """Хронометраж вступительного слова: (минут, слов) — по блоку opening, иначе 5 минут."""
    b = _opening_block(m)
    minutes = b.minutes if b is not None and b.minutes > 0 else 5
    return minutes, minutes * WORDS_PER_MINUTE


def _speaker_line(m: Meeting, item: AgendaItem, si: int) -> str:
    """«ФИО, должность» в именительном — должность из повестки, иначе из участников (как в build_scenario)."""
    from .models import _find_role
    sp = item.speakers[si]
    role = (item.speaker_roles[si] if si < len(item.speaker_roles) and item.speaker_roles[si] else "") \
        or _find_role(m.participants, sp)
    return sp + (f", {role}" if role else "")


def build_opening_input(m: Meeting) -> str:
    """Вход для вступительного слова: сначала общие материалы, затем вопросы повестки по порядку.

    Общие материалы (без вопроса) — масштаб и приоритеты: название, тезисы, вывод.
    По вопросу — формулировка, докладчики «ФИО, должность» в именительном (только назвать: слово
    передают реплики сценария) и тезисы/подводка
    его материалов: Искре — понять, о чём вопрос, а не пересказать доклад. Предложения и поручения
    не передаём вовсе: им место после докладов и в заключительном слове.
    Нет ни одного материала — пустая строка (маршрут ответит 400).
    """
    if not m.materials:
        return ""
    minutes, words = opening_words(m)
    head = [f"Совещание: {m.full_title or m.title}"]
    if m.date:
        head.append(f"Дата: {m.date}")
    if m.chair:
        head.append(f"Председатель: {m.chair}")
    head.append(f"Хронометраж вступительного слова: {minutes} мин – около {words} слов, не больше.")
    parts = ["\n".join(head)]

    common = [x for x in m.materials if not str(x.question_num or "").strip()]
    if common:
        lines = ["Общие материалы (без привязки к вопросу):"]
        for mat in common:
            lines.append("Материал: " + (mat.title or mat.filename or mat.type))
            if mat.theses:
                lines.append("Тезисы:\n" + "\n".join("- " + t for t in mat.theses))
            if mat.conclusion:
                lines.append("Вывод: " + mat.conclusion)
        parts.append("\n".join(lines))

    for q in _questions(m):
        lines = [f"Вопрос {q.num}. {q.title}"]
        names = [i for i, sp in enumerate(q.speakers) if sp]
        if names:
            # только назвать, кто доложит: вступительное слово звучит целиком до первого вопроса, слово
            # передают реплики сценария «по N вопросу прошу доложить …» (решение Оркестратора 02.10)
            lines.append("Докладчики: " + "; ".join(_speaker_line(m, q, i) for i in names))
        for mat in (x for x in m.materials if str(x.question_num) == str(q.num)):
            if mat.intro:
                lines.append("О чём вопрос: " + clean_intro(mat.intro))
            if mat.theses:
                lines.append("Тезисы материала:\n" + "\n".join("- " + t for t in mat.theses))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def generate_opening(input_text: str) -> list[str]:
    """Искра пишет вступительное слово; ответ — пункты по предложению (как заключительное слово)."""
    content = str(_call_iskra_raw(OPENING_PROMPT, input_text)).strip()
    return split_bullets(_norm_dashes(content))


# ── разбор материала к вопросу ──────────────────────────────────────────

MATERIAL_PROMPT = (
    "Ты разбираешь МАТЕРИАЛ к совещанию (справку, доклад, презентацию, письмо, аналитическую записку и т.п.) "
    "для председателя совещания. Из разбора соберутся вступительное слово, вывод и проект поручений.\n"
    "Верни СТРОГО валидный JSON без пояснений и без markdown, по схеме:\n"
    '{"type":"...","question_num":"1","speaker":"...","title":"...","intro":"...","summary":"...",'
    '"figures":[{"title":"...","unit":"%","type":"bar_h|bar_v|line|donut","items":[["2024",39],["2025",45]],"source":"..."}],'
    '"theses":["..."],"conclusion":"...","proposals":["..."],'
    '"orders":[{"text":"...","assignee":"...","deadline":""}]}\n'
    "Поля:\n"
    "- type – тип материала (Доклад выступающего | Презентация | Справка | Статистика / данные | "
    "Аналитическая записка | Инфографика | Нормативный документ | Письмо / обращение | Тезисы выступления | "
    "Саммари / одностраничник | Материал к вопросу).\n"
    "- question_num – к какому вопросу повестки относится (1, 2, 3...), если понятно; иначе пустая строка.\n"
    "- speaker – чей доклад (Фамилия Имя Отчество), если это доклад конкретного человека; иначе пустая строка.\n"
    "- title – краткое название материала.\n"
    "- intro – ПОДВОДКА К ВОПРОСУ: 1-2 коротких предложения, о чём вопрос, без обращения к докладчику: "
    "«О подключении школ к скоростному интернету», «Речь о готовности школ к отопительному сезону» – "
    "а НЕ «Прошу доложить о …» (слово передают отдельные реплики сценария). Без цифр и деталей – только суть.\n"
    "- summary – ПОДРОБНОЕ САММАРИ: ВСЕ цифры, значения, факты, проблемы и выводы материала, ничего важного не теряя; "
    "объём не ограничен. Абзацы разделяй пустой строкой; перечни цифр и показателей – пунктами, каждый с новой строки, "
    "начиная с «- ». Цифры переноси точно, как в тексте.\n"
    "- figures – диаграммы, ТОЛЬКО если в тексте есть ряд чисел: динамика по годам/месяцам, сравнение регионов/объектов, "
    "структура долей. 0–4 штуки; нет рядов – пустой массив. type по смыслу: динамика – line (много точек) или bar_v, "
    "сравнение – bar_h, структура долей целого – donut. items – пары [подпись, число] из текста, от 2 до 12; число – "
    "числом, без единиц и пробелов; unit – единица («%», «тыс. человек», «млрд руб.»); title – что показано; "
    "source – строка или цитата из текста, откуда цифры. Одна цифра – не диаграмма. Ничего не досчитывай и не дорисовывай.\n"
    "- theses – 3-4 тезиса для вступительного слова председателя, КАЖДЫЙ 20-25 слов. Формула тезиса: "
    "ФАКТ → ЗНАЧЕНИЕ ФАКТА → УПРАВЛЕНЧЕСКИЙ ВЫВОД. Один тезис – одна мысль: не складывай в него факт, три цифры, "
    "проблему и поручение. Только самые верхнеуровневые цифры (масштаб, результат, динамика, проблема), не больше "
    "одной-двух на тезис, округлённые для устной речи («около 260 тысяч», «более 2,5 тысячи», «порядка 713 млрд рублей»). "
    "Активный язык («Наша задача – …», «Важно обеспечить …», «Необходимо …»).\n"
    "- conclusion – вывод: 1-2 предложения, к чему всё ведёт, какое решение напрашивается.\n"
    "- proposals – 1-4 предложения к решению по итогам вопроса, каждое – одно предложение активным языком "
    "(«Предлагаю определить …», «Прошу поддержать …», «Необходимо …»). Из них соберётся заключительное слово.\n"
    "- orders – ПРЕДЛОЖЕНИЯ ПОРУЧЕНИЙ по итогам материала (человек сам отметит, какие включить): не только явно "
    "звучащие в тексте, но и вытекающие из выводов. Каждое – по формуле КТО → ЧТО ДЕЛАЕТ → КАКОЙ РЕЗУЛЬТАТ → "
    "К КАКОМУ СРОКУ. assignee – исполнитель (Фамилия И.О. или орган: «Минпросвещения России», «органы исполнительной "
    "власти субъектов»), если его можно определить из текста, иначе пустая строка. deadline – срок ДД.ММ.ГГГГ ТОЛЬКО если "
    "он прямо есть в тексте, иначе пустая строка. Нет оснований для поручений – пустой массив.\n"
    "Правила:\n"
    "1. Не выдумывай цифры, сроки, исполнителей, решения и поручения Президента и Правительства – только то, что есть "
    "в тексте. Правдоподобная выдуманная цифра опаснее отсутствующей.\n"
    "2. Тезисы, вывод, предложения и поручения – только текст: без markdown, без **, без нумерации и маркеров внутри строки.\n"
    "3. Без канцеляризмов: «представляется целесообразным», «следует отметить», «в рамках реализации», "
    "«осуществлять комплекс мероприятий», «принять исчерпывающие меры», «активизировать работу» – называй конкретное действие.\n"
    "4. Фамилии приводи к виду «Фамилия Имя Отчество».\n"
    "5. Если тип неясен – ставь «Материал к вопросу».\n"
    "6. Во всех текстовых полях – только короткое тире «–», не длинное «—».\n"
    "7. Никогда не отвечай прозой и не задавай вопросов – только JSON по схеме."
)

MATERIAL_STRICT_REMINDER = (
    "Предыдущий ответ был не JSON, а текст. Ответь ЕЩЁ РАЗ и верни ТОЛЬКО один JSON-объект по схеме из инструкции, "
    "без пояснений, без markdown и без вопросов.\n\n"
)


def _parse_json(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return json.loads(content)


def analyze_material(text: str, filename: str = "", meeting: "Meeting | None" = None) -> dict:
    """Разобрать материал через Искру → результат по схеме `check_material_schema`.

    Замечания (отброшенные диаграммы, длина тезисов, докладчик не из текста) — строками
    в `result["problems"]`: маршрут отдаёт их верхним `warnings`. `result["warnings"]` —
    прежний вид для старого окна: [{name, field, suggestion}] по докладчику.
    """
    context = ""
    if meeting is not None:
        qs = [f"{a.num}. {a.title}" for a in meeting.agenda if a.num.strip()]
        if qs:
            context += "Вопросы повестки:\n" + "\n".join(qs) + "\n\n"
        ps = [p.name + (f" – {p.role}" if p.role else "") for p in meeting.participants]
        if ps:
            context += "Участники совещания:\n" + "\n".join(ps) + "\n\n"
    user = (f"Имя файла: {filename}\n\n" if filename else "") + (context or "") + text
    last_err = None
    result = None
    strict = False
    for _ in range(2):  # одна повторная попытка при сбое; после ответа прозой – со строгим напоминанием про JSON
        try:
            content = _call_iskra_raw(MATERIAL_PROMPT, (MATERIAL_STRICT_REMINDER + user) if strict else user)
            result = _parse_json_result(content)
            break
        except ValueError as e:
            last_err = e
            strict = True
        except Exception as e:
            last_err = e
    if result is None:
        raise RuntimeError("Искра не ответила: " + str(last_err))
    # привести ответ к схеме материала: лишнее отбросить, «список» из строки не разбирать по буквам
    problems: list[str] = []
    result = check_material_schema(result, problems)
    # ограничитель: спикер должен реально встречаться в тексте (против галлюцинаций)
    warnings = []
    sp = result.get("speaker") or ""
    if sp and _surname(sp) and _norm(_surname(sp)) not in _norm(text):
        warnings.append({"name": sp, "field": "материал – докладчик", "suggestion": ""})
        problems.append(f"докладчик «{sp}» не найден в тексте материала – проверьте")
    if not result.get("text"):
        result["text"] = text            # содержимое материала сохраняется вместе с разбором
    result["warnings"] = warnings
    result["problems"] = problems
    result["filename"] = filename
    return result


def _order_key(text: str) -> str:
    """Поручение для сравнения на дубль: регистр, ё, пробелы, кавычки и точка в конце не различают."""
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[«»\"'“”]", "", t)
    t = " ".join(t.split())
    return t.rstrip(" .;")


def _material_orders(data: dict) -> list[dict]:
    """Поручения из результата: объекты {text, assignee, deadline}; строки старых ответов — тоже."""
    out = []
    for o in data.get("orders") or []:
        if isinstance(o, dict):
            text = _as_text(o.get("text"))
            if text:
                out.append({"text": text, "assignee": _as_text(o.get("assignee")),
                            "deadline": _as_text(o.get("deadline"))})
        elif _as_text(o):
            out.append({"text": _as_text(o), "assignee": "", "deadline": ""})
    return out


def _store_material(m: Meeting, data: dict, filename: str = "") -> Material:
    """Сохранить материал + отмеченные поручения (с material_id) + вплести в сценарий (подводка и предложение)."""
    from . import charts
    qnum = str(data.get("question_num") or "")
    orders = _material_orders(data)
    figures = []
    for f in data.get("figures") or []:
        try:
            clean = charts.check_figure(f)
            clean["svg"] = charts.svg(clean)       # svg только свой — из items
        except ValueError:
            continue
        clean["id"] = f"f{len(figures) + 1}"
        figures.append(clean)
    conclusion = _as_text(data.get("conclusion"))
    proposals = _as_texts(data.get("proposals"))
    mat = Material(
        id=new_id(),
        question_num=qnum,
        type=data.get("type") or "Материал к вопросу",
        speaker=data.get("speaker") or "",
        title=data.get("title") or "",
        text=data.get("text") or "",
        filename=filename or data.get("filename") or "",
        summary=data.get("summary") or "",
        intro=data.get("intro") or "",
        theses=[t for t in (data.get("theses") or []) if t],
        proposal=(compose_proposal(conclusion, proposals) if (conclusion or proposals) else "") or data.get("proposal") or "",
        orders=[o["text"] for o in orders],
        figures=figures,
        conclusion=conclusion,
        proposals=proposals,
    )
    m.materials.append(mat)
    seen = {_order_key(x.text) for x in m.orders}
    for o in orders:
        key = _order_key(o["text"])
        if not key or key in seen:
            continue
        seen.add(key)
        m.orders.append(Order(text=o["text"], assignee=o["assignee"], deadline=o["deadline"],
                              agenda_num=qnum, material_id=mat.id, source="material"))
    inject_material(m, mat)
    return mat


def apply_material(m: Meeting, result: dict, filename: str = "") -> Material:
    """Применить разобранный материал (новый поток: загрузка файла → Искра → предпросмотр).
    Вернёт сам новый материал: его id уходит в ответ `apply` (пометка «в пакете» — именно ему)."""
    return _store_material(m, result, filename=filename or result.get("filename") or "")


# ── перечень поручений: из материалов и из файла (решение пользователя 02.10) ──

ORDERS_PROPOSE_MAX = 100          # предложений за один прогон — больше человек галочками не разберёт


def file_orders_first(orders: list[Order]) -> list[Order]:
    """Порядок перечня (решение пользователя 02.10): поручения из приложенного файла — выше остальных;
    внутри групп порядок прежний (сортировка устойчивая)."""
    return [o for o in orders if o.source == "file"] + [o for o in orders if o.source != "file"]

ORDERS_PROMPT = (
    "Ты готовишь ПРОЕКТ ПЕРЕЧНЯ ПОРУЧЕНИЙ председателя по итогам совещания. Ниже – либо материалы к вопросам "
    "совещания (у каждого метка [M…], номер вопроса, вывод, предложения, тезисы), либо текст документа с "
    "поручениями. Сформулируй поручения; человек сам отметит, какие включить.\n"
    "Верни СТРОГО валидный JSON без пояснений и без markdown, по схеме:\n"
    '{"orders":[{"text":"...","assignee":"...","deadline":"","question_num":"1","material_id":"M1"}]}\n'
    "Поля:\n"
    "- text – поручение в ОФИЦИАЛЬНОЙ ФОРМЕ одним абзацем, как в перечне поручений Правительства: по формуле "
    "КТО → ЧТО ДЕЛАЕТ → КАКОЙ РЕЗУЛЬТАТ → К КАКОМУ СРОКУ. КТО – орган или должность в дательном падеже и, если "
    "человек назван в тексте, его И.О. Фамилия в дательном падеже в скобках: «Минпросвещения России "
    "(С.С. Кравцову) совместно с Минфином России обеспечить …», «Координационному центру Правительства "
    "Российской Федерации (Д.И. Годунову) подготовить …». Дальше – глагол действия в неопределённой форме "
    "и ожидаемый результат. Срок в text не пиши – он отдельным полем. Без номера в начале.\n"
    "- assignee – исполнитель коротко (орган или Фамилия И.О.), если его можно определить из текста, иначе пустая строка.\n"
    "- deadline – срок ТОЛЬКО если он прямо есть в тексте: точная дата – ДД.ММ.ГГГГ («до 25 июня 2026 г.» → "
    "25.06.2026); срок словами, без точной даты, – как в тексте («начиная с 1 сентября 2026 г.», «ежеквартально»); "
    "нет срока в тексте – пустая строка. Срок НИКОГДА не придумывай.\n"
    "- question_num – номер вопроса повестки, к которому относится поручение, если понятно; иначе пустая строка.\n"
    "- material_id – метка материала [M…], из которого поручение выведено; для документа с поручениями – пустая строка.\n"
    "Правила:\n"
    "1. Не выдумывай исполнителей, цифры, сроки, решения и поручения Президента и Правительства – только то, что "
    "есть в тексте или прямо следует из выводов и предложений. Нет оснований для поручения – не пиши его.\n"
    "2. Одно поручение – одно действие одного исполнителя (соисполнители – «совместно с …»); не дублируй поручения.\n"
    "3. Без канцеляризмов: «принять исчерпывающие меры», «активизировать работу», «осуществлять комплекс "
    "мероприятий» – называй конкретное действие и результат.\n"
    "4. Документ с поручениями – перенеси КАЖДОЕ поручение из него в том же порядке, не сокращая смысла; "
    "срок – из строки «Срок – …» этого поручения.\n"
    "5. Только короткое тире «–», не длинное «—». Без markdown и маркеров внутри строки.\n"
    "6. Никогда не отвечай прозой и не задавай вопросов – только JSON по схеме."
)


def check_order_proposals(data, warnings: list[str] | None = None) -> list[dict]:
    """Ответ Искры по поручениям → [{text, assignee, deadline, question_num, material_id}]; лишнее отбрасываем."""
    raw = data.get("orders") if isinstance(data, dict) else data
    out = []
    for o in _as_records(raw, "text"):
        text = _as_text(o.get("text"))
        if not text:
            continue
        if len(out) >= ORDERS_PROPOSE_MAX:
            if warnings is not None:
                warnings.append(f"поручений больше {ORDERS_PROPOSE_MAX} – показаны первые {ORDERS_PROPOSE_MAX}")
            break
        out.append({"text": text[:2000], "assignee": _as_text(o.get("assignee"))[:300],
                    "deadline": normalize_deadline(_as_text(o.get("deadline")))[:DEADLINE_MAX],
                    "question_num": _as_text(o.get("question_num"))[:10],
                    "material_id": _as_text(o.get("material_id"))[:64]})
    return out


def propose_orders(input_text: str, warnings: list[str] | None = None) -> list[dict]:
    """Один прогон Искры по поручениям; ответ прозой — повтор со строгим напоминанием про JSON."""
    last_err = None
    strict = False
    for _ in range(2):
        try:
            content = _call_iskra_raw(ORDERS_PROMPT, (MATERIAL_STRICT_REMINDER + input_text) if strict else input_text)
            return check_order_proposals(_parse_json_result(content), warnings)
        except ValueError as e:
            last_err = e
            strict = True
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err))


def orders_input_from_materials(m: Meeting, materials: list[tuple[str, Material]]) -> str:
    """Вход Искре: материалы без готовых поручений — метка, вопрос, вывод, предложения, тезисы."""
    head = [f"Совещание: {m.full_title or m.title}"]
    if m.chair:
        head.append(f"Председатель: {m.chair}")
    parts = ["\n".join(head)]
    titles = {a.num: a.title for a in m.agenda if a.num.strip()}
    for label, mat in materials:
        q = str(mat.question_num or "").strip()
        lines = [f"[{label}] Материал: " + (mat.title or mat.filename or mat.type),
                 (f"Вопрос {q}. {titles.get(q, '')}".rstrip() if q else "Общий материал (без вопроса)")]
        if mat.speaker:
            lines.append("Докладчик: " + mat.speaker)
        if mat.conclusion:
            lines.append("Вывод: " + mat.conclusion)
        if mat.proposals:
            lines.append("Предложения:\n" + "\n".join("- " + p for p in mat.proposals))
        elif mat.proposal and not mat.conclusion:
            lines.append("Предложение: " + mat.proposal)
        if mat.theses:
            lines.append("Тезисы:\n" + "\n".join("- " + t for t in mat.theses))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ── Помощник повестки: команда на естественном языке → добавление вопроса ──

AGENDA_CMD_SYSTEM = (
    "Ты — ассистент, который по команде пользователя добавляет вопрос в повестку совещания. "
    "Верни ТОЛЬКО один JSON-объект (без markdown-огорожек, без пояснений, без кавычек вокруг) с полями:\n"
    '{"title": "формулировка темы вопроса", "position": <номер позиции от 1 или null>, '
    '"speakers": ["Фамилия Имя Отчество докладчика", "...содокладчики"]}.\n'
    "Правила:\n"
    "- position: если в команде указано место («первым», «вторым», «в конец», «в конце») — поставь число; иначе null.\n"
    "- Первый в speakers — докладчик, остальные — содокладчики.\n"
    "- Тему вопроса запиши аккуратно, без кавычек по краям.\n"
    "- ФИО людей бери как в команде.\n"
    "- Не выдумывай то, чего нет в команде.\n"
    "- Верни только JSON-объект."
)


def _parse_json_lenient(text: str) -> dict:
    """Извлечь JSON-объект из ответа Искры (снимаем markdown и лишний текст)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    a = t.find("{")
    b = t.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise RuntimeError("Искра не вернула JSON: " + t[:200])
    try:
        return json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError("Искра вернула некорректный JSON: " + str(e) + " :: " + t[:200])


def agenda_context_text(m: Meeting) -> str:
    """Краткий текст текущей повестки для контекста Искры."""
    lines = []
    for a in m.agenda:
        who = ", ".join([sp for sp in a.speakers if sp])
        if not a.num.strip():
            lines.append("— " + a.title + ((" (" + who + ")") if who else ""))
        else:
            lines.append(a.num + ". " + a.title + ((" — " + who) if who else ""))
    return "\n".join(lines) if lines else "(повестка пуста)"


def parse_agenda_command(agenda_text: str, command: str) -> dict:
    """Искра превращает команду пользователя в структурированное действие (add_question)."""
    user = "Текущая повестка:\n" + (agenda_text or "(повестка пуста)") + "\n\nКоманда пользователя: «" + command + "»"
    raw = _call_iskra_raw(AGENDA_CMD_SYSTEM, user)
    return _parse_json_lenient(raw)


def _resolve_speaker(name: str) -> tuple[str, str]:
    """«Гуров» → (полное ФИО, должность) из общего реестра людей (если найдено)."""
    name = (name or "").strip()
    if not name:
        return "", ""
    matches = portal.people_search(name)
    if not matches:
        return name, ""
    low = name.lower()
    pick = None
    for p in matches:
        if str(p.get("surname", "")).lower() == low:
            pick = p
            break
    if pick is None:
        pick = matches[0]
    return str(pick.get("name") or name), str(pick.get("role") or "")


def apply_add_question(m: Meeting, action: dict) -> AgendaItem:
    """Добавить вопрос в повестку по структурированному действию Искры."""
    title = str(action.get("title") or "").strip().strip('"').strip()
    if not title:
        raise RuntimeError("Искра не определила тему вопроса")
    speakers = []
    roles = []
    asked = [str(nm).strip() for nm in (action.get("speakers") or [])]
    found = portal.people_parallel(_resolve_speaker, asked, what="докладчики из команды «Искре»")
    for nm in asked:
        r, role = found.get(nm, (nm, ""))
        if r and r not in speakers:
            speakers.append(r)
            roles.append(role)
    item = AgendaItem(
        num="",
        title=title,
        speakers=speakers,
        speaker_roles=roles,
        speaker_minutes=[5] * len(speakers),
    )
    pos = action.get("position")
    try:
        pos = int(pos)
    except (TypeError, ValueError):
        pos = None
    # индексы реальных вопросов (с номером) — «Вступительное слово» не считаем
    qidx = [i for i, a in enumerate(m.agenda) if a.num.strip()]
    if pos and pos >= 1 and (pos - 1) < len(qidx):
        insert_at = qidx[pos - 1]   # перед pos-м вопросом (после вступительного слова)
    else:
        # в конец: перед «Подведением итогов», если оно в конце
        insert_at = len(m.agenda)
        if m.agenda and _is_closing(m.agenda[-1].title):
            insert_at = len(m.agenda) - 1
    m.agenda.insert(insert_at, item)
    renumber_agenda(m)
    sync_agenda_participants(m)
    return item

