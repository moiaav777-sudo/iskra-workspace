"""FastAPI-приложение «Модератор совещаний».

Здесь только вход в приложение: проверка пользователя из портала, журнал,
статика и подключение маршрутов. Сами маршруты разложены по разделам брифа:

- `routes_meetings` — совещание целиком: список, создание, шапка, архив,
  приём повестки из сервиса «Повестка», выгрузка HTML;
- `routes_agenda` — повестка, докладчики, участники;
- `routes_content` — поручения, сценарий, глоссарий;
- `routes_materials` — материалы к вопросу;
- `routes_results` — вкладка «Результаты»: документы из «Транскрибатора»;
- `routes_iskra` — разбор документов Искрой и справочник имён.

Общие проверки лежат в `deps`, схемы запросов — в `schemas`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import portal, storage
from .deps import current_user
from . import routes_agenda, routes_content, routes_iskra, routes_materials, routes_meetings, routes_results

app = FastAPI(title="Модератор совещаний", docs_url=None, redoc_url=None, openapi_url=None)

BASE_DIR = Path(__file__).resolve().parent.parent

# ── авторизация: вход и права — в портале workspace.example.ru ────────────────

ALLOWED_PATHS = {"/healthz"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _client_ip(request: Request) -> str:
    # за nginx реальный адрес приходит в X-Real-IP (порт 8765 слушает только 127.0.0.1)
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _origin_ok(request: Request) -> bool:
    """Защита от CSRF: изменяющий запрос должен прийти со страницы этого же сайта."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # не-браузерные клиенты и старые браузеры; cookie всё равно SameSite=Lax
    # X-Forwarded-Host не используем: его может прислать кто угодно и снять защиту
    host = request.headers.get("host") or ""
    return origin.split("://", 1)[-1] == host


# Правки одного совещания идут по очереди: иначе два человека, добавившие вопрос
# одновременно, перезаписывают документ друг друга и одна правка молча пропадает.
# Долгие обращения к Искре (разбор, сценарий, заключительное слово) из очереди исключены,
# чтобы они не держали совещание заблокированным минутами.
_SLOW = re.compile(r"/(analyze|analyze-file|generate|closing|opening|command|"
                   r"propose-from-materials|propose-from-file)$")
_meeting_locks: dict[str, asyncio.Lock] = {}


def _meeting_lock(mid: str) -> asyncio.Lock:
    lock = _meeting_locks.get(mid)
    if lock is None:
        lock = _meeting_locks[mid] = asyncio.Lock()
    return lock


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method in UNSAFE_METHODS and not _origin_ok(request):
        return JSONResponse({"detail": "Запрос с чужого сайта отклонён"}, status_code=403)
    if path in ALLOWED_PATHS:
        return await call_next(request)
    user = portal.user_from_headers(request.headers)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется вход"}, status_code=401)
        return RedirectResponse(portal.LOGIN_URL, status_code=302)
    current_user.set(user)
    request.state.user = user
    request.state.login = user["login"]
    storage.set_current_login(user["login"])
    bucket = portal.start_request(user["login"])
    action = portal.describe(request.method, path)
    mid = portal.meeting_id(path, dict(request.query_params)) if action else ""
    before = storage.get(mid) if mid and request.method == "DELETE" else None   # после удаления названия уже не узнать
    lock = _meeting_lock(mid) if mid and request.method in UNSAFE_METHODS and not _SLOW.search(path) else None
    if lock is not None:
        async with lock:
            response = await call_next(request)
    else:
        response = await call_next(request)
    if bucket.get("registry_late"):
        response.headers["X-Registry-Late"] = "1"   # интерфейс скажет «реестр не ответил — проверьте должности»
    # неудачный вызов «Искры» тоже стоил токенов — его расход в журнал (../team/DEV-CHECKLIST.md §4)
    spent = bucket.get("tokens_in", 0) or bucket.get("tokens_out", 0)
    if action and (response.status_code < 400 or spent):
        mid = mid or bucket["object"]
        if mid and request.method != "GET" and response.status_code < 400:
            storage.note_editor(mid, user["login"], user["name"])   # для предупреждения о параллельной правке
        m = before or (storage.get(mid) if mid else None)
        portal.send_audit(
            login=user["login"], action=action, ip=_client_ip(request),
            object=(f"{mid} · {m.title}" if m and m.title else mid)[:300],
            details="\n".join(([f"ошибка, код {response.status_code} – действие не выполнено"]
                                if response.status_code >= 400 else []) +
                               list(dict.fromkeys(bucket["changes"])))[:4000],
            tokens_in=bucket.get("tokens_in", 0), tokens_out=bucket.get("tokens_out", 0),
        )
    return response


