"""Связь с порталом workspace.example.ru: пользователь из заголовков nginx и журнал действий.

Вход, пароли и права живут в портале. nginx перед каждым запросом спрашивает портал
и передаёт сюда X-Portal-User / X-Portal-Role вместе с общим секретом X-Portal-Secret —
без верного секрета заголовкам не доверяем (порт 8765 на VPS доступен локальным процессам).
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import queue
import re
import threading
import urllib.request
from pathlib import Path
from urllib.parse import unquote

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOGIN_URL = "/login?next=/moderator/"
log = logging.getLogger("moderator.portal")

# Что накопилось за текущий запрос: сводки изменений из storage.save и объект, если он появился по ходу
# (например, id нового совещания). Объект общий для потоков запроса, поэтому его можно дополнять.
_request: contextvars.ContextVar[dict | None] = contextvars.ContextVar("portal_request", default=None)


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def user_from_headers(headers) -> dict | None:
    secret = _env("PORTAL_SECRET")
    if not secret or not hmac.compare_digest(headers.get("x-portal-secret", ""), secret):
        return None
    login = headers.get("x-portal-user", "").strip()
    if not login:
        return None
    role = headers.get("x-portal-role", "employee")
    # Круг показа от портала: логины подчинённых через запятую, «*» — видно всё.
    team_raw = (headers.get("x-portal-team") or "").strip()
    team = ["*"] if team_raw == "*" else [t for t in (x.strip() for x in team_raw.split(",")) if t]
    return {"login": login, "role": role, "is_admin": role == "admin",
            "name": unquote(headers.get("x-portal-name", "")) or login, "team": team}


_people: dict = {"time": 0.0, "list": []}


def people() -> list[dict]:
    """Сотрудники пространства — для окна «Доступ». Берём у портала, держим минуту."""
    import json as _json
    import time as _time
    import urllib.request as _req
    if _time.time() - _people["time"] < 60 and _people["list"]:
        return _people["list"]
    secret = _env("PORTAL_SECRET")
    url = _env("PORTAL_URL", "http://127.0.0.1:8700") + "/internal/users"
    try:
        request = _req.Request(url, headers={"X-Portal-Secret": secret})
        with _req.urlopen(request, timeout=5) as response:
            data = _json.loads(response.read().decode("utf-8", "replace"))
        rows = [{"login": u.get("login", ""), "name": u.get("name") or u.get("login", "")}
                for u in data.get("users", []) if u.get("active", True)]
    except Exception:            # портал молчит — окно «Доступ» просто покажет, что список не пришёл
        return _people["list"]
    _people.update(time=_time.time(), list=rows)
    return rows


# ── реестр людей: один на пространство, живёт в портале (../CLAUDE.md п. 5а) ──
# «ФИО → должность → ведомство» раньше лежал в трёх местах и расходился: своя таблица
# `registry` здесь, реестр «Повестки» и `names.json`. Теперь только портал. Разбор идёт
# на сервере, браузера тут нет, поэтому ходим сами, с общим секретом.
# Портал молчит — работаем без подсказок, а не падаем. Сами в реестр не пишем: пишет человек,
# из окна «Добавить в реестр?» в браузере (POST /api/people портала).

PEOPLE_SOURCE = "moderator"
_PEOPLE_TTL = 60            # с. — подсказки дёргаются на каждом открытии совещания
_PEOPLE_PAUSE = 30          # с. — после отказа портала не ждём таймаута на каждом участнике
PEOPLE_TIMEOUT = 1.5        # с. — подсказки и должности: портал на той же машине, дольше ждать незачем
CHECK_TIMEOUT = 4.0         # с. — обязательная сверка ФИО перед документом: ждём дольше, паузы нет
_people_cache: dict[tuple, tuple[float, object]] = {}
_people_down_until = 0.0
_people_lock = threading.Lock()


def _people_call(method: str, path: str, body: dict | None = None, *,
                 timeout: float | None = None, shared_pause: bool = True):
    """Запрос к реестру портала. 404 — «нет такого» (None), остальные сбои — исключение.

    `shared_pause` — общая пауза после отказа: подсказки и поиск должностей её соблюдают и
    включают, чтобы не ждать таймаута на каждом человеке. Обязательная сверка ФИО идёт
    с `shared_pause=False`: чужой медленный поиск её не отключает (ревьюер RC-2026-09-21-6, К1).
    """
    import time as _time
    import urllib.error
    global _people_down_until
    if shared_pause and _time.time() < _people_down_until:
        raise OSError("реестр людей в портале не отвечает")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        _env("PORTAL_URL", "http://127.0.0.1:8700") + path, data=data, method=method,
        headers={"X-Portal-Secret": _env("PORTAL_SECRET"), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or PEOPLE_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if shared_pause:
            _people_down_until = _time.time() + _PEOPLE_PAUSE
        raise
    except Exception:
        if shared_pause:
            _people_down_until = _time.time() + _PEOPLE_PAUSE
        raise


def _people_cached(key: tuple, fetch):
    import time as _time
    with _people_lock:
        hit = _people_cache.get(key)
        if hit and _time.time() - hit[0] < _PEOPLE_TTL:
            return hit[1]
    value = fetch()
    with _people_lock:
        _people_cache[key] = (_time.time(), value)
    return value


def _person(row: dict) -> dict:
    name = str(row.get("name") or "")
    return {"name": name, "surname": name.split(" ", 1)[0],
            "role": str(row.get("role") or ""), "organization": str(row.get("organization") or "")}


PEOPLE_BUDGET = 3.0         # с. на все вопросы к реестру в одном запросе (SRE 21.09)
PEOPLE_WORKERS = 6


def _budget_left(cap: float) -> float:
    """Сколько ещё можно ждать реестр в этом запросе: бюджет один на весь запрос, а не на вызов."""
    import time as _time
    bucket = _request.get()
    now = _time.monotonic()
    if bucket is None:
        return cap
    deadline = bucket.get("people_deadline")
    if deadline is None:
        deadline = bucket["people_deadline"] = now + PEOPLE_BUDGET
    return max(0.0, min(cap, deadline - now))


def request_bucket() -> dict | None:
    """Корзинка текущего запроса (None вне запроса): там же копятся общие на запрос сроки."""
    return _request.get()


def registry_late() -> bool:
    """Реестр не ответил вовремя в этом запросе — интерфейс попросит проверить должности."""
    return bool((_request.get() or {}).get("registry_late"))


# Отказ реестра внутри потока: вызов вернул «нет в реестре», но на самом деле портал не ответил
# (пауза после отказа, мгновенная ошибка). Поток отмечает это здесь, и `people_parallel` оставляет
# след — строку в server.log и пометку человеку, как у опоздавших (ревьюер RC-2026-09-21-7, п. 2).
_thread = threading.local()


def _note_failure() -> None:
    marks = getattr(_thread, "failures", None)
    if marks is not None:
        marks.append(1)


def people_parallel(fn, keys, budget: float | None = None, what: str = "реестр людей") -> dict:
    """Вопросы к реестру по списку — параллельно и в пределах бюджета запроса, а не по очереди.

    `fn(key)` вызывается для каждого ключа в пуле потоков; кто не успел — того нет в ответе
    (вызывающий берёт значение по умолчанию). Опоздавшие не теряются молча: строка в журнал
    медленных и пометка в ответе (`X-Registry-Late`), интерфейс скажет «проверьте должности».
    Контекста запроса в потоках нет: `fn` не должен читать `_request` (логин передаётся заранее).
    """
    from concurrent.futures import ThreadPoolExecutor, wait
    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys:
        return {}
    left = _budget_left(PEOPLE_BUDGET if budget is None else budget)
    answers: dict = {}
    if left > 0:
        def run(key):
            _thread.failures = []
            try:
                return fn(key), bool(_thread.failures)
            finally:
                _thread.failures = None

        pool = ThreadPoolExecutor(max_workers=min(PEOPLE_WORKERS, len(keys)))
        try:
            futures = {pool.submit(run, k): k for k in keys}
            done, _late = wait(futures, timeout=left)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)   # опоздавшие дорабатывают в фоне
        results = {futures[f]: f.result() for f in done if f.exception() is None}
        answers = {k: value for k, (value, _failed) in results.items()}
        failed = sum(1 for _value, bad in results.values() if bad)
    else:
        failed = 0
    missed = len(keys) - len(answers)
    log = logging.getLogger("uvicorn.error")
    if missed:
        log.warning("реестр людей не ответил вовремя: %s — %d из %d без ответа", what, missed, len(keys))
    if failed:
        log.warning("реестр людей отказал: %s — %d из %d без ответа", what, failed, len(keys))
    if missed or failed:
        bucket = _request.get()
        if bucket is not None:
            bucket["registry_late"] = True
    return answers


def people_search(query: str, limit: int = 20) -> list[dict]:
    """Люди по фамилии, должности или ведомству. Портал молчит — пустой список."""
    from urllib.parse import quote
    query = (query or "").strip()
    if len(query) < 2:
        return []

    def fetch():
        data = _people_call("GET", f"/internal/people?q={quote(query)}&limit={int(limit)}") or {}
        return [_person(r) for r in data.get("items", []) if isinstance(r, dict)]
    try:
        return _people_cached(("q", query.lower(), int(limit)), fetch)
    except Exception as e:
        _note_failure()
        logging.getLogger("moderator").warning("реестр людей: поиск не удался: %s", e)
        return []


def people_get(name: str) -> dict | None:
    """Одна запись реестра по ФИО. Нет или портал молчит — None."""
    from urllib.parse import quote
    name = " ".join((name or "").split())
    if not name:
        return None

    def fetch():
        row = _people_call("GET", f"/internal/people/one?name={quote(name)}")
        return _person(row) if isinstance(row, dict) else None
    try:
        return _people_cached(("one", name.lower()), fetch)
    except Exception as e:
        _note_failure()
        logging.getLogger("moderator").warning("реестр людей: запись не прочитана: %s", e)
        return None


def note_person(name: str, role: str = "", organization: str = "") -> None:
    """Запомнить человека, которого нашёл разбор или импорт, — в реестр его не пишем.

    В реестр пишет только человек (../CLAUDE.md п. 5а, решение 21.09). В конце запроса
    сервис спросит у портала, новый ли это человек, и поставит его в очередь окна
    «Добавить в реестр?».
    """
    name = " ".join((name or "").split())
    if not name:
        return
    bucket = _request.get()
    if bucket is not None:
        bucket.setdefault("people", {})[name.lower()] = {
            "name": name, "role": (role or "").strip(), "organization": (organization or "").strip()}


def noted_people() -> list[dict]:
    bucket = _request.get() or {}
    return list((bucket.get("people") or {}).values())


def current_login() -> str:
    return (_request.get() or {}).get("login", "")


def people_suggest(person: dict, login: str | None = None) -> str:
    """Что портал думает о человеке: new | differs | same. Реестр при этом не меняется.

    `force: false` — это вопрос, а не запись: разошедшаяся должность ложится у портала
    в варианты, новый человек не добавляется. Портал молчит — «» (окно не показываем).
    """
    body = {"name": person["name"], "role": person.get("role", ""),
            "organization": person.get("organization", ""), "force": False,
            "login": current_login() if login is None else login, "source": PEOPLE_SOURCE,
            "document": person.get("document", "")}
    try:
        answer = _people_call("POST", "/internal/people", body) or {}
    except Exception as e:
        _note_failure()
        logging.getLogger("moderator").warning("реестр людей: не спросили о человеке: %s", e)   # без ФИО
        return ""
    return str(answer.get("suggest") or "")


def people_check(names: list[str]) -> list[dict] | None:
    """Сверка ФИО перед выводом в документ: exact | close | unknown по каждому. None — портал молчит."""
    names = [" ".join((n or "").split()) for n in names if (n or "").strip()]
    if not names:
        return []
    try:
        answer = _people_call("POST", "/internal/people/check", {"names": names},
                              timeout=CHECK_TIMEOUT, shared_pause=False) or {}
    except Exception as e:
        logging.getLogger("moderator").warning("реестр людей: сверка не удалась: %s", e)
        return None
    return [x for x in answer.get("items", []) if isinstance(x, dict)]


def start_request(login: str = "") -> dict:
    bucket = {"changes": [], "object": "", "tokens_in": 0, "tokens_out": 0, "login": login}
    _request.set(bucket)
    return bucket


def note_tokens(tokens_in: int, tokens_out: int) -> None:
    """Расход «Искры» за этот запрос. Уходит в журнал портала вместе с самим действием,
    чтобы на вкладке «Искра» было видно, кто и на что потратил."""
    bucket = _request.get()
    if bucket is not None:
        bucket["tokens_in"] = bucket.get("tokens_in", 0) + max(0, int(tokens_in or 0))
        bucket["tokens_out"] = bucket.get("tokens_out", 0) + max(0, int(tokens_out or 0))


def note_change(summary: str) -> None:
    bucket = _request.get()
    if bucket is not None and summary:
        bucket["changes"].append(summary)


def note_object(obj: str) -> None:
    bucket = _request.get()
    if bucket is not None:
        bucket["object"] = obj


# ── описание действий для журнала ───────────────────────────────────────

_ACTIONS = [
    # Разбор ничего не меняет, но стоит токенов «Искры» — пишем, чтобы расход был виден по людям.
    ("POST", r"/api/analyze", "Разбор документа Искрой"),
    ("POST", r"/api/analyze-file", "Разбор файла Искрой"),
    ("POST", r"/api/inbox", "Принят документ из «Приёмной» (Материалы)"),
    ("PUT", r"/api/meetings/[^/]+/access", "Изменён доступ к совещанию"),
    ("POST", r"/api/meetings", "Создано совещание"),
    ("POST", r"/api/meetings/import-from-agenda", "Импорт повестки из сервиса «Повестка»"),
    ("POST", r"/api/meetings/find-or-create", "Совещание найдено или заведено"),
    ("POST", r"/api/meetings/[^/]+/people/replace", "Исправлено ФИО по реестру людей"),
    ("POST", r"/api/meetings/[^/]+/people/pending/resolve", "Решение о реестре людей"),
    ("PUT", r"/api/meetings/[^/]+/folder", "Привязана папка материалов"),
    ("PUT", r"/api/meetings/[^/]+/header", "Изменены данные совещания"),
    ("DELETE", r"/api/meetings/[^/]+", "Совещание отправлено в архив"),
    ("POST", r"/api/meetings/[^/]+/restore", "Совещание восстановлено из архива"),
    ("DELETE", r"/api/meetings/[^/]+/permanent", "Совещание удалено навсегда"),
    ("*", r"/api/meetings/[^/]+/agenda/command", "Повестка изменена командой"),
    ("POST", r"/api/meetings/[^/]+/agenda/apply-changes", "Внесены отмеченные изменения (Искра)"),
    ("*", r"/api/meetings/[^/]+/agenda.*", "Изменена повестка"),
    ("*", r"/api/meetings/[^/]+/participants.*", "Изменён состав участников"),
    # перечень поручений: предложения Искры ничего не меняют, но стоят токенов — расход виден по людям
    ("POST", r"/api/meetings/[^/]+/orders/propose-from-materials", "Перечень поручений из материалов (Искра)"),
    ("POST", r"/api/meetings/[^/]+/orders/propose-from-file", "Разобран файл с поручениями (Искра)"),
    ("POST", r"/api/meetings/[^/]+/orders/bulk", "Добавлены поручения в перечень"),
    ("GET", r"/api/meetings/[^/]+/orders/export\.docx", "Выгружен перечень поручений в Word"),
    ("*", r"/api/meetings/[^/]+/orders.*", "Изменены поручения"),
    ("POST", r"/api/meetings/[^/]+/scenario/generate", "Сформирован сценарий"),
    ("POST", r"/api/meetings/[^/]+/scenario/opening-from-theses", "Вступительное слово из сервиса «Тезисы»"),
    ("*", r"/api/meetings/[^/]+/scenario.*", "Изменён сценарий"),
    ("*", r"/api/meetings/[^/]+/glossary.*", "Изменён глоссарий"),
    ("POST", r"/api/meetings/[^/]+/materials/upload", "Загружены материалы"),
    ("POST", r"/api/meetings/[^/]+/materials/analyze", "Материал разобран Искрой"),
    ("*", r"/api/meetings/[^/]+/materials.*", "Изменены материалы"),
    ("POST", r"/api/meetings/[^/]+/results", "Получен документ во вкладку «Результаты» (Транскрибатор)"),
    ("DELETE", r"/api/meetings/[^/]+/results/[^/]+", "Убран документ из вкладки «Результаты»"),
    ("POST", r"/api/meetings/[^/]+/closing", "Сформировано заключительное слово"),
    ("POST", r"/api/meetings/[^/]+/opening", "Сформировано вступительное слово (Искра, из материалов)"),
    ("POST", r"/api/meetings/[^/]+/import", "Импортированы данные"),
    ("POST", r"/api/meetings/[^/]+/apply", "Применён разбор документа"),
    ("POST", r"/api/analyze", "Документ отправлен на разбор в Искру"),
    ("POST", r"/api/analyze-file", "Файл отправлен на разбор в Искру"),
    ("POST", r"/api/extract", "Извлечён текст из файла"),
    ("GET", r"/api/meetings/[^/]+/export\.docx", "Выгружен модератор в Word"),
    ("GET", r"/api/meetings/[^/]+/export/agenda\.html", "Выгружена повестка (для пакета совещания)"),
    ("GET", r"/api/meetings/[^/]+/export/participants\.html", "Выгружен список участников (для пакета совещания)"),
    ("GET", r"/api/meetings/[^/]+/export/agenda\.docx", "Выгружена повестка в Word (для пакета совещания)"),
    ("GET", r"/api/meetings/[^/]+/export/participants\.docx", "Выгружен список участников в Word (для пакета совещания)"),
    ("GET", r"/api/meetings/[^/]+/export", "Выгружен модератор-бриф"),
]
_MID = re.compile(r"^/api/meetings/([^/]+)")
# адреса, где первый сегмент — не ключ совещания (ключ станет известен по ходу запроса)
_NOT_A_MID = {"import-from-agenda", "find-or-create"}


def describe(method: str, path: str) -> str | None:
    for m, pattern, action in _ACTIONS:
        if (m == "*" and method != "GET" or m == method) and re.fullmatch(pattern, path):
            return action
    return None


def meeting_id(path: str, query: dict) -> str:
    m = _MID.match(path)
    if m and unquote(m.group(1)) not in _NOT_A_MID:
        return unquote(m.group(1))
    return query.get("meeting_id", "")


_queue: queue.Queue = queue.Queue(maxsize=10_000)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _deliver() -> None:
    while True:
        event = _queue.get()
        secret = _env("PORTAL_SECRET")
        url = _env("PORTAL_URL", "http://127.0.0.1:8700") + "/internal/audit"
        body = json.dumps({"service": "moderator", **event}, ensure_ascii=False).encode("utf-8")
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json", "X-Portal-Secret": secret})
                urllib.request.urlopen(req, timeout=5).close()
                break
            except Exception as e:  # журнал не должен ломать работу
                if attempt == 2:
                    log.warning("audit не отправлен: %s | %s", e, body[:500])


def send_audit(**event) -> None:
    """Событие в журнал портала: по одному, в порядке действий, не задерживая ответ пользователю."""
    global _worker
    if not _env("PORTAL_SECRET"):
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_deliver, name="portal-audit", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait(event)
    except queue.Full:
        log.warning("очередь журнала переполнена, событие потеряно: %s", event.get("action"))