# ── медленные запросы ───────────────────────────────────────────────────
# SRE видит, что запрос шёл 10 с, но не видит какой. Пишем в logs/server.log метод, форму
# адреса (ключи заменены на {id}), длительность и код — без данных и параметров.
# Логгер «uvicorn.error» — тот, что log_config.json уже направляет в server.log.

SLOW_SECONDS = 3.0
_slow_log = logging.getLogger("uvicorn.error")
_ID_SEG = re.compile(r"^(?:[0-9a-f]{8,}|\d+|meeting-\d+)$")
_NOT_ID = {"find-or-create", "import-from-agenda"}


def path_shape(path: str) -> str:
    """«/api/meetings/meeting-16/scenario/3fa9c1d2e4» → «/api/meetings/{id}/scenario/{id}»."""
    out, prev = [], ""
    for seg in path.split("/"):
        if seg and ((prev == "meetings" and seg not in _NOT_ID) or _ID_SEG.match(seg)):
            seg = "{id}"
        out.append(seg)
        prev = seg
    return "/".join(out)


@app.middleware("http")
async def slow_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    spent = time.monotonic() - started
    if spent > SLOW_SECONDS:
        _slow_log.warning("медленный запрос: %s %s %.1f с, код %s",
                          request.method, path_shape(request.url.path), spent, response.status_code)
    return response


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# Адреса соседей отдаёт сервер, в самой странице абсолютных адресов нет.
# Папку совещания ведут «Материалы» (../CLAUDE.md п. 9): заводим её там, пакет берём оттуда же.
MATERIALS_LINKS = {
    "link": "/materials/api/events/link",
    "card": "/materials/api/events/{id}",
    "package": "/materials/api/events/{id}/export/package.zip",
    "files": "/materials/api/events/{id}/files",
    # «Собрать пакет» (решение пользователя 07.10): привести папку в порядок — номера и реестр 00
    "registry": "/materials/api/events/{id}/registry",
    "folder": "/materials/#event={id}",
    # вкладка «Пакет» — их же карточка папки в iframe (режим встраивания, решение Оркестратора 02.10);
    # адрес встраивания живёт только здесь — сменится у «Материалов», правим одну строку
    "embed": "/materials/embed/{id}",
}


# В реестр людей пишет только человек — из окна «Добавить в реестр?», запросом из браузера
# под вошедшим (../CLAUDE.md п. 5а). Адрес портала, как и адреса «Материалов», отдаёт сервер.
PEOPLE_LINKS = {"add": "/api/people"}

# «Транскрибатор» кладёт во вкладку «Результаты» транскрипцию, саммари и решения; пустая вкладка
# ведёт к нему — разобрать запись и «Приложить к совещанию».
TRANSCRIBER_LINKS = {"home": "/transcriber/"}


@app.get("/api/me")
def me(request: Request):
    u = request.state.user
    return {"login": u["login"], "name": u["name"], "is_admin": u["is_admin"], "team": u.get("team", []),
            "materials": MATERIALS_LINKS, "people": PEOPLE_LINKS,
            "transcriber": TRANSCRIBER_LINKS}


# ── статика ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


# ── маршруты ────────────────────────────────────────────────────────────
# «Повестка» и материалы объявляются раньше общих маршрутов совещания,
# чтобы адреса вида /api/meetings/import-from-agenda не ловились как {mid}.

app.include_router(routes_meetings.router)
app.include_router(routes_agenda.router)
app.include_router(routes_content.router)
app.include_router(routes_materials.router)
app.include_router(routes_results.router)
app.include_router(routes_iskra.router)
